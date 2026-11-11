"""Bind fixed Hub cache declarations to original, finitely traced source calls.

No source or SDK is executed, no cache/model file is read, and original loader
arguments are never changed. NOT_USED describes one checked entry scope; it
does not remove or rewrite the candidate's original dependency declarations.
"""

import ast
import copy
import hashlib
import re

from seampact.configuration.resource_origins import resource_origin_facts, _bound_calls
from seampact.source.dependency_closure import resolve_resource_calls
from seampact.source.inspection import units


RULE = "source-fixed-hub-call/1"
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")


def _ast_key(expression):
    return ast.dump(ast.parse(expression, mode="eval").body, include_attributes=False)


def _reference(value, identity, depth=0):
    if depth > 32:
        return True  # An unexamined configuration must not prove non-use.
    if type(value) is dict:
        ref = value.get("$dependency")
        if type(ref) is dict and ref.get("id") == identity:
            return True
        return any(_reference(child, identity, depth + 1) for child in value.values())
    if type(value) in (list, tuple):
        return any(_reference(child, identity, depth + 1) for child in value)
    return False


def _model_argument(node, target):
    keywords = {item.arg: item.value for item in node.keywords if item.arg is not None}
    if target.endswith(".from_pretrained"):
        position = 1 if target.startswith("peft.PeftModel") else 0
        return node.args[position] if len(node.args) > position else keywords.get("pretrained_model_name_or_path",
            keywords.get("model_id", keywords.get("model_name_or_path")))
    if target == "vllm.LLM":
        return node.args[0] if node.args else keywords.get("model")
    if target in {"huggingface_hub.snapshot_download", "huggingface_hub.hf_hub_download"}:
        return node.args[0] if node.args else keywords.get("repo_id")
    return None


def _sites(inspector, row):
    """Exact complete syntax sites, never a choice among literal candidates."""
    expression = (row.get("placement") or {}).get("expression")
    if type(expression) is not str or not expression.strip():
        raise ValueError("original placement expression is absent")
    wanted = _ast_key(expression)
    evidence = row.get("evidence")
    if type(evidence) is not list or not evidence:
        raise ValueError("complete original model call evidence is absent")
    for item in evidence:
        if (type(item) is not dict or item.get("path") not in inspector.files
                or type(item.get("start")) is not int or type(item.get("end")) is not int
                or not 1 <= item["start"] <= item["end"]):
            raise ValueError("original source evidence range is invalid")
        available = dict(units(item["path"], inspector.data(item["path"])))
        cell = item.get("unit", "file")
        if cell not in available or item["end"] > len(available[cell].splitlines()):
            raise ValueError("original source evidence exceeds the frozen source unit")
    facts = [fact for fact in resource_origin_facts(inspector, evidence)
             if fact.get("role") == "model" and type(fact.get("expression")) is str
             and _ast_key(fact["expression"]) == wanted]
    if not facts:
        raise ValueError("no complete cited original model call matches placement.expression")
    parsed, result = {}, []
    for fact in facts:
        key = (fact["path"], fact["unit"])
        if key not in parsed:
            available = dict(units(fact["path"], inspector.data(fact["path"])))
            if fact["unit"] not in available:
                raise ValueError("cited source unit is absent")
            tree = ast.parse(available[fact["unit"]])
            parents = {id(child): parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
            parsed[key] = tree, parents, list(_bound_calls(tree.body, fact["path"], {}))
        tree, parents, calls = parsed[key]
        matches = []
        for node, target in calls:
            model = _model_argument(node, target or "")
            if (target == fact["loader"] and node.lineno == fact["start"] and node.end_lineno == fact["end"]
                    and model is not None and ast.dump(model, include_attributes=False) == wanted):
                matches.append(node)
        if len(matches) != 1:
            raise ValueError("cited model site is ambiguous or lacks its exact original expression")
        node = matches[0]
        current, child, callable_scope = parents.get(id(node)), node, False
        source_scope = "MODULE_OR_CLASS_INITIALIZATION"
        while current is not None:
            if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                callable_scope = child in getattr(current, "body", []) if not isinstance(current, ast.Lambda) else False
                source_scope = ("CALLABLE" if callable_scope else "DEFINITION_TIME_EXPRESSION")
                if (callable_scope and current.name.startswith("__") and current.name.endswith("__")
                        and current.name != "__init__"):
                    source_scope = "IMPLICIT_SOURCE_PROTOCOL"
                break
            child = current
            current = parents.get(id(current))
        site = dict(path=fact["path"], unit=fact["unit"], start=fact["start"], end=fact["end"],
            target=fact["loader"], expression=fact["expression"], call_expression=ast.unparse(node),
            call_ast_sha256=hashlib.sha256(ast.dump(node, include_attributes=False).encode()).hexdigest(),
            source_scope=source_scope,
            source_sha256=inspector.files[fact["path"]]["sha256"])
        if site not in result:
            result.append(site)
    return result


def _matches(site, call):
    evidence = call.get("evidence") or []
    return (site["unit"] == "file" and call.get("path") == site["path"] and call.get("line") == site["start"]
        and call.get("target") == site["target"] and call.get("call_ast_sha256") == site["call_ast_sha256"]
        and call.get("call_expression") == site["call_expression"]
        and any(item.get("path") == site["path"] and item.get("unit", "file") == site["unit"]
            and item.get("start") == site["start"] and item.get("end") == site["end"] for item in evidence))


def resource_scope(inspector, entry, row, candidate=None):
    """Prove non-use only from a complete finite trace and exact absent sites."""
    result = dict(schema="source-resource-scope/1", status="UNKNOWN", entry_id=entry.get("id"),
        dependency_id=row.get("id"), declared_entry_ids=copy.deepcopy(row.get("entry_ids", [])),
        sites=[], source_evidence=[], trace_status="UNKNOWN", negative_scope_status="UNKNOWN", negative_scope_gaps=[], reason=None,
        scope="one frozen entry/configuration only; candidate declaration and required flag remain unchanged")
    try:
        if row.get("kind") != "weight" or row.get("configurable") is not False:
            raise ValueError("source non-use rule covers fixed weight rows only")
        if _reference(entry, row.get("id")):
            raise ValueError("entry contains an explicit reference to this dependency")
        sites = _sites(inspector, row)
        trace = resolve_resource_calls(inspector, entry, candidate)
        result.update(sites=sites, source_evidence=trace["branch_evidence"], trace_status=trace["status"],
            negative_scope_status=trace.get("negative_scope_status"), negative_scope_gaps=trace.get("negative_scope_gaps", []))
        if (trace["status"] != "CHECKED" or trace.get("unresolved_targets")
                or trace.get("negative_scope_status") != "CHECKED"):
            raise ValueError("finite entry trace is not completely checked")
        reached = [site for site in sites if any(_matches(site, call) for call in trace["calls"])]
        if reached:
            result.update(status="USED", reason="the entry trace reaches a cited original model source site")
        elif any(site["source_scope"] != "CALLABLE" or site["unit"] != "file" for site in sites):
            raise ValueError("absence from the callable trace cannot exclude module/class initialization or other source units")
        else:
            result.update(status="NOT_USED", reason="all exact cited model sites are absent from this complete finite entry trace")
    except (ValueError, TypeError, KeyError, OSError, SyntaxError) as error:
        result["reason"] = str(error)
    return result


def _value(call, name, default):
    keywords = call.get("kwargs") or {}
    if name not in keywords:
        return default
    item = keywords[name]
    if type(item) is not dict or item.get("status") != "CHECKED" or "value" not in item:
        raise ValueError("original loader parameter is not a checked literal: " + name)
    return item["value"]


def _bound_call(call, spec):
    if call.get("path_condition_status") != "CHECKED" or call.get("extra_arguments") != "CLOSED":
        raise ValueError("original loader path/extra argument domain is not closed")
    args, keywords = call.get("args"), call.get("kwargs")
    if type(args) is not list or type(keywords) is not dict:
        raise ValueError("original loader argument trace is unavailable")
    accepted = {"pretrained_model_name_or_path", "cache_dir", "revision", "local_files_only", "device_map"}
    unsupported = set(keywords) - accepted
    if unsupported:
        raise ValueError("loader cache semantics are unsupported for parameters: " + ", ".join(sorted(unsupported)))
    if len(args) > 1 or bool(args) and "pretrained_model_name_or_path" in keywords:
        raise ValueError("model loader has ambiguous/additional positional arguments")
    if args:
        model = args[0]
        if type(model) is not dict or model.get("status") != "CHECKED" or "value" not in model:
            raise ValueError("original model identifier is not a checked literal")
        model = model["value"]
    else:
        model = _value(call, "pretrained_model_name_or_path", None)
    if type(model) is not str or model != spec["repo_id"]:
        raise ValueError("original model identifier differs from the declared Hub repository")
    cache = _value(call, "cache_dir", None)
    if type(cache) is not str or cache != spec["cache_dir"]:
        raise ValueError("original cache_dir is not the exact declared backing cache; environment defaults are unproved")
    revision = _value(call, "revision", None)
    ref = "main" if revision is None or revision == "main" else revision
    if (type(ref) is not str or ref != spec["ref"] or ref != "main" and
            (not _COMMIT.fullmatch(ref) or ref != spec["revision"])):
        raise ValueError("original revision is not the declared main ref or exact fixed commit")
    local = _value(call, "local_files_only", False)
    if type(local) is not bool:
        raise ValueError("original local_files_only is not a checked boolean")
    if "device_map" in keywords and _value(call, "device_map", None) != "auto":
        raise ValueError("only the checked original device_map='auto' option is covered by this cache binding rule")
    return dict(repo_id=model, cache_dir=cache, revision=revision, ref=ref, local_files_only=local,
                require_offline=not local)


def prove_hub_cache_binding(inspector, entry, row, candidate=None):
    """Check original Hub values against a backing-cache declaration only.

    The separate cache checker must validate its spec and actual file/SDK/offline
    state. Even CHECKED here grants no loader, source execution or lifecycle
    permission, and does not establish that required_files is complete.
    """
    result = dict(schema="source-fixed-hub-call-proof/1", rule=RULE, status="MISSING",
        entry_id=entry.get("id"), dependency_id=row.get("id"), require_offline=True,
        calls=[], source_evidence=[], issues=[], reason=None, trace_status="UNKNOWN",
        negative_scope_status="UNKNOWN", negative_scope_gaps=[],
        scope="original loader values and declared backing cache only; no argument override, cache availability, completeness or execution permission")
    try:
        placement = row.get("placement") or {}
        spec = placement.get("hub_cache")
        if row.get("kind") != "weight" or row.get("configurable") is not False or row.get("mappings"):
            raise ValueError("fixed Hub source proof requires a fixed weight row without parameter overrides")
        if (type(spec) is not dict or spec.get("schema") != "fixed-hub-cache/1"
                or type(spec.get("repo_id")) is not str or not spec["repo_id"]
                or type(spec.get("cache_dir")) is not str or not spec["cache_dir"]
                or type(spec.get("revision")) is not str or not _COMMIT.fullmatch(spec["revision"])
                or spec.get("ref") not in ("main", spec["revision"])):
            raise ValueError("fixed Hub cache declaration lacks its finite source-comparison fields")
        sites = _sites(inspector, row)
        trace = resolve_resource_calls(inspector, entry, candidate)
        result["source_evidence"] = trace["branch_evidence"]
        result.update(trace_status=trace["status"], negative_scope_status=trace.get("negative_scope_status"),
                      negative_scope_gaps=trace.get("negative_scope_gaps", []))
        if (trace["status"] != "CHECKED" or trace.get("unresolved_targets")
                or trace.get("negative_scope_status") != "CHECKED"):
            raise ValueError("finite entry trace is not completely checked")
        reached = [(site, call) for site in sites for call in trace["calls"] if _matches(site, call)]
        if not reached:
            raise ValueError("the checked entry trace does not reach a complete cited original model call")
        for site, call in reached:
            if not (site["target"].startswith("transformers.") and site["target"].endswith(".from_pretrained")):
                raise ValueError("original model call has no supported Transformers fixed-cache binding protocol")
            values = _bound_call(call, spec)
            result["calls"].append(dict(site=site, original_values=values,
                branch_conditions=call["branch_conditions"], status="CHECKED"))
        result.update(status="CHECKED", require_offline=any(call["original_values"]["require_offline"] for call in result["calls"]))
    except (ValueError, TypeError, KeyError, OSError, SyntaxError) as error:
        result["reason"] = str(error)
        result["issues"].append(dict(issue_code="resource_locator_unsupported", reason=str(error)))
    return result
