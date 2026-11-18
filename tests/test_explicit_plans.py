"""The retired plan endpoint must not start a task."""
import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from seampact.server import make_server


class RetiredPlanTests(unittest.TestCase):
    def test_plan_submission_is_rejected_without_starting_execution(self):
        platform = Mock()
        server = make_server(platform, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = "http://127.0.0.1:" + str(server.server_address[1]) + "/api/plans"
        request = urllib.request.Request(url, data=json.dumps({"plans": []}).encode(),
                                         headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=5)
        error = raised.exception
        self.addCleanup(error.close)
        self.assertEqual(error.code, 410)
        self.assertIn("停用", json.load(error)["error"])
        platform.chat.start_plan.assert_not_called()


if __name__ == "__main__":
    unittest.main()
