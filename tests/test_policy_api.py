"""Real HTTP routes, immutable task provenance and read-only observation."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from seampact.server import make_server
from seampact.service import Platform


class PolicyAPITests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.platform = Platform(self.temporary.name)
        self.server = make_server(self.platform, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:" + str(self.server.server_address[1])

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.platform.close()
        self.temporary.cleanup()

    def request(self, path, body=None):
        request = Request(self.url + path, data=json.dumps(body).encode() if body is not None else None,
                          headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=10) as response:
            return json.load(response)

    def test_save_select_and_read_routes_do_not_start_training(self):
        version = self.request("/api/policies/save", dict(name="稳定参考", notes="保留原配置"))
        self.request("/api/policies/select", dict(version_id=version["id"]))
        snapshot = self.request("/api/policies")
        self.assertEqual(snapshot["active"]["id"], version["id"])
        self.assertEqual(snapshot["jobs"], [])
        self.assertEqual(self.request("/api/policies/" + version["id"])["version"]["name"], "稳定参考")
        self.assertEqual(self.request("/api/state")["scheduler"]["policies"]["active"]["id"], version["id"])
        self.assertFalse(self.platform.policy_training.busy)

    def test_activity_is_bound_to_task_version_and_excludes_other_tasks_and_hidden_features(self):
        version = self.request("/api/policies/save", dict(name="任务参考"))
        self.request("/api/policies/select", dict(version_id=version["id"]))
        arrival = self.platform.execution.begin("task")
        self.platform.store.save("task", "task", dict(id="task", status="RUNNING", execution=arrival, events=[], calls=[]))
        self.platform.store.save("capability_decision", "decision", dict(id="decision", at=1, task_id="task",
            sequence=1, event="before_tool", observation=["PRIVATE_OBSERVATION"],
            candidates=[dict(kind="run_hook", hook="inspect", features=["PRIVATE_FEATURE"])],
            selection=dict(index=0), policy_version=arrival["policy_version"]))
        self.platform.store.save("capability_decision", "other", dict(id="other", task_id="another", selection=dict(index=0)))
        activity = self.request("/api/tasks/task/activity")
        self.assertEqual(activity["policy_version"]["id"], version["id"])
        self.assertEqual([item["id"] for item in activity["decisions"]], ["decision"])
        self.assertNotIn("PRIVATE_", json.dumps(activity))
        self.assertIsNone(activity["metrics"]["ram_byte_s"])
        self.assertIsNone(activity["metrics"]["quality"])
        with self.assertRaises(HTTPError):
            self.request("/api/policies/select", dict(version_id=version["id"]))
        self.assertEqual(self.platform.execution.policy_context()["id"], version["id"])
        self.platform.execution.finish("task", delivered=False, status="FAILED")
        self.platform.store.save("task", "task", dict(id="task", status="FAILED", execution=arrival,
            objective_result=self.platform.store.get("capability_task_result", "task"), events=[], calls=[]))
        second = self.request("/api/policies/save", dict(name="后续版本"))
        self.request("/api/policies/select", dict(version_id=second["id"]))
        self.assertEqual(self.request("/api/tasks/task/activity")["policy_version"]["id"], version["id"])

    def test_invalid_training_and_resume_fail_without_changing_live_policy(self):
        version = self.request("/api/policies/save", dict(name="保持使用"))
        self.request("/api/policies/select", dict(version_id=version["id"]))
        for path, body in (("/api/policies/train", dict(mode="fresh", recipe_id="missing")),
                           ("/api/policies/training/resume", dict(job_id="missing"))):
            with self.assertRaises(HTTPError):
                self.request(path, body)
        self.assertEqual(self.request("/api/policies")["active"]["id"], version["id"])
        self.assertEqual(self.platform.store.list("policy_training"), [])


if __name__ == "__main__":
    unittest.main()
