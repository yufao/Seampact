"""Independent candidate completeness checks; no source execution or admission."""

import ast

from .inspection import units


def _function(inspector, entry):
    tree = ast.parse(dict(units(entry["path"], inspector.data(entry["path"])))[entry.get("unit", "file")])
    node = tree
    for part in entry["symbol"].split("."):
        matches = [n for n in getattr(node, "body", []) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                   and n.name == part]
        if len(matches) != 1:
            return None
        node = matches[0]
    return node if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) else None


def dictionary_requirements(inspector, entry):
    """Unconditional literal-key reads of explicit original dictionary arguments.

    Branches, computed keys and optional get() reads are not promoted to required
    keys. A single-assignment prefix alias preserves the original parameter path.
    """
    if entry.get("kind") not in {"function", "method"} or not hasattr(inspector, "data"):
        return []
    node = _function(inspector, entry)
    if node is None:
        return []
    known = {a.arg: (a.arg, ()) for a in node.args.posonlyargs + node.args.args + node.args.kwonlyargs}
    writes = {}
    for n in ast.walk(node):
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            writes[n.id] = writes.get(n.id, 0) + 1
    result = []

    def origin(value):
        if isinstance(value, ast.Name):
            return known.get(value.id)
        if isinstance(value, ast.Subscript) and isinstance(value.slice, ast.Constant) and isinstance(value.slice.value, str):
            base = origin(value.value)
            if base:
                return base[0], base[1] + (value.slice.value,)
        return None

    def immediate(value):
        if isinstance(value, (ast.Lambda, ast.GeneratorExp, ast.ListComp, ast.SetComp, ast.DictComp)):
            return
        yield value
        if isinstance(value, ast.IfExp):
            yield from immediate(value.test)  # Neither conditional value is mandatory.
            return
        for child in ast.iter_child_nodes(value):
            yield from immediate(child)

    for statement in node.body:
        # Only definitely executed straight-line reads. Conditional reads remain
        # for the original code to validate, not a fabricated mandatory config.
        if isinstance(statement, (ast.If, ast.For, ast.While, ast.Try, ast.With, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            for n in ast.walk(statement):
                if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
                    known.pop(n.id, None)
            continue
        for value in immediate(statement):
            if isinstance(value, ast.Subscript) and isinstance(value.ctx, ast.Load):
                path = origin(value)
                if path and path[1] and not writes.get(path[0]):
                    result.append(dict(parameter=path[0], keys=list(path[1]), path=entry["path"],
                        unit=entry.get("unit", "file"), start=value.lineno, end=value.end_lineno,
                        status="SOURCE_FACT", scope="unconditional literal-key read"))
        if isinstance(statement, (ast.Assign, ast.AnnAssign)):
            value = origin(statement.value)
            targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    if value and writes.get(target.id) == 1:
                        known[target.id] = value
                    else:
                        known.pop(target.id, None)
        if isinstance(statement, (ast.Return, ast.Raise)):
            break
    return result


def candidate_issues(inspector, entry, dependencies=()):
    result = []

    def issue(code, reason, fields, evidence=()):
        result.append(dict(issue_code=code, reason=reason, field_paths=fields, evidence=list(evidence),
            repair_action="按原定义补齐候选；使用用户配置或原默认值，不猜测数值、凭据或生成参数。"))

    try:
        requirements = dictionary_requirements(inspector, entry)
        values = entry.get("fixed_arguments") or {}
        for requirement in requirements:
            parameter = requirement["parameter"]
            if parameter not in values or not isinstance(values[parameter], dict):
                continue
            current = values[parameter]
            for key in requirement["keys"]:
                if isinstance(current, dict) and "$dependency" in current:
                    break  # Runtime resource leaf is not a configuration object.
                if not isinstance(current, dict) or key not in current:
                    field = "fixed_arguments." + parameter + "." + ".".join(requirement["keys"])
                    if not any(field in row["field_paths"] for row in result):
                        issue("configuration_incomplete", "原函数必读的配置键缺失：" + parameter + "." + ".".join(requirement["keys"]),
                              [field], [requirement])
                    break
                current = current[key]
        # Check every literal resource reference where it actually occurs, before
        # a missing selection can hide a wrong destination or a missing mapping.
        def walk(value, trail):
            if isinstance(value, dict):
                if "$dependency" in value:
                    ref = value["$dependency"]
                    if isinstance(ref, dict):
                        yield trail, ref
                else:
                    for key, child in value.items():
                        yield from walk(child, trail + [key])
            elif isinstance(value, list):
                for i, child in enumerate(value):
                    yield from walk(child, trail + [i])
        for location, path in (("entry.fixed_arguments", ["fixed_arguments"]),
                               ("environment", ["environment"]), ("constructor.arguments", ["constructor", "arguments"])):
            value = entry
            for key in path:
                value = value.get(key, {}) if isinstance(value, dict) else {}
            for trail, ref in walk(value, []):
                dep = next((d for d in dependencies if d.get("id") == ref.get("id")), None)
                if dep is None:
                    if dependencies:
                        issue("candidate_incomplete", "资源引用没有对应的需求声明。", [".".join(path)])
                    continue
                matches = [m for m in dep.get("mappings", []) if m.get("entry_id") == entry.get("id") and
                           m.get("location") == location and trail and m.get("parameter") == trail[0] and
                           m.get("parameter_path", []) == trail[1:] and m.get("field") == ref.get("field")]
                if not matches:
                    declared = [m for m in dep.get("mappings", []) if m.get("entry_id") == entry.get("id")]
                    if declared:
                        issue("dependency_destination_mismatch", "资源引用的位置与声明的映射不一致。",
                              [".".join(str(k) for k in path + trail)], dep.get("evidence", []))
    except (ValueError, KeyError, TypeError, SyntaxError, OSError):
        # Source/shape errors are owned by the existing anchor and native checks.
        pass
    return result
