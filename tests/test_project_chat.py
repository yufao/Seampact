"""Project-bound conversations and basic file tools use the shared Agent host."""
import hashlib
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from seampact.agent_runtime import AgentHook, AgentRuntime, RunControl
from seampact.capability_catalog import ArtifactStore
from seampact.chat import ChatJobs
from seampact.project_spaces import ProjectSpaces
from seampact.store import Store


def completion(text=None, name=None, arguments=None, identity="tool"):
    message = dict(role="assistant", content=text)
    if name:
        message["tool_calls"] = [dict(id=identity, type="function", function=dict(
            name=name, arguments=json.dumps(arguments or {}, ensure_ascii=False)))]
    return dict(choices=[dict(finish_reason="tool_calls" if name else "stop", message=message)])


class Bound:
    def __init__(self, *args):
        pass

    def role_profile(self, role):
        return dict(base_url="http://example.invalid", model="test", declared_context_window=1000000)

    def role_key(self, role):
        return ""

    def public(self):
        return {"chat": {"model": "test"}}

    def resources(self):
        return {}


class AdmissionLockOrderTests(unittest.TestCase):
    def test_chat_and_native_admission_do_not_invert_controller_store_order(self):
        from seampact.office_experiment import start_native_task
        for native in (False, True):
            with self.subTest(native=native):
                deployment, store = threading.RLock(), threading.RLock()
                controller_ready, admission_attempted = threading.Event(), threading.Event()
                acquired, errors, coordinator_reads = [], [], []

                class DeploymentLock:
                    def __enter__(self):
                        if threading.current_thread().name == "admission":
                            admission_attempted.set()
                        deployment.acquire()
                    def __exit__(self, *args):
                        deployment.release()

                lock = DeploymentLock()
                jobs = ChatJobs.__new__(ChatJobs)
                jobs._start_lock = store
                def availability():
                    with lock:
                        pass
                    def coordinator():
                        held = store.acquire(timeout=0.5)
                        coordinator_reads.append(held)
                        if held:
                            store.release()
                    thread = threading.Thread(target=coordinator)
                    thread.start()
                    thread.join(1)
                    return []
                jobs.catalog = SimpleNamespace(router=SimpleNamespace(lock=lock), public=availability,
                    tools_for_project=lambda project: availability())
                def snapshot(*args, **kwargs):
                    with lock:
                        self.assertEqual(kwargs["capability_snapshot"], [])
                        return dict(task_id="fixture")
                jobs._start = snapshot
                platform = SimpleNamespace(deployments=SimpleNamespace(lock=lock),
                    projects=SimpleNamespace(lock=store), capabilities=jobs.catalog)

                def controller():
                    with lock:
                        controller_ready.set()
                        admission_attempted.wait(1)
                        held = store.acquire(timeout=0.5)
                        acquired.append(held)
                        if held:
                            store.release()

                def admit():
                    try:
                        if native:
                            start_native_task(platform, dict(project_id="fixture", rag={}))
                        else:
                            jobs.start({})
                    except Exception as error:
                        errors.append(error)

                with patch("seampact.office_experiment._start_native_task",
                           side_effect=lambda *a, **k: jobs.start({}, capability_snapshot=k["capability_snapshot"])):
                    worker = threading.Thread(target=controller, name="controller")
                    caller = threading.Thread(target=admit, name="admission")
                    worker.start()
                    self.assertTrue(controller_ready.wait(1))
                    caller.start()
                    worker.join(2)
                    caller.join(2)
                self.assertFalse(worker.is_alive())
                self.assertFalse(caller.is_alive())
                self.assertEqual(errors, [])
                self.assertEqual(acquired, [True], "admission held store while waiting for deployment")
                self.assertTrue(coordinator_reads)
                self.assertTrue(all(coordinator_reads), "availability waited for coordinator while holding store")


class ProjectChatTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.store = Store(self.home / "state.db")
        self.addCleanup(self.store.close)
        self.projects = ProjectSpaces(self.home, self.store)
        self.project = self.projects.default()["id"]
        self.catalog = SimpleNamespace(router=None, tools=lambda: [], public=lambda: [],
            artifacts=ArtifactStore(self.home / "artifacts"), invoke=lambda *a, **kw: self.fail("capability invoked"))
        self.jobs = ChatJobs(self.store, None, self.catalog, projects=self.projects)
        self.jobs.transport.stream = lambda *args: completion("answer")

    def start(self, **body):
        body.setdefault("messages", [dict(role="user", content="question")])
        with patch("seampact.chat.FrozenProfiles", Bound):
            receipt = self.jobs.start(body)
        thread = self.jobs._threads.get(receipt["task_id"])
        if thread:
            thread.join(3)
            self.assertFalse(thread.is_alive(), "small fake-provider task did not finish")
        return receipt, self.store.get("task", receipt["task_id"])

    def runtime(self, responses, hooks=(), control=None, **options):
        scope = self.projects.begin_task(self.project, "conversation", "task")
        task = dict(id="task", status="RUNNING", conversation_id="conversation", project_id=self.project)
        bodies = []
        def stream(url, key, body, delta):
            bodies.append(body)
            return responses.pop(0)
        host = AgentRuntime(self.store, self.catalog, projects=self.projects,
            transport=SimpleNamespace(stream=stream), hooks=hooks, **options)
        host.run(task, [dict(role="user", content="use files")], Bound(), control=control)
        return task, bodies

    def test_real_pi_external_name_collision_namespaces_only_workspace_and_records_final_delivery(self):
        external_calls = []
        native = dict(type="function", function=dict(name="list_files", description="Native cloud files",
            parameters=dict(type="object", properties={}, additionalProperties=False)))
        def execute(name, arguments, call_id=None):
            external_calls.append((name, arguments, call_id))
            return dict(content="native listing", error=None)
        def delivery(payload, context):
            if payload["text"] == "raw final":
                return dict(action="replace", text="delivered final")
        task, bodies = self.runtime([
            completion("looking", name="list_files", identity="native-list"),
            completion("local", name="workspace_list_files", arguments=dict(area="workspace"), identity="workspace-list"),
            completion("raw final")], [AgentHook("delivery", "before_delivery", delivery, mode="complete")],
            external_tools=[native], external_execute=execute)
        self.assertEqual(task["status"], "COMPLETED", task.get("error"))
        self.assertEqual(task["host"]["package"], "@earendil-works/pi-agent-core")
        self.assertEqual(external_calls, [("list_files", {}, "native-list")])
        specs = {item["function"]["name"]: item["function"] for item in bodies[0]["tools"]}
        self.assertEqual(specs["list_files"]["description"], "Native cloud files")
        self.assertIn("本地会话工作区", specs["workspace_list_files"]["description"])
        self.assertIn("read_file", specs)
        self.assertNotIn("workspace_read_file", specs)
        requested = [message["tool_calls"][0]["function"]["name"] for message in task["wire_messages"] if message.get("tool_calls")]
        self.assertEqual(requested, ["list_files", "workspace_list_files"])
        self.assertEqual(task["final_response"], "delivered final")
        self.assertEqual(task["response"], "lookinglocaldelivered final")
        self.assertEqual(task["wire_messages"][-1]["content"], "raw final")

    def test_prefixed_workspace_collision_is_rejected_before_model_execution(self):
        tools = [dict(type="function", function=dict(name=name, parameters=dict(type="object")))
                 for name in ("list_files", "workspace_list_files")]
        task, bodies = self.runtime([], external_tools=tools, external_execute=lambda *a, **kw: self.fail("called"))
        self.assertEqual(task["status"], "FAILED")
        self.assertIn("名称不能重复", task["error"])
        self.assertEqual(bodies, [])

    def test_file_tools_use_existing_checks_and_persist_delivered_bytes(self):
        self.projects.write_file(self.project, None, "data", "input.txt", "source text")
        checks, outputs = [], []
        hooks = [AgentHook("input", "before_tool", lambda payload, context: checks.append(payload["name"])),
                 AgentHook("output", "after_tool", lambda payload, context: outputs.append(payload)),
                 AgentHook("delivery", "before_delivery", lambda payload, context: checks.append(
                     "files" if payload["files"] else "text"))]
        task, bodies = self.runtime([
            completion(name="list_files"), completion(name="read_file", arguments=dict(path="input.txt", limit=6)),
            completion(name="write_file", arguments=dict(path="report.txt", content="真实结果")),
            completion(name="deliver_file", arguments=dict(path="report.txt")), completion("done")], hooks)
        self.assertEqual(task["status"], "COMPLETED", task.get("error"))
        self.assertEqual([item["name"] for item in outputs], ["list_files", "read_file", "write_file", "deliver_file"])
        self.assertEqual(outputs[1]["result"]["content"], "source")
        self.assertEqual(self.projects.read_file(self.project, "conversation", "workspace", "report.txt")["content"], "真实结果")
        file = task["files"][0]
        data = self.catalog.artifacts.path(file["sha256"]).read_bytes()
        self.assertEqual(data, "真实结果".encode())
        self.assertEqual(file["sha256"], hashlib.sha256(data).hexdigest())
        self.assertIn("files", checks)
        names = {tool["function"]["name"] for tool in bodies[0]["tools"]}
        self.assertTrue({"list_files", "read_file", "write_file", "deliver_file"}.issubset(names))

    def test_delivery_block_and_readonly_data_are_enforced(self):
        self.projects.write_file(self.project, None, "data", "source.txt", "original")
        task, _ = self.runtime([
            completion(name="write_file", arguments=dict(area="data", path="source.txt", content="changed")),
            completion(name="write_file", arguments=dict(path="report.txt", content="report")),
            completion(name="deliver_file", arguments=dict(path="report.txt"))],
            [AgentHook("guard", "before_delivery", lambda payload, context: not payload["files"])])
        self.assertEqual(task["status"], "BLOCKED")
        self.assertEqual(task["files"], [])
        self.assertEqual(self.projects.read_file(self.project, None, "data", "source.txt")["content"], "original")
        self.assertTrue(self.catalog.artifacts.path(hashlib.sha256(b"report").hexdigest()).is_file())

    def test_cancellation_gate_prevents_local_write(self):
        control = RunControl()
        task, _ = self.runtime([completion(name="write_file", arguments=dict(path="report.txt", content="report"))],
            [AgentHook("cancel", "before_tool", lambda payload, context: control.cancel())], control)
        self.assertEqual(task["status"], "CANCELLED")
        self.assertEqual(self.projects.list_files(self.project, "conversation", "workspace")["entries"], [])

    def test_normal_completion_finishes_lease_and_continuation_keeps_project(self):
        project = self.projects.create(dict(name="Experiment"))["id"]
        receipt, task = self.start(project_id=project)
        self.assertEqual((receipt["project_id"], task["project_id"]), (project, project))
        self.assertEqual(self.projects.task_scope(task["id"])["status"], "FINISHED")
        messages = self.jobs.conversation(receipt["conversation_id"])["messages"] + [dict(role="user", content="next")]
        continued, next_task = self.start(conversation_id=receipt["conversation_id"], messages=messages)
        self.assertEqual(next_task["project_id"], project)
        with patch("seampact.chat.FrozenProfiles", Bound), self.assertRaisesRegex(ValueError, "不能更改"):
            self.jobs.start(dict(project_id=self.project, conversation_id=receipt["conversation_id"], messages=messages))
        self.assertEqual(self.jobs.conversations(project_id=project)["total"], 1)
        self.assertEqual(self.jobs.conversations()["total"], 0)

    def test_legacy_history_attaches_without_rewriting_original_task(self):
        original = dict(id="old", phase="conversation", status="COMPLETED", response="saved answer",
            started_at="2026-01-01", messages=[dict(role="user", content="saved question")],
            wire_messages=[dict(role="system", content="old prompt")])
        self.store.save("task", "old", original)
        detail = self.jobs.conversation("old")
        self.assertEqual(detail["project_id"], self.project)
        self.assertEqual(self.store.get("task", "old"), original)
        self.assertEqual(self.projects.session("old")["project_id"], self.project)
        self.projects.archive(self.project)
        self.assertEqual(self.jobs.conversation("old")["messages"][-1]["content"], "saved answer")

    def test_active_session_is_reserved_until_worker_ends(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def stream(*args):
            entered.set()
            release.wait(3)
            return completion("answer")
        self.jobs.transport.stream = stream
        with patch("seampact.chat.FrozenProfiles", Bound):
            receipt = self.jobs.start(dict(messages=[dict(role="user", content="question")]))
            self.assertTrue(entered.wait(1))
            with self.assertRaisesRegex(ValueError, "正在运行"):
                self.jobs.start(dict(conversation_id=receipt["conversation_id"],
                    messages=[dict(role="user", content="question")]))
            with self.assertRaises(ValueError):
                self.projects.cleanup(self.project, dict(target="workspace"))
            thread = self.jobs._threads[receipt["task_id"]]
            release.set()
            thread.join(3)
        self.assertEqual(self.projects.task_scope(receipt["task_id"])["status"], "FINISHED")

    def test_uncertain_call_retains_lease_then_reconciles_actual_drain(self):
        physical = [dict(physical="UNKNOWN")]
        self.jobs.runtime = SimpleNamespace(call=lambda *args: physical)
        receipt, task = self.start()
        self.assertEqual(task["workspace_status"], "UNKNOWN")
        self.assertEqual(self.projects.task_scope(task["id"])["status"], "UNKNOWN")
        with self.assertRaises(ValueError):
            self.projects.cleanup(self.project, dict(target="scratch"))
        physical[0]["physical"] = "RETIRED"
        self.assertEqual(self.jobs.reconcile_project_leases(self.project), [task["id"]])
        self.assertEqual(self.projects.task_scope(task["id"])["status"], "FINISHED")

    def test_restart_requires_old_instance_release_before_reconciling(self):
        self.projects.begin_task(self.project, "old", "task")
        self.projects.finish_task("task", drained=False)
        self.store.save("task", "task", dict(id="task", status="INTERRUPTED", project_id=self.project,
            workspace_controller="prior-controller", calls=[dict(instance_id="old-instance")]))
        router = SimpleNamespace(lock=threading.RLock(), affinity={},
            instances={"old-instance": dict(state="UNKNOWN", in_flight=0, released=False)}, release_task=lambda identity: None)
        self.catalog.router = router
        self.assertEqual(self.jobs.reconcile_project_leases(self.project), [])
        router.instances["old-instance"].update(state="STOPPED", released=True)
        self.assertEqual(self.jobs.reconcile_project_leases(self.project), ["task"])

    def test_lost_response_retains_scope_even_after_scheduler_releases_affinity(self):
        released = []
        router = SimpleNamespace(lock=threading.RLock(), affinity={},
            instances={"worker": dict(state="UNKNOWN", in_flight=0)}, release_task=released.append)
        self.catalog.router = router
        self.catalog.tools = lambda: [dict(type="function", function=dict(name="unit", parameters=dict(type="object")))]
        self.catalog.name_to_id = lambda name: "unit"
        self.catalog.cancel = lambda identity: dict(requested=True)
        def invoke(unit_id, arguments, on_event, **options):
            on_event(dict(call_id="physical", instance_id="worker", kind="call_started"))
            raise ValueError("response lost")
        self.catalog.invoke = invoke
        self.jobs.scheduler = SimpleNamespace(begin=lambda *a, **kw: {},
            finish=lambda *a, **kw: router.affinity.clear())
        responses = [completion(name="unit"), completion("reported error")]
        self.jobs.transport.stream = lambda *args: responses.pop(0)
        _, task = self.start()
        self.assertEqual(task["workspace_status"], "UNKNOWN")
        self.assertEqual(self.projects.task_scope(task["id"])["status"], "UNKNOWN")
        self.assertEqual(released, [])

    def test_failed_scratch_cleanup_keeps_uncertain_lease_and_reason(self):
        finish = self.projects.finish_task
        def fail_cleanup(identity, drained=True):
            if drained:
                raise ValueError("unsafe scratch entry")
            return finish(identity, drained=False)
        with patch.object(self.projects, "finish_task", side_effect=fail_cleanup):
            _, task = self.start()
        self.assertEqual(task["workspace_status"], "UNKNOWN")
        self.assertIn("unsafe scratch", task["workspace_error"])
        self.assertEqual(self.projects.task_scope(task["id"])["status"], "UNKNOWN")
        self.assertEqual(self.jobs.reconcile_project_leases(self.project), [task["id"]])


if __name__ == "__main__":
    unittest.main()
