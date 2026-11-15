"""Project choices for future tasks, separate from installed worker lifecycles."""

import copy

from seampact.capability_contract import digest, tool_definition
from seampact.guard_contract import GUARD_INTERFACES, _source_backed, applicable_subject, compatible
from seampact.office_policy import normalize_policy
from seampact.store import now


def _declared_operation(unit):
    declared = unit.get("capability", {}).get("operation")
    return copy.deepcopy(declared) if _source_backed(declared) else {"kind": "other"}


def belongs_to_project(catalog, unit, project_id):
    group = catalog._group(unit["runtime_group"])
    owners = {binding["project_id"] for binding in group.get("bindings", {}).values()
              if binding.get("form") == "project"}
    if group.get("project_owner"):
        owners.add(group["project_owner"])
    return not owners or owners == {project_id}


def application_config(units, candidates, guard_policy=None, *, mode="scheduler"):
    """Freeze source-backed guard and business-operation metadata for hooks."""
    indexed = {unit["id"]: unit for unit in units}
    frozen = []
    seen = set()
    for item in candidates:
        role, identity = item["role"], item["unit_id"]
        if (role, identity) in seen:
            raise ValueError("办公检查候选重复：" + role)
        seen.add((role, identity))
        unit = indexed.get(identity)
        if unit is None or not compatible(role, unit):
            raise ValueError("办公检查能力缺失或接口不匹配：" + role)
        frozen.append(dict(role=role, unit_id=identity, guard=copy.deepcopy(unit["capability"]["guard"])))
    policy = normalize_policy(guard_policy)
    _check_required(policy, frozen)
    declared = {unit["tool_name"]: _declared_operation(unit)
                for unit in units if unit.get("tool_name") and not unit.get("capability", {}).get("guard")}
    return dict(mode=mode, guard_candidates=sorted(frozen, key=lambda item: (item["role"], item["unit_id"])),
                operations=declared, guard_policy=policy)


def _check_required(policy, candidates):
    for role in policy["required_outbound"]:
        matches = [item for item in candidates if item["role"] == role and
                   applicable_subject(role, "before_tool", item["guard"])]
        if not matches:
            raise ValueError("外发必需检查不可用或不支持检查外发消息：" + role)
        if len(matches) > 1:
            raise ValueError("外发必需检查有多个可用能力，请明确绑定：" + role)


class TaskCapabilityCatalog:
    """Pin tool names and installed identities for one admitted task."""

    def __init__(self, catalog, units):
        self.catalog = catalog
        self.units = copy.deepcopy(units)
        self.names = {unit["tool_name"]: unit["id"] for unit in units}
        if len(self.names) != len(units):
            raise ValueError("项目能力工具名称重复")

    def __getattr__(self, name):
        return getattr(self.catalog, name)

    def tools(self):
        return [tool_definition(unit["tool_name"], unit["capability"]) for unit in self.units]

    def tools_for_project(self, project_id):
        return self.tools()

    def name_to_id(self, name):
        if name not in self.names:
            raise ValueError("本任务未启用这个能力：" + str(name))
        return self.names[name]


class ProjectApplications:
    def __init__(self, platform):
        self.platform = platform
        self.store, self.projects, self.catalog = platform.store, platform.projects, platform.capabilities

    def configuration(self, project_id):
        self.projects.get(project_id)
        return self.store.get("project_application", project_id) or dict(
            schema="project-application/1", project_id=project_id, revision=0,
            enabled_groups=None, bindings={}, guard_policy=normalize_policy())

    def snapshot(self, project_id, units):
        config = copy.deepcopy(self.configuration(project_id))
        enabled = config["enabled_groups"]
        scoped = [unit for unit in units if belongs_to_project(self.catalog, unit, project_id)]
        selected = [unit for unit in scoped if enabled is None or unit["runtime_group"] in enabled]
        eligible = [unit for unit in selected if unit.get("availability", {}).get("status") == "AVAILABLE"
                    and unit.get("dispatch", {}).get("status") in {"READY_INSTANCE", "COLD_STARTABLE"}]
        current, admitted = {unit["id"]: unit for unit in units}, {unit["id"] for unit in eligible}
        binding_status, candidates = [], []
        for role, identity in config["bindings"].items():
            unit = current.get(identity)
            status = ("DISABLED" if identity is None else "MISSING" if unit is None else "OTHER_PROJECT" if unit not in scoped else
                      "INCOMPATIBLE" if not compatible(role, unit) else
                      "DISABLED" if unit not in selected else "UNAVAILABLE" if identity not in admitted else "ENABLED")
            binding_status.append(dict(role=role, unit_id=identity, status=status))
            if status == "ENABLED":
                candidates.append(dict(role=role, unit_id=identity,
                                       guard=copy.deepcopy(unit["capability"]["guard"])))
        # A configured role selects one provider or disables the role.
        # Otherwise every matching provider competes as its own candidate.
        for role in GUARD_INTERFACES:
            if role in config["bindings"]:
                continue
            matches = [unit for unit in eligible if unit.get("capability", {}).get("guard") and compatible(role, unit)]
            candidates.extend(dict(role=role, unit_id=unit["id"],
                                   guard=copy.deepcopy(unit["capability"]["guard"])) for unit in matches)
            if len(matches) == 1:
                binding_status.append(dict(role=role, unit_id=matches[0]["id"], status="ENABLED", source="automatic"))
        declared = {unit["tool_name"]: _declared_operation(unit)
                    for unit in eligible if unit.get("tool_name") and not unit.get("capability", {}).get("guard")}
        config.update(source="project", binding_status=binding_status,
                      guard_policy=normalize_policy(config.get("guard_policy")),
                      guard_candidates=sorted(candidates, key=lambda item: (item["role"], item["unit_id"])), operations=declared,
                      eligible_capability_ids=sorted(admitted),
                      enabled_group_ids=sorted({unit["runtime_group"] for unit in selected}))
        config["digest"] = digest(config)
        return config, eligible

    def view(self, project_id, units=None):
        units = self.catalog.public() if units is None else units
        snapshot, eligible = self.snapshot(project_id, units)
        libraries = []
        for group_id in sorted({unit["runtime_group"] for unit in units}):
            members = [unit for unit in units if unit["runtime_group"] == group_id]
            permitted = all(belongs_to_project(self.catalog, unit, project_id) for unit in members)
            enabled = permitted and (snapshot["enabled_groups"] is None or group_id in snapshot["enabled_groups"])
            package = members[0]["package"]
            name = package.get("name", group_id) if isinstance(package, dict) else package
            libraries.append(dict(group_id=group_id, name=name, enabled=enabled,
                                  project_eligible=permitted, capability_count=len(members),
                                  callable_count=sum(unit["id"] in snapshot["eligible_capability_ids"] for unit in members),
                                  availability=members[0].get("availability")))
        return dict(application=snapshot, libraries=libraries,
                    applies_to="future_tasks", runtime_actions=False)

    def configure(self, project_id, body):
        if set(body) - {"project_id", "enabled_groups", "bindings", "guard_policy"}:
            raise ValueError("项目能力配置只接收启用库和办公应用绑定")
        # Availability can wait for the controller; read before project/store locking.
        units = self.catalog.public()
        with self.projects._locked():
            self.projects._available(project_id)
            config = copy.deepcopy(self.configuration(project_id))
            if "enabled_groups" in body:
                groups = body["enabled_groups"]
                if (groups is not None and (not isinstance(groups, list) or
                        any(not isinstance(group, str) for group in groups) or len(set(groups)) != len(groups))):
                    raise ValueError("启用库须为不重复的运行组列表；空列表停用全部库")
                scoped = {unit["runtime_group"] for unit in units if belongs_to_project(self.catalog, unit, project_id)}
                if groups is not None and set(groups) - scoped:
                    raise ValueError("不能启用未安装或属于其他项目的库")
                config["enabled_groups"] = None if groups is None else sorted(groups)
            bindings = body.get("bindings", config["bindings"])
            if not isinstance(bindings, dict) or set(bindings) - set(GUARD_INTERFACES):
                raise ValueError("办公应用绑定角色无效")
            indexed = {unit["id"]: unit for unit in units}
            for role, identity in bindings.items():
                if identity is None:
                    continue
                unit = indexed.get(identity) if isinstance(identity, str) else None
                if unit is None or not belongs_to_project(self.catalog, unit, project_id) or not compatible(role, unit):
                    raise ValueError("办公应用绑定不属于本项目或不匹配已有接口：" + role)
            config.update(bindings=copy.deepcopy(bindings), guard_policy=normalize_policy(body.get("guard_policy", config.get("guard_policy"))),
                          revision=config["revision"] + 1, updated_at=now())
            self.store.save("project_application", project_id, config)
        return self.view(project_id, units)

    def runtime_options(self, task_id, snapshot):
        unavailable = [row["role"] for row in snapshot["binding_status"]
                       if row["status"] not in {"ENABLED", "DISABLED"}]
        if unavailable:
            raise ValueError("本项目的办公检查绑定尚不可调用，请处理资源；若接口不兼容，请重新分析来源并重新绑定：" + ", ".join(unavailable))
        policy = normalize_policy(snapshot.get("guard_policy"))
        candidates = snapshot.get("guard_candidates", [])
        _check_required(policy, candidates)
        if not candidates:
            return {}
        from seampact.office_application import build_hooks
        return build_hooks(self.platform, task_id, dict(mode="scheduler",
            operations=copy.deepcopy(snapshot.get("operations", {})),
            guard_candidates=copy.deepcopy(candidates), guard_policy=policy))
