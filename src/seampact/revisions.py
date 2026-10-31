"""Revision materials only: frozen diffs and independently parsed structural facts."""

import ast
import hashlib
import json
import re
import sys
from pathlib import Path

from . import inspection


EXTRACTOR_VERSION = "source-fact/1"


def select_candidate_entries(candidate, selected_ids):
    """Select exported entries only; leave original code and boundaries intact."""
    import copy
    if not isinstance(selected_ids, list) or any(not isinstance(value, str) for value in selected_ids):
        raise ValueError("保留入口需要字符串 ID 列表")
    entries = candidate.get("entries")
    if not isinstance(entries, list) or any(not isinstance(entry, dict) or not isinstance(entry.get("id"), str)
                                           for entry in entries):
        raise ValueError("候选入口格式不完整，请先修正候选")
    available = [entry["id"] for entry in entries]
    if len(set(available)) != len(available) or len(set(selected_ids)) != len(selected_ids):
        raise ValueError("入口 ID 不能重复")
    if set(selected_ids) - set(available):
        raise ValueError("选择中包含当前候选不存在的入口，请刷新后重新选择")
    selected = set(selected_ids)
    result = copy.deepcopy(candidate)
    result["entries"] = [entry for entry in result["entries"] if entry["id"] in selected]
    if isinstance(result.get("runtime_groups"), list):
        groups = []
        for group in result["runtime_groups"]:
            if not isinstance(group, dict) or not isinstance(group.get("entries"), list):
                raise ValueError("运行组格式不完整，请先修正候选")
            group["entries"] = [identity for identity in group["entries"] if identity in selected]
            if group["entries"]:
                groups.append(group)
        result["runtime_groups"] = groups
    if isinstance(result.get("dependencies"), list):
        dependencies = []
        for dependency in result["dependencies"]:
            if not isinstance(dependency, dict):
                raise ValueError("依赖格式不完整，请先修正候选")
            scope = dependency.get("entry_ids")
            if isinstance(scope, list) and scope:
                dependency["entry_ids"] = [identity for identity in scope if identity in selected]
                if not dependency["entry_ids"]:
                    continue
            if isinstance(dependency.get("mappings"), list):
                dependency["mappings"] = [mapping for mapping in dependency["mappings"]
                    if not isinstance(mapping, dict) or mapping.get("entry_id") in selected]
            if selected:
                dependencies.append(dependency)
        result["dependencies"] = dependencies
    return result, dict(retained_ids=[identity for identity in available if identity in selected],
                        excluded_ids=[identity for identity in available if identity not in selected])


def _object(value, fields, label):
    if not isinstance(value, dict) or set(value) - set(fields):
        raise ValueError(label + "需要对象且只能包含约定字段")
    return value


def _text(value, limit, label):
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError(label + "需要不超过 " + str(limit) + " 字的文字")
    return value.strip()


def _choice(value, choices, label):
    if not isinstance(value, str) or value not in choices:
        raise ValueError(label + "不在允许范围内")
    return value


def intervention(value=None, previous=None):
    """Declarations only: no inferred human origin or automatic rule acceptance."""
    value = _object({} if value is None else value,
        ("actor", "cause", "change_target", "summary", "evidence", "generalization"), "介入记录")
    previous = previous or {}
    actor = _object(value.get("actor", {}), ("kind", "name"), "介入者")
    general = _object(value.get("generalization", {}),
        ("disposition", "pattern_key", "target", "status", "resolution", "version", "evidence"), "共性问题")
    general = dict(previous.get("generalization") or {}, **general)
    result = dict(actor=dict(kind=_choice(actor.get("kind", "unknown"),
        {"user", "external_agent", "platform_model", "unknown"}, "介入者类型"),
        name=_text(actor.get("name", ""), 160, "介入者名称")))
    for field, choices in (
            ("cause", {"resource_configuration", "discovery_gap", "unsupported_rule", "checker_defect",
                       "build_defect", "semantic_mismatch", "unknown"}),
            ("change_target", {"configuration", "candidate", "source", "platform", "unknown"})):
        result[field] = _choice(value.get(field, previous.get(field, "unknown")), choices, field)
    for field in ("summary", "evidence"):
        result[field] = _text(value.get(field, previous.get(field, "")), 4000 if field == "summary" else 8000, field)
    result["generalization"] = dict(
        disposition=_choice(general.get("disposition", "needs_review"),
            {"case_specific", "needs_review", "generalizable"}, "共性处理范围"),
        target=_choice(general.get("target", "unknown"),
            {"analysis", "rule", "checker", "builder", "theory", "unknown"}, "共性处理去向"),
        status=_choice(general.get("status", "open"), {"open", "implemented", "verified"}, "共性处理状态"),
        **{field: _text(general.get(field, ""), limit, field) for field, limit in
           (("pattern_key", 160), ("resolution", 4000), ("version", 256), ("evidence", 8000))})
    general = result["generalization"]
    if general["disposition"] == "generalizable" and (not general["pattern_key"] or general["target"] == "unknown"):
        raise ValueError("通用问题需要 pattern_key 和明确处理去向")
    if general["status"] != "open":
        if general["target"] == "unknown" or not general["version"]:
            raise ValueError("共性问题结案声明需要明确处理去向和版本")
        required = "resolution" if general["status"] == "implemented" else "evidence"
        if not general[required]:
            raise ValueError("共性问题 " + general["status"] + " 声明需要 " + required)
    return result


def declared_result(value=None, previous=None):
    value = _object({} if value is None else value, ("status", "summary", "evidence"), "目标运行结果声明")
    value = dict(previous or {}, **value)
    return dict(status=_choice(value.get("status", "unknown"),
        {"unknown", "succeeded", "failed", "partial"}, "目标运行结果"),
        summary=_text(value.get("summary", ""), 4000, "结果说明"),
        evidence=_text(value.get("evidence", ""), 8000, "结果依据"))


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def candidate_diff(before, after):
    """JSON-pointer field paths; values (including resource secrets) are not returned."""
    result = dict(added=[], removed=[], changed=[])

    def visit(left, right, path=""):
        if isinstance(left, dict) and isinstance(right, dict):
            for key in sorted(left.keys() | right.keys()):
                child = path + "/" + str(key).replace("~", "~0").replace("/", "~1")
                if key not in left:
                    result["added"].append(child)
                elif key not in right:
                    result["removed"].append(child)
                else:
                    visit(left[key], right[key], child)
        elif isinstance(left, list) and isinstance(right, list):
            for index in range(max(len(left), len(right))):
                child = path + "/" + str(index)
                if index >= len(left):
                    result["added"].append(child)
                elif index >= len(right):
                    result["removed"].append(child)
                else:
                    visit(left[index], right[index], child)
        elif type(left) is not type(right) or left != right:
            result["changed"].append(path or "/")

    visit(before, after)
    result["counts"] = {key: len(paths) for key, paths in result.items()}
    return result


def source_diff(old_request, new_request):
    """Compare complete frozen manifests, without opening withheld source."""
    old = {item["path"]: item["sha256"] for item in old_request.get("files", [])}
    new = {item["path"]: item["sha256"] for item in new_request.get("files", [])}
    result = dict(added=sorted(new.keys() - old.keys()), removed=sorted(old.keys() - new.keys()),
        modified=sorted(path for path in old.keys() & new.keys() if old[path] != new[path]),
        unchanged=sorted(path for path in old.keys() & new.keys() if old[path] == new[path]))
    result["counts"] = {key: len(paths) for key, paths in result.items()}
    result.update(old_source_version=old_request.get("source_version"),
                  source_version=new_request.get("source_version"))
    withheld = [{item["path"] for item in request.get("withheld_sources", [])}
                for request in (old_request, new_request)]
    result["withheld"] = dict(added=sorted(withheld[1] - withheld[0]),
        removed=sorted(withheld[0] - withheld[1]), current=sorted(withheld[1]))
    coverage = [{item["path"]: {key: item.get(key) for key in ("kind", "readable", "status")}
                 for item in request.get("inventory", [])} for request in (old_request, new_request)]
    result["coverage"] = dict(inventory=candidate_diff(*coverage), before_files=len(old), after_files=len(new),
        dependency_closure="UNKNOWN", note="Unchanged files may be affected by new imports, registration or dynamic dependencies.")
    fields = ("entry", "observation", "configuration", "resources", "user_resources", "runtime_environment", "entry_profile")
    result["configuration"] = candidate_diff(*[{key: request.get(key) for key in fields}
                                               for request in (old_request, new_request)])
    return result


def _name(node):
    if isinstance(node, ast.Call):
        return _name(node.func)
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _name(node.value)
        return prefix + "." + node.attr if prefix else ""
    return ""


def _extract(inspector, path, remote):
    facts = dict(symbols=[], imports=[], calls=[], entry_candidates=[], registration_candidates=[],
                 unknown=["DYNAMIC_DEPENDENCIES_AND_REGISTRATION_NOT_CLOSED"], dependency_closure="UNKNOWN", parse_status="PARSED")
    if remote:
        # These operations parse frozen bytes in the selected interpreter. No source import/run.
        symbols = inspector.inspect_symbols(path)
        imports = inspector.inspect_imports(path=path)
        entries = inspector.entry_candidates(path=path)
        for answer, field in ((symbols, "symbols"), (imports, "imports"), (entries, "candidates")):
            if not isinstance(answer, dict) or not isinstance(answer.get(field), list) or answer.get("status") == "WITHHELD":
                raise ValueError("target source tool returned no structural fact list")
        if symbols.get("sha256") != inspector.files[path]["sha256"] or path not in imports.get("paths", []):
            raise ValueError("target source facts do not match frozen file")
        for item in symbols.get("symbols", []):
            facts["symbols"].append({key: item[key] for key in ("symbol", "unit", "line", "end_line") if key in item})
            if item.get("decorators"):
                facts["registration_candidates"].append(dict(unit=item.get("unit"), line=item.get("line"),
                    target=item.get("symbol"), kind="decorated_symbol_candidate"))
            for name in item.get("calls", []):
                if re.fullmatch(r"[\w.]+", name):
                    facts["calls"].append(dict(target=name, unit=item.get("unit"), line=item.get("line")))
        facts["imports"] = [{key: item[key] for key in ("unit", "line", "module", "names", "level", "kind") if key in item}
                            for item in imports.get("imports", []) if item.get("path") == path]
        facts["entry_candidates"] = [{key: item[key] for key in ("unit", "line", "kind") if key in item}
                                    for item in entries.get("candidates", []) if item.get("path") == path]
        facts["registration_candidates"].extend(dict(item, kind="top_level_registration_candidate")
            for item in facts["entry_candidates"] if item.get("kind") == "top_level_call")
        if any(item.get("unknown") or item.get("status") == "WITHHELD" for item in (symbols, imports, entries)):
            facts["unknown"].append("TARGET_PARSER_INCOMPLETE")
            facts["parse_status"] = "PARTIAL"
    elif path.lower().endswith((".py", ".pyi", ".ipynb")):
        for unit, text in inspection.units(path, inspector.data(path)):
            tree = inspection.native_ast(text, filename=path)

            def visit(node, prefix=""):
                for child in ast.iter_child_nodes(node):
                    if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                        name = prefix + child.name
                        facts["symbols"].append(dict(symbol=name, unit=unit, line=child.lineno,
                            end_line=child.end_lineno, kind=type(child).__name__, body_digest=_digest(ast.dump(child))))
                        for decorator in child.decorator_list:
                            facts["registration_candidates"].append(dict(unit=unit, line=decorator.lineno,
                                target=_name(decorator), kind="decorator"))
                        visit(child, name + ".")
                    else:
                        visit(child, prefix)
            visit(tree)
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    facts["imports"].append(dict(unit=unit, line=node.lineno, module=getattr(node, "module", None),
                        level=getattr(node, "level", 0), names=[alias.name for alias in node.names]))
                elif isinstance(node, ast.Call):
                    target = _name(node.func)
                    facts["calls"].append(dict(unit=unit, line=node.lineno, target=target or "<dynamic>"))
                elif isinstance(node, ast.If) and any(isinstance(n, ast.Name) and n.id == "__name__" for n in ast.walk(node.test)):
                    facts["entry_candidates"].append(dict(unit=unit, line=node.lineno, kind="name_guard_candidate"))
            facts["entry_candidates"].extend(dict(unit=unit, line=n.lineno, kind="main_callable", symbol=n.name)
                for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "main")
    else:
        facts["unknown"].append("FILE_PARSER_UNSUPPORTED")
        facts["parse_status"] = "UNSUPPORTED"
    facts["registration_candidates"].extend(item for item in facts["calls"]
        if any(word in item["target"].lower() for word in ("register", "registry", "add_route", "plugin")))
    return facts


def _parser(inspector):
    environment = inspector.request.get("runtime_environment") or {}
    target = getattr(inspector, "target_runtime", None)
    if target:
        record = target.record
        from .target_runtime import checker_identity
        if record.get("status") != "READY" or not record.get("image_id") or record.get("identity") != checker_identity(environment):
            raise ValueError("target parser identity unavailable or stale")
        frozen = inspector.request.get("target_runtime") or {}
        if any(frozen.get(key, record.get(key)) != record.get(key) for key in ("identity", "image_id")):
            raise ValueError("target parser differs from frozen request")
        return dict(kind="target", identity=record["identity"], image_id=record["image_id"]), True
    if environment or inspector.request.get("target_runtime"):
        raise ValueError("frozen target parser is not attached; platform parser cannot stand in for it")
    return dict(kind="platform", python=sys.version, implementation=sys.implementation.cache_tag,
                environment_digest=_digest(environment)), False


def build_review_context(old_inspector, new_inspector, store=None, home=None):
    """Cache facts, never checker permissions; return changed facts and related anchors."""
    owned = None
    if store is None and home is not None:
        from .store import Store
        owned = store = Store(Path(home) / "source-facts.sqlite")
    difference = source_diff(old_inspector.request if old_inspector else {}, new_inspector.request)
    changed = set(difference["added"] + difference["modified"])
    extractor = dict(version=EXTRACTOR_VERSION, digest=_digest([Path(__file__).read_text(encoding="utf-8"),
        Path(inspection.__file__).read_text(encoding="utf-8")]))
    reuse = dict(hits=0, created=0, failed=0, extractor=extractor)
    facts, references = [], []
    try:
        try:
            parser, remote = _parser(new_inspector)
            parser_error = False
        except ValueError:
            parser, remote, parser_error = dict(kind="unavailable"), False, True
        reuse["parser"] = parser
        reuse["unavailable_paths"] = []
        for path, record in new_inspector.files.items():
            if path in difference["withheld"]["current"]:
                continue
            key = _digest(dict(path=path, sha256=record["sha256"], extractor=extractor, parser=parser))
            cached = store.get("source_fact", key) if store and not parser_error else None
            if cached:
                reuse["hits"] += 1
            else:
                try:
                    if parser_error:
                        raise ValueError("parser unavailable")
                    value = _extract(new_inspector, path, remote)
                    cached = dict(path=path, sha256=record["sha256"], extractor=extractor, parser=parser, **value)
                    if store:
                        store.save("source_fact", key, cached)
                    reuse["created"] += 1
                except (ValueError, SyntaxError, UnicodeError, KeyError, TypeError, OSError):
                    reuse["failed"] += 1
                    reuse["unavailable_paths"].append(path)
                    cached = dict(path=path, sha256=record["sha256"], parse_status="UNAVAILABLE",
                        unknown=["STRUCTURAL_EXTRACTION_UNAVAILABLE"], dependency_closure="UNKNOWN")
            if path in changed:
                facts.append(cached)
            if cached.get("imports") or cached.get("entry_candidates") or cached.get("registration_candidates"):
                references.append(dict(path=path, fact_id=key, changed=path in changed,
                    kinds=[name for name in ("imports", "entry_candidates", "registration_candidates") if cached.get(name)]))
    finally:
        if owned:
            owned.close()
    return dict(source_diff=difference, facts=facts, related_candidates=references, reuse=reuse,
        changed_symbols=[dict(path=fact["path"], change="CHANGED_FILE_RECHECK", **symbol)
                         for fact in facts for symbol in fact.get("symbols", [])],
        coverage="FILE_LOCAL_FACTS_ONLY; dynamic dependencies/registration UNKNOWN; unchanged files may be affected",
        access="Full frozen source remains queryable through source_tools; add_source changes source_version and requires a fresh review.",
        permissions="NOT_REUSED; complete candidate requires current mechanical checks")
