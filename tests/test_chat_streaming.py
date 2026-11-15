"""Offline checks for incremental content, complete tools and failed partials."""
import io
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from seampact.chat import ChatJobs
from seampact.providers import ProviderError, Transport, completion_body
from seampact.store import Store


def frame(delta=None, finish=None, **extra):
    return ("data: " + json.dumps(dict(choices=[dict(index=0, delta=delta or {},
        finish_reason=finish)], **extra), ensure_ascii=False) + "\n\n").encode("utf-8")


def completion(text="done", calls=None):
    return dict(choices=[dict(finish_reason="tool_calls" if calls else "stop",
        message=dict(role="assistant", content=text, **({"tool_calls": calls} if calls else {})))])


class ProviderStreamingTests(unittest.TestCase):
    def request(self, raw, visible):
        with patch("urllib.request.build_opener") as opener:
            opener.return_value.open.return_value = io.BytesIO(raw)
            result = Transport().stream("http://example.invalid/chat/completions", "", {}, visible.append)
            body = json.loads(opener.return_value.open.call_args.args[0].data)
            self.assertTrue(body["stream"])
            self.assertTrue(body["stream_options"]["include_usage"])
            return result

    def test_interleaved_tool_fragments_reasoning_and_usage(self):
        raw = frame({"reasoning_content": "private reasoning"}) + frame({"content": "你好"})
        raw += frame({"tool_calls": [dict(index=1, id="second", type="function",
            function=dict(name="read_", arguments="{")), dict(index=0, id="first",
            function=dict(name="execute_", arguments="{"))]})
        raw += frame({"tool_calls": [dict(index=0, function=dict(name="harness_plan", arguments="}")),
            dict(index=1, function=dict(name="conversation_memory", arguments="}"))]})
        raw += frame(finish="tool_calls")
        raw += b'data: {"choices": [], "usage": {"prompt_tokens": 12}}\n\n'
        raw += b"data: [DONE]\n\n"
        visible = []
        result = self.request(raw, visible)
        self.assertEqual(visible, ["你好"])
        message = result["choices"][0]["message"]
        self.assertEqual(message["reasoning_content"], "private reasoning")
        self.assertEqual([c["id"] for c in message["tool_calls"]], ["first", "second"])
        self.assertEqual([c["function"]["name"] for c in message["tool_calls"]],
                         ["execute_harness_plan", "read_conversation_memory"])
        self.assertEqual(message["tool_calls"][0]["function"]["arguments"], "{}")
        self.assertEqual(result["usage"], {"prompt_tokens": 12})

    def test_content_is_delivered_before_the_rest_of_the_response_is_read(self):
        visible = []
        class Response(io.BytesIO):
            def __iter__(self):
                yield b"\n"
                self.assert_incremental()
                yield frame({"content": "second"}, "stop").splitlines(keepends=True)[0]
                yield b"\n"
            def assert_incremental(self):
                if visible != ["first"]:
                    raise AssertionError("first delta was buffered until response end")
        with patch("urllib.request.build_opener") as opener:
            opener.return_value.open.return_value = Response(frame({"content": "first"}))
            result = Transport().stream("http://example.invalid", "", {}, visible.append)
        self.assertEqual(result["choices"][0]["message"]["content"], "firstsecond")

    def test_response_field_evidence_does_not_promote_unknown_reasoning_to_answer(self):
        raw = frame({"reasoning": "private dialect"}) + frame({"content": "answer"}, "stop")
        result = self.request(raw, [])
        evidence = result["_seampact_response_fields"]
        self.assertEqual(evidence["fields"]["reasoning"], dict(chars=15, types=["str"]))
        self.assertEqual(evidence["fields"]["content"]["chars"], 6)
        self.assertNotIn("private dialect", json.dumps(evidence))
        self.assertEqual(result["choices"][0]["message"]["content"], "answer")
        self.assertNotIn("reasoning", result["choices"][0]["message"])

    def test_eof_or_done_without_finish_never_returns_a_complete_tool(self):
        partial = frame({"content": "partial", "tool_calls": [dict(index=0, id="call",
            function=dict(name="execute_harness_plan", arguments="{"))]})
        for ending in (b"", b"data: [DONE]\n\n"):
            visible = []
            with self.assertRaises(ProviderError) as raised:
                self.request(partial + ending, visible)
            self.assertEqual(raised.exception.kind, "response_incomplete")
            self.assertEqual(visible, ["partial"])

    def test_single_json_response_is_accepted_without_retry(self):
        visible = []
        result = self.request(json.dumps(completion("single JSON")).encode(), visible)
        self.assertEqual(result["choices"][0]["message"]["content"], "single JSON")
        self.assertEqual(visible, ["single JSON"])
        self.assertFalse(completion_body(dict(model="test"), [dict(role="user", content="q")])["stream"])

    def test_explicit_stream_rejection_has_a_bounded_diagnostic(self):
        error = urllib.error.HTTPError("http://example.invalid", 400, "private text", {},
            io.BytesIO(b'{"error": {"message": "stream is not supported"}}'))
        with patch("urllib.request.build_opener") as opener:
            opener.return_value.open.side_effect = error
            with self.assertRaises(ProviderError) as raised:
                Transport().stream("http://example.invalid", "", {}, lambda text: None)
        self.assertEqual(raised.exception.kind, "stream_unsupported")
        self.assertNotIn("private text", str(raised.exception))

    def test_optional_usage_rejection_is_distinguished_from_streaming_support(self):
        error = urllib.error.HTTPError("http://example.invalid", 400, "private text", {},
            io.BytesIO(b'{"error": {"message": "stream_options is not supported"}}'))
        with patch("urllib.request.build_opener") as opener:
            opener.return_value.open.side_effect = error
            with self.assertRaises(ProviderError) as raised:
                Transport().stream("http://example.invalid", "", {}, lambda text: None)
        self.assertEqual(raised.exception.kind, "stream_options_unsupported")

    def test_streaming_without_optional_usage_parameter(self):
        visible = []
        with patch("urllib.request.build_opener") as opener:
            opener.return_value.open.return_value = io.BytesIO(frame({"content": "still streamed"}, "stop"))
            Transport().stream("http://example.invalid", "", {}, visible.append, include_usage=False)
            body = json.loads(opener.return_value.open.call_args.args[0].data)
        self.assertTrue(body["stream"])
        self.assertNotIn("stream_options", body)
        self.assertEqual(visible, ["still streamed"])


class ChatStreamingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = Store(Path(temporary.name) / "state.db")
        self.addCleanup(self.store.close)
        self.jobs = ChatJobs(self.store, None, None,
            SimpleNamespace(call=lambda method, *args: []), SimpleNamespace(feedback=lambda *args: None))
        self.bound = SimpleNamespace(role_profile=lambda role: dict(base_url="http://example.invalid",
            model="test", declared_context_window=1000000), role_key=lambda role: "")
        self.catalog = SimpleNamespace(router=None, tools=lambda: [])
        self.task = dict(id="task", conversation_id="conversation", status="RUNNING", phase="conversation",
            messages=[dict(role="user", content="question")], model_calls=0, usage=[], calls=[], response=None)

    def run_task(self):
        self.jobs._run(self.task, self.task["messages"], self.bound, self.catalog)

    def test_running_deltas_then_durable_terminal_record(self):
        observed = []
        def stream(url, key, body, on_delta):
            on_delta("first")
            batch = self.jobs.stream_events("task", timeout=0)
            observed.extend(batch["events"])
            self.assertFalse(batch["done"])
            self.assertEqual(self.store.get("task", "task")["status"], "RUNNING")
            on_delta("second")
            return completion("firstsecond")
        self.jobs.transport.stream = stream
        self.run_task()
        self.assertTrue(any(e["type"] == "delta" and e["text"] == "first" for e in observed))
        final = self.jobs.stream_events("task", max(e["id"] for e in observed), timeout=0)
        self.assertTrue(final["done"])
        self.assertEqual(final["events"][0]["status"], "COMPLETED")
        self.assertEqual(self.store.get("task", "task")["response"], "firstsecond")
        self.assertNotIn("task", self.jobs._streams)

    def test_partial_error_is_failed_not_history_and_is_not_retried(self):
        calls = []
        def stream(url, key, body, on_delta):
            calls.append(body)
            on_delta("unfinished")
            raise ProviderError("stream ended", kind="response_incomplete")
        self.jobs.transport.stream = stream
        self.run_task()
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.task["status"], "FAILED")
        self.assertEqual(self.task["partial_response"], "unfinished")
        self.assertIsNone(self.task["response"])
        self.assertEqual(self.jobs.conversation("conversation")["messages"], self.task["messages"])
        self.assertFalse(any(m["role"] == "assistant" for m in self.task["wire_messages"]))

    def test_only_explicit_rejection_can_fallback_once(self):
        self.jobs.transport.stream = lambda *args: (_ for _ in ()).throw(
            ProviderError("unsupported", kind="stream_unsupported"))
        sent = []
        self.jobs.transport.send = lambda url, key, body: sent.append(body) or completion("fallback")
        self.run_task()
        self.assertEqual(self.task["status"], "COMPLETED")
        self.assertEqual(self.task["model_calls"], 2)
        self.assertEqual(len(sent), 1)
        self.assertFalse(sent[0]["stream"])

    def test_optional_usage_rejection_keeps_streaming_and_counts_both_requests(self):
        options = []
        def stream(url, key, body, on_delta, *, include_usage=True):
            options.append(include_usage)
            if include_usage:
                raise ProviderError("unsupported option", kind="stream_options_unsupported")
            on_delta("still ")
            on_delta("streamed")
            return completion("still streamed")
        self.jobs.transport.stream = stream
        self.jobs.transport.send = lambda *args: self.fail("optional rejection must preserve streaming")
        self.run_task()
        self.assertEqual(options, [True, False])
        self.assertEqual(self.task["status"], "COMPLETED")
        self.assertEqual(self.task["model_calls"], 2)
        self.assertEqual(self.task["response"], "still streamed")

    def test_partial_response_never_falls_back_even_with_a_retryable_error_kind(self):
        def stream(url, key, body, on_delta):
            on_delta("received")
            raise ProviderError("unsupported", kind="stream_unsupported")
        self.jobs.transport.stream = stream
        self.jobs.transport.send = lambda *args: self.fail("must not replay partial completion")
        self.run_task()
        self.assertEqual(self.task["status"], "FAILED")
        self.assertEqual(self.task["partial_response"], "received")
        self.assertEqual(self.task["model_calls"], 1)

    def test_complete_fragmented_tool_executes_once_and_failed_followup_does_not_replay_it(self):
        tool = frame({"tool_calls": [dict(index=0, id="call", type="function",
            function=dict(name="real_", arguments="{"))]})
        tool += frame({"tool_calls": [dict(index=0, function=dict(name="tool", arguments="}"))]}, "tool_calls")
        followup = frame({"content": "unfinished"})
        self.catalog.tools = lambda: [dict(type="function", function=dict(name="real_tool", parameters=dict(type="object")))]
        self.catalog.name_to_id = lambda name: "unit"
        executions = []
        self.catalog.invoke = lambda *args, **kwargs: executions.append("once") or dict(output="actual", files=[])
        with patch("urllib.request.build_opener") as opener:
            opener.return_value.open.side_effect = [io.BytesIO(tool), io.BytesIO(followup),
                io.BytesIO(frame({"content": "continued"}, "stop"))]
            self.run_task()
            self.assertEqual(self.task["status"], "FAILED")
            next_task = dict(self.task, id="next", status="RUNNING", model_calls=0, usage=[], response=None)
            next_messages = self.task["messages"] + [dict(role="user", content="continue")]
            self.jobs._run(next_task, next_messages, self.bound, self.catalog,
                           self.task["wire_messages"], self.task["context_messages"])
        self.assertEqual(executions, ["once"])
        self.assertEqual(next_task["status"], "COMPLETED")
        self.assertEqual(next_task["response"], "continued")

    def test_lagging_cursor_receives_current_snapshot_without_unbounded_event_history(self):
        self.store.save("task", "task", self.task)
        self.jobs._stream_event("task", "reset")
        for _ in range(300):
            self.jobs._stream_event("task", "delta", "x")
        batch = self.jobs.stream_events("task", after=0, timeout=0)
        self.assertEqual(batch["events"], [dict(id=301, type="reset", text="x" * 300)])
        self.assertEqual(len(self.jobs._streams["task"]["events"]), 256)


if __name__ == "__main__":
    unittest.main()
