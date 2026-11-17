"""Conversation memory stays scoped and preserves actual tool results."""
import json
import sys
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from seampact.chat import ChatJobs
from seampact.store import Store


class Bound:
    def role_profile(self, role):
        return {"base_url": "http://example.invalid", "model": "test", "parameters": {},
                "declared_context_window": 1000000}

    def role_key(self, role):
        return "frozen-key"


class Catalog:
    router = None

    def tools(self):
        return [{"type": "function", "function": {"name": "unit", "parameters": {"type": "object"}}}]

    def business_units(self):
        return [{"id": "unit", "tool_name": "unit"}]

    def name_to_id(self, name):
        return "unit"

    def invoke(self, unit_id, arguments, **kwargs):
        return dict(output={"evidence": "actual"}, files=[])


class Runtime:
    def call(self, method, *args):
        if method == "status_view":
            return {"legal_actions": [], "used": 0, "quota": 1}
        if method == "task_calls":
            return []
        raise AssertionError(method)

    def context(self, identity):
        return nullcontext()


class Scheduler:
    def select_plan(self, plans, observation):
        return plans[0], "token"

    def feedback(self, *args):
        pass


class ChatContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "state.db")
        self.addCleanup(self.store.close)
        self.jobs = ChatJobs(self.store, None, None, Runtime(), Scheduler())
        self.plan = dict(id="p", units=["unit"], requirements=[], data_edges=[], control_edges=[],
                         steps=[dict(unit_id="unit",contract_digest="fixture")],
                         placements=[], binding_status="ok", semantic_status="ok")

    def task(self, identity="t", conversation="c"):
        return dict(id=identity, conversation_id=conversation, status="RUNNING", phase="conversation",
                    messages=[dict(role="user", content="question")], model_calls=0, usage=[], calls=[])

    def test_configured_deadline_cancels_a_running_business_task(self):
        task = self.task()
        task["deadline_s"] = 0.05
        self.store.save("task", task["id"], task)

        def run_until_cancelled(task, messages, bound, *, control, **kwargs):
            self.assertTrue(control.cancelled.wait(2))
            task["status"] = "CANCELLED"

        with patch("seampact.chat.AgentRuntime.run", side_effect=run_until_cancelled):
            self.jobs._run(task, task["messages"], Bound(), Catalog())
        self.assertEqual(task["status"], "CANCELLED")
        self.assertNotIn(task["id"], self.jobs._controls)

    def test_execution_budget_is_independent_of_evaluation_deadline(self):
        for execution_timeout, evaluation_deadline, should_cancel in ((None, 0.05, False), (0.05, 900, True)):
            with self.subTest(execution_timeout=execution_timeout):
                task = self.task(identity="task-" + str(execution_timeout))
                task.update(deadline_s=evaluation_deadline, execution_timeout_s=execution_timeout)
                self.store.save("task", task["id"], task)

                def observe_control(task, messages, bound, *, control, **kwargs):
                    self.assertEqual(control.cancelled.wait(0.2), should_cancel)
                    request = control.model_request()
                    if execution_timeout is None:
                        self.assertIsNone(request.timeout())
                        self.assertIsNone(request.deadline_at)
                    task["status"] = "CANCELLED" if should_cancel else "COMPLETED"

                with patch("seampact.chat.AgentRuntime.run", side_effect=observe_control):
                    self.jobs._run(task, task["messages"], Bound(), Catalog())
                self.assertEqual(task["deadline_s"], evaluation_deadline)
                self.assertNotIn(task["id"], self.jobs._controls)

    def test_tool_result_is_archived_and_reused_without_executing_again(self):
        request_bodies = []
        responses = [
            dict(choices=[dict(finish_reason="tool_calls", message=dict(role="assistant", content=None,
                tool_calls=[dict(id="call-1", type="function", function=dict(name="unit", arguments="{}"))]))]),
            dict(choices=[dict(finish_reason="stop", message=dict(role="assistant", content="answer"))]),
            dict(choices=[dict(finish_reason="stop", message=dict(role="assistant", content="next answer"))])]
        def send(url, key, body):
            self.assertEqual(key, "frozen-key")
            request_bodies.append(body)
            return responses.pop(0)
        self.jobs.transport.send = send
        self.jobs.transport.stream = lambda url, key, body, on_delta: send(url, key, body)
        task = self.task()
        self.jobs._run(task, task["messages"], Bound(), Catalog())
        self.assertEqual(task["status"], "COMPLETED")
        result_message = next(m for m in task["wire_messages"] if m["role"] == "tool")
        ref = json.loads(result_message["content"])["archive_ref"]
        from seampact.context_memory import ContextMemory
        archive = ContextMemory(self.store, scope="chat/c").read(ref, path="/messages/0/content", count=2000)
        self.assertIn("actual", json.dumps(archive))
        with self.assertRaises((ValueError, KeyError)):
            ContextMemory(self.store, scope="chat/other").read(ref, path="/messages/0/content", count=2000)
        next_task = self.task("t2")
        next_messages = task["messages"] + [dict(role="assistant", content="answer"), dict(role="user", content="next")]
        self.jobs._run(next_task, next_messages, Bound(), Catalog(), task["wire_messages"], task["context_messages"])
        self.assertEqual(next_task["status"], "COMPLETED")
        self.assertTrue(any(m.get("tool_call_id") == "call-1" for m in request_bodies[-1]["messages"]))
        self.assertEqual(len(request_bodies), 3)

    def test_context_error_compacts_once_and_counts_summary(self):
        from seampact.providers import ProviderError
        requests = []
        def send(url, key, body):
            requests.append((key, body))
            if len(requests) == 1:
                raise ProviderError("too long", kind="context_length")
            if len(requests) == 2:
                return dict(usage={"prompt_tokens": 40}, choices=[dict(finish_reason="stop",
                    message=dict(role="assistant", content="earlier facts"))])
            return dict(usage={"prompt_tokens": 20}, choices=[dict(finish_reason="stop",
                message=dict(role="assistant", content="done"))])
        self.jobs.transport.send = send
        self.jobs.transport.stream = lambda url, key, body, on_delta: send(url, key, body)
        prior = [dict(role="system", content="system"), dict(role="user", content="first"),
                 dict(role="assistant", content="old evidence " * 500),
                 dict(role="user", content="second"), dict(role="assistant", content="second answer"),
                 dict(role="user", content="third"), dict(role="assistant", content="third answer"),
                 dict(role="user", content="fourth"), dict(role="assistant", content="fourth answer")]
        task = self.task()
        self.jobs._run(task, [dict(role="user", content="next")], Bound(), Catalog(), prior, prior)
        self.assertEqual(task["status"], "COMPLETED")
        self.assertEqual(task["model_calls"], 3)
        self.assertEqual(task["usage"], [None, {"prompt_tokens": 40}, {"prompt_tokens": 20}])
        self.assertEqual(task["context_events"][0]["operation"], "context_compaction")
        self.assertTrue(all(key == "frozen-key" for key, _ in requests))
        self.assertTrue(any("old evidence" in str(m) for m in task["wire_messages"]))
        self.assertFalse(any("old evidence" in str(m) for m in requests[-1][1]["messages"]))

    def test_interrupted_tool_group_is_closed_without_reexecution(self):
        sent = []
        self.jobs.transport.send = lambda url, key, body: sent.append(body) or dict(
            choices=[dict(finish_reason="stop", message=dict(role="assistant", content="done"))])
        self.jobs.transport.stream = lambda url, key, body, on_delta: self.jobs.transport.send(url, key, body)
        prior = [dict(role="system", content="system"), dict(role="user", content="first"),
                 dict(role="assistant", content=None, tool_calls=[dict(id="lost", type="function",
                     function=dict(name="execute_harness_plan", arguments="{}"))])]
        task = self.task()
        self.jobs._run(task, [dict(role="user", content="next")], Bound(), Catalog(), prior, prior)
        tool = next(m for m in sent[0]["messages"] if m.get("tool_call_id") == "lost")
        self.assertIn("中断", tool["content"])
        self.assertEqual(task["status"], "COMPLETED")


if __name__ == "__main__":
    unittest.main()

