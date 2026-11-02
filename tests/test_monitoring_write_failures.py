import json
import tempfile
import time
import unittest
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from seampact.monitoring import ResourceMonitor


def make_monitor(tmp_path):
    store = SimpleNamespace(list=lambda kind: [])
    deployments = SimpleNamespace(backend=lambda: {"kind": "local", "gpus": ""})
    runtime = SimpleNamespace(snapshot=lambda: {"calls": {}, "used": 0, "quota": 1})
    return ResourceMonitor(SimpleNamespace(home=tmp_path, store=store, deployments=deployments, runtime=runtime))


class MonitoringWriteFailureTests(unittest.TestCase):
    def test_failure_does_not_leave_running_session_or_invent_samples(self):
        for failed_type in ("sample", "summary"):
            with self.subTest(failed_type=failed_type), tempfile.TemporaryDirectory() as folder:
                monitor = make_monitor(Path(folder))
                original = monitor._append_record

                def append(identity, record):
                    if record["type"] == failed_type:
                        raise OSError("simulated write failure")
                    original(identity, record)

                with patch.object(monitor, "_append_record", side_effect=append):
                    session = monitor.start({"metrics": ["workload"], "interval_s": .5})
                    if failed_type == "summary":
                        for _ in range(100):
                            if monitor.status()["current"]["sample_count"]:
                                break
                            time.sleep(.01)
                        monitor.stop(session["id"])
                    monitor.thread.join(2)
                    self.assertFalse(monitor.thread.is_alive())
                    self.assertIsNone(monitor.status()["current"])
                    meta = monitor._meta(session["id"])
                    rows = [json.loads(line) for line in monitor.export_path(session["id"]).read_text(encoding="utf-8").splitlines()]
                    samples = [row for row in rows if row["type"] == "sample"]
                    self.assertEqual(meta["status"], "FAILED")
                    self.assertEqual(meta["sample_count"], len(samples))
                    self.assertEqual(meta["summary"]["sample_count"], len(samples))
                    self.assertGreaterEqual(meta["summary"]["elapsed_s"], 0)
                    if failed_type == "sample":
                        self.assertEqual(meta["summary"]["sampling_errors"], 1)
                        self.assertEqual(rows[-1]["type"], "summary")
                    else:
                        self.assertEqual(meta["sampling_error"], "SUMMARY_WRITE_FAILED")

    def test_coverage_uses_elapsed_time_and_only_valid_edges(self):
        with tempfile.TemporaryDirectory() as folder:
            monitor = make_monitor(Path(folder))
            monitor.summary = {"sample_count": 0, "series": {}, "sampling_errors": 0}
            def sample(stamp, cpu, identity):
                return {"processes": [{"scope": "process", "pid": 1, "identity": identity,
                    "monotonic_s": stamp, "status": "OBSERVED", "cpu_seconds": cpu, "rss_bytes": 10}],
                    "containers": [], "devices": []}
            monitor._update_summary(sample(10, 1, "first"), 1)
            monitor._update_summary(sample(11, 2, "first"), 1)
            monitor._update_summary(sample(12, 5, "second"), 1)
            meta = {"started_monotonic_s": 9}
            with patch("seampact.monitoring.time.monotonic", return_value=13):
                monitor._finish_summary(meta)
            series = monitor.summary["series"]["process:1"]
            self.assertEqual(monitor.summary["elapsed_s"], 4)
            self.assertEqual(series["cpu_time_coverage"], .25)
            self.assertEqual(series["integrals"]["rss_bytes"]["valid_time_coverage"], .25)

            monitor.summary = {"sample_count": 0, "series": {}, "sampling_errors": 0}
            monitor._update_summary(sample(10, 1, "first"), 1)
            with patch("seampact.monitoring.time.monotonic", return_value=13):
                monitor._finish_summary(meta)
            self.assertIsNone(monitor.summary["series"]["process:1"]["cpu_time_coverage"])
