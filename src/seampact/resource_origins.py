"""Finite, source-only resource facts and post-construction initialization gaps.

Facts describe original syntax, never resource availability or a download plan.
No source is imported and no Hub identifier is turned into a filesystem path.
"""

import ast
import importlib.util
import re
from pathlib import PurePosixPath

from .inspection import units


def _sources(inspector, paths=None):
    for path in paths if paths is not None else inspector.files:
        if not path.endswith((".py", ".ipynb")):
            continue
        for cell, source in units(path, inspector.data(path)):
            try:
                yield path, cell, ast.parse(source)
            except SyntaxError:
                continue


def _module(path):
    parts = list(PurePosixPath(path.replace("\\", "/")).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _imports(statement, bindings, path):
    if isinstance(statement, ast.Import):
        for alias in statement.names:
            bindings[alias.asname or alias.name.split(".")[0]] = alias.name if alias.asname else alias.name.split(".")[0]
    elif isinstance(statement, ast.ImportFrom):
        module = statement.module or ""
        if statement.level:
            package = _module(path) if path.endswith("/__init__.py") else _module(path).rpartition(".")[0]
            try:
                module = importlib.util.resolve_name("." * statement.level + module, package)
            except (ValueError, ImportError):
                return
        for alias in statement.names:
            if alias.name != "*":
                bindings[alias.asname or alias.name] = module + "." + alias.name


def _qualified(expression, bindings):
    if isinstance(expression, ast.Name):
        return bindings.get(expression.id)
    if isinstance(expression, ast.Attribute):
        parent = _qualified(expression.value, bindings)
        return parent + "." + expression.attr if parent else None
    return None


def _location(path, cell, node):
    return dict(path=path, unit=cell, start=node.lineno, end=node.end_lineno)


def _literal(expression):
    try:
        value = ast.literal_eval(expression)
        return value if isinstance(value, str) else None
    except (ValueError, TypeError):
        return None


def _explicit_path(value):
    return bool(value and (value.startswith((".", "/", "\\", "~")) or re.match(r"^[A-Za-z]:[\\/]", value)))


def _written_names(node):
    return {value.id for value in ast.walk(node) if isinstance(value, ast.Name)
            and isinstance(value.ctx, (ast.Store, ast.Del))}


def _bound_calls(body, path, inherited):
    """Walk lexical statements without treating a shadowed import as a loader."""
    bindings = dict(inherited)
    for statement in body:
        _imports(statement, bindings, path)
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            local = {name: value for name, value in bindings.items() if name not in _written_names(statement)}
            parameters = [*statement.args.posonlyargs, *statement.args.args, *statement.args.kwonlyargs]
            parameters += [value for value in (statement.args.vararg, statement.args.kwarg) if value is not None]
            for parameter in parameters:
                local.pop(parameter.arg, None)
            yield from _bound_calls(statement.body, path, local)
            bindings.pop(statement.name, None)
            continue
        if isinstance(statement, ast.ClassDef):
            yield from _bound_calls(statement.body, path, bindings)
            bindings.pop(statement.name, None)
            continue
        for field in ("body", "orelse", "finalbody"):
            values = getattr(statement, field, None)
            if isinstance(values, list):
                yield from _bound_calls(values, path, bindings)
        if isinstance(statement, ast.Try):
            for handler in statement.handlers:
                yield from _bound_calls(handler.body, path, bindings)
        # Expressions directly owned by this statement, excluding nested bodies.
        for field, value in ast.iter_fields(statement):
            if field in {"body", "orelse", "finalbody", "handlers"}:
                continue
            for expression in value if isinstance(value, list) else [value]:
                if isinstance(expression, ast.AST):
                    for call in (node for node in ast.walk(expression) if isinstance(node, ast.Call)):
                        yield call, _qualified(call.func, bindings)
        if not isinstance(statement, (ast.Import, ast.ImportFrom)):
            for name in _written_names(statement):
                bindings.pop(name, None)


def _resource_call(call, qualified):
    if not qualified:
        return None
    keywords = {keyword.arg: keyword.value for keyword in call.keywords}
    if qualified in {"torch.load", "safetensors.torch.load_file", "safetensors.safe_open", "joblib.load"}:
        return "checkpoint", "local_file", next(iter(call.args), keywords.get("f", keywords.get("filename")))
    if qualified in {"huggingface_hub.snapshot_download", "huggingface_hub.hf_hub_download"}:
        return "model", "hub_id", next(iter(call.args), keywords.get("repo_id"))
    if qualified.endswith(".from_pretrained") and qualified.split(".")[0] in {"transformers", "peft"}:
        position = 1 if qualified.startswith("peft.PeftModel") else 0
        expression = call.args[position] if len(call.args) > position else keywords.get("pretrained_model_name_or_path",
            keywords.get("model_id", keywords.get("model_name_or_path")))
        return "model", "hub_or_local", expression
    if qualified == "vllm.LLM":
        return "model", "hub_or_local", next(iter(call.args), keywords.get("model"))
    return None


def _literal_candidates(tree, call, expression, accepted, evidence, path, cell):
    """Cited same-scope assignments, without choosing branches or resolving names.

    Only direct string assignments and enclosing if tests are retained. Other
    control constructs, enclosing captures and global/nonlocal writes remain
    outside this small syntax-fact rule.
    """
    if not isinstance(expression, ast.Name):
        return []
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    lexical = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda,
               ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)

    def scope(node):
        current = parents.get(node)
        while current is not None and not isinstance(current, lexical):
            current = parents.get(current)
        return current

    def covered(node):
        return any(item["path"] == path and item.get("unit", "file") == cell and
                   item["start"] <= node.lineno and node.end_lineno <= item["end"] for item in evidence)

    owner = scope(call)
    if owner is None:
        return []
    nodes = [node for node in ast.walk(owner) if scope(node) is owner]
    if any(isinstance(node, (ast.Global, ast.Nonlocal)) and expression.id in node.names for node in nodes):
        return []
    result = []
    for statement in nodes:
        if not isinstance(statement, (ast.Assign, ast.AnnAssign)) or statement.value is None:
            continue
        targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
        if not any(isinstance(target, ast.Name) and target.id == expression.id for target in targets):
            continue
        value = _literal(statement.value)
        if value is None or not covered(statement) or (statement.end_lineno, statement.end_col_offset) >= (call.lineno, call.col_offset):
            continue
        conditions, current, child = [], parents.get(statement), statement
        supported = True
        while current is not owner:
            if isinstance(current, ast.If):
                if not covered(current.test):
                    supported = False
                    break
                conditions.append(dict(**_location(path, cell, current.test), expression=ast.unparse(current.test),
                    branch="body" if child in current.body else "orelse",
                    conditional_range=_location(path, cell, current)))
            elif isinstance(current, ast.stmt):
                supported = False
                break
            child, current = current, parents.get(current)
        if supported:
            result.append(dict(**_location(path, cell, statement), expression=ast.unparse(statement.value), literal=value,
                source_kind="local_path" if accepted == "local_file" or accepted == "hub_or_local" and _explicit_path(value) else accepted,
                conditions=list(reversed(conditions)), status="SOURCE_ASSIGNMENT_CANDIDATE", selection="NOT_INFERRED",
                lexical_scope=dict(kind=type(owner).__name__, name=getattr(owner, "name", None),
                    start=getattr(owner, "lineno", 1), end=getattr(owner, "end_lineno",
                        max((node.end_lineno for node in tree.body), default=1)))))
    return sorted(result, key=lambda item: (item["start"], item["end"]))


def resource_origin_facts(inspector, evidence):
    """Return typed original loading/cache syntax inside complete evidence ranges.

    Dynamic identifiers stay unknown. ``accepted_origins`` records the original
    loader's input category, not an inferred value. Cache directories are separate
    facts and cannot satisfy a model/checkpoint requirement.
    """
    result = []
    paths = list(dict.fromkeys(item["path"] for item in evidence))
    for path, cell, tree in _sources(inspector, paths):
        for call, loader in _bound_calls(tree.body, path, {}):
            if not any(item["path"] == path and item.get("unit", "file") == cell and
                       item["start"] <= call.lineno and call.end_lineno <= item["end"] for item in evidence):
                continue
            loading = _resource_call(call, loader)
            if loading is None:
                continue
            role, accepted, expression = loading
            value = _literal(expression) if expression is not None else None
            kind = ("local_path" if accepted == "local_file" and value is not None or
                    accepted == "hub_or_local" and _explicit_path(value) else
                    accepted if value is not None else "unknown")
            fact = dict(**_location(path, cell, call), loader=loader, role=role,
                expression=ast.unparse(expression) if expression is not None else None,
                literal=value, source_kind=kind, accepted_origins=accepted,
                status="SOURCE_FACT", binding="IMPORT_SYNTAX_ONLY",
                missing=("dynamic resource expression is not resolved by this finite source fact rule" if value is None else
                         "original loader accepts a Hub identifier or local directory; no availability was checked" if kind == "hub_or_local" else
                         "resource availability was not checked"))
            if value is None:
                fact["literal_candidates"] = _literal_candidates(tree, call, expression, accepted, evidence, path, cell)
            result.append(fact)
            for keyword in call.keywords:
                if keyword.arg in {"cache_dir", "download_dir", "local_dir"}:
                    result.append(dict(**_location(path, cell, keyword.value), loader=loader, role="cache" if keyword.arg != "local_dir" else "download_destination",
                        expression=ast.unparse(keyword.value), literal=_literal(keyword.value),
                        source_kind="cache_directory" if keyword.arg != "local_dir" else "local_directory",
                        parameter=keyword.arg, accepted_origins="directory", status="SOURCE_FACT", binding="IMPORT_SYNTAX_ONLY",
                        missing="directory configuration does not prove that the model/checkpoint is present"))
    return sorted(result, key=lambda row: (row["path"], row["unit"], row["start"], row["role"]))


def initialization_gaps(inspector, entry):
    """Find visible initialization of this exact class occurrence outside __init__.

    A direct constructor binding has no source occurrence linking it to these
    operations. Require the existing original-prefix binding instead. Branches,
    object aliases and reassignments are tracked only within visible AST blocks;
    this does not claim complete interprocedural discovery.
    """
    if entry.get("kind") != "method" or entry.get("binding_program") is not None:
        return []
    symbol = entry.get("symbol", "").split(".")
    if len(symbol) != 2:
        return []
    target_path, target_cell = entry["path"], entry.get("unit", "file")
    sources = list(_sources(inspector))
    target = next((tree for path, cell, tree in sources if (path, cell) == (target_path, target_cell)), None)
    if target is None:
        return []
    owner = next((node for node in target.body if isinstance(node, ast.ClassDef) and node.name == symbol[0]), None)
    method = next((node for node in getattr(owner, "body", []) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and node.name == symbol[1]), None)
    arguments = list(method.args.posonlyargs) + list(method.args.args) if method else []
    if not arguments or not any(isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                               and node.value.id == arguments[0].arg for node in ast.walk(method)):
        return []
    modules = {_module(target_path)}
    if _module(target_path).startswith("src."):
        modules.add(_module(target_path)[4:])
    class_names = {module + "." + symbol[0] for module in modules}
    profiles = []
    operations = {"load_state_dict", "to", "cuda", "cpu", "eval", "train"}

    def preserved_by_initializers(profile):
        declared = entry.get("initializers", [])
        if len(declared) != len(profile["operations"]):
            return False
        for supplied, operation in zip(declared, profile["operations"]):
            if supplied.get("symbol") not in {operation["method"], symbol[0] + "." + operation["method"]}:
                return False
            original = next((node for node in owner.body if isinstance(node, ast.FunctionDef)
                             and node.name == operation["method"]), None)
            if original is None:
                return False
            call = ast.parse(operation["expression"], mode="eval").body
            parameters = [*original.args.posonlyargs, *original.args.args][1:]
            if len(call.args) > len(parameters) or any(keyword.arg is None for keyword in call.keywords):
                return False
            values = dict(zip((parameter.arg for parameter in parameters), call.args))
            for keyword in call.keywords:
                if keyword.arg in values:
                    return False
                values[keyword.arg] = keyword.value
            try:
                values = {name: ast.literal_eval(value) for name, value in values.items()}
            except (ValueError, TypeError):
                return False
            configured = supplied.get("arguments", {})
            if type(configured) is not dict or configured.keys() != values.keys():
                return False
            if any(type(configured[name]) is not type(value) or configured[name] != value for name, value in values.items()):
                return False
        return True

    def block(body, path, cell, inherited, inherited_objects=None):
        bindings, objects = dict(inherited), dict(inherited_objects or {})
        for statement in body:
            _imports(statement, bindings, path)
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                local = dict(bindings)
                for node in ast.walk(statement):
                    if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
                        local.pop(node.id, None)
                parameters = [*statement.args.posonlyargs, *statement.args.args, *statement.args.kwonlyargs]
                parameters += [value for value in (statement.args.vararg, statement.args.kwarg) if value is not None]
                for parameter in parameters:
                    local.pop(parameter.arg, None)
                block(statement.body, path, cell, local)
                bindings.pop(statement.name, None)
                continue
            if isinstance(statement, ast.ClassDef):
                bindings[statement.name] = _module(path) + "." + statement.name
                continue
            # Nested control blocks keep their lexical import/object context,
            # without merging branch-specific assignments into later code.
            for field in ("body", "orelse", "finalbody"):
                children = getattr(statement, field, None)
                if isinstance(children, list):
                    block(children, path, cell, bindings, objects)
            if isinstance(statement, ast.Try):
                for handler in statement.handlers:
                    block(handler.body, path, cell, bindings, objects)
            if any(isinstance(getattr(statement, field, None), list) for field in ("body", "orelse", "finalbody")):
                for name in _written_names(statement):
                    objects.pop(name, None)
                    bindings.pop(name, None)
            calls = ast.walk(statement.value) if isinstance(statement, (ast.Assign, ast.AnnAssign, ast.Expr, ast.Return)) and statement.value is not None else ()
            for call in (node for node in calls if isinstance(node, ast.Call)):
                function = call.func
                if isinstance(function, ast.Attribute) and isinstance(function.value, ast.Name) and function.attr in operations:
                    record = objects.get(function.value.id)
                    if record is not None and not record["used"]:
                        operation = dict(**_location(path, cell, call), method=function.attr, expression=ast.unparse(call))
                        record["operations"].append(operation)
                else:
                    # The first ordinary use ends this visible initialization
                    # prefix. Later training/request mutations are not setup.
                    names = {node.id for argument in call.args for node in ast.walk(argument) if isinstance(node, ast.Name)}
                    names.update(node.id for keyword in call.keywords for node in ast.walk(keyword.value) if isinstance(node, ast.Name))
                    if isinstance(function, ast.Attribute) and isinstance(function.value, ast.Name):
                        names.add(function.value.id)
                    for name in names:
                        if name in objects:
                            objects[name]["used"] = True
            if isinstance(statement, (ast.Assign, ast.AnnAssign)):
                value = statement.value
                record = None
                if isinstance(value, ast.Call) and _qualified(value.func, bindings) in class_names:
                    record = dict(construction=_location(path, cell, value), operations=[], used=False)
                    profiles.append(record)
                elif isinstance(value, ast.Name):
                    record = objects.get(value.id)
                for assigned in (statement.targets if isinstance(statement, ast.Assign) else [statement.target]):
                    for name in (node.id for node in ast.walk(assigned) if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)):
                        bindings.pop(name, None)
                        objects.pop(name, None)
                        if isinstance(assigned, ast.Name) and record is not None:
                            objects[name] = record
            elif isinstance(statement, (ast.Delete, ast.AugAssign)):
                for node in ast.walk(statement):
                    if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
                        objects.pop(node.id, None)
                        bindings.pop(node.id, None)

    for path, cell, tree in sources:
        block(tree.body, path, cell, {})
    gaps = []
    for profile in profiles:
        if not profile["operations"] or preserved_by_initializers(profile):
            continue
        gaps.append(dict(code="initialization_incomplete", status="MISSING", entry_id=entry.get("id"),
            owner=dict(path=target_path, unit=target_cell, symbol=symbol[0]),
            construction=profile["construction"], operations=profile["operations"],
            reason="direct constructor binding does not preserve source post-construction initialization; bind the complete original B-CALL prefix where supported",
            scope="finite visible construction/alias/import syntax; not complete interprocedural initialization discovery"))
    return gaps
