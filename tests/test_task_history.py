"""Ended task panel cleanup preserves execution, conversation and run evidence."""

import copy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from seampact.service import Platform
from seampact.store import Store, now


class TaskHistoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name) / "platform"
        self.platform = Platform(self.home)
        self.addCleanup(self.platform.close)
        self.store = self.platform.store

    def task(self, identity, status="COMPLETED", **fields):
        task = dict(id=identity, status=status, finished_at=now(), phase="conversation",
                    conversation_id="conversation", messages=[dict(role="user", content="original input")],
                    response="original result", calls=[], request_sha256="frozen-request")
        task.update(fields)
        self.store.save("task", identity, task)
        return task

    def test_batch_cleanup_preserves_chat_research_and_request_evidence_after_reopen(self):
        first = self.task("first")
        second = self.task("second", "FAILED", policy_run_id="research")
        run = dict(id="research", status="COMPLETED", evidence_task_ids=["second"])
        self.store.save("scheduler_run", "research", run)
        self.task("running", "RUNNING", conversation_id="other")
        result = self.platform.delete_tasks(dict(task_ids=["first", "second", "first"]))
        self.assertEqual(result, dict(deleted=["first", "second"]))
        self.assertEqual([t["id"] for t in self.platform.state()["tasks"]], ["running"])
        self.assertEqual(self.store.get("task", "first"), first)
        self.assertEqual(self.store.get("task", "second"), second)
        self.assertEqual(self.store.get("scheduler_run", "research"), run)
        self.assertEqual(self.platform.chat.conversation("conversation")["task"]["id"], "second")
        self.assertEqual(len(self.store.list("task")), 3)
        marker = self.store.get("task_deleted", "first")
        self.assertEqual(self.platform.delete_tasks(dict(task_ids=["first"])), dict(deleted=["first"]))
        self.assertEqual(self.store.get("task_deleted", "first"), marker)
        reopened = Store(self.store.path)
        try:
            self.assertEqual([t["id"] for t in reopened.task_history()], ["running"])
            self.assertEqual(reopened.get("task", "first")["request_sha256"], "frozen-request")
        finally:
            reopened.close()

    def test_bad_batch_cannot_partially_hide_finished_records(self):
        self.task("done")
        self.task("running", "RUNNING")
        for body in [None, {}, dict(task_ids=[]), dict(task_ids="done"), dict(task_ids=["done", None]),
                     dict(task_ids=["done", " "]), dict(task_ids=["done", "missing"]),
                     dict(task_ids=["done", "running"])]:
            with self.subTest(body=body), self.assertRaises(ValueError):
                self.platform.delete_tasks(body)
            self.assertIsNone(self.store.get("task_deleted", "done"))

    def test_terminal_states_allow_cleanup_but_blocked_and_unknown_do_not(self):
        finished = ["COMPLETED", "FAILED", "INTERRUPTED", "CANCELLED", "CANCELED", "TIMEOUT", "TIMED_OUT", "STOPPED"]
        for status in finished:
            self.task(status, status)
        self.assertEqual(self.platform.delete_tasks(dict(task_ids=finished))["deleted"], finished)
        for status in ["BLOCKED", "UNKNOWN", "QUEUED", "CANCELLING", "RUNNING"]:
            self.task(status, status)
            with self.subTest(status=status), self.assertRaisesRegex(ValueError, "已结束"):
                self.platform.delete_tasks(dict(task_ids=[status]))

    def test_unretired_calls_keep_even_failed_tasks_visible(self):
        self.task("done", "FAILED")
        for physical in ["ENABLED", "RESERVED", "AUTHORIZED", "STARTED", "UNKNOWN"]:
            snapshot = dict(calls=dict(call=dict(task_id="done", physical=physical)), roots={})
            with self.subTest(physical=physical), patch.object(self.platform.runtime, "snapshot", return_value=snapshot):
                with self.assertRaisesRegex(ValueError, "调用"):
                    self.platform.delete_tasks(dict(task_ids=["done"]))
            self.assertIsNone(self.store.get("task_deleted", "done"))
        snapshot["calls"]["call"]["physical"] = "RETIRED"
        with patch.object(self.platform.runtime, "snapshot", return_value=snapshot):
            self.platform.delete_tasks(dict(task_ids=["done"]))

    def test_unended_roots_and_owner_leases_block_cleanup(self):
        self.task("done")
        for state in ["ENABLED", "ACTIVE", "UNKNOWN"]:
            snapshot = dict(calls={}, roots=dict(root=dict(task_id="done", state=state)))
            with self.subTest(state=state), patch.object(self.platform.runtime, "snapshot", return_value=snapshot):
                with self.assertRaisesRegex(ValueError, "根请求"):
                    self.platform.delete_tasks(dict(task_ids=["done"]))
        self.platform.deployments.affinity[("done", "group")] = "instance"
        with self.assertRaisesRegex(ValueError, "实例"):
            self.platform.delete_tasks(dict(task_ids=["done"]))
        self.platform.deployments.affinity.clear()
        self.platform.managed_services.leases["service:one"] = {"done": dict(unknown=True)}
        with self.assertRaisesRegex(ValueError, "模型服务"):
            self.platform.delete_tasks(dict(task_ids=["done"]))
        self.assertIsNone(self.store.get("task_deleted", "done"))
        self.platform.managed_services.leases.clear()

    def test_write_failure_rolls_back_the_entire_batch(self):
        self.task("one")
        self.task("two")
        def partial_write(identities):
            self.store.save("task_deleted", identities[0], dict(id=identities[0]))
            raise OSError("storage write failed")
        with patch.object(self.store, "hide_tasks", side_effect=partial_write):
            with self.assertRaisesRegex(OSError, "storage write failed"):
                self.platform.delete_tasks(dict(task_ids=["one", "two"]))
        self.assertEqual(self.store.list("task_deleted"), [])
        self.assertEqual(len(self.store.task_history()), 2)

    def test_state_is_rechecked_after_runtime_observation(self):
        task = self.task("done")
        def observe():
            changed = copy.deepcopy(task)
            changed["status"] = "RUNNING"
            self.store.save("task", "done", changed)
            return dict(calls={}, roots={})
        with patch.object(self.platform.runtime, "snapshot", side_effect=observe):
            with self.assertRaisesRegex(ValueError, "已结束"):
                self.platform.delete_tasks(dict(task_ids=["done"]))
        self.assertIsNone(self.store.get("task_deleted", "done"))


if __name__ == "__main__":
    unittest.main()
