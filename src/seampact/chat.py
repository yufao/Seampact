"""Web conversation jobs, history and visible events around the shared Agent host."""

import copy
import json
import subprocess
import threading
import uuid
from collections import deque

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
                 model_connection=None, hooks=(), optional_hooks=None):
        self.store, self.profiles, self.catalog = store, profiles, catalog
        self.runtime, self.scheduler = runtime, scheduler
        self.transport = Transport()
        self.dispatch, self.model_connection = dispatch, model_connection
        self.hooks, self.optional_hooks = tuple(hooks), optional_hooks
        self._controls, self._threads = {}, {}
        self._stream_condition = threading.Condition()
        self._streams = {}
        self.closed = False
        for task in self.store.tasks_with_status("RUNNING", summary=False):
            if self._conversational(task) or task.get("origin") == "EXPLICIT_TASK_PLAN":
                task.update(status="INTERRUPTED", error="平台已重启，原任务未恢复；已保存的对话仍可查看。", finished_at=now())
                self.store.save("task", task["id"], task)

    def _stream_event(self, identity, kind, text="", **facts):
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

    def conversations(self, query="", limit=None, offset=0):
        """Conversation list from per-task heads; tool traces and model context are not read."""
        deleted = {row["id"] for row in self.store.list("chat_deleted")}
        needle = query.casefold() if query else ""
        groups = {}
        for head in self.store.conversation_heads():
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
        return dict(id=identity, messages=messages, legacy=not bool(latest.get("messages")),
                    task={key: latest.get(key) for key in ("id", "status", "response", "partial_response", "error", "latency_ms", "context_status", "context_events", "files", "cancel")})

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

    def start(self, body):
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
        identity = uuid.uuid4().hex
        task = dict(id=identity, status="RUNNING", started_at=now(), response=None,
            error=None, partial_response="", calls=[], model_calls=0, usage=[], latency_ms=None, phase="conversation",
            conversation_id=conversation_id, messages=copy.deepcopy(messages),
            context_calibration=copy.deepcopy(prior_calibration), models=bound.public(), role_resources=bound.resources(),
            capability_pool=copy.deepcopy(catalog.public()))
        self.store.save("task", identity, task)
        self._stream_event(identity, "reset")
        self._controls[identity] = RunControl()
        thread = threading.Thread(target=self._run, args=(task, messages, bound, catalog, prior_wire, prior_context),
                                  daemon=True, name="seampact-agent-" + identity[:8])
        self._threads[identity] = thread
        thread.start()
        return dict(task_id=identity, conversation_id=conversation_id)

    def start_plan(self, body):
        raise ValueError("旧多方案执行入口已停用；请在对话中使用已安装的能力工具")

    def _run(self, task, messages, bound, catalog, prior_wire=None, prior_context=None):
        control = self._controls.setdefault(task["id"], RunControl())
        if self.closed:
            control.cancel()
        host = AgentRuntime(self.store, catalog, transport=self.transport, dispatch=self.dispatch,
                            hooks=self.hooks, optional_hooks=self.optional_hooks,
                            connection=self.model_connection, scheduler=self.scheduler)
        def event(value):
            kind = value["kind"]
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
            # Drop only this task's lease. Shared instance reclamation belongs
            # to the scheduler and respects other in-flight work.
            router = getattr(catalog, "router", None)
            try:
                if router is not None:
                    router.release_task(task["id"])
            finally:
                self._controls.pop(task["id"], None)
                self._threads.pop(task["id"], None)
                with self._stream_condition:
                    self._streams.pop(task["id"], None)
                    self._stream_condition.notify_all()

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
