"""Real environment boundaries supplied by a task owner, independent of learners.

An adapter returns decision events or a real terminal event. It owns native
objects and continuation; the scheduler never treats a finished Unit as a task
terminal. Adapters are local Python callables, not generated reward programs.
"""

import copy
import hashlib
import importlib.util
import json
import math
import sys
import time
from pathlib import Path

from .capabilities import freeze_representation, represent


def digest_file(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def freeze_scene(spec, capabilities=None):
    value = copy.deepcopy(spec)
    if not value.get("id") or not value.get("version"):
        raise ValueError("场景需要 id 与 version")
    freeze_representation(value, capabilities or {})
    for key in ("observation_schema", "candidate_schema"):
        fields = value.get(key)
        if not isinstance(fields, list) or not fields or len(set(fields)) != len(fields):
            raise ValueError(key + " 需要不重复、有顺序的数值特征名称")
    scales = value.setdefault("observation_scales", [1.0] * len(value["observation_schema"]))
    if len(scales) != len(value["observation_schema"]) or any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0 for v in scales):
        raise ValueError("observation_scales 需要与特征对应的正数，运行内固定")
    adapter = value["adapter"]
    path = Path(adapter["path"]).resolve()
    if not path.is_file() or path.suffix != ".py":
        raise ValueError("场景适配器需要实际的本地 Python 文件")
    files = [path] + [Path(p).resolve() for p in adapter.get("dependencies", [])]
    adapter["path"] = str(path)
    adapter["files"] = [{"path": str(p), "sha256": digest_file(p)} for p in files]
    if not adapter.get("symbol"):
        raise ValueError("场景适配器需要 factory 函数名称")
    tasks = value.get("tasks", [])
    if value.get("tasks_path"):
        task_path = Path(value["tasks_path"]).resolve()
        raw = task_path.read_text(encoding="utf-8")
        tasks = [json.loads(line) for line in raw.splitlines() if line.strip()]
        value["tasks_file"] = dict(path=str(task_path), sha256=digest_file(task_path))
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("场景需要真实任务列表或 JSONL tasks_path")
    ids = [task.get("id") for task in tasks]
    if any(not isinstance(i, str) or not i for i in ids) or len(set(ids)) != len(ids):
        raise ValueError("每个任务需要不重复的字符串 id")
    value["tasks"] = tasks
    for uid, binding in value.get("native_usage", {}).items():
        if uid not in value.get("unit_ids", []) or not isinstance(binding, dict):
            raise ValueError("native_usage must bind an actual scene Unit")
        if "model_free" in binding and type(binding["model_free"]) is not bool:
            raise ValueError("model_free must be a boolean with a source basis")
        if binding.get("model_free") and (not isinstance(binding.get("basis"), str) or not binding["basis"].strip()):
            raise ValueError("model_free requires its actual source basis")
        fields = binding.get("fields", {})
        if not isinstance(fields, dict) or set(fields) - {"requests", "prompt_tokens", "completion_tokens", "total_tokens"}:
            raise ValueError("native usage fields must name measured requests or token counts")
        if any(not isinstance(path, list) or any(type(part) not in (str, int) for part in path) for path in fields.values()):
            raise ValueError("native usage selectors must be explicit result paths")
    value.setdefault("environment_version", "UNKNOWN")
    roles = value.setdefault("model_roles", ["chat"])
    if not isinstance(roles, list) or len(set(roles)) != len(roles) or any(r != "chat" for r in roles):
        raise ValueError("场景行为模型角色需显式声明为 chat 或空列表；教师仅用于评价")
    resources = []
    for resource in value.get("resources", []):
        path = Path(resource if isinstance(resource, str) else resource["path"]).resolve()
        files = sorted(p for p in path.rglob("*") if p.is_file() and ".cache" not in p.parts) if path.is_dir() else [path]
        resources.extend(dict(path=str(p), sha256=digest_file(p)) for p in files)
    value["resource_files"] = resources
    return value


def _vector(value, size, label):
    if not isinstance(value, list) or len(value) != size or any(
            type(x) not in (int, float) or not math.isfinite(x) for x in value):
        raise ValueError(label + " 与冻结的数值特征 schema 不符")
    return value


class FrozenCatalog:
    """A run's existing native bindings, owners and nonsecret Unit interfaces."""
    def __init__(self, catalog, unit_ids=None):
        self.catalog = catalog
        with catalog.lock:
            current = {unit["id"]: unit for unit in catalog.public()}
            selected = list(dict.fromkeys(unit_ids)) if unit_ids is not None else [
                unit["id"] for unit in catalog.business_units()]
            for uid in selected:
                if uid not in current or current[uid]["status"] != "READY":
                    raise ValueError("运行创建时 Unit 尚不可执行：" + uid)
                if current[uid].get("execution") == "REMOTE":
                    raise ValueError("该 Unit 使用既有远程路由，当前运行不能冻结该路由；不能静默改为本地执行：" + uid)
            self.frozen = catalog.freeze_resources(selected)
            self.units = {uid: copy.deepcopy(current[uid]) for uid in selected}
            self.resources = catalog.snapshot_resources(selected, frozen=self.frozen)

    def public(self):
        return copy.deepcopy(list(self.units.values()))

    def unit(self, unit_id):
        if unit_id not in self.frozen:
            raise ValueError("Unit 不在本次冻结能力池内")
        binding, entry, unit = self.frozen[unit_id]
        with binding.lock:
            value = copy.deepcopy(unit)
            value["initialization"] = binding.initialization_state(entry, unit)
        return value

    def business_units(self):
        return [unit for unit in self.public() if unit["status"] == "READY" and
                unit["scheduling"]["visibility"] == "business" and unit["scheduling"]["activation"] == "business"]

    def tools(self):
        return [dict(type="function", function=dict(name=unit["tool_name"], description=unit["description"],
                    parameters=unit["parameters"])) for unit in self.business_units()]

    def name_to_id(self, name):
        found = [unit["id"] for unit in self.business_units() if unit["tool_name"] == name]
        if len(found) != 1:
            raise ValueError("冻结能力池没有该原生工具")
        return found[0]

    def validate_arguments(self, unit_id, arguments, native_arguments=(), deferred_arguments=()):
        if unit_id not in self.frozen:
            raise ValueError("Unit 不在本次冻结能力池内")
        binding, entry, unit = self.frozen[unit_id]
        return self.catalog._validate_bound_arguments(binding, entry, unit, arguments,
            native_arguments=native_arguments, deferred_arguments=deferred_arguments)

    def snapshot_resources(self):
        return copy.deepcopy(self.resources)

    def invoke(self, unit_id, arguments, native_arguments=()):
        return self.catalog.invoke_frozen(self.frozen, unit_id, arguments, native_arguments)


class SceneContext:
    def __init__(self, platform, run_id, profiles=None, catalog=None):
        self.platform, self.run_id = platform, run_id
        self.profiles = profiles or platform.profiles
        self.catalog = catalog if catalog is not None else FrozenCatalog(platform.catalog, [])
        self.allowed = set()
        self.calls = []
        self.model_calls = []
        self.wire_reasoning = {}
        self.native_usage = {}
        self.upstream_costs = {}

    def invoke(self, unit_id, arguments, native_arguments=()):
        if unit_id not in self.allowed:
            raise ValueError("场景尝试调用本次动作之外的 Unit")
        started = time.perf_counter()
        unit = self.catalog.unit(unit_id)
        call = dict(unit_id=unit_id, status="RUNNING", initialization_before=unit.get("initialization"))
        self.calls.append(call)
        try:
            with self.platform.runtime.context(self.run_id):
                result = self.catalog.invoke(unit_id, arguments, native_arguments)
            call["status"] = "RETURN"
            binding = self.native_usage.get(unit_id, {})
            if binding.get("model_free") and binding.get("basis"):
                call.update(model_requests=0, usage=dict(prompt_tokens=0, completion_tokens=0, total_tokens=0),
                            usage_provenance=binding["basis"])
            elif binding.get("fields"):
                from dataclasses import asdict, is_dataclass
                payload = asdict(result) if is_dataclass(result) else result
                values = {}
                missing = {}
                for name, path in binding["fields"].items():
                    try:
                        current = payload
                        for part in path:
                            current = current[part]
                        if type(current) is not int or current < 0:
                            raise ValueError("usage value is not an actual nonnegative integer")
                        values[name] = current
                    except (KeyError, IndexError, TypeError, ValueError) as error:
                        missing[name] = str(error)
                call.update(model_requests=values.pop("requests", None), usage=values,
                            usage_provenance="module_return:" + unit_id, usage_missing=missing)
            return result
        except Exception as error:
            call.update(status="ERROR", reason=str(error))
            raise
        finally:
            call["duration_ms"] = (time.perf_counter() - started) * 1000
            after = self.catalog.unit(unit_id)
            if call["status"] == "RETURN":
                call["initialization_before"] = after.get("last_initialization_before", call["initialization_before"])
            call["initialization_after"] = after.get("initialization")
            call["cold_start"] = call["initialization_before"] != "BOUND"
            call["binding_duration_ms"] = after.get("last_binding_duration_ms") if call["status"] == "RETURN" else None

    def complete(self, messages, tools=None, role="chat"):
        from .providers import Transport, completion_body
        started = time.perf_counter()
        profile = self.profiles.role_profile(role)
        if role not in self.model_roles:
            raise ValueError("场景未声明使用该业务模型角色：" + role)
        if profile.get("provider") == "deepseek":
            messages = copy.deepcopy(messages)
            for message in messages:
                calls = message.get("tool_calls") or []
                if message.get("role") == "assistant" and calls and "reasoning_content" not in message:
                    recorded = self.wire_reasoning.get(calls[0].get("id"))
                    if recorded is not None:
                        message["reasoning_content"] = recorded
        call = dict(role=role, model=profile["model"], usage=None)
        self.model_calls.append(call)
        try:
            result = Transport().send(profile["base_url"] + "/chat/completions",
                self.profiles.role_key(role), completion_body(profile, messages, tools))
            call["usage"] = result.get("usage")
            for choice in result.get("choices", []):
                message = choice.get("message", {})
                if "reasoning_content" in message:
                    for tool in message.get("tool_calls") or []:
                        self.wire_reasoning[tool["id"]] = message["reasoning_content"]
            return result
        finally:
            call["duration_ms"] = (time.perf_counter() - started) * 1000


class SceneSession:
    def __init__(self, spec, platform, run_id, profiles=None, catalog=None):
        self.spec, self.platform = spec, platform
        self.catalog = catalog if catalog is not None else FrozenCatalog(platform.catalog, spec.get("unit_ids", []))
        for item in spec["adapter"]["files"]:
            if digest_file(item["path"]) != item["sha256"]:
                raise ValueError("场景适配源码已改变，请新建运行")
        for item in spec.get("resource_files", []):
            if digest_file(item["path"]) != item["sha256"]:
                raise ValueError("场景模型／数据资源已改变，请新建运行")
        adapter = spec["adapter"]
        name = "seampact_scene_" + digest_file(adapter["path"])
        module_spec = importlib.util.spec_from_file_location(name, adapter["path"])
        module = importlib.util.module_from_spec(module_spec)
        sys.modules[name] = module
        module_spec.loader.exec_module(module)
        self.context = SceneContext(platform, run_id, profiles, self.catalog)
        self.context.model_roles = spec.get("model_roles", ["chat"])
        self.context.native_usage = copy.deepcopy(spec.get("native_usage", {}))
        self.environment = getattr(module, adapter["symbol"])(copy.deepcopy(adapter.get("config", {})), self.context)
        self.last = None

    def _event(self, event):
        started = time.perf_counter()
        if not isinstance(event, dict) or event.get("kind") not in {"decision", "terminal"}:
            raise ValueError("场景必须返回 decision 或真正的 terminal 事件；等待由原环境处理")
        if event["kind"] == "terminal":
            if type(event.get("terminated")) is not bool or type(event.get("truncated")) is not bool:
                raise ValueError("终止事件需要明确 terminated/truncated")
            if not (event["terminated"] or event["truncated"]):
                raise ValueError("非结束快照不能用作 terminal")
            self.last = event
            return event
        if self.spec.get("representation"):
            represent(event, self.spec["representation"])
        _vector(event.get("observation"), len(self.spec["observation_schema"]), "观测")
        event["raw_observation"] = list(event["observation"])
        event["observation"] = [value / scale for value, scale in zip(event["observation"], self.spec["observation_scales"])]
        candidates = event.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            raise ValueError("决策事件没有候选；环境应等待下一真实事件或结束")
        ids = [c.get("id") for c in candidates]
        if any(not isinstance(i, str) for i in ids) or len(set(ids)) != len(ids):
            raise ValueError("本次候选需要不重复 id")
        required = set(event.get("required_units", []))
        pool = set(self.spec.get("unit_ids", []))
        live = {u["id"] for u in self.catalog.public() if u["status"] == "READY"}
        for candidate in candidates:
            _vector(candidate.get("features"), len(self.spec["candidate_schema"]), "候选特征")
            units = set(candidate.get("units", []))
            if units - pool:
                raise ValueError("候选包含冻结能力池以外的 Unit")
            if type(candidate.get("legal")) is not bool:
                raise ValueError("候选需显式给出 legal")
            # Environment obligations cannot be traded against a reward.
            candidate["legal"] = candidate["legal"] and required <= units and units <= live
            groups = candidate.get("exclusive_groups", [])
            if len(groups) != len(set(groups)):
                candidate["legal"] = False
        if not any(c["legal"] for c in candidates):
            raise ValueError("当前无可执行且满足必需工作的候选")
        self.last = event
        event["representation_duration_ms"] = (time.perf_counter() - started) * 1000
        return event

    def reset(self, task, seed):
        self.context.calls.clear()
        self.context.model_calls.clear()
        self.context.wire_reasoning.clear()
        self.context.upstream_costs = copy.deepcopy(task.get("upstream_costs", {}))
        return self._event(self.environment.reset(copy.deepcopy(task), seed))

    def step(self, index):
        event = self.last
        if not event or event["kind"] != "decision":
            raise ValueError("当前不在决策事件")
        candidate = event["candidates"][index]
        if not candidate["legal"]:
            raise ValueError("不可选择非法候选")
        self.context.allowed = set(candidate.get("units", []))
        first_call = len(self.context.calls)
        try:
            result = self._event(self.environment.step(candidate))
            called = {call["unit_id"] for call in self.context.calls[first_call:]}
            if set(event.get("required_units", [])) - called:
                raise ValueError("场景适配器没有执行本次已确定的必需 Unit")
            return result
        finally:
            self.context.allowed.clear()

    def close(self):
        close = getattr(self.environment, "close", None)
        if close:
            close()


def arrays(event):
    return dict(state=event["observation"], candidates=[c["features"] for c in event["candidates"]],
                legal=[c["legal"] for c in event["candidates"]])
