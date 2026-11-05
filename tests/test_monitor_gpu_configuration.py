"""Explicit GPU identities persist without changing allocation or live sessions."""
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from seampact.observability.monitoring import ResourceMonitor


A = "GPU-12345678-1234-1234-1234-123456789abc"
B = "GPU-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


class MonitorGPUConfiguration(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.platform = SimpleNamespace(home=Path(folder.name),
            store=SimpleNamespace(list=lambda kind: []),
            deployments=SimpleNamespace(backend=lambda: dict(kind="docker", gpus="")))
        self.monitor = ResourceMonitor(self.platform)
        self.addCleanup(self.monitor.close)

    def test_probe_and_save_survive_restart_and_clear_keeps_allocated_candidates(self):
        with patch("seampact.observability.resource_observation.query", return_value=(
                [["1", A, "Example", "40000"]], None)) as query:
            saved = self.monitor.configure_gpus(dict(devices=[1]))
        self.assertEqual(saved["status"], "SAVED")
        self.assertEqual(saved["gpu_uuids"], [A])
        self.assertEqual(saved["devices"][0]["name"], "Example")
        query.assert_called_once_with("index,uuid,name,memory.total", ["1"])
        restarted = ResourceMonitor(self.platform)
        self.addCleanup(restarted.close)
        self.assertEqual(restarted.capabilities()["gpu_uuids"], [A])
        self.assertEqual(restarted.capabilities()["gpu_devices"], saved["devices"])
        self.platform.deployments.backend = lambda: dict(kind="docker", gpus="device=" + B)
        with patch("seampact.observability.resource_observation.query") as query:
            cleared = restarted.configure_gpus(dict(devices=[]))
        query.assert_not_called()
        self.assertEqual(cleared["gpu_uuids"], [B])
        self.assertEqual(cleared["devices"], [])

    def test_partial_probe_failure_preserves_all_previous_saved_devices(self):
        with patch("seampact.observability.resource_observation.query", return_value=(
                [["0", A, "First", "100"]], None)):
            self.monitor.configure_gpus(dict(devices=[A]))
        previous = self.monitor.gpu_settings_path.read_bytes()
        with patch("seampact.observability.resource_observation.query", side_effect=[
                ([["1", B, "Second", "200"]], None), (None, "COMMAND_FAILED")]):
            with self.assertRaisesRegex(ValueError, "无法识别 GPU"):
                self.monitor.configure_gpus(dict(devices=[B, "9"]))
        self.assertEqual(self.monitor.gpu_settings_path.read_bytes(), previous)
        self.assertEqual(self.monitor.capabilities()["gpu_uuids"], [A])

    def test_settings_write_failure_keeps_previous_configuration(self):
        with patch("seampact.observability.resource_observation.query", return_value=(
                [["0", A, "First", "100"]], None)):
            self.monitor.configure_gpus(dict(devices=[A]))
        previous = self.monitor.gpu_settings_path.read_bytes()
        with patch("seampact.observability.monitoring.os.replace", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                self.monitor.configure_gpus(dict(devices=[]))
        self.assertEqual(self.monitor.gpu_settings_path.read_bytes(), previous)
        self.assertEqual(self.monitor.capabilities()["gpu_uuids"], [A])

    def test_live_session_selection_is_frozen_when_configuration_changes(self):
        with patch("seampact.observability.resource_observation.query", return_value=(
                [["0", A, "First", "100"]], None)):
            self.monitor.configure_gpus(dict(devices=[A]))
        with patch("seampact.observability.monitoring.shutil.which", return_value="nvidia-smi"), \
                patch("seampact.observability.monitoring.threading.Thread.start"):
            session = self.monitor.start(dict(metrics=["gpu_memory"], gpu_uuids=[A]))
        self.monitor.thread = None
        with patch("seampact.observability.resource_observation.query", return_value=(
                [["1", B, "Second", "200"]], None)):
            self.monitor.configure_gpus(dict(devices=[B]))
        self.assertEqual(self.monitor.current["gpu_uuids"], [A])
        with patch("seampact.observability.monitoring.query", return_value=(
                [[A, "First", "100", "10", "20", "30"],
                 [B, "Second", "200", "20", "30", "40"]], None)) as query:
            sample = self.monitor._capture(session)
        query.assert_called_once_with("uuid,name,memory.total,memory.used,utilization.gpu,power.draw", [A])
        self.assertEqual([d["gpu_uuid"] for d in sample["devices"]], [A])
        self.assertEqual(json.loads(self.monitor.export_path(session["id"]).read_text(encoding="utf-8").splitlines()[0])["gpu_uuids"], [A])


if __name__ == "__main__":
    unittest.main()
