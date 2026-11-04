"""Frozen task input reaches an original callable through the normal HTTP executor."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from seampact.chat import ChatJobs
from seampact.source.inspection import Inspector
from seampact.runtime import Coordinator, OwnerRuntime
from seampact.server import make_server
from seampact.store import Store
from seampact.units import UnitCatalog


class ExplicitPlanTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root / "original.py").write_text(
            'def echo(value: str) -> str:\n    return value\n'
            'def fail(value: str) -> str:\n    raise ValueError("original failure")\n', encoding="utf-8")
        inspector = Inspector.freeze(root, root / "snapshots", "source", ["original.py"], "goal", "original")
        inspector.request["source_id"] = "source"
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        runtime = OwnerRuntime(Coordinator(self.store))
        self.addCleanup(runtime.close)
        self.catalog = UnitCatalog(root / "platform", self.store, runtime)
        self.catalog.install(inspector, dict(boundaries=[], entries=[
            dict(id=name, path="original.py", symbol=name, kind="function") for name in ("echo", "fail")]), "analysis")
        self.addCleanup(self.catalog.detach, "source")
        self.feedback = []
        scheduler = SimpleNamespace(select_plan=lambda plans, observation: (plans[0], "token"),
                                    feedback=lambda *args: self.feedback.append(args))
        self.jobs = ChatJobs(self.store, None, self.catalog, runtime, scheduler)
        self.server = make_server(SimpleNamespace(chat=self.jobs), port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.api = "http://127.0.0.1:" + str(self.server.server_address[1])

    def submit(self, entry="echo", value="unchanged"):
        unit = next(u for u in self.catalog.public() if u["entry_id"] == entry)
        requirement = ["Run original once with the supplied exact string"]
        plan = dict(requirements=requirement, plans=[dict(id="whole", requirements=requirement,
                    steps=[dict(unit=unit["tool_name"], arguments=dict(value=value))])])
        request = urllib.request.Request(self.api + "/api/plans", data=json.dumps(plan).encode(),
                                         headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=5) as response:
            receipt = json.load(response)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            task = self.store.get("task", receipt["task_id"])
            if task["status"] != "RUNNING":
                return plan, receipt, task
            time.sleep(0.01)
        self.fail("original lightweight plan did not complete")

    def test_exact_unicode_input_no_model_roundtrip_and_saved_identity(self):
        value = "中文：it’s unchanged — \"quotes\"\nnew line\tend"
        with patch.object(self.jobs.transport, "send", side_effect=AssertionError("no model should see the input")):
            plan, receipt, task = self.submit(value=value)
        self.assertEqual(task["status"], "COMPLETED", task.get("error"))
        self.assertEqual(task["plan"]["results"][0]["result"], value)
        self.assertEqual(task["request"], plan)
        expected = hashlib.sha256(json.dumps(plan, ensure_ascii=False, sort_keys=True,
                                             separators=(",", ":")).encode()).hexdigest()
        self.assertEqual(receipt["request_sha256"], expected)
        self.assertEqual(task["model_calls"], 0)
        installed = next(unit for unit in self.catalog.public() if unit["entry_id"] == "echo")
        self.assertEqual(task["plan"]["contract_digests"],
                         {installed["id"]: installed["typed_contract"]["digest"]})
        self.assertEqual(self.feedback[0][0:2], ("token", True))
        self.assertEqual(self.jobs.conversations()["conversations"], [])

    def test_original_failure_and_invalid_arguments_are_preserved(self):
        _, _, task = self.submit(entry="fail")
        self.assertEqual(task["status"], "FAILED")
        self.assertIn("original failure", task["plan"]["results"][0]["error"])
        self.assertEqual(self.feedback[0][0:2], ("token", False))
        before = len(self.store.list("task"))
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.submit(value=42)
        self.assertEqual(raised.exception.code, 400)
        raised.exception.close()
        self.assertEqual(len(self.store.list("task")), before)


if __name__ == "__main__":
    unittest.main()
