"""The generic host owns tools, required gates, delivery and cancellation."""
import json
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from seampact.agent_runtime import AgentHook, AgentRuntime, RunControl, model_connection
from seampact.providers import ProviderError
from seampact.store import Store


def completion(text=None, calls=None):
    message = dict(role="assistant", content=text)
    if calls:
        message["tool_calls"] = calls
    return dict(choices=[dict(finish_reason="tool_calls" if calls else "stop", message=message)])


class AgentRuntimeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = Store(Path(temporary.name) / "state.db")
        self.addCleanup(self.store.close)
        self.catalog = SimpleNamespace(tools=lambda: [], invoke=lambda *a, **kw: None, router=None)
        self.profiles = SimpleNamespace(role_profile=lambda role: dict(base_url="http://example.invalid",
            model="test", declared_context_window=1000000), role_key=lambda role: "")
        self.task = dict(id="task", conversation_id="conversation", status="RUNNING")
        self.messages = [dict(role="user", content="question")]
        self.events = []

    def run_host(self, stream, **kwargs):
        transport = SimpleNamespace(stream=stream)
        host = AgentRuntime(self.store, self.catalog, transport=transport, **kwargs)
        return host.run(self.task, self.messages, self.profiles, on_event=self.events.append)

    def test_direct_response_streams_without_a_synthetic_check_or_decision(self):
        lifecycle = []
        scheduler = SimpleNamespace(begin=lambda *a, **kw: lifecycle.append(("begin", kw)),
                                    finish=lambda *a, **kw: lifecycle.append(("finish", kw)))
        def stream(url, key, body, delta):
            delta("hello")
            self.assertTrue(any(event["kind"] == "delta" for event in self.events))
            return completion("hello")
        self.run_host(stream, scheduler=scheduler)
        self.assertEqual(self.task["response"], "hello")
        self.assertEqual(lifecycle[-1][1]["quality"], None)
        self.assertFalse(any(event["kind"] == "hook_started" for event in self.events))

    def test_complete_arguments_files_and_model_continuation_use_one_dispatcher(self):
        calls, bodies = [], []
        self.catalog.tools = lambda: [dict(type="function", function=dict(name="create_file", parameters=dict(type="object")))]
        self.catalog.name_to_id = lambda name: "capability-id"
        expected = dict(nested={"text": "a\\b\n中文", "values": [1, 2]}, optional=False)
        file = dict(name="report.txt", sha256="abc", media_type="text/plain")
        def dispatch(unit_id, arguments, **options):
            calls.append((unit_id, arguments, options))
            return dict(output={"real": True}, files=[file], call={"call_id": "real-call"})
        def stream(url, key, body, delta):
            bodies.append(body)
            if len(bodies) == 1:
                return completion(calls=[dict(id="tool-1", type="function", function=dict(
                    name="create_file", arguments=json.dumps(expected)))])
            self.assertIn("real", bodies[-1]["messages"][-1]["content"])
            delta("done")
            return completion("done")
        self.run_host(stream, dispatch=dispatch)
        self.assertEqual(calls[0][:2], ("capability-id", expected))
        self.assertEqual(calls[0][2]["run_id"], "task")
        self.assertEqual(self.task["files"], [file])
        self.assertEqual(self.task["calls"], [{"call_id": "real-call"}])
        self.assertEqual(len(bodies), 2)

    def test_required_input_tool_and_delivery_hooks_cannot_be_omitted(self):
        invoked, checked = [], []
        self.catalog.tools = lambda: [dict(type="function", function=dict(name="write", parameters=dict(type="object")))]
        self.catalog.name_to_id = lambda name: "write-id"
        hooks = [AgentHook(event, event, lambda payload, context, e=event: checked.append(e))
                 for event in ("input_received", "before_tool", "after_tool", "before_delivery")]
        hooks.append(AgentHook("optional", "input_received", lambda *a: self.fail("optional hook omitted"), required=False))
        requests = []
        def stream(url, key, body, delta):
            requests.append(body)
            if len(requests) == 1:
                return completion(calls=[dict(id="c", function=dict(name="write", arguments='{"value": 7}'))])
            delta("ok")
            return completion("ok")
        self.run_host(stream, hooks=hooks, optional_hooks=lambda *a: [],
                      dispatch=lambda *a, **kw: invoked.append(a) or dict(output="written"))
        self.assertEqual(invoked, [("write-id", {"value": 7})])
        self.assertEqual(checked, ["input_received", "before_tool", "after_tool", "before_delivery"])

    def test_required_tool_block_precedes_external_execution(self):
        self.catalog.tools = lambda: [dict(type="function", function=dict(name="write", parameters=dict(type="object")))]
        def stream(*args):
            return completion(calls=[dict(id="c", function=dict(name="write", arguments="{}"))])
        self.run_host(stream, hooks=[AgentHook("guard", "before_tool", lambda *a: False)],
                      dispatch=lambda *a, **kw: self.fail("blocked call executed"))
        self.assertEqual(self.task["status"], "BLOCKED")

    def test_full_delivery_check_buffers_raw_stream_and_replaces_only_after_check(self):
        def check(payload, context):
            self.assertEqual(payload["text"], "firstsecond")
            self.assertFalse(any(event["kind"] == "delta" for event in self.events))
            return dict(action="replace", text="approved")
        def stream(url, key, body, delta):
            delta("first")
            delta("second")
            self.assertFalse(any(event["kind"] == "delta" for event in self.events))
            return completion("firstsecond")
        self.run_host(stream, hooks=[AgentHook("whole", "before_delivery", check, mode="complete")])
        self.assertEqual(self.task["response"], "approved")
        self.assertEqual([event["text"] for event in self.events if event["kind"] == "delta"], ["approved"])

    def test_buffered_partial_response_cannot_fallback_or_become_delivered(self):
        count = []
        def stream(url, key, body, delta):
            count.append(1)
            delta("unverified")
            raise ProviderError("unsupported", kind="stream_unsupported")
        self.run_host(stream, hooks=[AgentHook("whole", "before_delivery", lambda *a: None, mode="complete")])
        self.assertEqual(count, [1])
        self.assertEqual(self.task["status"], "FAILED")
        self.assertEqual(self.task["partial_response"], "")
        self.assertEqual(self.task["generation"]["received_chars"], len("unverified"))

    def test_cancellation_stops_delivery_without_claiming_provider_stop(self):
        control = RunControl()
        def stream(url, key, body, delta):
            delta("first")
            control.cancel()
            delta("second")
            self.fail("cancelled callback returned")
        host = AgentRuntime(self.store, self.catalog, transport=SimpleNamespace(stream=stream))
        host.run(self.task, self.messages, self.profiles, on_event=self.events.append, control=control)
        self.assertEqual(self.task["status"], "CANCELLED")
        self.assertEqual(self.task["partial_response"], "first")
        self.assertEqual(self.task["cancel"]["model_stop"], "UNKNOWN")

    def test_received_noncontent_fragments_also_prevent_context_replay(self):
        calls = []
        def stream(url, key, body, delta, *, control):
            calls.append(1)
            control.progress(b'data: {"reasoning_content": "already generated"}')
            raise ProviderError("context error after data", kind="context_length")
        self.run_host(stream)
        self.assertEqual(calls, [1])
        self.assertEqual(self.task["status"], "FAILED")

    def test_managed_model_uses_dependency_readiness_and_real_request_leases(self):
        operations = []
        @contextmanager
        def request_scope(groups, owner, run_id):
            operations.append(("request", groups, owner, run_id))
            yield
            operations.append(("request_end",))
        specs = {"root": dict(id="root", depends_on=["dependency"]), "dependency": dict(id="dependency")}
        manager = SimpleNamespace(validate_connection=lambda profile: specs["root"], get=lambda identity: specs[identity],
            placement=lambda identity: dict(group_id="service:" + identity), request_scope=request_scope,
            acquire=lambda *a, **kw: operations.append(("acquire", a, kw)),
            release_lease=lambda owner: operations.append(("release", owner)),
            deployments=SimpleNamespace(ensure_ready=lambda placement, unit: operations.append(("ready", placement))))
        profiles = SimpleNamespace(managed_services=manager,
            role_profile=lambda role: dict(resource_kind="managed"), role_key=lambda role: "")
        with model_connection("chat", profiles, "task"):
            operations.append(("model",))
        self.assertEqual([item[1]["group_id"] for item in operations if item[0] == "ready"],
                         ["service:dependency", "service:root"])
        self.assertLess(next(i for i, item in enumerate(operations) if item[0] == "request"),
                        next(i for i, item in enumerate(operations) if item[0] == "model"))
        self.assertEqual(operations[-1], ("release", "task"))


if __name__ == "__main__":
    unittest.main()
