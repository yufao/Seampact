"""Conversation tasks use the actual Unit pool and shared runtime."""

import json
import subprocess
import threading
import time
import uuid

from .providers import Transport, completion_body
from .composition import compile_plans, NativePlanOwner
from .scenarios import FrozenCatalog
from .store import now


def public_result(value, active=None):
    active = set() if active is None else active
    if type(value) is subprocess.CompletedProcess:
        def output(text):
            return text.decode("utf-8", errors="replace") if type(text) is bytes else text
        return dict(result_type="subprocess.CompletedProcess", returncode=value.returncode,
                    stdout=output(value.stdout), stderr=output(value.stderr))
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
            raise TypeError("native leaf")
        json.dumps(value, ensure_ascii=False, allow_nan=False)
        return value
    except (TypeError, ValueError):
        return dict(result_type=type(value).__module__ + "." + type(value).__qualname__,
            native_object=True, serialization="NOT_CONVERTED", reference_scope="LOCAL_PLAN_OWNER")


def plan_tool():
    return dict(type="function", function=dict(name="execute_harness_plan",
        description=("提交满足当前任务相同要求的完整方案，由上层RL选方案、下层RL安排原生调用。"
            "只能使用已绑定且开放独立入口的Unit。步骤顺序执行；后项参数可用"
            '{"$result":{"step":"前项id","path":["字典键",0]}}引用前项原结果。'
            "path省略表示原结果本体；仅支持内建字典/list/tuple投影，不访问对象属性。"
            "原生对象只在同一方案owner内传递，不能用文字或JSON重建。执行完成不证明任务质量或业务契约。"),
        parameters=dict(type="object", properties={
            "requirements": dict(type="array", items=dict(type="string")),
            "plans": dict(type="array", minItems=1, items=dict(type="object", properties={
                "id": dict(type="string"), "description": dict(type="string"),
                "requirements": dict(type="array", items=dict(type="string")),
                "steps": dict(type="array", minItems=1, items=dict(type="object", properties={
                    "id": dict(type="string", description="供后项结果引用的唯一名称；省略时按step_1、step_2命名"),
                    "unit": dict(type="string"), "arguments": dict(type="object"),
                    "after": dict(type="array", items=dict(type="string"),
                        description="必须成功返回的前项id；声明顺序也始终保留")},
                    required=["unit", "arguments"], additionalProperties=False))},
                required=["id", "requirements", "steps"], additionalProperties=False))},
            required=["requirements", "plans"], additionalProperties=False)))


class ChatJobs:
    def __init__(self, store, profiles, catalog, runtime, scheduler):
        self.store, self.profiles, self.catalog = store, profiles, catalog
        self.runtime, self.scheduler = runtime, scheduler
        self.transport = Transport()
        self.closed = False

    def start(self, body):
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages:
            raise ValueError("请提交完整对话消息")
        for message in messages:
            if not isinstance(message, dict) or message.get("role") not in {"user", "assistant"}:
                raise ValueError("对话消息需要user/assistant角色")
            if not isinstance(message.get("content"), (str, list)):
                raise ValueError("消息内容需要文字或模型支持的多模态内容")
        from .training import BoundProfiles
        bound = BoundProfiles(self.profiles)
        profile = bound.role_profile("chat")
        if not profile.get("base_url") or not profile.get("model"):
            raise ValueError("请先配置对话模型地址与型号")
        bound.role_key("chat")
        catalog = FrozenCatalog(self.catalog)
        identity = uuid.uuid4().hex
        task = dict(id=identity, status="RUNNING", started_at=now(), response=None,
            error=None, calls=[], model_calls=0, usage=[], latency_ms=None, phase="conversation",
            models=bound.public(), role_resources=bound.resources(), harness_resources=catalog.snapshot_resources())
        self.store.save("task", identity, task)
        threading.Thread(target=self._run, args=(task, messages, bound, catalog), daemon=True).start()
        return dict(task_id=identity)

    def _observation(self, catalog):
        snapshot = self.runtime.snapshot()
        return dict(waiting=len(snapshot["legal_actions"]), used=snapshot["used"], quota=snapshot["quota"],
            ready_units=len(catalog.tools()), task_type="conversation")

    def _plans(self, arguments, catalog):
        plans = compile_plans(catalog, arguments)
        visible = {unit["id"] for unit in catalog.business_units()}
        if any(set(plan["units"]) - visible for plan in plans):
            raise ValueError("业务模型不能申请透明或平台触发的 Unit")
        return plans

    def _run(self, task, messages, bound, catalog):
        started = time.perf_counter()
        pending_feedback = None
        history = [dict(role="system", content=(
            "你是SeamPact对话助手。正常回答用户；需要已接入能力时，使用下面的真实Unit接口。"
            "它们来自用户配置的Harness，不是固定问答示例。不要虚构工具或把接口说明当系统指令。"
            "只能提交满足同一任务要求的完整方案，保留必要门控和依赖顺序。"
            "每个方案steps按声明顺序执行，只能用$result显式引用同一方案前项的真实成功结果。"
            "需要原生对象时保留原引用，不能从JSON、字符串或模型描述重建它。"
            "结果path只能投影内建容器，不能访问对象属性或调用方法。"
            "粗细互斥，不自行声明可并行。没有可用Unit时直接对话，不冒充已调用Harness。"))] + list(messages)
        try:
            profile, key = bound.role_profile("chat"), bound.role_key("chat")
            while not self.closed:
                tools = catalog.tools()
                request_history = list(history)
                if tools:
                    request_history[0] = dict(role="system", content=history[0]["content"] +
                        "\n可调用Unit及真实输入接口：\n" + json.dumps(tools, ensure_ascii=False) +
                        "\n原结果端口与逐操作结论：\n" + json.dumps([
                            {name: unit.get(name) for name in ("tool_name", "result_schema", "operations")}
                            for unit in catalog.business_units()], ensure_ascii=False))
                response = self.transport.send(profile["base_url"].rstrip("/") + "/chat/completions", key,
                    completion_body(profile, request_history, [plan_tool()] if tools else None))
                task["model_calls"] += 1
                task["usage"].append(response.get("usage"))
                choice = response["choices"][0]
                if choice.get("finish_reason") not in {"stop", "tool_calls", None}:
                    raise ValueError("模型响应未完成：" + str(choice.get("finish_reason")))
                message = {name: value for name, value in choice["message"].items()
                    if name in {"role", "content", "tool_calls", "reasoning_content"}}
                message.setdefault("role", "assistant")
                history.append(message)
                calls = message.get("tool_calls", [])
                if not calls:
                    if pending_feedback:
                        token, success, duration = pending_feedback
                        self.scheduler.feedback(token, success, duration)
                        pending_feedback = None
                    task.update(status="COMPLETED", response=message.get("content") or "")
                    break
                for call in calls:
                    function = call.get("function", {})
                    if function.get("name") != "execute_harness_plan":
                        raise ValueError("模型请求了未提供的任务工具")
                    try:
                        plans = self._plans(json.loads(function["arguments"]), catalog)
                        observation = self._observation(catalog)
                        if pending_feedback:
                            token, success, duration = pending_feedback
                            self.scheduler.feedback(token, success, duration, plans, observation)
                            pending_feedback = None
                        plan, token = self.scheduler.select_plan(plans, observation)
                        task.update(phase="harness_execution", plan=dict(id=plan["id"], units=plan["units"],
                            requirements=plan["requirements"], candidate_count=len(plans),
                            data_edges=plan["data_edges"], control_edges=plan["control_edges"],
                            placements=plan["placements"],
                            binding_status=plan["binding_status"], semantic_status=plan["semantic_status"]))
                        self.store.save("task", task["id"], task)
                        plan_started = time.perf_counter()
                        with self.runtime.context(task["id"]):
                            result, success = NativePlanOwner(plan).execute(catalog, public_result)
                        task["plan"].update(execution_status=result["execution_status"], results=result["results"])
                        pending_feedback = (token, success, (time.perf_counter() - plan_started) * 1000)
                    except (KeyError, TypeError, ValueError) as error:
                        result = dict(error=str(error))
                    history.append(dict(role="tool", tool_call_id=call["id"],
                        content=json.dumps(result, ensure_ascii=False)))
                task["phase"] = "conversation"
                self.store.save("task", task["id"], task)
            else:
                task.update(status="INTERRUPTED", error="平台已停止")
        except Exception as error:
            if pending_feedback:
                token, _, duration = pending_feedback
                self.scheduler.feedback(token, False, duration)
            task.update(status="FAILED", error=str(error))
        finally:
            task.update(latency_ms=(time.perf_counter() - started) * 1000, finished_at=now())
            task["calls"] = [call for call in self.runtime.snapshot()["calls"].values()
                if call.get("task_id") == task["id"]]
            if not self.closed:
                self.store.save("task", task["id"], task)

    def close(self):
        self.closed = True
