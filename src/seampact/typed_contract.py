"""Replayable operation contracts for the existing finite native checks.

The checker re-derives operation decisions from installed Unit and group facts.
Runtime predicates remain obligations of the coordinator and deployment owner.
"""

import ast
import copy
import hashlib
import json
from seampact.target_runtime import core_identity


SCHEMA = "typed-operation-contract/1"
KERNEL = "native-binding-operation-replay/1"
OPERATIONS = ("invoke", "export_entry", "dispatch", "remote", "prepare", "prewarm", "scale_out", "scale_in",
              "materialize_guarded_entry")
_STRUCTURE_BINDING = "SOURCE_VALUE_CONDITIONED_NATIVE_ENTRY"
_UNIT_FIELDS = (
    "id", "entry_id", "source_id", "source_version", "installation_id", "analysis_id",
    "path", "source_unit", "symbol", "kind", "boundary_ids", "runtime_group", "binding",
    "effective_binding", "input_schema", "parameters", "result_schema", "native_input_schemas",
    "omitted_object_parameters", "construction", "fixed_value_bindings", "inheritance_binding",
    "execution_context", "import_context", "completion_contract", "dependency_closure", "result_structure",
    "operations", "host_condition", "call_condition", "structure_admission", "typed_evidence",
)
_GROUP_FIELDS = (
    "id", "source_id", "source_version", "installation_id", "analysis_id", "entries", "units",
    "status", "grouping", "same_process", "object_coalescing", "construction_lifetime",
    "request_lifetime", "session_recovery", "remote_conditions", "host", "host_status", "contract",
    "runtime_environment",
)


def _digest(value):
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


# This identifies the checker loaded by this process, including its finite rules.
CHECKER_ID = _digest(core_identity("checker"))


def _fields(value, names):
    return {name: copy.deepcopy(value.get(name)) for name in names}


def _unit_basis(unit):
    """Freeze producer conditions, preserving raw process observations on Unit.

    Only these exact system-generated closure locations are projected. Program
    parameters/configuration and unrelated dictionaries are not traversed.
    """
    basis = _fields(unit, _UNIT_FIELDS)
    closure = basis.get('dependency_closure')
    if not isinstance(closure, dict):
        return basis

    def type_fact(fact):
        if not isinstance(fact, dict) or fact.get('rule') != 'source-finite-type-reference/1':
            return
        observation = fact.get('check_process_observation')
        if not isinstance(observation, dict):
            return
        for producer, fields in (('builtin', ('binding',)),
                                 ('sdk_class', ('loaded_binding', 'installed_source', 'metaclass'))):
            current = observation.get(producer)
            if isinstance(current, dict) and current.get('status') == 'OBSERVATION_ONLY':
                for name in fields:
                    if name in current:
                        current[name] = 'RECHECK_AT_ORIGINAL_RUNTIME_OCCURRENCE'

    def configuration_condition(condition):
        if not isinstance(condition, dict) or condition.get('original_reader') != 'yaml.safe_load':
            return
        identity = condition.get('reader_identity')
        if not isinstance(identity, dict):
            return
        # installed_source remains: MATCHED is an actual value-proof gate.
        # Required code digests, source/value identity, statuses and every
        # decoder/stream/runtime obligation remain part of the frozen basis.
        if 'loaded' in identity:
            identity['loaded'] = 'RECHECK_AT_ORIGINAL_RUNTIME_READ'
        if 'observed_files' in identity:
            identity['observed_files'] = dict(status='CHECK_PROCESS_OBSERVATION_NOT_FROZEN',
                                              required='see required_files')

    for fact in closure.get('type_reference_facts') or []:
        type_fact(fact)
    for condition in closure.get('configuration_conditions') or []:
        configuration_condition(condition)
    for protocol in closure.get('external_protocols') or []:
        if not isinstance(protocol, dict):
            continue
        if protocol.get('rule') == 'source-finite-type-reference/1':
            type_fact(protocol.get('type_reference'))
        elif protocol.get('rule') in {'source-frozen-yaml-values/1', 'source-frozen-configuration-sequence/1'}:
            configuration_condition(protocol.get('condition'))
    return basis


def _candidate_basis(candidate):
    return dict(candidate, boundaries=candidate.get("boundaries", []))


def _context_basis(request):
    return {name: request.get(name) for name in (
        "entry_profile", "rules", "configuration", "dependency_bindings", "runtime_environment",
        "user_resources", "observation", "resources", "target_runtime",
    )}


def profile_membership(inspector, entry, candidate):
    """Decide the retained-entry structural domain before operation checking."""
    profile = dict(id="native-retained-entry/1", status="UNKNOWN", source="frozen syntax and explicit host form",
                   path=entry.get("path"), unit=entry.get("unit", "file"), symbol=entry.get("symbol"), reason=None)
    path = entry.get("path")
    if not isinstance(path, str) or path not in inspector.files:
        return dict(profile, reason="entry source is outside the frozen scope")
    if entry.get("kind") not in ("function", "method", "cli"):
        return dict(profile, status="OUT", reason="entry kind has no retained native host form")
    ids = entry.get("boundary_ids", [])
    rows = candidate.get("boundaries", [])
    if (not isinstance(ids, list) or any(not isinstance(identity, str) for identity in ids)
            or not isinstance(rows, list)):
        return dict(profile, reason="boundary selections do not identify frozen source anchors")
    boundaries = {row["id"]: row for row in rows if isinstance(row, dict) and isinstance(row.get("id"), str)}
    selected = [boundaries.get(identity) for identity in ids]
    if any(row is None for row in selected):
        return dict(profile, reason="selected boundary source is missing")
    for boundary in selected:
        if not isinstance(boundary.get("path"), str) or boundary["path"] not in inspector.files:
            return dict(profile, reason="boundary source is outside the frozen scope")
        if boundary.get("mode") not in ("W", "H", "R"):
            return dict(profile, status="OUT", reason="boundary mode has no finite native template")
        if boundary.get("mode") == "R" and boundary.get("rule", "sequential-async-loop-body/1") not in (
                "sequential-async-loop-body/1", "inplace-callsite/1"):
            return dict(profile, status="OUT", reason="R rule has no finite native template")
    if entry.get("kind") == "cli":
        command = entry.get("command")
        if selected or not isinstance(command, list) or not command or any(not isinstance(item, str) for item in command):
            return dict(profile, status="OUT", reason="retained CLI requires original argv and no in-process boundaries")
        return dict(profile, id="native-retained-cli/1", status="IN")
    try:
        if not isinstance(entry.get("symbol"), str) or not isinstance(entry.get("unit", "file"), str):
            return dict(profile, reason="native callable source anchor is incomplete")
        from seampact.source.inspection import native_ast
        from seampact.source.verification import source_unit
        tree = native_ast(source_unit(inspector, path, entry.get("unit", "file")))
        node = tree
        parts = entry.get("symbol", "").split(".")
        for part in parts:
            matches = [member for member in getattr(node, "body", [])
                       if isinstance(member, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and member.name == part]
            if len(matches) != 1:
                if len(parts) == 2 and path.endswith(".py"):
                    from seampact.source.frozen_inheritance import FrozenInheritance
                    _, method = FrozenInheritance(inspector).resolve(path, entry["symbol"])
                    node = method.node
                    break
                return dict(profile, reason="native callable source anchor is absent or ambiguous")
            node = matches[0]
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return dict(profile, status="OUT", reason="entry is outside the callable syntax domain")
        if (entry["kind"] == "method") != (len(parts) == 2):
            return dict(profile, status="OUT", reason="entry shape disagrees with its original receiver")
        if any(isinstance(value, (ast.Yield, ast.YieldFrom)) for value in ast.walk(node)):
            return dict(profile, status="OUT", reason="streaming lifetime has no retained callable host template")
        return dict(profile, status="IN", asynchronous=isinstance(node, ast.AsyncFunctionDef))
    except (ValueError, KeyError, TypeError, SyntaxError, OSError) as error:
        return dict(profile, reason=str(error))


def entry_evidence(inspector, candidate, unit, entry, checked):
    """Keep the exact installed owner scope and selected buildable boundaries."""
    selected = unit.get("boundary_ids")
    selected = selected if isinstance(selected, list) else []
    rows = candidate.get("boundaries")
    declarations = {row["id"]: row for row in rows if isinstance(row, dict) and isinstance(row.get("id"), str)} if isinstance(rows, list) else {}
    boundaries = {}
    for identity in selected:
        if not isinstance(identity, str):
            continue
        declaration, check = declarations.get(identity), checked.get(identity)
        if not declaration or not check or check.get("buildable") is not True:
            continue
        obligations = check["obligations"]
        boundaries[identity] = dict(id=identity, path=declaration["path"], symbol=declaration["symbol"],
            source_unit=declaration.get("unit", "file"), mode=declaration["mode"],
            source_anchor=copy.deepcopy(obligations["source_anchor"]),
            binding_rule=copy.deepcopy(obligations["binding_rule"]),
            lowering=copy.deepcopy(check.get("lowering")),
            controller_schedule=check["operations"]["controller_schedule"]["status"])
    structural = unit.get("binding") == _STRUCTURE_BINDING
    result = dict(profile=profile_membership(inspector, entry, candidate),
                  entry_checked=(not structural and (unit.get("status") == "READY" or bool(unit.get("host_condition"))
                                 or unit.get('status') == 'CALL_CONDITION_DEFERRED' and bool(unit.get('call_condition')))),
                  entry=_digest(entry), boundaries=boundaries)
    if structural:
        from seampact.source.value_conditioned_admission import compile_structure_plan, verify_structure_plan
        current = compile_structure_plan(inspector, entry, candidate)
        supplied = unit.get("structure_admission")
        matched = (unit.get("status") == "STRUCTURE_ONLY" and
                   verify_structure_plan(inspector, entry, candidate, supplied) and
                   _structure_mapping(unit, entry, current) and
                   all(checked.get(identity) == current.get("whole_w") for identity in selected))
        result.update(_structure_evidence(entry, current, matched))
    return result


def _structure_mapping(unit, entry, plan):
    """Keep material binding and original ports tied to the independently checked H."""
    return (plan.get("status") == "STRUCTURE_CHECKED" and unit.get("entry_id") == entry.get("id")
            and unit.get("source_version") == plan.get("source_version")
            and unit.get("path") == entry.get("path") and unit.get("symbol") == entry.get("symbol")
            and unit.get("kind") == entry.get("kind")
            and unit.get("effective_binding") == "function"
            and unit.get("source_unit", "file") == entry.get("unit", "file")
            and unit.get("boundary_ids") == entry.get("boundary_ids")
            and unit.get("input_schema") == plan.get("interface")
            and unit.get("parameters") == plan.get("interface")
            and unit.get("result_schema") == plan.get("declared_result_schema")
            and not unit.get("native_input_schemas") and not unit.get("construction")
            and not unit.get("inheritance_binding") and not unit.get("call_condition")
            and not unit.get("fixed_value_bindings") and not unit.get("omitted_object_parameters"))


def _structure_evidence(entry, plan, checked):
    return dict(structural_entry_checked=bool(checked), structure_entry_id=entry.get("id"),
                structure_digest=plan.get("plan_digest"),
                structure_support=_fields(plan, ("schema", "source_version", "entry_digest", "candidate_digest",
                                                 "checker_identity", "installation_evidence")))


def installation_evidence(inspector, candidate):
    """Bind a freshly checked installation to its actual frozen input scope."""
    request = inspector.request
    files = request.get("files", [])
    versioned = dict(files=files, withheld_sources=request["withheld_sources"]) if request.get("withheld_sources") else files
    version = hashlib.sha256(json.dumps(versioned, sort_keys=True).encode()).hexdigest()
    if version != request.get("source_version") or {row["path"]: row for row in files} != inspector.files:
        raise ValueError("typed contract source manifest differs from the frozen version")
    for path in inspector.files:
        inspector.data(path)
    return dict(schema="native-installation-evidence/1", checker=CHECKER_ID,
                source_version=version, source_scope=_digest(versioned),
                candidate=_digest(_candidate_basis(candidate)), context=_digest(_context_basis(request)),
                entry_profile=copy.deepcopy(request.get("entry_profile") or {}),
                rules=copy.deepcopy(request.get("rules") or {}))


def binding_evidence(inspector, candidate, frozen):
    """Check in-memory binding inputs without rereading the frozen repository."""
    result = dict(frozen, source_version=inspector.request.get("source_version"),
                  candidate=_digest(_candidate_basis(candidate)), context=_digest(_context_basis(inspector.request)))
    if "structural_entry_checked" in frozen:
        # This new material operation requires a fresh actual source replay;
        # the historical entry_checked shortcut is deliberately not used.
        from seampact.source.value_conditioned_admission import compile_structure_plan
        entries = [entry for entry in candidate.get("entries", []) if isinstance(entry, dict)
                   and entry.get("id") == frozen.get("structure_entry_id")]
        entry = entries[0] if len(entries) == 1 else {}
        current = compile_structure_plan(inspector, entry, candidate)
        result.update(_structure_evidence(entry, current, current.get("status") == "STRUCTURE_CHECKED"))
        result["entry_checked"] = False
    return result


def _decision(allowed, rule, facts, guards=(), reason=None, unknown=True):
    return dict(status="SUPPORTED" if allowed else "UNKNOWN" if unknown else "UNSUPPORTED",
                allowed=bool(allowed), rule=rule, premises=list(facts), guards=list(guards), reason=reason,
                formal_proof=False)


def _derive(unit, group):
    closure = unit.get("dependency_closure") or {}
    export = unit.get("operations", {}).get("export_entry", {})
    evidence = unit.get("typed_evidence") or {}
    conditional = unit.get('binding') == 'SOURCE_CONDITIONAL_NATIVE_ENTRY' and unit.get('call_condition', {}).get('status') == 'REQUIRES_ACTUAL_CALL'
    bound = (unit.get("binding") == "SOURCE_CHECKED_NATIVE_ENTRY" or conditional) and (
             export.get("status") in {"SOURCE_BOUND", "ADMITTED", "CONDITIONAL_BOUND"}
             and closure.get("status") == "CHECKED" and closure.get("scope_covered") is True
             and not closure.get("unresolved_targets") and not closure.get("missing_dependency_ids")
             and isinstance(unit.get("input_schema"), dict)
             and evidence.get("entry_checked") is True
             and evidence.get("profile", {}).get("status") == "IN")
    contracts = (group or {}).get("contract") or {}
    host = ((group or {}).get("host") or {}).get("members", {}).get(unit.get("entry_id"), {})
    hosted = (bound and host.get("status") == "HOST_CHECKED"
              and unit.get("entry_id") in contracts.get("root_entries", []))
    group_operations = contracts.get("operations", {})
    common = ["SOURCE_BOUND_NATIVE_ENTRY", "CHECKED_DEPENDENCY_CLOSURE", "FROZEN_INSTALLATION_INPUTS"]
    guards = ["ORIGINAL_INPUTS_AND_EXITS", "ORIGINAL_OWNER_AND_ORDER", "CURRENT_BINDING_OR_CHECKED_HOST"]
    if conditional:
        guards += ['ACTUAL_CALL_VALUES_CHECKED', 'WORKER_INDEPENDENT_SAME_CALL_CHECK']
    result = {}
    for operation in ("invoke", "export_entry", "dispatch"):
        extra = ["ACTUAL_ENABLEMENT", "UNIQUE_PERMIT", "CURRENT_GENERATION", "RESOURCE_ADMISSION"] if operation == "dispatch" else []
        result[operation] = _decision(bound, "native-source-bound-entry/1", common, guards + extra,
                                      None if bound else "source binding, input interface or complete checked dependency scope is missing")
        if conditional and bound:
            result[operation]['status'] = 'CONDITIONAL'
    result["remote"] = _decision(hosted, "native-checked-group-host/1", common + ["HOST_MEMBER_AND_PORTS_CHECKED"],
        guards + ["MATCHED_GROUP_INSTANCE", "ACTUAL_PORT_VALUE", "ORIGINAL_GROUP_OWNER", "REMOTE_RESULT_RETIREMENT"],
        None if hosted else "this member has no checked runtime-group host and port contract")
    start = hosted and group_operations.get("start", {}).get("status") == "SUPPORTED"
    result["prepare"] = _decision(start, "native-group-start/1", ["CHECKED_GROUP_HOST", "GROUP_START_SUPPORTED"],
        ["CURRENT_HOST_ENVIRONMENT", "ACTUAL_CAPACITY", "FROZEN_CONSTRUCTION", "PREPARE_MODE_FROM_GROUP_CONTRACT"],
        None if start else "runtime-group start conditions are unresolved")
    prewarm = start and contracts.get("prepare", {}).get(unit.get("entry_id"), {}).get("status") == "SUPPORTED"
    result["prewarm"] = _decision(prewarm, "native-owner-prepare/1", ["GROUP_START_SUPPORTED", "ENTRY_PREPARE_SUPPORTED"],
        ["ORIGINAL_CONSTRUCTION_COUNT_AND_EFFECTS", "NO_ACTIVE_CALL", "ACTUAL_CAPACITY"],
        None if prewarm else "this member has no checked early-preparation rule")
    lifecycle = contracts.get("lifecycle") or {}
    for operation, group_operation, permission in (("scale_out", "add_replica", "replicate"),
                                                   ("scale_in", "remove_replica", "reclaim")):
        permitted = (hosted and lifecycle.get(permission, {}).get("status") == "SUPPORTED"
                     and group_operations.get(group_operation, {}).get("status") == "SUPPORTED")
        if operation == "scale_out":
            permitted = permitted and contracts.get("replicas", {}).get("replicable") is True
        runtime_guards = ["ACTUAL_CAPACITY", "NEW_OWNER_IDENTITY"] if operation == "scale_out" else [
            "OWNER_LEASE_ENDED", "ROOTS_AND_PERMITS_DRAINED", "NO_UNKNOWN_CALLS",
            "PROCESS_AND_BACKEND_RELEASE_CONFIRMED"]
        result[operation] = _decision(permitted, "native-group-lifecycle/1",
            ["CHECKED_GROUP_HOST", permission.upper() + "_SUPPORTED", group_operation.upper() + "_SUPPORTED"],
            runtime_guards, None if permitted else "the frozen group lifecycle obligations remain unresolved")
    structural = unit.get("binding") == _STRUCTURE_BINDING
    plan = unit.get("structure_admission")
    plan = plan if isinstance(plan, dict) else {}
    support = evidence.get("structure_support") or {}
    material = (structural and unit.get("status") == "STRUCTURE_ONLY"
                and evidence.get("entry_checked") is False and evidence.get("structural_entry_checked") is True
                and evidence.get("profile", {}).get("status") == "IN"
                and evidence.get("structure_entry_id") == unit.get("entry_id")
                and evidence.get("structure_digest") == plan.get("plan_digest")
                and support == _fields(plan, ("schema", "source_version", "entry_digest", "candidate_digest",
                                             "checker_identity", "installation_evidence"))
                and plan.get("schema") == "source-value-conditioned-structure/1"
                and plan.get("status") == "STRUCTURE_CHECKED"
                and plan.get("plan_digest") == _digest({key: value for key, value in plan.items() if key != "plan_digest"})
                and plan.get("source_version") == unit.get("source_version")
                and evidence.get("entry") == plan.get("entry_digest")
                and unit.get("path") == plan.get("source_anchor", {}).get("path")
                and unit.get("symbol") == plan.get("source_anchor", {}).get("symbol")
                and unit.get("kind") == "function" and unit.get("source_unit", "file") == "file"
                and unit.get("effective_binding") == "function"
                and unit.get("boundary_ids") == [plan.get("whole_w", {}).get("id")]
                and plan.get("interface") == unit.get("input_schema") == unit.get("parameters")
                and plan.get("declared_result_schema") == unit.get("result_schema")
                and not unit.get("native_input_schemas") and not unit.get("construction")
                and not unit.get("inheritance_binding") and not unit.get("call_condition")
                and not unit.get("fixed_value_bindings") and not unit.get("omitted_object_parameters")
                and plan.get("permissions") and all(value is False for value in plan["permissions"].values()))
    result["materialize_guarded_entry"] = _decision(bool(material), "value-conditioned-entry-materialization/1",
        ["ACTUAL_STRUCTURAL_SOURCE_REPLAY", "ORIGINAL_INTERFACE_AND_W", "COMPLETE_PURE_PREFIX"],
        ["CURRENT_STRUCTURAL_EVIDENCE", "NO_SOURCE_EXECUTION", "NO_SEMANTIC_EXPORT_OR_CALL_PERMISSION"],
        None if material else "an independently replayed structure-only original entry is required")
    if structural:
        for operation in OPERATIONS:
            if operation == "materialize_guarded_entry":
                continue
            result[operation] = _decision(False, "value-conditioned-semantic-obligations/1",
                ["STRUCTURAL_MATERIAL_IS_NOT_SEMANTIC_PERMISSION"],
                ["COMPLETE_SAME_CALL_CONTROLLER_PROOF", "ACTUAL_WORKER_SAME_LOCK_PROOF",
                 "ORIGINAL_ENABLEMENT_AND_EXITS", "CURRENT_ATOMIC_RESOURCE_PERMIT"],
                "structure-only material requires independent same-call controller and actual worker proofs; "
                "this operation's original host, export and lifecycle conditions remain required")
    return result


def _body(unit, group):
    evidence = unit.get("typed_evidence") or {}
    host = (group or {}).get("contract") or {}
    closure = unit.get("dependency_closure") or {}
    boundaries = evidence.get("boundaries", {})
    construction = dict(entry_binding=unit.get("effective_binding"), original_anchor=_fields(unit, ("path", "source_unit", "symbol", "kind")),
        constructor_witness=copy.deepcopy(unit.get("construction")), inheritance=copy.deepcopy(unit.get("inheritance_binding")),
        selected_boundaries=copy.deepcopy(unit.get("boundary_ids")), boundary_recipes={
            identity: _fields(row, ("mode", "binding_rule", "lowering")) for identity, row in boundaries.items()})
    contract = dict(input=copy.deepcopy(unit.get("input_schema")), output=copy.deepcopy(unit.get("result_schema")),
        owner=dict(entry_id=unit.get("entry_id"), binding=unit.get("effective_binding"),
                   group=unit.get("runtime_group"), host_scope=copy.deepcopy(host.get("scope"))),
        exits=dict(completion=copy.deepcopy(unit.get("completion_contract")),
                   unresolved=["SEMANTIC_EXIT_EQUIVALENCE", "TIMEOUT_AND_EFFECT_PRESERVATION"]),
        coverage=dict(entry=unit.get("entry_id"), selected_boundaries=copy.deepcopy(unit.get("boundary_ids")),
                      whole_task_work="CHECK_AT_SCENE_AND_RUNTIME"),
        resources=dict(dependency_status=closure.get("status"), scope_covered=closure.get("scope_covered"),
                       dependencies=copy.deepcopy(closure.get("dependency_ids", [])),
                       working_set="REQUIRED_FROM_TASK_AND_PLACEMENT", demand="REQUIRED_FROM_RUN_CONFIGURATION"))
    return dict(schema=SCHEMA, kernel=KERNEL, checker=CHECKER_ID, formal_proof=False,
        scope="existing finite source binding, dependency, group-host and lifecycle checks",
        identity=_fields(unit, ("id", "entry_id", "source_id", "source_version", "installation_id", "runtime_group")),
        profile=copy.deepcopy(evidence.get("profile") or dict(id="native-retained-entry/1", status="UNKNOWN")),
        contract=contract, construction=construction,
        support=dict(unit=_digest(_unit_basis(unit)),
                     group=_digest(_fields(group, _GROUP_FIELDS)) if group is not None else None,
                     installation=copy.deepcopy(evidence), closure=_fields(closure, (
                         "status", "scope_covered", "dependency_ids", "missing_dependency_ids", "unresolved_targets")),
                     invalidation=["SOURCE_SCOPE", "CHECKER_VERSION", "CANDIDATE", "CONFIGURATION", "UNIT_OR_GROUP_EVIDENCE"]),
        operations=_derive(unit, group))


def build_contract(unit, *, group=None):
    """Generate only after a normal installation has produced typed_evidence."""
    evidence = unit.get("typed_evidence") or {}
    if (evidence.get("schema") != "native-installation-evidence/1" or evidence.get("checker") != CHECKER_ID
            or evidence.get("source_version") != unit.get("source_version")):
        raise ValueError("typed contract requires current mechanical installation evidence")
    if unit.get("runtime_group") and group is None:
        raise ValueError("typed contract requires the actual installed runtime group")
    body = _body(unit, group)
    return dict(body, digest=_digest(body))


def verify_contract(unit, *, group=None, evidence=None):
    """Replay decisions and compare current evidence, identities and checker version."""
    def invalid(reason):
        return dict(status="RECHECK_REQUIRED", valid=False, reason=reason, contract_digest=None,
                    schema=SCHEMA, kernel=KERNEL)
    if not isinstance(unit, dict):
        return invalid("native Unit record is missing")
    contract = unit.get("typed_contract")
    if not isinstance(contract, dict) or contract.get("schema") != SCHEMA or contract.get("kernel") != KERNEL:
        return invalid("installed typed contract is missing or uses an older schema; recheck the candidate")
    if contract.get("checker") != CHECKER_ID:
        return invalid("finite checker implementation changed; recheck the candidate")
    if unit.get("status") in {"SUPERSEDED", "DETACHED"}:
        return invalid("the source installation is no longer active")
    if unit.get("runtime_group"):
        if not isinstance(group, dict) or group.get("id") != unit["runtime_group"]:
            return invalid("the current runtime-group evidence is required")
        if (group.get("source_id") != unit.get("source_id") or group.get("source_version") != unit.get("source_version")
                or group.get("installation_id") != unit.get("installation_id")
                or unit.get("id") not in group.get("units", [])
                or unit.get("entry_id") not in group.get("entries", [])):
            return invalid("Unit and runtime group belong to different frozen installations")
    elif group is not None:
        return invalid("the Unit has no matching runtime group")
    if evidence is not None and unit.get("typed_evidence") != evidence:
        return invalid("frozen source, candidate, profile or configuration inputs changed")
    if unit.get("binding") == _STRUCTURE_BINDING:
        if evidence is None:
            return invalid("structure-only material requires current actual source/binding evidence")
        if evidence.get("structural_entry_checked") is not True or evidence.get("entry_checked") is not False:
            return invalid("the independent structural entry proof is unavailable")
        try:
            if not _derive(unit, group)["materialize_guarded_entry"]["allowed"]:
                return invalid("the current structure material, original mapping or proof content changed")
        except (ValueError, KeyError, TypeError):
            return invalid("the current structural material is malformed")
    try:
        if group is not None:
            group_contract = group.get("contract") or {}
            group_content = {key: value for key, value in group_contract.items() if key != "digest"}
            group_digest = hashlib.sha256(json.dumps(group_content, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            if group_contract.get("digest") != group_digest:
                return invalid("runtime-group contract content changed")
        expected = build_contract(unit, group=group)
    except (ValueError, KeyError, TypeError) as error:
        return invalid(str(error))
    if contract != expected:
        return invalid("typed decisions or their installed Unit/group evidence changed; recheck the candidate")
    return dict(status="VALID", valid=True, reason=None, contract_digest=expected["digest"], schema=SCHEMA, kernel=KERNEL)


def operation_status(unit, operation, *, group=None, evidence=None):
    """Return a replayed static permission together with its runtime obligations."""
    checked = verify_contract(unit, group=group, evidence=evidence)
    return _operation_status(unit, operation, group, checked)


def _operation_status(unit, operation, group, checked):
    if not checked["valid"]:
        return dict(status="RECHECK_REQUIRED", allowed=False, reason=checked["reason"], guards=[],
                    contract_digest=None, formal_proof=False)
    if operation not in OPERATIONS:
        return dict(status="UNSUPPORTED", allowed=False, reason="unknown native operation: " + str(operation),
                    guards=[], contract_digest=checked["contract_digest"], formal_proof=False)
    value = copy.deepcopy(unit["typed_contract"]["operations"][operation])
    value["contract_digest"] = checked["contract_digest"]
    deferred = (unit.get("host_condition") or {}).get("status") == "ENVIRONMENT_DEFERRED"
    hosted = ((group or {}).get("host") or {}).get("members", {}).get(unit.get("entry_id"), {}).get("status") == "HOST_CHECKED"
    conditional = unit.get('status') == 'CALL_CONDITION_DEFERRED' and bool(unit.get('call_condition'))
    material = operation == "materialize_guarded_entry" and unit.get("binding") == _STRUCTURE_BINDING and unit.get("status") == "STRUCTURE_ONLY"
    if value["allowed"] and unit.get("status") != "READY" and not (deferred and hosted) and not conditional and not material:
        value.update(status="RECHECK_REQUIRED", allowed=False, reason="the installed native binding is unavailable")
    return value


def contract_checks(unit, *, group=None, evidence=None):
    """Replay once for a catalog observation, then expose every operation."""
    checked = verify_contract(unit, group=group, evidence=evidence)
    return dict(contract_check=checked, operation_checks={operation: _operation_status(unit, operation, group, checked)
                                                        for operation in OPERATIONS})


def require_operation(unit, operation, *, group=None, evidence=None):
    checked = operation_status(unit, operation, group=group, evidence=evidence)
    if not checked["allowed"]:
        raise ValueError("native " + str(operation) + " requires recheck: " + str(checked["reason"]))
    return checked


def require_boundary(unit, *, group, binding, entry, call, root, gate):
    """Check an enabled gate in the limited scope of its original active owner."""
    dispatch = require_operation(unit, "dispatch", group=group)
    evidence = unit["typed_evidence"]
    if (getattr(binding, "detached", False) or _digest(entry) != evidence.get("entry")
            or not root or root.get("state") != "ACTIVE" or root.get("unit_id") != unit["id"]
            or not call or call.get("physical") != "ENABLED" or call.get("logical") != "WAITING"
            or call.get("root_id") != root.get("id") or call.get("task_id") != root.get("task_id")):
        raise ValueError("native boundary is outside the active original root")
    if (not gate or gate.get("active") is not True or gate.get("scope") != "native-boundary"
            or gate.get("id") != call.get("gate") or not call.get("owner")
            or (gate.get("owner"), gate.get("generation")) != (call.get("owner"), call.get("generation"))):
        raise ValueError("native boundary gate or original owner generation changed")
    label = gate.get("unit") or {}
    if label != (call.get("unit") or {}):
        raise ValueError("native boundary registration differs from the enabled call")
    if (label.get("source_id"), label.get("source_version"), label.get("installation_id")) != (
            unit.get("source_id"), unit.get("source_version"), unit.get("installation_id")):
        raise ValueError("native boundary belongs to another source installation")
    if root.get("host") == "local":
        if label.get("instance_id") is not None:
            raise ValueError("remote native boundary cannot belong to a local root")
        owner = getattr(binding, "entry_owner_ids", {}).get(unit["id"])
        if owner is not None and owner != call["owner"]:
            raise ValueError("native boundary does not belong to the bound original owner")
    elif (not root.get("host") or label.get("instance_id") != root["host"]
          or label.get("runtime_group") != unit.get("runtime_group")):
        raise ValueError("native boundary belongs to another runtime-group instance")

    boundary_id = label.get("boundary_id")
    if boundary_id is None:
        if evidence.get("boundaries") or any(label.get(name) != unit.get(name) for name in (
                "id", "path", "symbol", "source_unit")):
            raise ValueError("native whole-entry gate does not match the installed root")
        scope = "ORIGINAL_WHOLE_ENTRY_GATE"
        formal_status = "UNKNOWN"
    else:
        boundary = evidence.get("boundaries", {}).get(boundary_id)
        if (not boundary or label.get("root_unit_id") != unit["id"]
                or label.get("id") != unit["source_version"][:16] + ":" + boundary_id
                or any(label.get(name) != boundary.get(name) for name in ("path", "symbol", "source_unit", "mode"))):
            raise ValueError("native gate has no matching installed boundary anchor")
        scope = "ORIGINAL_OWNER_BOUNDARY_GATE"
        formal_status = boundary["controller_schedule"]
    return dict(scope=scope, boundary_id=boundary_id, contract_digest=dispatch["contract_digest"],
                formal_proof=False, controller_schedule=formal_status)
