"""Polling reads active tasks and bounded summaries; full records are read on demand."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from seampact.chat import ChatJobs
from seampact.capability_contract import digest
from seampact.run_history import history_summary
from seampact.service import TASK_PAGE, Platform
from seampact.store import Store


def chat_task(identity, conversation, status="COMPLETED", text="问题", stamp="2026-01-01T00:00:00+00:00"):
    return dict(id=identity, status=status, phase="conversation", conversation_id=conversation,
                messages=[dict(role="user", content=text)], response="回答 " + identity,
                wire_messages=[dict(role="tool", content="x" * 2000)], context_messages=[dict(role="user", content=text)],
                started_at=stamp, finished_at=None if status == "RUNNING" else stamp, latency_ms=5, calls=[])


class TaskQueries(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = Store(Path(self.directory.name) / "state.sqlite3")
        self.addCleanup(self.store.close)

    def plans(self, query):
        """Query plans of the task statements one store call actually executes."""
        statements = []
        self.store.db.set_trace_callback(statements.append)
        try:
            query()
        finally:
            self.store.db.set_trace_callback(None)
        return [" ".join(row[3] for row in self.store.db.execute("EXPLAIN QUERY PLAN " + statement))
                for statement in statements if "documents AS task" in statement]

    def test_pages_are_bounded_summaries_in_save_order(self):
        for index in range(7):
            self.store.save("task", "t%d" % index, chat_task("t%d" % index, "c%d" % index))
        self.store.hide_tasks(["t5"])
        first, cursor = self.store.task_summaries(3)
        self.assertEqual([task["id"] for task in first], ["t6", "t4", "t3"])
        self.assertTrue(all("wire_messages" not in task and "messages" not in task and task["response"] for task in first))
        second, cursor = self.store.task_summaries(3, cursor)
        self.assertEqual(([task["id"] for task in second], cursor), (["t2", "t1", "t0"], None))
        # The record itself is untouched and still read whole by identity.
        self.assertEqual(len(self.store.get("task", "t6")["wire_messages"][0]["content"]), 2000)

    def test_active_and_recent_queries_use_indexes_not_history_scans(self):
        for index in range(40):
            self.store.save("task", "t%d" % index, chat_task("t%d" % index, "c%d" % (index % 4)))
        self.store.save("other", "x", dict(id="x"))
        for query, index in ((lambda: self.store.tasks_with_status("RUNNING", "CANCELLING"), "task_status"),
                             (lambda: self.store.task_counts(), "task_status"),
                             (lambda: self.store.task_summaries(5), "documents_kind"),
                             (lambda: self.store.task_summaries(5, 20), "documents_kind"),
                             (lambda: next(self.store.conversation_tasks("c1")), "task_conversation"),
                             (lambda: self.store.tasks_finished_since("2026"), "task_finished")):
            plans = self.plans(query)
            self.assertTrue(any(index in plan for plan in plans), (index, plans))
            for plan in plans:
                self.assertNotIn("SCAN task", plan)
                self.assertNotIn("TEMP B-TREE", plan)
                # Within a kind, only the recency index may walk rows; it stops at the page limit.
                self.assertTrue(index == "documents_kind" or "documents_kind" not in plan, (index, plan))

    def test_status_counts_and_recent_ends(self):
        self.store.save("task", "old", chat_task("old", "c", stamp="2025-06-01T00:00:00+00:00"))
        self.store.save("task", "new", chat_task("new", "c", "FAILED", stamp="2026-02-01T00:00:00+00:00"))
        self.store.save("task", "live", chat_task("live", "c", "RUNNING"))
        self.assertEqual(self.store.task_counts(), dict(total=3, running=1))
        self.assertEqual([task["id"] for task in self.store.tasks_with_status("RUNNING", "CANCELLING")], ["live"])
        self.assertEqual(self.store.tasks_finished_since("2026-01-01"),
                         [dict(status="FAILED", finished_at="2026-02-01T00:00:00+00:00", latency_ms=5)])

    def test_conversations_read_heads_and_only_their_latest_task(self):
        self.store.save("task", "a1", chat_task("a1", "a", text="第一段 会话"))
        self.store.save("task", "b1", chat_task("b1", "b", text="另一段"))
        self.store.save("task", "a2", chat_task("a2", "a", text="第一段 会话"))
        self.store.save("task", "plan", dict(id="plan", origin="EXPLICIT_TASK_PLAN", status="COMPLETED", phase="harness_execution"))
        jobs = ChatJobs(self.store, None, None, None, None)
        listed = jobs.conversations()
        self.assertEqual([(row["id"], row["turns"], row["title"]) for row in listed["conversations"]],
                         [("a", 2, "第一段 会话"), ("b", 1, "另一段")])
        self.assertEqual(jobs.conversations(limit=1, offset=1), dict(total=2, conversations=listed["conversations"][1:]))
        self.assertEqual([row["id"] for row in jobs.conversations("回答 A1")["conversations"]], ["a"])
        with patch.object(self.store, "list", side_effect=AssertionError("full history decode")):
            detail = jobs.conversation("a")
        self.assertEqual((detail["task"]["id"], detail["messages"][-1]["content"]), ("a2", "回答 a2"))
        with self.assertRaisesRegex(ValueError, "没有这段"):
            jobs.conversation("plan")


class PlatformPaging(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.platform = Platform(Path(self.directory.name) / "platform")

    def tearDown(self):
        self.platform.close()
        self.directory.cleanup()

    def test_state_carries_running_tasks_and_one_page(self):
        store = self.platform.store
        store.save("task", "live", chat_task("live", "live", "RUNNING"))
        for index in range(TASK_PAGE + 5):
            store.save("task", "t%03d" % index, chat_task("t%03d" % index, "c"))
        with patch.object(store, "task_history", side_effect=AssertionError("full history")), \
             patch.object(store, "list", wraps=store.list) as listed:
            state = self.platform.state()
        self.assertNotIn("task", [call.args[0] for call in listed.call_args_list])
        identities = [task["id"] for task in state["tasks"]]
        self.assertEqual(identities[0], "live")
        self.assertEqual(len(identities), TASK_PAGE + 1)
        self.assertTrue(all("wire_messages" not in task for task in state["tasks"]))
        older = self.platform.tasks(before=state["tasks_older"])
        self.assertEqual(([task["id"] for task in older["tasks"]], older["older"]),
                         (["t004", "t003", "t002", "t001", "t000", "live"], None))
        full = self.platform.task("t000")
        self.assertEqual(full["wire_messages"][0]["content"], "x" * 2000)
        store.hide_tasks(["t000"])
        with self.assertRaisesRegex(ValueError, "没有这个任务"):
            self.platform.task("t000")

    def test_run_progress_is_incremental_and_summarized(self):
        jobs = self.platform.training
        manifest = dict(episodes=3)
        folder = jobs.directory / "run"
        folder.mkdir()
        (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        self.platform.store.save("scheduler_run", "run", dict(id="run", status="RUNNING", config_digest=digest(manifest),
            joint=dict(windows=2, service_windows=[1, 2], route_windows=[1, 2], resource_windows=[1, 2])))
        window = dict(kind="joint_window", episode=1, reward=0.5, eligible=True, parallelism=dict(mean_active_roots=1.0),
                      tasks=[dict(calls=["large"])], transitions=[1, 2], resource_ops=[dict(id=1), dict(id=2)],
                      service=dict(arrived=2))
        def append_record(name, record):
            with (folder / (name + ".jsonl")).open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record) + "\n")

        append_record("history", window)
        append_record("curves", dict(episode=1, reward=0.5, metrics=["large"], stats=dict(loss=1)))
        self.assertEqual(history_summary(window), dict(kind="joint_window", episode=1, reward=0.5, eligible=True,
            parallelism=dict(mean_active_roots=1.0), service=dict(arrived=2), resource_ops_count=2))
        first = jobs.progress("run")
        self.assertEqual((first["history_total"], first["curves"]), (1, [dict(episode=1, reward=0.5)]))
        self.assertNotIn("tasks", first["history"][0])
        with (folder / "history.jsonl").open("ab") as stream:
            stream.write(b'{"episode": 2, "partial"')
        self.assertEqual(jobs.progress("run", first["history_total"], first["curves_total"])["history"], [])
        with (folder / "history.jsonl").open("ab") as stream:
            stream.write(b': true}\n')
        append_record("history", dict(window, episode=3))
        later = jobs.progress("run", first["history_total"], first["curves_total"])
        self.assertEqual(([item["episode"] for item in later["history"]], later["history_total"], later["curves"]),
                         ([2, 3], 3, []))
        # Export and replay still read every complete record in full.
        detail = jobs.detail("run")
        self.assertEqual([item["episode"] for item in detail["history"]], [1, 2, 3])
        self.assertEqual(detail["history"][0]["tasks"], [dict(calls=["large"])])
        self.assertEqual(jobs.record("run")["manifest"], manifest)
        self.assertNotIn("history", jobs.record("run"))
        # Polling keeps run totals and leaves per-window series to the run's own reads.
        polled = jobs.snapshot()["runs"][0]["joint"]
        self.assertEqual(polled, dict(windows=2))
        self.assertEqual(self.platform.store.get("scheduler_run", "run")["joint"]["service_windows"], [1, 2])


if __name__ == "__main__":
    unittest.main()
