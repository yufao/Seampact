"""Real HTTP recording flow and complete-line downloads, without model execution."""
import json
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from seampact.server import make_server
from seampact.service import Platform


class MonitorAPIChecks(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.platform = Platform(Path(self.directory.name) / "var")
        self.server = make_server(self.platform, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:" + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.platform.close()
        self.directory.cleanup()

    def request(self, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.base + path, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.load(response)

    def test_real_recording_preflight_detail_and_download(self):
        self.assertIsNone(self.request("/api/monitor")["current"])
        self.assertEqual(self.request("/api/operations/preflight")["status"], "READY")
        session = self.request("/api/monitor/start", dict(metrics=["platform_cpu", "platform_memory", "workload"], interval_s=.5, label="核验记录"))
        identity = session["id"]
        for _ in range(50):
            if self.platform.monitor.detail(identity, limit=60)["samples"]:
                break
            time.sleep(.01)
        self.assertEqual(self.request("/api/operations/preflight")["status"], "DRAIN")
        detail = self.request("/api/monitor/sessions/" + identity + "?limit=60")
        self.assertEqual(detail["session"]["label"], "核验记录")
        self.assertTrue(detail["samples"])
        self.assertIn("rss_bytes", detail["samples"][0]["processes"][0])
        result = self.request("/api/monitor/stop", dict(session_id=identity))
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(self.request("/api/operations/preflight")["status"], "READY")
        exported = self.request("/api/monitor/sessions/" + identity + "/export?format=json")
        self.assertGreater(exported["summary"]["sample_count"], 0)
        path = self.platform.monitor.export_path(identity)
        with path.open("ab") as stream:
            stream.write(b'{"type":"sample","unfinished":')
        with urllib.request.urlopen(self.base + "/api/monitor/sessions/" + identity + "/export?format=jsonl", timeout=5) as response:
            raw = response.read()
            self.assertIn("attachment", response.headers["Content-Disposition"])
        records = [json.loads(line) for line in raw.decode().splitlines()]
        self.assertEqual(records[0]["schema"], "seampact.resource-monitor/1")
        self.assertEqual(records[-1]["type"], "summary")
        self.assertNotIn(b'"unfinished":', raw)

    def test_invalid_selection_and_paths_are_rejected(self):
        for path in ("/api/operations/preflight?operation=unknown", "/api/monitor/sessions/not-a-session", "/api/monitor/sessions/" + "0" * 32 + "?limit=9999"):
            with self.assertRaises(urllib.error.HTTPError) as raised:
                self.request(path)
            self.assertEqual(raised.exception.code, 400)
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.request("/api/monitor/start", {"metrics": ["not_supported"]})
        self.assertEqual(raised.exception.code, 400)
        self.assertIsNone(self.platform.monitor.status()["current"])

    def test_frontend_gpu_configuration_and_task_cleanup_routes(self):
        identity = "GPU-12345678-1234-1234-1234-123456789abc"
        with patch("seampact.observability.resource_observation.query", return_value=(
                [["0", identity, "Example", "10000"]], None)):
            saved = self.request("/api/monitor/gpus", dict(devices=["0"]))
        self.assertEqual(saved["devices"][0]["gpu_uuid"], identity)
        self.assertIn(identity, self.request("/api/monitor")["capabilities"]["gpu_uuids"])
        self.assertIsNone(self.request("/api/monitor")["current"])
        self.platform.store.save("task", "ended", dict(id="ended", status="COMPLETED", response="evidence"))
        self.assertEqual(self.request("/api/tasks/delete", dict(task_ids=["ended"])), dict(deleted=["ended"]))
        self.assertEqual(self.request("/api/state")["tasks"], [])
        self.assertEqual(self.platform.store.get("task", "ended")["response"], "evidence")


if __name__ == "__main__":
    unittest.main()
