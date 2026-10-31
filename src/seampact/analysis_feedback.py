from __future__ import annotations

import copy
import json
from typing import Any


# These fields carry source material rather than decisions. The complete value is
# available through report_ref and its path in details_not_inlined.
_DETAIL_FIELDS = {"evidence", "source_evidence", "call_edges", "process_edges", "classification", "dependency_closure", "downstream_dependencies", "mapping_proofs", "configuration_flow", "code"}
_ISSUE_FIELDS = ("issue_code", "entry_id", "dependency_id", "field_paths", "repair_scope", "model_repairable", "reason", "repair_action", "category", "title", "target", "parameter", "location", "field", "blocking_static_check")


def compact_report(report: dict, reference: Any) -> dict:
    """Return independent, navigable check feedback without changing report.

    Paths in source_paths and details_not_inlined are JSON Pointers relative to
    the referenced full report. No execution or permission decision is inferred.
    """
    omitted: dict[str, int] = {}
    issues: list[dict] = []
    issue_index: dict[str, dict] = {}
    issue_groups: dict[str, str] = {}

    def child_path(path: str, key: Any) -> str:
        return path + "/" + str(key).replace("~", "~0").replace("/", "~1")

    def count(value: Any) -> int:
        return len(value) if isinstance(value, (list, dict, str)) else int(value is not None)

    def clean(value: Any, path: str) -> Any:
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                child = child_path(path, key)
                if key in _DETAIL_FIELDS:
                    if count(item):
                        omitted[child] = count(item)
                    if key in {"evidence", "source_evidence"} and isinstance(item, list):
                        result[key + "_count"] = len(item)
                        result[key + "_preview"] = [clean_evidence(x) for x in item[:2]]
                    continue
                # Issues have a single canonical representation below.
                if key in {"issues", "candidate_issues", "model_repairs", "external_conditions", "pending_execution"} and isinstance(item, list):
                    continue
                result[key] = clean(item, child)
            return result
        if isinstance(value, list):
            return [clean(v, child_path(path, i)) for i, v in enumerate(value)]
        return copy.deepcopy(value)

    def clean_evidence(value: Any) -> Any:
        if isinstance(value, dict):
            return {k: copy.deepcopy(v) for k, v in value.items() if k in {"path", "unit", "symbol", "start", "end", "line", "kind"} and not isinstance(v, (list, dict))}
        if isinstance(value, (str, int, float, bool)) or value is None:
            return copy.deepcopy(value)
        return None

    def visit(value: Any, path: str = "", entry_id: Any = None, dependency_id: Any = None, is_issue: bool = False) -> None:
        if isinstance(value, dict):
            entry_id = value.get("entry_id", entry_id)
            dependency_id = value.get("dependency_id", dependency_id)
            if is_issue:
                issue = {key: copy.deepcopy(value[key]) for key in _ISSUE_FIELDS if key in value}
                if entry_id is not None:
                    issue.setdefault("entry_id", copy.deepcopy(entry_id))
                if dependency_id is not None:
                    issue.setdefault("dependency_id", copy.deepcopy(dependency_id))
                # Do not infer model_repairable or any scope from placement.
                evidence = value.get("source_evidence", value.get("evidence"))
                if isinstance(evidence, list):
                    issue["evidence_count"] = len(evidence)
                    issue["evidence_preview"] = [clean_evidence(x) for x in evidence[:2]]
                identity = {k: v for k, v in issue.items() if k not in {"evidence_count", "evidence_preview", "category", "title", "target"}}
                fingerprint = json.dumps(identity, sort_keys=True, ensure_ascii=False, default=str)
                if fingerprint in issue_index:
                    issue_index[fingerprint]["source_paths"].append(path)
                else:
                    issue["source_paths"] = [path]
                    issues.append(issue)
                    issue_index[fingerprint] = issue
                for group in ("model_repairs", "external_conditions", "pending_execution"):
                    if path.startswith("/feedback/" + group + "/"):
                        issue_groups[fingerprint] = group
                        break
            for key, item in value.items():
                child = child_path(path, key)
                if key in {"evidence", "source_evidence", "call_edges", "process_edges", "classification", "mapping_proofs", "configuration_flow", "execution_contracts", "unresolved"}:
                    continue
                if key in {"issues", "candidate_issues", "model_repairs", "external_conditions", "pending_execution"} and isinstance(item, list):
                    for i, issue in enumerate(item):
                        visit(issue, child_path(child, i), entry_id, dependency_id, True)
                elif key == "entries" and isinstance(item, list):
                    for i, entry in enumerate(item):
                        eid = entry.get("id", entry_id) if isinstance(entry, dict) else entry_id
                        visit(entry, child_path(child, i), eid, dependency_id)
                elif key == "dependencies" and isinstance(item, list):
                    for i, dep in enumerate(item):
                        did = dep.get("id", dependency_id) if isinstance(dep, dict) else dependency_id
                        visit(dep, child_path(child, i), entry_id, did)
                elif key == "entry_checks" and isinstance(item, dict):
                    for eid, check in item.items():
                        visit(check, child_path(child, eid), eid, dependency_id)
                else:
                    visit(item, child, entry_id, dependency_id)
        elif isinstance(value, list):
            for i, item in enumerate(value):
                visit(item, child_path(path, i), entry_id, dependency_id)

    visit(report)
    result = {"schema": "compact-check-feedback/1", "report_ref": copy.deepcopy(reference)}
    for key in ("source_version", "status", "outcome", "execution", "admitted", "admission_granted", "split_permission", "export_permission", "parallel", "remote", "scope", "boundaries", "entries", "resources", "missing", "installation", "targets"):
        if key in report:
            result[key] = clean(report[key], child_path("", key))
    readiness = report.get("entry_readiness")
    if isinstance(readiness, list):
        result["entry_readiness"] = [clean({k: v for k, v in entry.items() if k in {"entry_id", "path", "symbol", "status", "binding_status", "reason", "initialization", "execution", "parameters", "operations", "scope", "next_step"}}, child_path("/entry_readiness", i)) for i, entry in enumerate(readiness)]
        omitted["/entry_readiness"] = len(readiness)
    # Select decisions and interfaces; detailed source and repeated findings stay
    # in the referenced report and the canonical issue arrays.
    dependencies = report.get("dependencies")
    if isinstance(dependencies, list):
        result["dependencies"] = []
        for i, dep in enumerate(dependencies):
            path = child_path("/dependencies", i)
            selected = {k: v for k, v in dep.items() if k in {"id", "kind", "status", "execution", "required", "configurable", "entry_ids", "mappings", "selection_allowed", "entry_checks"}}
            checks = selected.get("entry_checks")
            if isinstance(checks, dict):
                selected["entry_checks"] = {eid: {k: v for k, v in check.items() if k in {"status", "mapping_count"}} for eid, check in checks.items()}
            result["dependencies"].append(clean(selected, path))
            omitted[path] = len(dep)
    bindings = report.get("entry_bindings")
    if isinstance(bindings, dict):
        result["entry_bindings"] = {k: copy.deepcopy(v) for k, v in bindings.items() if k in {"status", "has_bindable_entries", "reason", "initialization", "parallel", "remote"}}
        result["entry_bindings"]["entries"] = [clean({k: v for k, v in entry.items() if k in {"id", "path", "unit", "symbol", "kind", "status", "reason", "parameters", "input_schema", "result_schema", "native_input_schemas", "operations", "field_paths"}}, child_path("/entry_bindings/entries", i)) for i, entry in enumerate(bindings.get("entries", []))]
        groups = bindings.get("runtime_groups")
        if isinstance(groups, dict):
            selected_groups = {k: v for k, v in groups.items() if k in {"schema", "profile", "proposal_gaps", "proposal_status"}}
            selected_groups["groups"] = [{k: v for k, v in group.items() if k in {"id", "entries", "status", "grouping", "same_process", "object_coalescing", "construction_lifetime", "request_lifetime", "replicas", "session_recovery", "gaps", "host_status", "remote_conditions"}} for group in groups.get("groups", [])]
            result["entry_bindings"]["runtime_groups"] = clean(selected_groups, "/entry_bindings/runtime_groups")
            omitted["/entry_bindings/runtime_groups"] = len(groups)
        omitted["/entry_bindings/entries"] = len(bindings.get("entries", []))
    feedback = report.get("feedback")
    if isinstance(feedback, dict):
        result["feedback"] = clean(feedback, "/feedback")
    if "execution_contracts" in report:
        omitted["/execution_contracts"] = count(report["execution_contracts"])
    for key, value in report.items():
        if key not in result and key not in {"feedback", "entry_bindings", "execution_contracts"}:
            omitted.setdefault(child_path("", key), count(value))
    # The resource checker uses mapping-relative paths while feedback uses
    # report-relative paths. Merge only when all other identifying facts agree.
    grouped = [item for fp, item in issue_index.items() if fp in issue_groups]
    for fp, item in list(issue_index.items()):
        if fp in issue_groups:
            continue
        for existing in grouped:
            fields = ("issue_code", "entry_id", "dependency_id", "reason", "repair_action")
            if not all(item.get(k) == existing.get(k) for k in fields):
                continue
            a, b = item.get("field_paths"), existing.get("field_paths")
            if not (isinstance(a, list) and isinstance(b, list) and len(a) == len(b) == 1 and isinstance(a[0], str) and isinstance(b[0], str) and b[0].endswith("." + a[0]) and a[0].startswith("mappings[")):
                continue
            for key in ("parameter", "location", "field"):
                if key in item:
                    existing.setdefault(key, item[key])
            existing["source_paths"].extend(item["source_paths"])
            del issue_index[fp]
            break
    if isinstance(feedback, dict):
        for group in ("model_repairs", "external_conditions", "pending_execution"):
            if group in feedback:
                result["feedback"][group] = [item for fingerprint, item in issue_index.items() if issue_groups.get(fingerprint) == group]
    for item in result.get("feedback", {}).get("external_conditions", []):
        for key in ("category", "title", "target"):
            item.pop(key, None)
    result["issues"] = [item for fingerprint, item in issue_index.items() if fingerprint not in issue_groups]
    result["details_not_inlined"] = [{"path": path, "count": n} for path, n in omitted.items()]
    return result
