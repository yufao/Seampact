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
import uuid
import sys
import time
from pathlib import Path

from seampact.capabilities import VERSION_2, freeze_representation, represent
from seampact.runtime import _task_context


def digest_file(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def _freeze_research_binding(value):
    if "research_protocol" not in value:
        if "research_data" in value:
            raise ValueError("research_data requires an explicit research_protocol")
        return None
    if value["research_protocol"] != "multisource-review/1":
        raise ValueError("unknown research_protocol")
    binding = value.get("research_data")
    if not isinstance(binding, dict) or binding.get("schema") != "research-data-binding/1":
        raise ValueError("multisource-review/1 requires research-data-binding/1")
    path, expected = binding.get("manifest_path"), binding.get("manifest_sha256")
    if not isinstance(path, str) or not path.strip() or path != path.strip():
        raise ValueError("research data binding requires a manifest path")
    if not isinstance(expected, str) or len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected):
        raise ValueError("research data binding requires a lowercase sha256")
    path = Path(path).resolve()
    if digest_file(path) != expected:
        raise ValueError("research data manifest sha256 mismatch")
    value["research_data"] = dict(schema="research-data-binding/1", manifest_path=str(path), manifest_sha256=expected)
    return dict(path=str(path), sha256=expected)


def freeze_scene(spec, capabilities=None):
    value = copy.deepcopy(spec)
    if not value.get("id") or not value.get("version"):
        raise ValueError("场景需要 id 与 version")
    if "research_protocol" in value and "research_task_ids" in value:
        raise ValueError("research_task_ids are set only by run validation")
    research_resource = _freeze_research_binding(value)
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
    if research_resource:
        resources.append(research_resource)
    value["resource_files"] = resources
    return value


def _freeze_mixed_scene(value, capabilities):
    """Freeze task routes inside the existing arrival/resource window.

    Routes are ordinary scene adapters, with their own installed Unit subset and
    criteria. The union uses one capability-features/3 schema; route names and
    task classes are result metadata, never learned features.
    """
    routes = value.get("routes")
    if not isinstance(routes, dict) or not routes or any(
            not isinstance(key, str) or not key or not isinstance(route, dict)
            for key, route in routes.items()):
        raise ValueError("mixed routes must be named scene objects")
    if any("research_data" in route or "research_protocol" in route for route in routes.values()):
        raise ValueError("research data binding belongs to the shared mixed scene")
    if (value.get("representation") or {}).get("version") != VERSION_2:
        raise ValueError("mixed routes require the shared capability-features/3 representation")
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
                from seampact.evaluation import validate_criteria
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
            if unit_ids is not None:
                selected = list(dict.fromkeys(unit_ids))
            else:
                # A source may be locally deferred but cold-startable on its
                # checked host. It still needs an actual deferred binding and
                # passes the same host/start-gap checks below.
                selected = []
                deferred = getattr(catalog, "deferred", {})
                for uid, unit in current.items():
                    policy = unit.get("scheduling") or {}
                    cold = (uid in deferred and (unit.get("dispatch") or {}).get("status") in {"COLD_STARTABLE", "READY_INSTANCE"}
                            and ((unit.get("operations") or {}).get("group_host") or {}).get("status") == "HOST_CHECKED")
                    if (policy.get("visibility") == "business" and policy.get("activation") == "business"
                            and (unit.get("status") == "READY" or cold)):
                        selected.append(uid)
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
                if self.router.backend().get("kind") == "docker":
                    allocation = self.router.gpu_allocation(group["id"], probe=True)
                    self.placement[uid]["gpu_devices"] = allocation["devices"]
                    self.router.check_gpu_placement(self.placement[uid])
            remote = {uid for uid, value in self.placement.items() if value["kind"] == "REMOTE"}
            self.frozen = catalog.freeze_resources(selected, remote=remote)
            self.units = {uid: copy.deepcopy(current[uid]) for uid in selected}
            # Freeze the same checked operation evidence that will be consumed
            # by dispatch and execution; readiness is checked separately below.
            from seampact.typed_contract import require_operation
            self.contract_digests = {}
            for uid in selected:
                stored = catalog.store.get("unit", uid) or {}
                group = catalog.store.get("runtime_group", stored.get("runtime_group") or "")
                result = require_operation(stored, "dispatch", group=group)
                if uid in remote:
                    require_operation(stored, "remote", group=group)
                self.contract_digests[uid] = result["contract_digest"]
            for uid in remote:
                for value in (self.units[uid], self.frozen[uid][2]):
                    value["execution"] = "REMOTE"
                    if value.get("status") != "CALL_CONDITION_DEFERRED":
                        value["status"] = "READY"
            self.resources = catalog.snapshot_resources(selected, frozen=self.frozen)
            self.service_placements, self.service_dependencies, self.shared_services = {}, {}, []
            self.role_services = {}
            self.control = {}
            for record in self.resources.get("units", []):
                uid = record["unit_id"]
                entry = self.frozen[uid][1]
                from seampact.configuration.dependencies import entry_uses_dependency
                rows = {r["id"]: r for r in record["requirements"]}
                dependencies = set()
                for identity, selection in record["bindings"].items():
                    row = rows.get(identity, {})
                    if row.get("required", True) is False and not entry_uses_dependency(entry, identity):
                        continue
                    connection = selection.get("connection") or {}
                    dependencies.update(self._freeze_service(connection))
                self.placement[uid]["resource_dependencies"] = sorted(dependencies)

    def _freeze_service(self, connection):
        if connection.get("resource_kind") != "managed":
            return []
        manager = getattr(getattr(self.router, "platform", None), "managed_services", None)
        if manager is None:
            raise ValueError("受管连接缺少服务执行器")
        manager.validate_connection(connection)
        visited = set()
        def visit(identity):
            placement = manager.placement(identity)
            group_id = placement["group_id"]
            if group_id in visited:
                return
            visited.add(group_id)
            self.service_placements[group_id] = placement
            spec = manager.get(identity)
            dependencies = [manager.placement(dep)["group_id"] for dep in spec.get("depends_on", [])]
            self.service_dependencies[group_id] = dependencies
            for dependency in dependencies:
                visit(dependency)
        visit(connection["managed_service_id"])
        return sorted(visited)

    def add_role_services(self, profiles, roles):
        for role in sorted(set(roles)):
            groups = self._freeze_service(profiles.role_profile(role))
            self.role_services[role] = groups
            self.shared_services = sorted(set(self.shared_services) | set(groups))

    def services_for_roles(self, roles):
        return sorted({group for role in roles for group in self.role_services.get(role, [])})

    def service_snapshot(self):
        return dict(schema="managed-service-bindings/1", allocations=copy.deepcopy(self.service_placements),
                    dependencies=copy.deepcopy(self.service_dependencies), shared_services=list(self.shared_services),
                    role_services=copy.deepcopy(self.role_services))

    def bind_run(self, run_id, control):
        self.control = dict(control)
        for value in [*self.placement.values(), *self.service_placements.values()]:
            value.update(run_id=run_id, start_timeout_s=control["start_timeout_s"])

    def remote_groups(self):
        return {**{value["group_id"]: value for value in self.placement.values() if value["kind"] == "REMOTE"},
                **self.service_placements}

    def public(self):
        return copy.deepcopy(list(self.units.values()))

    def require_operation(self, unit_id, operation):
        """Recheck current evidence against the identity frozen for this run."""
        if unit_id not in self.frozen:
            raise ValueError("Unit 不在本次冻结能力池内")
        with self.catalog.lock:
            current = self.catalog.store.get("unit", unit_id) or {}
            binding, entry, _ = self.frozen[unit_id]
            result = self.catalog._verify_operation(binding, current, operation=operation)
            candidate_entries = [item for item in binding.candidate.get("entries", [])
                                 if isinstance(item, dict) and item.get("id") == current.get("entry_id")]
            if len(candidate_entries) != 1 or candidate_entries[0] != entry:
                raise ValueError("冻结入口与已检查的绑定配置不一致")
            if result["contract_digest"] != self.contract_digests[unit_id]:
                raise ValueError("Unit 操作契约已改变；请重新冻结运行")
            return result

    def require_boundary(self, unit_id, call, root, gate):
        """Check the registered gate's finite scope under this frozen root."""
        from seampact.typed_contract import require_boundary
        self.require_operation(unit_id, "dispatch")
        with self.catalog.lock:
            unit = self.catalog.store.get("unit", unit_id) or {}
            group = self.catalog.store.get("runtime_group", unit.get("runtime_group") or "")
            binding, entry, _ = self.frozen[unit_id]
            result = require_boundary(unit, group=group, binding=binding, entry=entry,
                                      call=call, root=root, gate=gate)
            if result["contract_digest"] != self.contract_digests[unit_id]:
                raise ValueError("边界所属 Unit 契约已改变；请重新冻结运行")
            return result

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

    def runtime_observer(self):
        # One event's descriptive facts only. Later decisions and invocation
        # use the ordinary fresh methods; no permission is retained here.
        facts = {}
        return (lambda units: self.runtime_state(units, _facts=facts),
                lambda unit: self.dispatch_status(unit, _facts=facts))

    def _runtime_fact(self, group, unit, image, facts):
        if facts is None:
            return self.router.runtime_fact(group, unit, image)
        key = (group, unit, image)
        if key not in facts:
            facts[key] = self.router.runtime_fact(group, unit, image)
        return facts[key]

    def dispatch_status(self, unit_id, *, _facts=None):
        try:
            self.require_operation(unit_id, "dispatch")
        except ValueError as error:
            return dict(status="UNAVAILABLE", reason=str(error))
        placement = self.placement[unit_id]
        condition = self.units[unit_id].get("status") == "CALL_CONDITION_DEFERRED"
        def status(value, **facts):
            if condition:
                facts["call_condition"] = "REQUIRES_ACTUAL_VALUES"
            return dict(status=value, **facts)
        for group in placement.get("resource_dependencies", []):
            fact = self._runtime_fact(group, None, self.service_placements[group].get("image"), _facts)
            if not fact["ready_instance"]:
                return status("COLD_STARTABLE" if fact["cold_startable"] else "UNAVAILABLE",
                              reason=fact.get("reason") or "依赖模型尚未就绪")
        if placement["kind"] == "LOCAL":
            return status("READY_LOCAL")
        fact = self._runtime_fact(placement["group_id"], unit_id, placement.get("image"), _facts)
        if fact["ready_instance"]:
            return status("READY_INSTANCE")
        if fact["cold_startable"]:
            return status("COLD_STARTABLE")
        return status("UNAVAILABLE", reason=fact.get("reason") or "实例结果未决或不可启动")

    def runtime_state(self, unit_ids, *, _facts=None):
        facts = []
        for uid in unit_ids:
            placement = self.placement[uid]
            if placement["kind"] == "REMOTE":
                facts.append(self._runtime_fact(placement["group_id"], uid, placement.get("image"), _facts))
                continue
            binding, entry, unit = self.frozen[uid]
            group = unit.get("runtime_group")
            facts.append(dict(local=True, ready_instance=True, warm=binding.initialization_state(entry, unit) == "BOUND" and entry.get("kind") != "cli",
                              cold_startable=False, accepting=True, busy=binding.active_calls > 0,
                              queue=self.router.queue(group) if self.router and group else 0, state_age_ms=0.0))
        for group in sorted({group for uid in unit_ids for group in self.placement[uid].get("resource_dependencies", [])}):
            facts.append(self._runtime_fact(group, None, self.service_placements[group].get("image"), _facts))
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

    def check_call_conditions(self, unit_id, arguments, native_arguments=(), deferred_arguments=(), *, instance_identity=None):
        if unit_id not in self.frozen:
            raise ValueError("Unit 不在本次冻结能力池内")
        if self.placement[unit_id]["kind"] != "REMOTE" and self.frozen[unit_id][2].get("call_condition"):
            return dict(schema="per-call-condition-evidence/1", status="UNKNOWN", allowed=False,
                        reason="conditional call requires a group host")
        result = self.catalog.check_call_conditions(unit_id, arguments, native_arguments, deferred_arguments,
            frozen=self.frozen[unit_id], instance_identity=instance_identity)
        if self.placement[unit_id]["kind"] != "REMOTE" and result["status"] != "NOT_REQUIRED":
            return dict(result, status="UNKNOWN", allowed=False, reason="conditional call requires a group host")
        return result

    def require_call_conditions(self, unit_id, arguments, native_arguments=(), deferred_arguments=(), *, instance_identity=None):
        result = self.check_call_conditions(unit_id, arguments, native_arguments, deferred_arguments,
                                            instance_identity=instance_identity)
        if not result["allowed"]:
            raise ValueError("调用条件未满足：" + str(result.get("reason")))
        return result

    def require_call_condition_structure(self, unit_id):
        placement = self.placement.get(unit_id) or {}
        if placement.get("kind") != "REMOTE" or unit_id not in self.frozen:
            raise ValueError("条件成员只能冻结为已核对的远程组")
        result = self.catalog.require_call_condition_structure(unit_id, frozen=self.frozen[unit_id])
        if (result.get("status") != "STRUCTURE_CHECKED" or
                result.get("call_permission") != "REQUIRES_ACTUAL_CALL" or
                result.get("runtime_group") != placement.get("group_id") or
                result.get("contract_digest") != self.contract_digests.get(unit_id)):
            raise ValueError("条件成员的冻结组或契约身份已改变")
        self.require_operation(unit_id, "dispatch")
        self.require_operation(unit_id, "remote")
        group = self.catalog.store.get("runtime_group", placement["group_id"]) or {}
        if (group.get("contract") or {}).get("digest") != placement.get("contract_digest"):
            raise ValueError("条件成员的运行组契约已改变")
        artifact = self.catalog.store.get("deployment", placement["group_id"]) or {}
        if ((artifact.get("build") or {}).get("image_id") != placement.get("image_id") or
                artifact.get("binding_fingerprint") != placement.get("binding_fingerprint")):
            raise ValueError("条件成员的冻结镜像或资源绑定身份已改变")
        gap = self.router.start_gap(placement["group_id"], placement.get("image"))
        if gap:
            raise ValueError("条件成员的宿主工件身份未满足：" + str(gap))
        return result

    def require_import_only_plan(self, unit_id, proposal=None):
        self.require_call_condition_structure(unit_id)
        self.require_operation(unit_id, "prepare")
        if proposal is not None:
            return self.catalog.validate_worker_import_plan(unit_id, proposal, frozen=self.frozen[unit_id])
        return self.catalog.require_import_only_plan(unit_id, frozen=self.frozen[unit_id])

    def snapshot_resources(self):
        return copy.deepcopy(self.resources)

    def invoke(self, unit_id, arguments, native_arguments=()):
        self.require_operation(unit_id, "invoke")
        self.require_call_conditions(unit_id, arguments, native_arguments)
        groups = self.placement[unit_id].get("resource_dependencies", [])
        if not groups:
            return self._invoke(unit_id, arguments, native_arguments)
        manager = self.router.platform.managed_services
        owner = _task_context.get() or "call:" + uuid.uuid4().hex
        try:
            for group_id in groups:
                if self.control.get("mode") != "joint/1":
                    self.router.ensure_ready(self.service_placements[group_id], None)
                manager.acquire(group_id, owner, self.service_placements[group_id].get("run_id"))
            # A call lease is distinct from the enclosing task's residency lease.
            with manager.request_scope(groups, owner, run_id=self.service_placements[groups[0]].get("run_id")):
                return self._invoke(unit_id, arguments, native_arguments)
        finally:
            # Task leases are released by task/evaluation completion, not here.
            if not _task_context.get():
                manager.release_lease(owner)

    def _invoke(self, unit_id, arguments, native_arguments=()):
        # Concurrent tasks of one window share this catalog; facts belong to the calling task.
        self.require_operation(unit_id, "invoke")
        key = (_task_context.get(), unit_id)
        placement = self.placement.get(unit_id, dict(kind="LOCAL"))
        if placement["kind"] == "LOCAL":
            self.facts[key] = dict(execution="LOCAL")
            return self.catalog.invoke_frozen(self.frozen, unit_id, arguments, native_arguments)
        self.require_operation(unit_id, "remote")
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
        self.call_arguments = None
        self.clear_failure_evidence()

    def clear_failure_evidence(self):
        self.failure_evidence, self.failure_output_artifacts, self.raw_results = {}, [], []
        self.failure_evaluation_snapshot = self.active_failure_terminal = None
        self.evidence_blocked, self.evaluation_error = False, None
        self.failure_capture_gaps = []

    def record_failure_evidence(self, evidence, output_artifacts=()):
        """Keep actual scene values for passive capture if continuation fails."""
        self.failure_evidence = self._failure_value(evidence)
        self.failure_output_artifacts = self._failure_value(output_artifacts)

    def _failure_value(self, value, active=None):
        from seampact.scenes.evaluation_evidence import passive_value, PassiveEvidenceGap
        try:
            return passive_value(value)
        except PassiveEvidenceGap as error:
            # A later mutation cannot turn a previously unsupported actual
            # return into proof that its owner can be released.
            self.failure_capture_gaps.append(str(error))
            active = set() if active is None else active
            kind = type(value)
            if any(kind is candidate for candidate in (dict, list, tuple)) and id(value) not in active:
                active.add(id(value))
                try:
                    if kind is dict and all(type(key) is str for key in value):
                        return {key: self._failure_value(item, active) for key, item in value.items()}
                    if kind is list or kind is tuple:
                        return [self._failure_value(item, active) for item in value]
                finally:
                    active.remove(id(value))
            # Unsupported leaves stay as exact live references, never serialized.
            return value

    def freeze_failure_evidence(self, *, criteria=(), destination=None, interrupted_event=None,
                                reason="execution_error"):
        from seampact.scenes.evaluation_evidence import passive_evaluation_snapshot, PassiveEvidenceGap
        # Raw returns are retained before any adapter projection/artifact work.
        # Unsupported leaves must block capture, never disappear with the owner.
        terminal = dict(kind="terminal", terminated=False, truncated=True, reason=reason,
            evidence=dict(scene=self.failure_evidence, actual_unit_results=self.raw_results),
            output_artifacts=self.failure_output_artifacts, interrupted_event=interrupted_event)
        # Preserve the scene's existing evidence fields for downstream evaluation.
        if type(self.failure_evidence) is dict:
            terminal["evidence"] = dict(self.failure_evidence, actual_unit_results=self.raw_results)
        try:
            if self.failure_capture_gaps:
                raise PassiveEvidenceGap(self.failure_capture_gaps[0])
            snapshot = passive_evaluation_snapshot(terminal, self.calls, self.model_calls,
                criteria=criteria, destination=destination)
        except PassiveEvidenceGap as error:
            self.active_failure_terminal, self.evidence_blocked = terminal, True
            self.evaluation_error = dict(type="PassiveEvidenceGap", message=str(error))
            raise
        self.failure_evaluation_snapshot = snapshot
        return snapshot

    def invoke(self, unit_id, arguments, native_arguments=()):
        if unit_id not in self.allowed:
            raise ValueError("场景尝试调用本次动作之外的 Unit")
        started = time.perf_counter()
        call = dict(unit_id=unit_id, status="RUNNING", initialization_before=None)
        self.calls.append(call)
        raw = dict(call_index=len(self.calls) - 1, unit_id=unit_id, status="RUNNING",
                   arguments=self._failure_value(arguments), native_arguments=self._failure_value(native_arguments))
        self.raw_results.append(raw)
        facts, after = dict(execution="LOCAL"), None
        try:
            self.call_arguments = (unit_id, arguments, native_arguments)
            with self.platform.runtime.context(self.run_id), self.admission(unit_id) as root:
                if root is not None:
                    # Queueing before the grant is task time, kept in root.wait_ms, not module duration.
                    call["root"], started = root, time.perf_counter()
                # Read binding state while this request holds its host, not behind another task's call.
                call["initialization_before"] = self.catalog.unit(unit_id).get("initialization")
                try:
                    result = self.catalog.invoke(unit_id, arguments, native_arguments)
                    raw.update(status="RETURN", result=self._failure_value(result))
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
            raw.update(status="ERROR", error=dict(type=type(error).__name__, message=str(error)))
            raise
        finally:
            self.call_arguments = None
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
        from seampact.providers import Transport, completion_body, model_request_scope
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
        raw = None
        try:
            body = completion_body(profile, messages, tools)
            from seampact.scenes.evaluation_evidence import passive_value
            # This is the sender's actual input, without authentication headers.
            # Keep it for successful runs as well as the failure-only snapshot.
            call["request"] = passive_value(body)
            call["request_provenance"] = "transport_input_before_send"
            raw = dict(model_call_index=len(self.model_calls) - 1, role=role, status="RUNNING",
                       request=self._failure_value(body))
            self.raw_results.append(raw)
            with model_request_scope(self.profiles, role):
                result = Transport().send(profile["base_url"] + "/chat/completions",
                    self.profiles.role_key(role), body)
            raw.update(status="RETURN", result=self._failure_value(result))
            call["usage"] = result.get("usage")
            for choice in result.get("choices", []):
                message = choice.get("message", {})
                if "reasoning_content" in message:
                    for tool in message.get("tool_calls") or []:
                        self.wire_reasoning[tool["id"]] = message["reasoning_content"]
            return result
        except Exception as error:
            if raw is not None:
                raw.update(status="ERROR", error=dict(type=type(error).__name__, message=str(error)))
            raise
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

    def check_call_conditions(self, unit_id, arguments, native_arguments=(), deferred_arguments=(), *, instance_identity=None):
        self._check([unit_id])
        return self._catalog.check_call_conditions(unit_id, arguments, native_arguments, deferred_arguments,
                                                   instance_identity=instance_identity)

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

    def _call_condition_checks(self, candidate):
        preview = getattr(self.environment, "preview_calls", None)
        calls = preview(candidate) if preview else []
        units = set(candidate.get("units", []))
        if any(call.get("unit_id") not in units for call in calls):
            raise ValueError("调用预览超出候选的冻结 Unit")
        covered = {call["unit_id"] for call in calls}
        calls = list(calls) + [dict(unit_id=unit, arguments=None) for unit in sorted(units - covered)]
        return [(call["unit_id"], self.context.catalog.check_call_conditions(call["unit_id"], call.get("arguments")))
                for call in calls]

    def _call_conditions(self, candidate):
        return [result for _, result in self._call_condition_checks(candidate)]

    def candidate_intent(self, candidate):
        """Describe a checked call intent; this never grants invocation permission."""
        denied = dict(allowed=False, groups=[], signatures=[], imports=[])
        if not candidate["legal"] or any(self.context.catalog.dispatch_status(unit)["status"] == "UNAVAILABLE"
                                         for unit in candidate.get("units", [])):
            return denied
        groups, signatures, imports = set(), [], []
        for unit, result in self._call_condition_checks(candidate):
            if result.get("status") != "NOT_REQUIRED":
                signatures.append((unit, result.get("recipe_digest"), result.get("payload_digest")))
            if result.get("allowed"):
                continue
            reason = result.get("reason_code")
            if reason not in {"NO_WORKER", "DEFAULT_BINDING_UNKNOWN", "GENERATOR_BINDING_UNKNOWN", "REGISTRATION_BINDING_UNKNOWN", "SDK_BINDING_UNKNOWN"} or not result.get("recipe_digest") or not result.get("payload_digest"):
                return denied
            placement = self.catalog.placement.get(unit) or {}
            if placement.get("kind") != "REMOTE":
                return denied
            try:
                self.catalog.require_call_condition_structure(unit)
                if reason in {"DEFAULT_BINDING_UNKNOWN", "GENERATOR_BINDING_UNKNOWN", "REGISTRATION_BINDING_UNKNOWN", "SDK_BINDING_UNKNOWN"}:
                    identity = result.get("instance_identity")
                    if type(identity) is not dict or not identity.get("instance_id"):
                        return denied
                    if reason == 'SDK_BINDING_UNKNOWN':
                        if type(result.get('import_plan')) is not dict:
                            return denied
                        plan = self.catalog.require_import_only_plan(unit, proposal=result['import_plan'])
                    else:
                        plan = self.catalog.require_import_only_plan(unit)
                    if plan.get("status") != "CHECKED" or not plan.get("plan_digest"):
                        return denied
                    request = dict(unit=unit, group_id=placement["group_id"], instance_identity=identity,
                                   plan_digest=plan["plan_digest"])
                    if request not in imports:
                        imports.append(request)
            except ValueError:
                return denied
            if reason == "NO_WORKER":
                groups.add(placement["group_id"])
        return dict(allowed=True, groups=sorted(groups), signatures=signatures, imports=imports)

    def candidate_legal(self, candidate, *, preview=True):
        if not candidate["legal"]:
            return False
        if any(self.context.catalog.dispatch_status(unit)["status"] == "UNAVAILABLE"
               for unit in candidate.get("units", [])):
            return False
        conditions = (self._call_conditions(candidate) if preview else
                      [self.context.catalog.check_call_conditions(unit, None) for unit in candidate.get("units", [])])
        return all(item["allowed"] for item in conditions)

    def decision_mask(self, event, *, preview=True):
        mask = [self.candidate_legal(candidate, preview=preview) for candidate in event["candidates"]]
        for candidate, allowed in zip(event["candidates"], mask):
            candidate["decision_legal"] = allowed
        return mask

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
        # Share descriptive runtime facts only within this candidate event.
        # Conditions below and all subsequent action checks remain fresh.
        observer = getattr(self.catalog, "runtime_observer", None)
        runtime_state, dispatch_status = (observer() if observer else
                                         (getattr(self.catalog, "runtime_state", None), self.catalog.dispatch_status))
        represent_ms = 0.0
        if self.spec.get("representation"):
            represent_started = time.perf_counter()
            represent(event, self.spec["representation"], runtime_state)
            represent_ms = (time.perf_counter() - represent_started) * 1000
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
        dispatch_conditions_ms = 0.0
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
            dispatch_started = time.perf_counter()
            dispatch = {uid: dispatch_status(uid) for uid in sorted(units)}
            dispatchable = all(item["status"] != "UNAVAILABLE" for item in dispatch.values())
            conditions = self._call_conditions(candidate)
            dispatch_conditions_ms += (time.perf_counter() - dispatch_started) * 1000
            candidate.update(semantic_legal=semantic, dispatch=dispatch, dispatchable=dispatchable,
                             call_conditions=[dict(status=item["status"], reason=item.get("reason"),
                                                   reason_code=item.get("reason_code")) for item in conditions],
                             legal=semantic,
                             decision_legal=semantic and dispatchable and all(item["allowed"] for item in conditions))
        if not any(c["legal"] for c in candidates):
            reasons = {c["id"]: "语义不合法" if not c["semantic_legal"] else dict(
                       dispatch={uid: item.get("reason") for uid, item in c["dispatch"].items() if item["status"] == "UNAVAILABLE"},
                       call_conditions=c["call_conditions"])
                       for c in candidates}
            raise ValueError("当前无可执行且满足必需工作的候选：" + json.dumps(reasons, ensure_ascii=False))
        self.last = event
        event["represent_duration_ms"] = represent_ms
        event["dispatch_conditions_duration_ms"] = dispatch_conditions_ms
        event["representation_duration_ms"] = (time.perf_counter() - started) * 1000
        return event

    def reset(self, task, seed):
        if self.context.evidence_blocked:
            raise ValueError("场景失败证据仍依赖原owner，不能重置并清除实际材料")
        if self.spec.get("research_protocol") == "multisource-review/1":
            selected = self.spec.get("research_task_ids")
            if not isinstance(selected, list) or task.get("id") not in selected:
                raise ValueError("research task is outside the validated run selection")
            frozen = next((item for item in self.spec["tasks"] if item["id"] == task.get("id")), None)
            if frozen is None or frozen != task:
                raise ValueError("research task does not match the frozen task")
            if not self.spec.get("routes"):
                self._verify(self.spec)
        self.last = None
        self.context.allowed.clear()
        self.context.calls.clear()
        self.context.model_calls.clear()
        self.context.clear_failure_evidence()
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
        if not self.candidate_legal(candidate):
            raise ValueError("动作的逐次调用条件已不再满足")
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
        if self.context.evidence_blocked:
            return
        close = getattr(self.environment, "close", None)
        if close:
            close()


def arrays(event):
    return dict(state=event["observation"], candidates=[c["features"] for c in event["candidates"]],
                legal=[c.get("decision_legal", c["legal"]) for c in event["candidates"]])
