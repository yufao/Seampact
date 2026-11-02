import json
import time
import threading
import sys
from types import SimpleNamespace

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from seampact.monitoring import ResourceMonitor


class Store:
    def list(self, kind):
        if kind == "managed_service":
            return [{"docker": {"gpus": "device=GPU-12345678-1234-1234-1234-123456789abc"}}]
        return []


def platform(tmp_path):
    deployments = SimpleNamespace(backend=lambda: {"gpus": ""}, sample_resources=lambda: [])
    services = SimpleNamespace(groups=lambda: [], sample_resources=lambda ids: [])
    runtime = SimpleNamespace(snapshot=lambda: {"calls": {}, "used": 0, "quota": 1})
    return SimpleNamespace(home=tmp_path, store=Store(), deployments=deployments, managed_services=services, runtime=runtime)


class MonitoringTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.tmp_path = Path(self.temp.name)

    def test_opt_in_capture_export_and_restart(self):
        tmp_path = self.tmp_path
        monitor = ResourceMonitor(platform(tmp_path))
        assert monitor.status()["current"] is None
        with self.assertRaises(ValueError):
            monitor.start({"metrics": ["gpu_memory"], "gpu_uuids": ["GPU-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"]})
        session = monitor.start({"metrics": ["platform_cpu", "platform_memory", "workload"], "interval_s": 0.5})
        for _ in range(50):
            if monitor.status()["latest"]: break
            time.sleep(.01)
        detail = monitor.detail(session["id"])
        assert detail["samples"][0]["processes"][0]["scope"] == "process"
        assert detail["samples"][0]["workload"]["tasks_finished"] == 0
        monitor.stop(session["id"])
        records = [json.loads(row) for row in monitor.export_path(session["id"]).read_text(encoding="utf-8").splitlines()]
        assert records[0]["type"] == "header" and records[-1]["type"] == "summary"
        meta = monitor._meta(session["id"])
        meta["status"] = "RUNNING"
        monitor._save(meta)
        restarted = ResourceMonitor(platform(tmp_path))
        assert restarted._meta(session["id"])["status"] == "INTERRUPTED"
        assert restarted._meta(session["id"])["sample_count"] is None
        assert restarted.detail(session["id"])["total"] >= 1
        assert restarted.status()["current"] is None


    def test_counter_edges_require_identity_and_adjacent_observation(self):
        tmp_path = self.tmp_path
        monitor = ResourceMonitor(platform(tmp_path))
        monitor.summary = {"sample_count": 0, "series": {}, "sampling_errors": 0}
        def sample(t, cpu, identity="one"):
            return {"processes": [{"scope": "process", "pid": 1, "identity": identity,
                    "monotonic_s": t, "cpu_seconds": cpu, "rss_bytes": 10, "status": "OBSERVED"}],
                    "containers": [], "devices": []}
        monitor._update_summary(sample(0, 1), 1)
        monitor._update_summary(sample(1, 2), 1)
        monitor._update_summary(sample(2, 100, "new"), 1)
        monitor._update_summary({"processes": [], "containers": [], "devices": []}, 1)
        monitor._update_summary(sample(4, 102, "new"), 1)
        series = monitor.summary["series"]["process:1"]
        assert series["cpu_seconds_delta"] == 1
        assert series["integrals"]["rss_bytes"]["valid_time_s"] == 1


    def test_stop_waits_for_probe_and_capabilities(self):
        tmp_path = self.tmp_path
        monitor = ResourceMonitor(platform(tmp_path))
        self.enterContext(patch("seampact.monitoring.shutil.which", return_value=None))
        caps = {item["id"]: item for item in monitor.capabilities()["metrics"]}
        assert not caps["gpu_memory"]["available"]
        with self.assertRaises(ValueError):
            monitor.start({"metrics": ["gpu_memory"]})
        entered, release = threading.Event(), threading.Event()
        original = monitor._capture
        def capture(meta):
            entered.set()
            release.wait(5)
            return original(meta)
        self.enterContext(patch.object(monitor, "_capture", capture))
        session = monitor.start({"metrics": ["workload"], "interval_s": .5})
        assert entered.wait(2)
        assert monitor.stop(session["id"])["status"] == "STOPPING"
        with self.assertRaises(ValueError):
            monitor.start({"metrics": ["workload"]})
        release.set()
        monitor.thread.join(2)
        assert monitor._meta(session["id"])["status"] == "COMPLETED"
        assert monitor._meta(session["id"])["collector_sha256"]["monitoring.py"]
        assert monitor.export(session["id"], "json")["session"]["probe_config"]["interval_s"] == .5


    def test_selection_and_finished_outcomes(self):
        tmp_path = self.tmp_path
        obj = platform(tmp_path)
        tasks = [{"status": status, "ended_at" if status == "COMPLETED" else "finished_at": "2020-01-02T08:00:00+08:00", "latency_ms": 20}
                 for status in ("COMPLETED", "FAILED", "CANCELLED", "TIMEOUT")]
        original_list = obj.store.list
        obj.store.list = lambda kind: tasks if kind == "task" else original_list(kind)
        monitor = ResourceMonitor(obj)
        self.enterContext(patch("seampact.monitoring.shutil.which", return_value="nvidia-smi"))
        with self.assertRaisesRegex(ValueError, "请选择至少一张"):
            monitor.start({"metrics": ["gpu_memory"]})
        sample = monitor._capture({"metrics": ["workload"], "started_at": "2020-01-01T23:00:00+00:00", "gpu_uuids": []})
        workload = sample["workload"]
        assert (workload["tasks_finished"], workload["tasks_cancelled"], workload["tasks_timed_out"]) == (4, 1, 1)


    def test_service_container_is_not_sampled_twice(self):
        from seampact.deployment import Deployments
        manager = Deployments.__new__(Deployments)
        manager.lock = threading.RLock()
        manager.session = "current"
        manager.instances = {"service": {"session": "current", "released": False,
            "target_kind": "model-service", "handle": {"kind": "docker", "container": "not-probed"}}}
        assert manager.sample_resources() == []

    def test_close_waits_for_probe_before_dependencies_close(self):
        monitor = ResourceMonitor(platform(self.tmp_path))
        entered, release = threading.Event(), threading.Event()
        original = monitor._capture
        def blocked(meta):
            entered.set()
            release.wait(5)
            return original(meta)
        with patch.object(monitor, "_capture", blocked):
            monitor.start({"metrics": ["workload"]})
            self.assertTrue(entered.wait(2))
            closer = threading.Thread(target=monitor.close)
            closer.start()
            time.sleep(1.1)
            self.assertTrue(closer.is_alive())
            release.set()
            closer.join(2)
            self.assertFalse(closer.is_alive())
            self.assertFalse(monitor.thread.is_alive())
