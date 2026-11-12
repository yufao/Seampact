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
        self.assertEqual(self.task["calls"], [{"call_id": "real-call", "unit_id": "capability-id"}])
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

    def test_private_hook_events_keep_only_accounting_and_call_identity(self):
        def dispatch(unit, args, **options):
            options["on_event"](dict(kind="claims_extracted", call_id="c", data={"claim_text": "private claim"}))
            options["on_event"](dict(kind="model_usage", call_id="c", data=dict(
                request_id="provider-id", usage_returned=True,
                usage=dict(prompt_tokens=3, completion_tokens=2, private="usage text"))))
            return dict(output=dict(claim="private claim"), call=dict(call_id="c"))
        def check(payload, context):
            context.invoke("fact", {}, private_events=True)
            return dict(action="allow", fact_support="unknown")
        def stream(url, key, body, delta):
            delta("answer")
            return completion("answer")
        self.run_host(stream, hooks=[AgentHook("fact", "before_delivery", check, mode="complete")], dispatch=dispatch)
        self.assertEqual(self.task["response"], "answer")
        self.assertNotIn("private claim", json.dumps(self.task))
        self.assertNotIn("usage text", json.dumps(self.task))
        self.assertEqual(self.task["capability_usage"][0]["model_usage"][0]["data"]["usage"],
                         dict(prompt_tokens=3, completion_tokens=2))

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

    def test_real_pi_external_environment_and_replacements_keep_hidden_labels_out(self):
        bodies, executed, finished, observations, contexts = [], [], [], [], []
        external = dict(type="function", function=dict(name="native_read", parameters=dict(
            type="object", properties=dict(value=dict(type="integer")), required=["value"])))
        self.catalog.tools = lambda: [dict(type="function", function=dict(name="hidden_capability"))]
        self.catalog.name_to_id = lambda name: "hidden-id"
        def execute(name, arguments, call_id=None):
            executed.append((name, arguments, call_id))
            return dict(content="untrusted", error=None, hidden_evaluation_label="never model input")
        def stream(url, key, body, delta):
            bodies.append(body)
            self.assertNotIn("hidden_capability", [item["function"]["name"] for item in body["tools"]])
            if len(bodies) == 1:
                self.assertEqual(body["messages"][-1]["content"], "filtered input")
                return completion(calls=[dict(id="native-id", function=dict(name="native_read", arguments='{"value":7}'))])
            self.assertEqual(body["messages"][-1]["content"], "filtered result")
            self.assertNotIn("hidden_evaluation_label", json.dumps(body))
            delta("finished")
            return completion("finished")
        hooks = [
            AgentHook("input", "input_received", lambda *a: dict(action="replace", messages=[dict(role="user", content="filtered input")])),
            AgentHook("args", "before_tool", lambda *a: dict(action="replace", arguments=dict(value=9))),
            AgentHook("result", "after_tool", lambda *a: dict(action="replace", result=dict(content="filtered result", error=None))),
            AgentHook("visible", "tool_observation", lambda payload, context: observations.append(payload)),
            AgentHook("context", "model_context", lambda payload, context: contexts.append(payload)),
        ]
        scheduler = SimpleNamespace(begin=lambda *a, **kw: {}, finish=lambda *a, **kw: finished.append(kw))
        self.run_host(stream, external_tools=[external], external_execute=execute, allowed_capabilities=[],
                      hooks=hooks, evaluator=lambda task: dict(quality=1, utility=True), scheduler=scheduler)
        self.assertEqual(self.task["status"], "COMPLETED")
        self.assertEqual(self.task["host"]["package"], "@earendil-works/pi-agent-core")
        self.assertEqual(self.task["host"]["version"], "1.5.2")
        self.assertEqual(executed, [("native_read", dict(value=9), "native-id")])
        self.assertEqual(observations, [dict(name="native_read", content="filtered result", is_error=False)])
        self.assertIn("filtered result", contexts[-1]["tool_contents"])
        self.assertEqual(self.task["wire_messages"][2]["tool_calls"][0]["function"]["arguments"], '{"value":7}')
        self.assertEqual(finished[-1]["quality"], 1)
        kinds = [event["kind"] for event in self.events]
        self.assertLess(kinds.index("model_message"), kinds.index("tool_requested"))

    def test_native_pi_validation_keeps_raw_requested_call_and_error_result(self):
        bodies = []
        external = dict(type="function", function=dict(name="native", parameters=dict(
            type="object", properties=dict(value=dict(type="integer")), required=["value"])))
        def stream(url, key, body, delta):
            bodies.append(body)
            if len(bodies) == 1:
                return completion(calls=[dict(id="rejected-id", function=dict(name="native", arguments='{"value":"bad"}'))])
            self.assertEqual(body["messages"][-1]["tool_call_id"], "rejected-id")
            self.assertIn("Validation failed", body["messages"][-1]["content"])
            return completion("recovered")
        self.run_host(stream, external_tools=[external], external_execute=lambda *a, **kw: self.fail("invalid call executed"))
        self.assertEqual(self.task["status"], "COMPLETED")
        self.assertEqual(len([item for item in self.task["wire_messages"] if item["role"] == "tool"]), 1)
        self.assertEqual(len([event for event in self.events if event["kind"] == "model_message"]), 2)
        errors = [event["event"] for event in self.events if event["kind"] == "pi_event"
                  and event["event"]["type"] == "tool_execution_end"]
        self.assertTrue(errors[0]["isError"])

    def test_after_tool_block_ends_pi_instead_of_becoming_recoverable_error(self):
        requested, executed = [], []
        external = dict(type="function", function=dict(name="effect", parameters=dict(type="object")))
        def stream(*args):
            requested.append(1)
            return completion(calls=[dict(id="once", function=dict(name="effect", arguments="{}"))])
        self.run_host(stream, external_tools=[external], external_execute=lambda *a, **kw: executed.append(1) or dict(content="done"),
                      hooks=[AgentHook("stop", "after_tool", lambda *a: dict(action="block", reason="blocked"))])
        self.assertEqual(self.task["status"], "BLOCKED")
        self.assertEqual(requested, [1])
        self.assertEqual(executed, [1])

    def test_unknown_evaluation_is_not_a_zero_quality_measurement(self):
        finished = []
        scheduler = SimpleNamespace(begin=lambda *a, **kw: {}, finish=lambda *a, **kw: finished.append(kw))
        def evaluate(task):
            raise RuntimeError("native scorer unavailable")
        self.run_host(lambda *a: completion("done"), evaluator=evaluate, scheduler=scheduler)
        self.assertEqual(self.task["status"], "COMPLETED")
        self.assertEqual(self.task["evaluation"]["status"], "UNKNOWN")
        self.assertIsNone(finished[-1]["quality"])

    def test_model_turn_limit_stops_without_replaying_effects(self):
        requests, effects = [], []
        external = dict(type="function", function=dict(name="effect", parameters=dict(type="object")))
        def stream(*args):
            requests.append(1)
            return completion(calls=[dict(id="once", function=dict(name="effect", arguments="{}"))])
        self.run_host(stream, external_tools=[external], external_execute=lambda *a, **kw: effects.append(1) or dict(content="done"),
                      max_model_turns=1)
        self.assertEqual(self.task["status"], "FAILED")
        self.assertEqual(requests, [1])
        self.assertEqual(effects, [1])

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
