"""Project choices for future tasks, separate from installed worker lifecycles."""

import copy

from seampact.capability_contract import digest, tool_definition
from seampact.store import now


# These are the existing office application's interfaces, not inferred checks.
OFFICE_INTERFACES = {
    "sanitize": ("sanitize_text", {"text"}, {"sanitized_text", "was_modified"}),
    "injection": ("scan_prompt_injection", {"prompt"}, {"valid", "risk_score"}),
    "privacy": ("detect_and_anonymize", {"text", "entities", "anonymize"}, {"findings"}),
    "secrets": ("scan_text", {"text", "filename"}, {"detected"}),
    "code_scan": ("scan_python_source", {"files"}, {"issues"}),
    "self_check_input": ("self_check_input", {"user_message"}, {"decision", "is_blocked", "failed"}),
    "self_check_output": ("self_check_output", {"bot_message", "user_message"}, {"decision", "is_blocked", "failed"}),
    "fact_check": ("fact_check_response", {"question", "answer", "references"},
                   {"status", "claims", "claim_count"}),
}


def belongs_to_project(catalog, unit, project_id):
    group = catalog._group(unit["runtime_group"])
    owners = {binding["project_id"] for binding in group.get("bindings", {}).values()
              if binding.get("form") == "project"}
    return not owners or owners == {project_id}


def compatible(role, unit):
    entry, inputs, outputs = OFFICE_INTERFACES[role]
    capability = unit.get("capability", {})
    output = capability.get("output", {})
    output = output.get("schema", output)
    input_schema = capability.get("input", {})
    input_fields, output_fields = input_schema.get("properties", {}), output.get("properties", {})
    if role in {"self_check_input", "self_check_output"} and "allowed" in output_fields:
        outputs = {"allowed"}  # Previously installed self-check contract.
    if not (unit.get("entry_id") == entry and inputs <= set(input_fields) and outputs <= set(output_fields)):
        return False
    if role != "fact_check":
        return True
    claims = output_fields["claims"]
    fields = claims.get("items", {}).get("properties", {})
    return (set(inputs) <= set(input_schema.get("required", []))
            and input_fields["question"].get("type") == "string"
            and input_fields["answer"].get("type") == "string"
            and input_fields["references"].get("type") == "array"
            and input_fields["references"].get("items", {}).get("type") == "string"
            and output_fields["status"].get("type") == "string"
            and output_fields["claim_count"].get("type") == "integer"
            and claims.get("type") == "array"
            and fields.get("claim_text", {}).get("type") == "string"
            and fields.get("label", {}).get("type") == "string"
            and fields.get("labels_by_reference", {}).get("type") == "array"
            and fields["labels_by_reference"].get("items", {}).get("type") == "string")


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
            enabled_groups=None, bindings={})

    def preset(self, project_id, units):
        """Reuse unambiguous installed office interfaces without calling them."""
        candidates = [unit for unit in units if belongs_to_project(self.catalog, unit, project_id)]
        bindings, ambiguous = {}, []
        for role in OFFICE_INTERFACES:
            matches = [unit for unit in candidates if compatible(role, unit)]
            if len(matches) == 1:
                bindings[role] = matches[0]["id"]
            elif matches:
                ambiguous.append(role)
        return dict(bindings=bindings, ambiguous_roles=ambiguous)

    def snapshot(self, project_id, units):
        config = copy.deepcopy(self.configuration(project_id))
        enabled = config["enabled_groups"]
        scoped = [unit for unit in units if belongs_to_project(self.catalog, unit, project_id)]
        selected = [unit for unit in scoped if enabled is None or unit["runtime_group"] in enabled]
        eligible = [unit for unit in selected if unit.get("availability", {}).get("status") == "AVAILABLE"
                    and unit.get("dispatch", {}).get("status") in {"READY_INSTANCE", "COLD_STARTABLE"}]
        current, admitted = {unit["id"]: unit for unit in units}, {unit["id"] for unit in eligible}
        bindings, binding_status = {}, []
        for role, identity in config["bindings"].items():
            unit = current.get(identity)
            status = ("MISSING" if unit is None else "OTHER_PROJECT" if unit not in scoped else
                      "DISABLED" if unit not in selected else "UNAVAILABLE" if identity not in admitted else "ENABLED")
            binding_status.append(dict(role=role, unit_id=identity, status=status))
            if status == "ENABLED":
                bindings[role] = identity
        config.update(source="project", effective_bindings=bindings, binding_status=binding_status,
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
        return dict(application=snapshot, libraries=libraries, preset=self.preset(project_id, units),
                    applies_to="future_tasks", runtime_actions=False)

    def configure(self, project_id, body):
        if set(body) - {"project_id", "enabled_groups", "bindings", "use_office_preset"}:
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
            if body.get("use_office_preset") is not None and type(body["use_office_preset"]) is not bool:
                raise ValueError("办公预设选择须为布尔值")
            bindings = (self.preset(project_id, units)["bindings"] if body.get("use_office_preset") else
                        body.get("bindings", config["bindings"]))
            if not isinstance(bindings, dict) or set(bindings) - set(OFFICE_INTERFACES):
                raise ValueError("办公应用绑定角色无效")
            indexed = {unit["id"]: unit for unit in units}
            for role, identity in bindings.items():
                unit = indexed.get(identity) if isinstance(identity, str) else None
                if unit is None or not belongs_to_project(self.catalog, unit, project_id) or not compatible(role, unit):
                    raise ValueError("办公应用绑定不属于本项目或不匹配已有接口：" + role)
            config.update(bindings=copy.deepcopy(bindings), revision=config["revision"] + 1, updated_at=now())
            self.store.save("project_application", project_id, config)
        return self.view(project_id, units)

    def runtime_options(self, task_id, snapshot):
        unavailable = [row["role"] for row in snapshot["binding_status"]
                       if row["status"] not in {"ENABLED", "DISABLED"}]
        if unavailable:
            raise ValueError("本项目的办公检查绑定尚不可调用，请先处理资源或重新复用绑定：" + ", ".join(unavailable))
        bindings = snapshot["effective_bindings"]
        if not bindings:
            return {}
        from seampact.office_application import build_hooks
        return build_hooks(self.platform, task_id, dict(mode="scheduler", bindings=bindings))
