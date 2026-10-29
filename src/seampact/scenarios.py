"""Real environment boundaries supplied by a task owner, independent of learners.

An adapter returns decision events or a real terminal event. It owns native
objects and continuation; the scheduler never treats a finished Unit as a task
terminal. Adapters are local Python callables, not generated reward programs.
"""

import contextlib
import copy
import hashlib
import importlib.util
import json
import math
import sys
import time
from pathlib import Path

from .capabilities import freeze_representation, represent
from .runtime import _task_context


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
    if "routes" in value:
        return _freeze_mixed_scene(value, capabilities or {})
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
    for task in tasks:
        selected = task.get("unit_ids", value.get("unit_ids", []))
        if (not isinstance(selected, list) or any(not isinstance(uid, str) or not uid for uid in selected)
                or len(set(selected)) != len(selected) or set(selected) - set(value.get("unit_ids", []))):
            raise ValueError("task unit_ids must be a distinct subset of its frozen scene Units")
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


def _freeze_mixed_scene(value, capabilities):
    """Freeze task routes inside the existing arrival/resource window.

    Routes are ordinary scene adapters, with their own installed Unit subset and
    criteria. The union uses one capability-features/2 schema; route names and
    task classes are result metadata, never learned features.
    """
    routes = value.get("routes")
    if not isinstance(routes, dict) or not routes or any(
            not isinstance(key, str) or not key or not isinstance(route, dict)
            for key, route in routes.items()):
        raise ValueError("mixed routes must be named scene objects")
    if (value.get("representation") or {}).get("version") != "capability-features/2":
        raise ValueError("mixed routes require the shared capability-features/2 representation")
    tasks = value.get("tasks", [])
    if value.get("tasks_path"):
        path = Path(value["tasks_path"]).resolve()
        tasks = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        value["tasks_file"] = dict(path=str(path), sha256=digest_file(path))
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("mixed scene requires actual tasks")
    ids = [task.get("id") for task in tasks]
    if any(not isinstance(i, str) or not i for i in ids) or len(set(ids)) != len(ids):
        raise ValueError("mixed tasks require unique nonempty string ids")
    for task in tasks:
        route_id = task.get("route")
        if route_id not in routes:
            raise ValueError("each mixed task must select a frozen route")
        task_class = task.setdefault("task_class", routes[route_id].get("task_class", route_id))
        if not isinstance(task_class, str) or not task_class:
            raise ValueError("task_class must be nonempty text")
        selected = task.get("unit_ids", routes[route_id].get("unit_ids", []))
        if (not isinstance(selected, list) or any(not isinstance(uid, str) or not uid for uid in selected)
                or len(set(selected)) != len(selected)
                or set(selected) - set(routes[route_id].get("unit_ids", []))):
            raise ValueError("task unit_ids must be a distinct subset of its frozen route Units")
    pool = list(dict.fromkeys(uid for route in routes.values() for uid in route.get("unit_ids", [])))
    if "unit_ids" in value and set(value["unit_ids"]) != set(pool):
        raise ValueError("mixed unit_ids must equal the union of route Unit subsets")
    value["unit_ids"], value["tasks"] = pool, tasks
    frozen_routes = {}
    for route_id, route in routes.items():
        if "routes" in route:
            raise ValueError("nested mixed routes are unsupported")
        route_tasks = [copy.deepcopy(task) for task in tasks if task["route"] == route_id]
        if not route_tasks:
            raise ValueError("every route must have a real task")
        config = copy.deepcopy(route)
        config.update(id=route.get("id", value["id"] + ":" + route_id),
                      version=route.get("version", value["version"]), tasks=route_tasks,
                      representation=copy.deepcopy(value["representation"]))
        config.pop("tasks_path", None)
        config.setdefault("model_roles", value.get("model_roles", ["chat"]))
        config.setdefault("environment_version", value.get("environment_version", "UNKNOWN"))
        frozen_routes[route_id] = freeze_scene(config, capabilities)
        for scope in ("criteria", "evaluation_criteria"):
            if route.get(scope) is not None:
                from .evaluation import validate_criteria
                frozen_routes[route_id][scope] = validate_criteria(route[scope])
        _applicability(route.get("metric_applicability", {}))
    # Reuse single-scene validation for the shared feature and resource contract.
    shared = copy.deepcopy(value)
    shared.pop("routes")
    shared.pop("tasks_path", None)
    shared["adapter"] = copy.deepcopy(next(iter(frozen_routes.values()))["adapter"])
    shared = freeze_scene(shared, capabilities)
    all_files = {item["path"]: item for route in frozen_routes.values() for item in route["adapter"]["files"]}
    shared["adapter"]["files"] = list(all_files.values())
    all_resources = {item["path"]: item for route in frozen_routes.values() for item in route.get("resource_files", [])}
    all_resources.update({item["path"]: item for item in shared.get("resource_files", [])})
    shared["resource_files"] = list(all_resources.values())
    shared["routes"] = frozen_routes
    shared["model_roles"] = sorted(set(role for route in frozen_routes.values() for role in route["model_roles"]))
    shared["routing_contract"] = "task-routes/1"
    for task in shared["tasks"]:
        _applicability(task.get("metric_applicability", {}))
    return shared


def _applicability(value):
    if not isinstance(value, dict) or any(not isinstance(key, str) or not key or type(flag) is not bool
                                          for key, flag in value.items()):
        raise ValueError("metric_applicability must map metric ids to explicit booleans")
    return value


def task_route(scene, task):
    """Return the frozen route, rejecting mutable task-to-route remapping."""
    if not scene.get("routes"):
        return scene
    frozen = next((item for item in scene["tasks"] if item["id"] == task.get("id")), None)
    if (frozen is None or task.get("route") != frozen["route"] or task.get("task_class") != frozen["task_class"]
            or task.get("unit_ids") != frozen.get("unit_ids")):
        raise ValueError("task id, route and task_class must match the frozen task mapping")
    return scene["routes"][frozen["route"]]


def task_unit_ids(scene, task):
    """Installed Units this frozen task may use, for context and shared admission."""
    route = task_route(scene, task)
    frozen = next((item for item in scene.get("tasks", []) if item["id"] == task.get("id")), None)
    if "tasks" in scene and (frozen is None or task.get("unit_ids") != frozen.get("unit_ids")):
        raise ValueError("task Unit subset must match the frozen task mapping")
    selected = task.get("unit_ids", route.get("unit_ids", []))
    if (not isinstance(selected, list) or len(set(selected)) != len(selected)
            or set(selected) - set(route.get("unit_ids", []))):
        raise ValueError("task Unit subset is outside its frozen route")
    return list(selected)


def scene_task_criteria(scene, task, fallback, scope="criteria"):
    if scope not in {"criteria", "evaluation_criteria"}:
        raise ValueError("unknown route criterion scope")
    return copy.deepcopy(task_route(scene, task).get(scope, fallback))


def route_metric_applicability(scene, task, terminal=None):
    """Combine frozen task declarations with actual terminal applicability.

    A task/terminal may narrow a route requirement but cannot re-enable an
    explicitly inapplicable route metric. Missing metrics remain UNKNOWN in the
    evaluator; they are never silently marked inapplicable here.
    """
    route = task_route(scene, task)
    result = copy.deepcopy(_applicability(route.get("metric_applicability", {})))
    for declaration in (task.get("metric_applicability", {}), (terminal or {}).get("metric_applicability", {})):
        for key, flag in _applicability(declaration).items():
            if result.get(key) is False and flag:
                raise ValueError("task/terminal cannot enable a route-inapplicable metric: " + key)
            result[key] = flag
    return result


def route_metric_report(scene, records, criteria=None, evaluation_criteria=None):
    """Count every arrived record, including failures and absent feedback, per route.

    Defined metrics are seeded before counting. Missing feedback is UNKNOWN unless
    the frozen declaration explicitly makes that metric inapplicable. No quality
    values or denominators are combined across routes. Outcomes count final
    completions/failures and tasks unfinished at the horizon or final close;
    late_completed is the intersection of completed and horizon-unfinished.
    """
    tasks = {task["id"]: task for task in scene["tasks"]}
    result = {}
    for record in records:
        if record.get("status") == "NOT_ARRIVED":
            continue
        task = tasks[record["task_id"]]
        task_route(scene, task)
        key = task.get("route", scene["id"])
        bucket = result.setdefault(key, dict(route=key, tasks=0, metrics={}, _records=[],
            outcomes=dict(completed=0, failed=0, unfinished=0, late_completed=0)))
        bucket["tasks"] += 1
        status = record.get("status")
        bucket["outcomes"]["completed"] += int(status == "DONE")
        bucket["outcomes"]["failed"] += int(status == "FAILED")
        bucket["outcomes"]["unfinished"] += int(status == "UNFINISHED" or record.get("deadline_status") == "UNFINISHED")
        bucket["outcomes"]["late_completed"] += int(status == "DONE" and record.get("deadline_status") == "UNFINISHED")
        bucket["_records"].append((task, record))
        for scope, field in (("criteria", "feedback"), ("evaluation_criteria", "independent_feedback")):
            spec = scene_task_criteria(scene, task, criteria if scope == "criteria" else evaluation_criteria, scope)
            definitions = {item["id"]: item for item in (spec or {}).get("dimensions", [])}
            observed = {item["id"]: item for item in (record.get(field) or {}).get("metrics", [])}
            for mid in dict.fromkeys([*definitions, *observed]):
                identity = scope + ":" + mid
                bucket["metrics"].setdefault(identity, dict(id=mid, scope=scope,
                    definition=copy.deepcopy(definitions.get(mid, observed.get(mid))),
                    total=0, applicable=0, known=0, unknown=0, not_applicable=0, values=[]))
    for bucket in result.values():
        for task, record in bucket.pop("_records"):
            applicability = route_metric_applicability(scene, task)
            for row in bucket["metrics"].values():
                field = "feedback" if row["scope"] == "criteria" else "independent_feedback"
                metric = next((m for m in (record.get(field) or {}).get("metrics", []) if m["id"] == row["id"]), None)
                if metric is None:
                    applicable = applicability.get(row["id"], row["definition"].get("applicable", True))
                    metric = dict(status="UNKNOWN" if applicable else "NOT_APPLICABLE")
                status = metric["status"]
                if status not in {"KNOWN", "UNKNOWN", "NOT_APPLICABLE"}:
                    raise ValueError("unknown metric status")
                row["total"] += 1
                row[status.lower()] += 1
                row["applicable"] += int(status != "NOT_APPLICABLE")
                if status == "KNOWN":
                    row["values"].append(metric["value"])
    return list(result.values())


def _vector(value, size, label):
    if not isinstance(value, list) or len(value) != size or any(
            type(x) not in (int, float) or not math.isfinite(x) for x in value):
        raise ValueError(label + " 与冻结的数值特征 schema 不符")
    return value


class FrozenCatalog:
    """A run's native bindings, owners, Unit interfaces and frozen execution placement.

    LOCAL runs the bound owner in this process. REMOTE runs on the Unit's
    runtime-group host; the fixed functional-mode controller prepares an
    instance when needed. A placement never changes silently during a run.
    """
    def __init__(self, catalog, unit_ids=None, placement=None):
        self.catalog, self.router, self.facts = catalog, catalog.remote_router, {}
        with catalog.lock:
            current = {unit["id"]: unit for unit in catalog.public()}
            selected = list(dict.fromkeys(unit_ids)) if unit_ids is not None else [
                unit["id"] for unit in catalog.business_units()]
            requested = dict(placement or {})
            if set(requested) - set(selected) or any(value not in {"LOCAL", "REMOTE"} for value in requested.values()):
                raise ValueError("placement 只能为本次 Unit 指定 LOCAL 或 REMOTE")
            self.placement = {}
            for uid in selected:
                unit = current.get(uid)
                if unit is None:
                    raise ValueError("运行创建时 Unit 尚不可执行：" + uid)
                dispatch = unit.get("dispatch") or {}
                local = unit["status"] == "READY" and not unit.get("local_status") and uid in catalog.bindings
                kind = requested.get(uid) or ("LOCAL" if local else "REMOTE")
                if kind == "LOCAL":
                    if not local:
                        raise ValueError("运行创建时 Unit 尚不可在本进程执行：" + uid + "；" + str(dispatch.get("reason") or unit.get("reason")))
                    self.placement[uid] = dict(kind="LOCAL")
                    continue
                host = (unit.get("operations") or {}).get("group_host") or {}
                group = catalog.store.get("runtime_group", unit.get("runtime_group") or "")
                gap = ("Unit 没有运行组" if not group or self.router is None else
                       host.get("reason") or "成员未通过宿主核对" if host.get("status") != "HOST_CHECKED" else
                       None if dispatch.get("status") == "READY_INSTANCE" else self.router.start_gap(group["id"]))
                if gap:
                    raise ValueError("该 Unit 需在运行组宿主执行，但当前既无就绪实例也不可启动：" + uid + "；" + gap)
                artifact = catalog.store.get("deployment", group["id"]) or {}
                self.placement[uid] = dict(kind="REMOTE", group_id=group["id"], profile=group["contract"]["host_profile"],
                    contract_digest=group["contract"]["digest"],
                    image=(artifact.get("publish") or {}).get("reference") or artifact.get("image"),
                    image_id=(artifact.get("build") or {}).get("image_id"),
                    binding_fingerprint=artifact.get("binding_fingerprint"))
            remote = {uid for uid, value in self.placement.items() if value["kind"] == "REMOTE"}
            self.frozen = catalog.freeze_resources(selected, remote=remote)
            self.units = {uid: copy.deepcopy(current[uid]) for uid in selected}
            for uid in remote:
                self.units[uid].update(status="READY", execution="REMOTE")
                self.frozen[uid][2].update(status="READY", execution="REMOTE")
            self.resources = catalog.snapshot_resources(selected, frozen=self.frozen)

    def bind_run(self, run_id, control):
        for value in self.placement.values():
            if value["kind"] == "REMOTE":
                value.update(run_id=run_id, start_timeout_s=control["start_timeout_s"])

    def remote_groups(self):
        return {value["group_id"]: value for value in self.placement.values() if value["kind"] == "REMOTE"}

    def public(self):
        return copy.deepcopy(list(self.units.values()))

    def unit(self, unit_id):
        if unit_id not in self.frozen:
            raise ValueError("Unit 不在本次冻结能力池内")
        binding, entry, unit = self.frozen[unit_id]
        if self.placement[unit_id]["kind"] == "REMOTE":
            value = copy.deepcopy(unit)
            stored = self.catalog.store.get("unit", unit_id) or {}
            for key in ("initialization", "last_initialization_before", "last_binding_duration_ms"):
                value[key] = stored.get(key)
            return value
        with binding.lock:
            value = copy.deepcopy(unit)
            value["initialization"] = binding.initialization_state(entry, unit)
        return value

    def dispatch_status(self, unit_id):
        placement = self.placement[unit_id]
        if placement["kind"] == "LOCAL":
            return dict(status="READY_LOCAL")
        fact = self.router.runtime_fact(placement["group_id"], unit_id, placement.get("image"))
        if fact["ready_instance"]:
            return dict(status="READY_INSTANCE")
        if fact["cold_startable"]:
            return dict(status="COLD_STARTABLE")
        return dict(status="UNAVAILABLE", reason=fact.get("reason") or "实例结果未决或不可启动")

    def runtime_state(self, unit_ids):
        facts = []
        for uid in unit_ids:
            placement = self.placement[uid]
            if placement["kind"] == "REMOTE":
                facts.append(self.router.runtime_fact(placement["group_id"], uid, placement.get("image")))
                continue
            binding, entry, unit = self.frozen[uid]
            group = unit.get("runtime_group")
            facts.append(dict(local=True, ready_instance=True, warm=binding.initialization_state(entry, unit) == "BOUND",
                              cold_startable=False, accepting=True, busy=binding.active_calls > 0,
                              queue=self.router.queue(group) if self.router and group else 0, state_age_ms=0.0))
        def every(name):
            values = [fact.get(name) for fact in facts]
            return None if None in values else all(values)
        def some(name):
            values = [fact.get(name) for fact in facts]
            return True if True in values else None if None in values else False
        def total(name, combine):
            values = [fact.get(name) for fact in facts]
            return None if None in values else combine(values)
        return dict(local=every("local"), ready_instance=every("ready_instance"), warm=every("warm"),
                    cold_startable=some("cold_startable"), accepting=every("accepting"), busy=some("busy"),
                    queue=total("queue", sum), state_age_ms=total("state_age_ms", max))

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
        # Concurrent tasks of one window share this catalog; facts belong to the calling task.
        key = (_task_context.get(), unit_id)
        placement = self.placement.get(unit_id, dict(kind="LOCAL"))
        if placement["kind"] == "LOCAL":
            self.facts[key] = dict(execution="LOCAL")
            return self.catalog.invoke_frozen(self.frozen, unit_id, arguments, native_arguments)
        binding, entry, unit = self.frozen[unit_id]
        self.catalog._validate_bound_arguments(binding, entry, unit, arguments, native_arguments)
        if "run_id" not in placement:
            raise ValueError("运行组放置需要运行身份")
        self.facts[key] = dict(execution="REMOTE", group_id=placement["group_id"])
        result, facts = self.router.invoke_placed(placement, unit, arguments, native_arguments)
        self.facts[key] = facts
        return result


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
        # A joint window replaces this with its root-request admission.
        self.admission = lambda unit_id: contextlib.nullcontext()

    def invoke(self, unit_id, arguments, native_arguments=()):
        if unit_id not in self.allowed:
            raise ValueError("场景尝试调用本次动作之外的 Unit")
        started = time.perf_counter()
        call = dict(unit_id=unit_id, status="RUNNING", initialization_before=None)
        self.calls.append(call)
        facts, after = dict(execution="LOCAL"), None
        try:
            with self.platform.runtime.context(self.run_id), self.admission(unit_id) as root:
                if root is not None:
                    # Queueing before the grant is task time, kept in root.wait_ms, not module duration.
                    call["root"], started = root, time.perf_counter()
                # Read binding state while this request holds its host, not behind another task's call.
                call["initialization_before"] = self.catalog.unit(unit_id).get("initialization")
                try:
                    result = self.catalog.invoke(unit_id, arguments, native_arguments)
                finally:
                    facts = self.catalog.facts.pop((self.run_id, unit_id), facts)
                    after = self.catalog.unit(unit_id)
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
            call.update({key: facts.get(key) for key in ("execution", "group_id", "instance_id", "generation",
                         "start_wait_ms", "cold_instance", "round_trip_ms", "worker_ms", "transport_ms") if key in facts})
            after = after or {}
            if call["status"] == "RETURN":
                call["initialization_before"] = facts.get("initialization_before") or after.get(
                    "last_initialization_before", call["initialization_before"])
            call["initialization_after"] = facts.get("initialization_after") or after.get("initialization")
            call["cold_start"] = call["initialization_before"] != "BOUND"
            call["binding_duration_ms"] = (facts.get("binding_duration_ms") if "binding_duration_ms" in facts else
                after.get("last_binding_duration_ms")) if call["status"] == "RETURN" else None

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


class RouteCatalog:
    """Task-facing view of a shared installed catalog, never a capability builder."""
    def __init__(self, catalog, unit_ids):
        self._catalog, self._ids = catalog, frozenset(unit_ids)
        self.facts = catalog.facts

    def _check(self, unit_ids):
        if set(unit_ids) - self._ids:
            raise ValueError("Unit is outside the task's frozen route")

    def public(self):
        return [unit for unit in self._catalog.public() if unit["id"] in self._ids]

    def business_units(self):
        return [unit for unit in self._catalog.business_units() if unit["id"] in self._ids]

    def tools(self):
        return [dict(type="function", function=dict(name=unit["tool_name"], description=unit["description"],
                    parameters=unit["parameters"])) for unit in self.business_units()]

    def name_to_id(self, name):
        found = [unit["id"] for unit in self.business_units() if unit["tool_name"] == name]
        if len(found) != 1:
            raise ValueError("route has no unique native tool with this name")
        return found[0]

    def unit(self, unit_id):
        self._check([unit_id])
        return self._catalog.unit(unit_id)

    def dispatch_status(self, unit_id):
        self._check([unit_id])
        return self._catalog.dispatch_status(unit_id)

    def runtime_state(self, unit_ids):
        self._check(unit_ids)
        return self._catalog.runtime_state(unit_ids)

    def validate_arguments(self, unit_id, arguments, native_arguments=(), deferred_arguments=()):
        self._check([unit_id])
        return self._catalog.validate_arguments(unit_id, arguments, native_arguments, deferred_arguments)

    def invoke(self, unit_id, arguments, native_arguments=()):
        self._check([unit_id])
        return self._catalog.invoke(unit_id, arguments, native_arguments)


class SceneSession:
    def __init__(self, spec, platform, run_id, profiles=None, catalog=None):
        self.spec, self.platform = spec, platform
        self.catalog = catalog if catalog is not None else FrozenCatalog(platform.catalog, spec.get("unit_ids", []))
        self.context = SceneContext(platform, run_id, profiles, self.catalog)
        self.environment, self.route, self.task = None, spec, None
        self.task_units = list(spec.get("unit_ids", []))
        self.last = None
        self._verify(spec)
        # Mixed factories need their route-restricted context and start at reset.
        if not spec.get("routes"):
            self._load(spec)

    @staticmethod
    def _verify(spec):
        for item in spec["adapter"]["files"]:
            if digest_file(item["path"]) != item["sha256"]:
                raise ValueError("场景适配源码已改变，请新建运行")
        for item in spec.get("resource_files", []) + ([spec["tasks_file"]] if spec.get("tasks_file") else []):
            if digest_file(item["path"]) != item["sha256"]:
                raise ValueError("场景模型／数据资源已改变，请新建运行")

    def _load(self, spec):
        self._verify(spec)
        adapter = spec["adapter"]
        name = "seampact_scene_" + digest_file(adapter["path"])
        module_spec = importlib.util.spec_from_file_location(name, adapter["path"])
        module = importlib.util.module_from_spec(module_spec)
        sys.modules[name] = module
        module_spec.loader.exec_module(module)
        self.context.model_roles = spec.get("model_roles", ["chat"])
        self.context.native_usage = copy.deepcopy(spec.get("native_usage", {}))
        self.environment = getattr(module, adapter["symbol"])(copy.deepcopy(adapter.get("config", {})), self.context)

    def _event(self, event):
        started = time.perf_counter()
        if not isinstance(event, dict) or event.get("kind") not in {"decision", "terminal"}:
            raise ValueError("场景必须返回 decision 或真正的 terminal 事件；等待由原环境处理")
        if self.spec.get("routes"):
            event.update(route=self.task["route"], task_class=self.task["task_class"], task_id=self.task["id"])
        if event["kind"] == "terminal":
            if type(event.get("terminated")) is not bool or type(event.get("truncated")) is not bool:
                raise ValueError("终止事件需要明确 terminated/truncated")
            if not (event["terminated"] or event["truncated"]):
                raise ValueError("非结束快照不能用作 terminal")
            event["metric_applicability"] = route_metric_applicability(self.spec, self.task or {}, event)
            self.last = event
            return event
        pool = set(self.task_units)
        if set(event.get("required_units", [])) - pool:
            raise ValueError("required_units contains Units outside the task route")
        # Reject foreign candidates before representation looks up their cards.
        if any(set(candidate.get("units", [])) - pool for candidate in event.get("candidates", [])):
            raise ValueError("候选包含当前任务路由以外的 Unit")
        if self.spec.get("representation"):
            represent(event, self.spec["representation"], self.catalog.runtime_state)
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
        for candidate in candidates:
            _vector(candidate.get("features"), len(self.spec["candidate_schema"]), "候选特征")
            units = set(candidate.get("units", []))
            if units - pool:
                raise ValueError("候选包含冻结能力池以外的 Unit")
            if type(candidate.get("legal")) is not bool:
                raise ValueError("候选需显式给出 legal")
            # Environment obligations cannot be traded against a reward.
            groups = candidate.get("exclusive_groups", [])
            semantic = candidate["legal"] and required <= units and len(groups) == len(set(groups))
            # Semantic legality is the task/rule judgement; dispatchability is the current runtime fact.
            dispatch = {uid: self.catalog.dispatch_status(uid) for uid in sorted(units)}
            dispatchable = all(item["status"] != "UNAVAILABLE" for item in dispatch.values())
            candidate.update(semantic_legal=semantic, dispatch=dispatch, dispatchable=dispatchable,
                             legal=semantic and dispatchable)
        if not any(c["legal"] for c in candidates):
            reasons = {c["id"]: "语义不合法" if not c["semantic_legal"] else
                       {uid: item.get("reason") for uid, item in c["dispatch"].items() if item["status"] == "UNAVAILABLE"}
                       for c in candidates}
            raise ValueError("当前无可执行且满足必需工作的候选：" + json.dumps(reasons, ensure_ascii=False))
        self.last = event
        event["representation_duration_ms"] = (time.perf_counter() - started) * 1000
        return event

    def reset(self, task, seed):
        self.last = None
        self.context.allowed.clear()
        self.context.calls.clear()
        self.context.model_calls.clear()
        self.context.wire_reasoning.clear()
        self.context.upstream_costs = copy.deepcopy(task.get("upstream_costs", {}))
        if self.spec.get("routes"):
            route = task_route(self.spec, task)
            self.close()
            self.environment = None
            self.task, self.route = copy.deepcopy(task), route
            self.task_units = task_unit_ids(self.spec, task)
            self.context.catalog = RouteCatalog(self.catalog, self.task_units)
            self._verify(self.spec)
            self._load(route)
        else:
            self.task = copy.deepcopy(task)
            self.task_units = task_unit_ids(self.spec, task)
            self.context.catalog = RouteCatalog(self.catalog, self.task_units)
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
