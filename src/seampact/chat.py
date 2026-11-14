"""Web conversation jobs, history and visible events around the shared Agent host."""

import copy
import json
import math
import subprocess
import threading
import time
import uuid
from collections import deque
from contextlib import nullcontext

from seampact.providers import FrozenProfiles, Transport
from seampact.store import now
from seampact.agent_runtime import AgentRuntime, RunControl
from seampact.scenes.evaluation_evidence import passive_numpy_value, PassiveEvidenceGap


def public_result(value, active=None):
    active = set() if active is None else active
    if type(value) is subprocess.CompletedProcess:
        def output(text):
            return text.decode("utf-8", errors="replace") if type(text) is bytes else text
        result = dict(result_type="subprocess.CompletedProcess", returncode=value.returncode,
                      stdout=output(value.stdout), stderr=output(value.stderr))
        if hasattr(value, "seampact_completion"):
            result["completion"] = public_result(value.seampact_completion)
        return result
    kind = type(value)
    if kind in {dict, list, tuple}:
        if id(value) in active:
            return dict(result_type=kind.__module__ + "." + kind.__qualname__, serialization="CYCLIC_REFERENCE")
        if kind is dict:
            try:
                if any(type(key) not in {str, int, float, bool, type(None)} for key in value):
                    raise TypeError("native key")
                json.dumps(list(value), allow_nan=False)
            except (TypeError, ValueError):
                return dict(result_type="builtins.dict", serialization="NOT_CONVERTED", reference_scope="LOCAL_PLAN_OWNER")
        active.add(id(value))
        try:
            if kind is dict:
                return {key: public_result(item, active) for key, item in value.items()}
            return [public_result(item, active) for item in value]
        finally:
            active.remove(id(value))
    try:
        if kind not in {str, int, float, bool, type(None)}:
            try:
                return passive_numpy_value(value)
            except PassiveEvidenceGap:
                raise TypeError("native leaf") from None
        json.dumps(value, ensure_ascii=False, allow_nan=False)
        return value
    except (TypeError, ValueError):
        return dict(result_type=type(value).__module__ + "." + type(value).__qualname__,
            native_object=True, serialization="NOT_CONVERTED", reference_scope="LOCAL_PLAN_OWNER")


class ChatJobs:
    def __init__(self, store, profiles, catalog, runtime=None, scheduler=None, *, dispatch=None,
                 model_connection=None, hooks=(), optional_hooks=None, projects=None, code_execution=None,
                 applications=None):
        self.store, self.profiles, self.catalog = store, profiles, catalog
        self.runtime, self.scheduler = runtime, scheduler
        self.transport = Transport()
        self.dispatch, self.model_connection = dispatch, model_connection
        self.hooks, self.optional_hooks = tuple(hooks), optional_hooks
        self.projects = projects
        self.applications = applications
        self.code_execution = code_execution
        self._start_lock = projects.lock if projects is not None else threading.RLock()
        self._workspace_controller = uuid.uuid4().hex
        self._controls, self._threads = {}, {}
        self._stream_condition = threading.Condition()
        self._streams = {}
        self.closed = False
        for task in self.store.tasks_with_status("RUNNING", summary=False):
            if self._conversational(task) or task.get("origin") == "EXPLICIT_TASK_PLAN":
                task.update(status="INTERRUPTED", error="平台已重启，原任务未恢复；已保存的对话仍可查看。", finished_at=now())
                self.store.save("task", task["id"], task)

    def _stream_event(self, identity, kind, text="", **facts):
        # Phase metadata is public. Raw inputs, plans, candidates, check
        # evidence and feedback remain in the task's research record.
        fields = {
            "content_received": ("chars",), "hook_started": ("hook", "event", "required", "enforced_recheck"),
            "hook_result": ("hook", "event"), "hook_skipped": ("hook", "event"),
            "tool_requested": ("name", "call_id"), "before_tool": ("name", "call_id"),
            "tool_result": ("name", "call_id"), "before_delivery": ("chars", "file_count", "complete"),
            "model_started": ("request_index", "stream"), "file": ("file",),
            "scoped_operation_denied": ("name", "call_id", "hooks"),
            "scoped_operation_cleared": ("name", "call_id", "hooks"),
            "delivery_revision_requested": ("hooks",), "delivery_revision_accepted": ("hooks",),
            "delivery_revision_exhausted": ("hooks",), "task_finished": ("status",),
            "capability_event": ("call_id", "unit_id", "instance_id", "kind"),
            "resource_preparing": ("group_id", "role"), "resource_ready": ("groups", "role"),
        }
        if kind not in {"reset", "start", "delta"}:
            text = ""
            source = facts.get("data") if isinstance(facts.get("data"), dict) else facts
            facts = dict(data={name: copy.deepcopy(source[name]) for name in fields.get(kind, ()) if name in source})
        with self._stream_condition:
            stream = self._streams.setdefault(identity, dict(events=deque(maxlen=256), parts=[], sequence=0))
            if kind == "reset":
                stream["parts"] = []
            elif kind == "delta":
                stream["parts"].append(text)
            stream["sequence"] += 1
            stream["events"].append(dict(id=stream["sequence"], type=kind, text=text, **facts))
            self._stream_condition.notify_all()

    def _stream_text(self, identity):
        with self._stream_condition:
            stream = self._streams.get(identity)
            return "".join(stream["parts"]) if stream else ""

    def stream_events(self, identity, after=0, timeout=15):
        """Wait for new display deltas; subscriptions never invoke the model/tools."""
        if type(after) is not int or after < 0:
            raise ValueError("流式事件游标需要非负整数")
        if not self.store.get("task", identity):
            raise ValueError("没有这项对话任务")
        with self._stream_condition:
            self._stream_condition.wait_for(lambda: identity not in self._streams or
                self._streams[identity]["sequence"] > after, timeout=max(0, min(timeout, 15)))
            stream = self._streams.get(identity)
            if stream:
                events = [dict(event) for event in stream["events"] if event["id"] > after]
                if events and after < stream["events"][0]["id"] - 1:
                    events = [dict(id=stream["sequence"], type="reset", text="".join(stream["parts"]))]
                return dict(events=events, done=False)
        task = self.store.get("task", identity)
        done = task["status"] != "RUNNING"
        return dict(events=[dict(id=after + 1, type="done", status=task["status"])] if done else [], done=done)

    @staticmethod
    def _conversational(task):
        return task.get("origin") != "EXPLICIT_TASK_PLAN" and bool(
            task.get("conversation_id") or task.get("phase") in {"conversation", "harness_execution"})

    def _latest(self, identity):
        """The newest task of one conversation, without reading other history."""
        return next((task for task in self.store.conversation_tasks(identity) if self._conversational(task)), None)

    def conversations(self, query="", limit=None, offset=0, project_id=None):
        """Conversation list from per-task heads; tool traces and model context are not read."""
        if self.projects is not None:
            project_id = self.projects.get(project_id)["id"] if project_id is not None else self.projects.default()["id"]
        deleted = {row["id"] for row in self.store.list("chat_deleted")}
        needle = query.casefold() if query else ""
        groups = {}
        heads = self.store.conversation_heads(project_id=project_id) if self.projects is not None else self.store.conversation_heads()
        for head in heads:
            if (head["origin"] == "EXPLICIT_TASK_PLAN" or head["conversation"] in deleted
                    or not (head["threaded"] or head["phase"] in {"conversation", "harness_execution"})):
                continue
            group = groups.setdefault(head["conversation"], dict(latest=head, turns=0, responses=[]))
            group["turns"] += 1
            if needle:
                group["responses"].append(head["response"])
        result = []
        for identity, group in groups.items():
            latest = group["latest"]
            messages = json.loads(latest["messages"]) if latest["messages"] else []
            first = next((message["content"] for message in messages if message.get("role") == "user"), "")
            title = first if isinstance(first, str) else "多模态会话"
            title = " ".join(title.split())[:60] or "旧对话（仅保存回答）"
            if needle and needle not in title.casefold() and needle not in json.dumps(
                    [messages, group["responses"]], ensure_ascii=False).casefold():
                continue
            result.append(dict(id=identity, title=title, updated_at=latest["finished_at"] or latest["started_at"],
                               status=latest["status"], turns=group["turns"], legacy=not bool(messages)))
            if self.projects is not None:
                result[-1]["project_id"] = latest.get("project_id") or project_id
        page = result[offset:offset + limit] if limit else result[offset:]
        return dict(conversations=page, total=len(result))

    def conversation(self, identity):
        if self.store.get("chat_deleted", identity):
            raise ValueError("这段会话已从历史删除")
        latest = self._latest(identity)
        if latest is None:
            raise ValueError("没有这段历史会话")
        messages = copy.deepcopy(latest.get("messages") or [])
        # Only user-visible messages are restored; tool traces remain task details.
        if latest.get("status") == "COMPLETED" and latest.get("response"):
            messages.append(dict(role="assistant", content=latest["response"]))
        result = dict(id=identity, messages=messages, legacy=not bool(latest.get("messages")),
                    task=self.snapshot(latest))
        if self.projects is not None:
            with self.projects.lock:
                session = self.projects.session(identity)
                project_id = (session or {}).get("project_id") or latest.get("project_id") or self.projects.default()["id"]
                if latest.get("project_id") not in (None, project_id):
                    raise ValueError("会话的项目归属与任务记录不一致")
                if session or not self.projects.get(project_id).get("archived"):
                    self.projects.ensure_session(project_id, identity)
            result["project_id"] = project_id
            result["task"]["project_id"] = project_id
            result["task"].update({key: latest[key] for key in
                ("workspace_status", "workspace_retention_reason", "workspace_error") if key in latest})
        return result

    @staticmethod
    def snapshot(task):
        """Normal chat state contains delivered content and structural status only."""
        result = {key: copy.deepcopy(task.get(key)) for key in
            ("id", "status", "response", "partial_response", "error", "latency_ms", "context_status", "files", "cancel")}
        result["context_events"] = [{key: copy.deepcopy(event[key]) for key in
            ("operation", "archive_ref", "status", "messages_before", "messages_after", "usage", "error_type") if key in event}
            for event in task.get("context_events", []) if isinstance(event, dict)]
        # Artifacts become attachments exclusively through delivery's files gate.
        delivered = {file.get("sha256") for file in task.get("files", []) if isinstance(file, dict)}
        artifacts = task.get("artifacts")
        result["artifacts"] = ([copy.deepcopy(file) for file in artifacts if isinstance(file, dict)
                                and file.get("sha256") in delivered] if isinstance(artifacts, list) else None)
        return result

    def delete_conversation(self, identity):
        if self.store.get("chat_deleted", identity):
            return dict(deleted=identity)
        detail = self.conversation(identity)
        if detail["task"]["status"] == "RUNNING":
            raise ValueError("请等这段会话的任务结束后再删除")
        # History visibility is separate from the execution evidence in tasks.
        self.store.save("chat_deleted", identity, dict(id=identity, deleted_at=now()))
        return dict(deleted=identity)

    def restore_conversation(self, identity):
        self.store.delete("chat_deleted", identity)
        return dict(restored=self.conversation(identity)["id"])

    def start(self, body, *, runtime_options=None, on_event=None, metadata=None, internal_task_id=None,
              capability_snapshot=None, apply_project_application=True, input_files=None):
        # Dynamic availability may wait for the coordinator, whose journal
        # uses the store lock. Read it before entering project admission.
        capability_snapshot = (self.catalog.public() if capability_snapshot is None else capability_snapshot)
        # Reserving the session and recording RUNNING are one admission step;
        # cleanup and simultaneous HTTP submissions use this same project lock.
        # Capability snapshots inspect deployment state. Match the controller's
        # deployment -> project/store order before entering admission.
        router = getattr(self.catalog, "router", None) or getattr(self.catalog, "remote_router", None)
        deployment_lock = getattr(router, "lock", None)
        with deployment_lock if deployment_lock is not None else nullcontext(), self._start_lock:
            return self._start(body, runtime_options=runtime_options, on_event=on_event, metadata=metadata,
                               internal_task_id=internal_task_id, capability_snapshot=capability_snapshot,
                               apply_project_application=apply_project_application, input_files=input_files)

    def _start(self, body, *, runtime_options=None, on_event=None, metadata=None, internal_task_id=None,
               capability_snapshot, apply_project_application=True, input_files=None):
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages:
            raise ValueError("请提交完整对话消息")
        for message in messages:
            if not isinstance(message, dict) or message.get("role") not in {"user", "assistant"}:
                raise ValueError("对话消息需要user/assistant角色")
            if not isinstance(message.get("content"), (str, list)):
                raise ValueError("消息内容需要文字或模型支持的多模态内容")
        if self.closed:
            raise ValueError("平台已停止")
        access = body.get("tool_access", "project")
        if access not in ("read_only", "project"):
            raise ValueError("工具权限须为 read_only 或 project")
        deadline = body.get("deadline_s")
        if deadline is not None and (type(deadline) not in (int, float) or
                not math.isfinite(deadline) or deadline <= 0):
            raise ValueError("任务时限需要有限正数")
        bound = FrozenProfiles(self.profiles)
        profile = bound.role_profile("chat")
        if not profile.get("base_url") or not profile.get("model"):
            raise ValueError("请先配置对话模型地址与型号")
        bound.role_key("chat")
        catalog = self.catalog
        conversation_id = body.get("conversation_id") or uuid.uuid4().hex
        if not isinstance(conversation_id, str) or not conversation_id.strip():
            raise ValueError("会话标识需要非空文字")
        if body.get("conversation_id"):
            previous = self.conversation(conversation_id)
            if previous["legacy"]:
                raise ValueError("旧对话没有保存完整问题，请新建会话")
            if previous["task"]["status"] == "RUNNING":
                raise ValueError("这段会话还有任务正在运行")
        project_id = None
        if self.projects is not None:
            requested = body.get("project_id")
            if requested is not None:
                requested = self.projects.get(requested)["id"]
            if body.get("conversation_id"):
                project_id = previous["project_id"]
                if requested not in (None, project_id):
                    raise ValueError("续聊不能更改会话所属项目")
            else:
                project_id = requested or self.projects.default()["id"]
            self.projects.ensure_session(project_id, conversation_id)
        prior_wire = None
        prior_context = None
        prior_calibration = None
        if body.get("conversation_id"):
            visible = previous["messages"]
            if len(messages) != len(visible) + 1 or messages[:-1] != visible or messages[-1]["role"] != "user":
                raise ValueError("续聊需要保留已保存的对话并追加一条用户消息")
            latest = self._latest(conversation_id)
            prior_wire = latest.get("wire_messages")
            prior_context = latest.get("context_messages")
            if (latest.get("models") or {}).get("chat") == bound.public().get("chat"):
                prior_calibration = latest.get("context_calibration")
        if internal_task_id is not None:
            if (not isinstance(internal_task_id, str) or len(internal_task_id) != 32
                    or any(c not in "0123456789abcdef" for c in internal_task_id)
                    or self.store.get("task", internal_task_id) is not None):
                raise ValueError("内部任务身份无效或已被使用")
        identity = internal_task_id or uuid.uuid4().hex
        task = dict(id=identity, status="RUNNING", started_at=now(), response=None,
            tool_access=access,
            error=None, partial_response="", calls=[], model_calls=0, usage=[], latency_ms=None, phase="conversation",
            conversation_id=conversation_id, messages=copy.deepcopy(messages),
            context_calibration=copy.deepcopy(prior_calibration), models=bound.public(), role_resources=bound.resources(),
            capability_pool=copy.deepcopy(capability_snapshot))
        if self.applications is not None and self.projects is not None and apply_project_application:
            from seampact.project_applications import TaskCapabilityCatalog
            application, eligible = self.applications.snapshot(project_id, capability_snapshot)
            task["application"] = application
            task["capability_pool"] = copy.deepcopy(eligible)
            transparent = {unit["id"] for unit in eligible if "guard" in unit.get("capability", {})}
            catalog = TaskCapabilityCatalog(self.catalog, [u for u in eligible if u["id"] not in transparent])
            application_options = self.applications.runtime_options(identity, application)
            runtime_options = dict(runtime_options or {})
            if application_options.get("hooks"):
                runtime_options["hooks"] = self.hooks + tuple(runtime_options.get("hooks", ())) + application_options["hooks"]
            runtime_options.update({key: value for key, value in application_options.items() if key != "hooks"})
        if metadata is not None:
            # Research labels stay in the task record, never in model context.
            task["experiment"] = copy.deepcopy(metadata)
        if deadline is not None:
            task["deadline_s"] = float(deadline)
        if self.projects is not None:
            task["project_id"] = project_id
            task["workspace_controller"] = self._workspace_controller
            scope = self.projects.begin_task(project_id, conversation_id, identity, input_files=input_files)
            task["workspace"] = {key: scope[key] for key in ("project_id", "conversation_id", "task_id")}
        try:
            self.store.save("task", identity, task)
            self._stream_event(identity, "reset")
            self._controls[identity] = RunControl()
            thread = threading.Thread(target=self._run, args=(task, messages, bound, catalog, prior_wire, prior_context),
                                      kwargs=dict(runtime_options=runtime_options, on_event=on_event),
                                      daemon=True, name="seampact-agent-" + identity[:8])
            self._threads[identity] = thread
            thread.start()
        except Exception:
            self._controls.pop(identity, None)
            self._threads.pop(identity, None)
            if self.projects is not None:
                self.projects.finish_task(identity, drained=True)
            task.update(status="FAILED", error="对话任务未能启动", finished_at=now())
            self.store.save("task", identity, task)
            raise
        result = dict(task_id=identity, conversation_id=conversation_id)
        if self.projects is not None:
            result["project_id"] = project_id
        return result

    def start_plan(self, body):
        raise ValueError("旧多方案执行入口已停用；请在对话中使用已安装的能力工具")

    def _run(self, task, messages, bound, catalog, prior_wire=None, prior_context=None, *,
             runtime_options=None, on_event=None):
        if self.projects is not None:
            task.setdefault("workspace_controller", self._workspace_controller)
        control = self._controls.setdefault(task["id"], RunControl())
        if self.closed:
            control.cancel()
        options = dict(hooks=self.hooks, optional_hooks=self.optional_hooks,
                       connection=self.model_connection, scheduler=self.scheduler, projects=self.projects,
                       code_execution=self.code_execution)
        options.update(runtime_options or {})
        host = AgentRuntime(self.store, catalog, transport=self.transport, dispatch=self.dispatch, **options)
        def event(value):
            if on_event is not None:
                on_event(value)
            kind = value["kind"]
            if kind in {"model_message", "tool_receipt", "tool_observation"}:
                return
            if kind == "delta":
                self._stream_event(task["id"], "delta", value["text"])
            elif kind == "content_received":
                self._stream_event(task["id"], kind, data=dict(chars=len(value.get("text", ""))))
            elif kind == "hook_result":
                self._stream_event(task["id"], kind, data=dict(hook=value.get("hook"), event=value.get("event")))
            elif kind == "tool_result":
                self._stream_event(task["id"], kind, data={name: value.get(name) for name in ("name", "call_id")})
            elif kind == "capability_event":
                details = value.get("event") or {}
                self._stream_event(task["id"], kind, data={name: details.get(name) for name in ("call_id", "unit_id", "instance_id", "kind")})
            else:
                self._stream_event(task["id"], kind, data=value)
        try:
            host.run(task, messages, bound, on_event=event, control=control,
                     prior_wire=prior_wire, prior_context=prior_context)
        finally:
            router = getattr(catalog, "router", None)
            try:
                if self.projects is not None:
                    # The coordinator can persist on another thread. Never
                    # wait for it while holding the shared project/store lock.
                    self._finish_project_scope(task, catalog, bound, control)
                elif router is not None:
                    router.release_task(task["id"])
            finally:
                self._controls.pop(task["id"], None)
                self._threads.pop(task["id"], None)
                with self._stream_condition:
                    self._streams.pop(task["id"], None)
                    self._stream_condition.notify_all()

    def _finish_project_scope(self, task, catalog, profiles=None, control=None):
        try:
            # A prewarm or replica launch can outlive the request that caused
            # it. Keep this task's project lease until its own action exits.
            dispatcher = getattr(self.dispatch, "__self__", self.scheduler)
            if dispatcher is not None and hasattr(dispatcher, "condition") and hasattr(dispatcher, "elastic_runs"):
                deadline = time.monotonic() + 30.0
                with dispatcher.condition:
                    while dispatcher.elastic_runs.get(task["id"], 0):
                        remaining = deadline - time.monotonic()
                        if remaining <= 0 or self.closed or getattr(dispatcher, "closed", False):
                            break
                        dispatcher.condition.wait(min(0.5, remaining))
            drained = self._workspace_drained(task["id"], catalog, profiles, control, task=task)
            router = getattr(catalog, "router", None)
            if drained and router is not None:
                router.release_task(task["id"])
            scope = self.projects.finish_task(task["id"], drained=drained)
        except Exception as error:
            # A cleanup failure cannot turn an unresolved scope into success.
            task["workspace_error"] = str(error)
            try:
                self.projects.finish_task(task["id"], drained=False)
            except Exception as retained_error:
                task["workspace_error"] += "; " + str(retained_error)
            scope = dict(status="UNKNOWN")
        task["workspace_status"] = scope.get("status", "UNKNOWN")
        if task["workspace_status"] != "FINISHED":
            task["workspace_retention_reason"] = "调用、实例归属或临时文件清理尚未确认结束；保留工作区租约，实际释放后可重新核对。"
        else:
            task.pop("workspace_retention_reason", None)
            task.pop("workspace_error", None)
        self.store.save("task", task["id"], task)
        return task["workspace_status"] == "FINISHED"

    def _workspace_drained(self, identity, catalog, profiles=None, control=None, *, task=None):
        """Read actual call and owner facts; thread termination is not a receipt."""
        if control is not None:
            with control.lock:
                if control.request is not None or control.calls:
                    return False
        router = getattr(catalog, "router", None)
        task = task if task is not None else self.store.get("task", identity) or {}
        if task.get("python_calls") and (self.code_execution is None or not self.code_execution.is_drained(task)):
            return False
        if router is not None and hasattr(router, "lock"):
            with router.lock:
                reservations = getattr(router, "capability_task_reservations", {})
                if any(owner == identity and count for (owner, _), count in reservations.items()):
                    return False
        calls = task.get("calls") or []
        events = [item.get("event") or {} for item in task.get("events", [])
                  if item.get("kind") == "capability_event"]
        def busy_for_task(record, instance_id):
            if record.get("state") == "STOPPED" and record.get("released") is True:
                return False
            # A started call without this task's terminal receipt remains
            # uncertain even if another call owns the shared instance now.
            starts = [item for item in events if item.get("instance_id") == instance_id
                      and item.get("kind") == "call_started"]
            receipts = [item for item in calls if item.get("instance_id") == instance_id]
            if (any(not item.get("call_id") for item in starts) or any(not item.get("call_id") or
                    item.get("outcome") not in {"result", "error", "cancelled"} for item in receipts)):
                return True
            started = {item["call_id"] for item in starts}
            terminal = {item["call_id"] for item in receipts}
            if not started.issubset(terminal):
                return True
            if record.get("in_flight") == 0:
                return False
            reservations = getattr(router, "capability_task_reservations", None)
            if not isinstance(reservations, dict) or task.get("workspace_controller") != self._workspace_controller:
                return True
            return bool(reservations.get((identity, instance_id), 0))
        if calls or events:
            if (any(item.get("kind") == "call_started" and not item.get("instance_id") for item in events)
                    or any(item.get("call_id") and not item.get("instance_id") for item in calls)):
                return False
            instances = {item["instance_id"] for item in calls + events if item.get("instance_id")}
            if not instances or router is None or not hasattr(router, "lock") or not hasattr(router, "instances"):
                return False
            with router.lock:
                for instance in instances:
                    record = router.instances.get(instance)
                    if not record or record.get("state") == "UNKNOWN" or busy_for_task(record, instance):
                        return False
                    # The prior controller's empty call map is not proof.
                    if (task.get("workspace_controller") != self._workspace_controller
                            and not (record.get("state") == "STOPPED" and record.get("released") is True)):
                        return False
        dispatcher = getattr(self.dispatch, "__self__", self.scheduler)
        if dispatcher is not None and all(hasattr(dispatcher, name) for name in ("condition", "queue", "running")):
            with dispatcher.condition:
                if any(request.get("run_id") == identity for request in
                       list(dispatcher.queue.values()) + list(dispatcher.running.values())):
                    return False
                if getattr(dispatcher, "elastic_runs", {}).get(identity, 0):
                    return False
        runtime = self.runtime or getattr(getattr(router, "platform", None), "runtime", None)
        if runtime is not None:
            try:
                if any(call.get("physical") != "RETIRED" for call in runtime.call("task_calls", identity)):
                    return False
            except Exception:
                return False
        if router is not None:
            if not all(hasattr(router, name) for name in ("lock", "affinity", "instances")):
                return False
            with router.lock:
                for (owner, _), instance_id in router.affinity.items():
                    if owner != identity:
                        continue
                    record = router.instances.get(instance_id)
                    if not record or record.get("state") == "UNKNOWN" or busy_for_task(record, instance_id):
                        return False
        manager = getattr(profiles, "managed_services", None) or getattr(
            getattr(router, "platform", None), "managed_services", None)
        if manager is not None:
            if not hasattr(manager, "lock") or not hasattr(manager, "leases"):
                return False
            with manager.lock:
                if any(identity in leases for leases in manager.leases.values()):
                    return False
        return True

    def reconcile_project_leases(self, project_id):
        """Release retained scopes only after their worker ended and calls drained."""
        if self.projects is None:
            return []
        reconciled = []
        with self.projects.lock:
            self.projects.get(project_id)
            scopes = self.projects.task_scopes(project_id, unresolved_only=True)
        for scope in scopes:
            identity = scope["task_id"]
            if scope.get("status") == "FINISHED" or identity in self._threads:
                continue
            task = self.store.get("task", identity)
            if not task or task.get("status") in {"RUNNING", "CANCELLING"}:
                continue
            if self._finish_project_scope(task, self.catalog, self.profiles):
                reconciled.append(identity)
        return reconciled

    def cancel(self, identity):
        task = self.store.get("task", identity)
        if not task or not self._conversational(task):
            raise ValueError("没有这项对话任务")
        control = self._controls.get(identity)
        if task["status"] != "RUNNING" or control is None:
            return dict(task_id=identity, status=task["status"], cancel=task.get("cancel"))
        result = control.cancel()
        dispatcher = getattr(self.dispatch, "__self__", self.scheduler)
        if dispatcher is not None and hasattr(dispatcher, "cancel"):
            dispatcher.cancel(identity)
        self._stream_event(identity, "cancel_requested", data=result)
        return dict(task_id=identity, status="CANCELLING", cancel=result)

    def close(self):
        """Stop delivery and request cancellation without claiming remote drain."""
        self.closed = True
        for control in list(self._controls.values()):
            control.cancel()
        dispatcher = getattr(self.dispatch, "__self__", self.scheduler)
        if dispatcher is not None and hasattr(dispatcher, "cancel"):
            for identity in list(self._controls):
                dispatcher.cancel(identity)
