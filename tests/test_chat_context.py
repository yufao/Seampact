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
        return {"base_url": "http://example.invalid", "model": "test", "parameters": {}}

    def role_key(self, role):
        return "frozen-key"


class Catalog:
    def tools(self):
        return [{"type": "function", "function": {"name": "unit", "parameters": {"type": "object"}}}]

    def business_units(self):
        return [{"id": "unit", "tool_name": "unit"}]


class Runtime:
    def snapshot(self):
        return {"legal_actions": [], "used": 0, "quota": 1, "calls": {}}

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
                         placements=[], binding_status="ok", semantic_status="ok")

    def task(self, identity="t", conversation="c"):
        return dict(id=identity, conversation_id=conversation, status="RUNNING", phase="conversation",
                    messages=[dict(role="user", content="question")], model_calls=0, usage=[], calls=[])

    def test_tool_result_is_archived_and_reused_without_executing_again(self):
        request_bodies = []
        responses = [
            dict(choices=[dict(finish_reason="tool_calls", message=dict(role="assistant", content=None,
                tool_calls=[dict(id="call-1", type="function", function=dict(name="execute_harness_plan", arguments="{}"))]))]),
            dict(choices=[dict(finish_reason="stop", message=dict(role="assistant", content="answer"))]),
            dict(choices=[dict(finish_reason="stop", message=dict(role="assistant", content="next answer"))])]
        def send(url, key, body):
            self.assertEqual(key, "frozen-key")
            request_bodies.append(body)
            return responses.pop(0)
        class Owner:
            def __init__(self, plan):
                pass
            def execute(self, catalog, convert):
                return dict(execution_status="RETURN", results=[{"evidence": "actual"}]), True
        self.jobs.transport.send = send
        task = self.task()
        with patch.object(self.jobs, "_plans", return_value=[self.plan]), patch("seampact.chat.NativePlanOwner", Owner):
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

