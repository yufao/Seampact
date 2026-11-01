"""Conversation plans keep their remote task identity and release owner leases."""
import copy
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from seampact.chat import ChatJobs
from seampact.deployment import Deployments
from seampact.runtime import Coordinator, OwnerRuntime, ProtocolError, _task_context
from seampact.store import Store


class Bound:
    def __init__(self, profiles): pass
    def role_profile(self, role): return dict(base_url="http://example.invalid", model="test", parameters={})
    def role_key(self, role): return "test-key"
    def public(self): return {}
    def resources(self): return {}


class ImmediateThread:
    def __init__(self, target, args, **kwargs): self.target, self.args = target, args
    def start(self): self.target(*self.args)


class RemoteCatalog:
    def __init__(self, store, router, ready):
        self.store, self.remote_router = store, router
        self.lock, self.bindings = threading.RLock(), {}
        schema = dict(type="object", properties=dict(value=dict(type="string")), required=["value"])
        self.row = dict(id="unit", tool_name="call_original", entry_id="entry", source_version="version",
            path="original.py", symbol="original", description="original entry", status="MISSING",
            runtime_group="group", parameters=schema, input_schema=schema,
            scheduling=dict(visibility="business", activation="business"),
            dispatch=dict(status="READY_INSTANCE" if ready else "COLD_STARTABLE"),
            operations=dict(group_host=dict(status="HOST_CHECKED"), export_entry=dict(status="SOURCE_BOUND")))
        store.save("runtime_group", "group", dict(id="group", contract=dict(host_profile="profile", digest="digest")))
        store.save("deployment", "group", dict(image="image", publish=dict(reference="sha256:fixed")))

    def public(self): return [copy.deepcopy(self.row)]
    def business_units(self): return self.public()
    def freeze_resources(self, selected, remote=()):
        return {uid: (None, {}, copy.deepcopy(self.row)) for uid in selected}
    def snapshot_resources(self, *args, **kwargs): return {}
    def _validate_bound_arguments(self, binding, entry, unit, arguments, native_arguments=(), deferred_arguments=()):
        if set(arguments) != {"value"} or type(arguments["value"]) is not str:
            raise ValueError("invalid original argument")


class ChatRemoteTests(unittest.TestCase):
    def exercise(self, *, ready=True, fail=False, model_fail=False):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "state.db")
            runtime = OwnerRuntime(Coordinator(store))
            routed, released = [], []
            def invoke(placement, unit, arguments, native_arguments):
                routed.append((copy.deepcopy(placement), _task_context.get(), copy.deepcopy(arguments)))
                if fail:
                    raise ProtocolError("response unknown; do not replay")
                return dict(original_value=arguments["value"]), dict(execution="REMOTE")
            router = SimpleNamespace(backend=lambda: dict(start_timeout_s=37), start_gap=lambda group: None,
                invoke_placed=invoke, release_task=released.append)
            catalog = RemoteCatalog(store, router, ready)
            scheduler = SimpleNamespace(select_plan=lambda plans, observation: (plans[0], "token"),
                                        feedback=lambda *args: None)
            jobs = ChatJobs(store, None, catalog, runtime, scheduler)
            requirements = ["Use the original entry once"]
            arguments = dict(requirements=requirements, plans=[dict(id="plan", requirements=requirements,
                steps=[dict(unit="call_original", arguments=dict(value="original input"))])])
            replies = [dict(choices=[dict(finish_reason="tool_calls", message=dict(role="assistant", content=None,
                tool_calls=[dict(id="call", type="function", function=dict(name="execute_harness_plan",
                    arguments=json.dumps(arguments)))]))]),
                dict(choices=[dict(finish_reason="stop", message=dict(role="assistant", content="reported"))])]
            def send(*args):
                if model_fail: raise ValueError("model unavailable")
                return replies.pop(0)
            jobs.transport.send = send
            try:
                with patch("seampact.training.BoundProfiles", Bound), patch("seampact.chat.threading.Thread", ImmediateThread):
                    accepted = jobs.start(dict(messages=[dict(role="user", content="execute original")]))
                task = store.get("task", accepted["task_id"])
                self.assertEqual(released, [task["id"]])
                placement = task["placements"]["unit"]
                self.assertEqual(placement["run_id"], task["id"])
                self.assertEqual(placement["start_timeout_s"], 37)
                self.assertEqual(placement["image"], "sha256:fixed")
                if model_fail:
                    self.assertEqual(task["status"], "FAILED")
                    self.assertEqual(routed, [])
                    return
                self.assertEqual(len(routed), 1, task.get("plan") or task.get("error"))
                self.assertEqual(routed[0][0], placement)
                self.assertEqual(routed[0][1], task["id"])
                self.assertEqual(routed[0][2], dict(value="original input"))
                self.assertEqual(task["status"], "COMPLETED")
                result = task["plan"]["results"][0]
                if fail:
                    self.assertIn("do not replay", result["error"])
                    self.assertEqual(task["plan"]["execution_status"], "STOPPED_AT_NON_SUCCESS_EXIT")
                else:
                    self.assertEqual(result["result"], dict(original_value="original input"))
                    self.assertEqual(task["plan"]["execution_status"], "RETURNED")
            finally:
                runtime.close()
                store.close()

    def test_ready_and_cold_remote_plans_receive_identity_and_configured_timeout(self):
        for ready in (True, False):
            with self.subTest(ready=ready): self.exercise(ready=ready)

    def test_unknown_result_is_not_replayed_and_lease_is_released(self):
        self.exercise(fail=True)

    def test_model_failure_also_releases_task_lease(self):
        self.exercise(model_fail=True)

    def test_remote_plan_cannot_enter_another_runs_locked_group(self):
        deployments = Deployments.__new__(Deployments)
        deployments.lock = threading.RLock()
        deployments.run_locks = {"group": dict(run_id="research-window")}
        with patch.object(deployments, "_route") as route, patch.object(deployments, "_call") as call:
            with self.assertRaisesRegex(ValueError, "research-window"):
                deployments.invoke_placed(dict(group_id="group", run_id="chat-task"), dict(id="unit"), {})
            route.assert_not_called()
            call.assert_not_called()


if __name__ == "__main__":
    unittest.main()
