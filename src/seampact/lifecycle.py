"""Finite lifecycle evidence from frozen source, never from candidate flags.

The closed-source subset permits synchronous Python owners with JSON-valued
construction state and read-only requests. It has no framework API summary:
an imported torch/transformers call remains UNKNOWN, including generate/eval.
The evidence is part of the group contract and is rebuilt in the worker.
"""

import ast
import hashlib
import json
from pathlib import PurePosixPath


RULE = "closed-python-owner-lifecycle/1"
OBLIGATIONS = ("request_isolation", "repeatable_initialization", "owner_end", "cleanup")
ITERATORS = frozenset({"enumerate", "range", "reversed", "zip"})
EXCEPTIONS = frozenset({"ValueError", "TypeError", "RuntimeError"})
BUILTINS = frozenset({"abs", "all", "any", "bool", "dict", "float", "int", "len",
    "list", "max", "min", "round", "sorted", "str", "sum", "tuple"}) | ITERATORS | EXCEPTIONS
ANNOTATIONS = frozenset({"str", "int", "bool", "float", "list", "dict", "tuple", "None"})


class LifecycleGap(ValueError):
    pass


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _literal(node, immutable=False):
    value = ast.literal_eval(node)
    def valid(item):
        if item is None or type(item) in (str, int, float, bool, bytes):
            return True
        if type(item) is tuple or (not immutable and type(item) is list):
            return all(valid(member) for member in item)
        if not immutable and type(item) is dict:
            return all(type(key) is str and not key.startswith("$") and valid(member)
                       for key, member in item.items())
        return False
    if not valid(value):
        raise LifecycleGap("construction/default/global value is outside the finite primitive subset")
    return value


class ClosedSource:
    """Check module initialization, reachable calls and unmodified native owners."""

    def __init__(self, inspector):
        self.inspector = inspector
        self.paths, self.modules, self.coverage, self.calls, self.checked = {}, {}, {}, [], set()
        self.active = set()
        for path in inspector.files:
            if not path.endswith(".py"):
                continue
            parts = list(PurePosixPath(path.replace("\\", "/")).with_suffix("").parts)
            package = parts[-1] == "__init__"
            if package:
                parts.pop()
            if parts and all(part.isidentifier() for part in parts):
                for name in (".".join(parts), ".".join(parts[1:]) if parts[0] == "src" else None):
                    if name:
                        self.paths[name] = (path, package)

    @staticmethod
    def fail(message, node=None):
        raise LifecycleGap(message + (" at line " + str(node.lineno) if node is not None else ""))

    def annotation(self, node):
        if node is None:
            return
        if isinstance(node, ast.Constant) and (node.value is None or isinstance(node.value, str)):
            return  # String annotations are not evaluated by Python.
        if isinstance(node, ast.Name) and node.id in ANNOTATIONS:
            return
        if isinstance(node, ast.Subscript):
            self.annotation(node.value)
            for member in node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]:
                self.annotation(member)
            return
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            self.annotation(node.left)
            self.annotation(node.right)
            return
        self.fail("evaluated annotation needs a lifecycle summary", node)

    def definition(self, node):
        if node.decorator_list or node.args.vararg or node.args.kwarg:
            self.fail("decorated/variadic callable needs a lifecycle summary", node)
        if getattr(node, "type_params", None):
            self.fail("type-parameter scope needs a lifecycle summary", node)
        for parameter in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]:
            self.annotation(parameter.annotation)
        self.annotation(node.returns)
        for default in [*node.args.defaults, *[value for value in node.args.kw_defaults if value is not None]]:
            _literal(default)

    def module(self, name):
        if name in self.modules:
            return self.modules[name]
        if name not in self.paths:
            self.fail("external import has no version-bound lifecycle/effect summary: " + name)
        path, package = self.paths[name]
        data = self.inspector.data(path)
        sha256 = hashlib.sha256(data).hexdigest()
        if sha256 != self.inspector.files[path]["sha256"]:
            self.fail("source digest differs from frozen lifecycle input: " + path)
        tree = ast.parse(data, filename=path)
        record = dict(path=path, namespace={}, imports=[])
        self.modules[name] = record
        self.coverage.setdefault(path, dict(path=path, sha256=sha256, modules=[]))
        # Package initializers execute before the selected module.
        for index in range(1, len(name.split("."))):
            parent = ".".join(name.split(".")[:index])
            if parent in self.paths:
                self.module(parent)
        namespace = record["namespace"]
        def bind(symbol, value):
            if symbol in namespace or symbol in BUILTINS or symbol in ANNOTATIONS:
                self.fail("rebound global/builtin needs a lifecycle summary: " + symbol)
            namespace[symbol] = value
        for statement in tree.body:
            if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant) and isinstance(statement.value.value, str):
                continue
            if isinstance(statement, ast.ImportFrom):
                if statement.module == "__future__" and statement.level == 0 and all(
                        alias.name == "annotations" for alias in statement.names):
                    if "__future__" in self.paths:
                        self.fail("shadowed future import has no lifecycle/effect summary", statement)
                    record["imports"].append(dict(module="__future__", summary="python-future-annotations/1"))
                    continue
                segments = name.split(".") if package else name.split(".")[:-1]
                if statement.level:
                    if statement.level > len(segments):
                        self.fail("relative import leaves frozen package", statement)
                    base = ".".join(segments[:len(segments) - statement.level + 1] +
                                    ([statement.module] if statement.module else []))
                else:
                    base = statement.module or ""
                imported = self.module(base)
                record["imports"].append(dict(module=base, path=imported["path"]))
                for alias in statement.names:
                    if alias.name == "*":
                        self.fail("star import needs a lifecycle summary", statement)
                    member = imported["namespace"].get(alias.name)
                    if member is None and base + "." + alias.name in self.paths:
                        child = base + "." + alias.name
                        self.module(child)
                        member = ("module", child)
                    if member is None:
                        self.fail("import binding is not established: " + base + "." + alias.name, statement)
                    bind(alias.asname or alias.name, member)
            elif isinstance(statement, ast.Import):
                for alias in statement.names:
                    if "." in alias.name and not alias.asname:
                        self.fail("dotted import without alias needs a lifecycle binding", statement)
                    imported = self.module(alias.name)
                    record["imports"].append(dict(module=alias.name, path=imported["path"]))
                    bind(alias.asname or alias.name, ("module", alias.name))
            elif isinstance(statement, ast.FunctionDef):
                self.definition(statement)
                bind(statement.name, ("function", name, statement))
            elif isinstance(statement, ast.ClassDef):
                if (statement.bases or statement.keywords or statement.decorator_list or getattr(statement, "type_params", None)):
                    self.fail("inherited/decorated/metaclass owner needs a lifecycle summary", statement)
                methods = {}
                for member in statement.body:
                    if isinstance(member, ast.Expr) and isinstance(member.value, ast.Constant) and isinstance(member.value.value, str):
                        continue
                    if not isinstance(member, ast.FunctionDef) or (member.name.startswith("__") and member.name != "__init__"):
                        self.fail("owner has implicit dispatch/destruction or untracked class state", member)
                    self.definition(member)
                    if member.name in methods:
                        self.fail("owner method is rebound", member)
                    methods[member.name] = member
                bind(statement.name, ("class", name, methods))
            elif isinstance(statement, (ast.Assign, ast.AnnAssign)):
                targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
                if isinstance(statement, ast.AnnAssign):
                    self.annotation(statement.annotation)
                if statement.value is None:
                    self.fail("uninitialized module state is outside lifecycle subset", statement)
                _literal(statement.value, immutable=True)
                for target in targets:
                    if not isinstance(target, ast.Name):
                        self.fail("module state write is outside lifecycle subset", target)
                    bind(target.id, ("constant",))
            else:
                self.fail("module initialization effect needs a lifecycle summary: " + type(statement).__name__, statement)
        self.coverage[path]["modules"].append(name)
        return record

    def entry_module(self, path):
        names = [name for name, (actual, _) in self.paths.items() if actual == path and not name.startswith("src.")]
        if not names:
            self.fail("entry module path is not established")
        name = min(names, key=lambda value: len(value.split(".")))
        self.module(name)
        return name

    def resolve(self, node, module, locals_):
        namespace = self.modules[module]["namespace"]
        if isinstance(node, ast.Name) and node.id not in locals_:
            return namespace.get(node.id)
        if isinstance(node, ast.Attribute):
            base = self.resolve(node.value, module, locals_)
            if base and base[0] == "module":
                return self.modules[base[1]]["namespace"].get(node.attr)
        return None

    def function(self, module, node, owner=None, initializing=False, fields=None):
        key = (module, node.lineno, owner[0] if owner else None, initializing)
        if key in self.checked:
            return
        if key in self.active:
            self.fail("recursive call needs a termination/lifetime summary", node)
        self.active.add(key)
        parameters = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
        receiver = parameters[0].arg if owner else None
        locals_ = {parameter.arg for parameter in parameters}
        locals_.update(value.id for value in ast.walk(node) if isinstance(value, ast.Name) and isinstance(value.ctx, ast.Store))
        fields = fields if fields is not None else set()

        def expression(value, iteration=False, raising=False):
            if isinstance(value, ast.Constant) and (value.value is None or type(value.value) in (str, int, float, bool, bytes)):
                return
            if isinstance(value, ast.Name):
                if value.id == receiver:
                    self.fail("native owner reference escapes its checked method/field", value)
                if value.id in locals_ or self.resolve(value, module, locals_) == ("constant",):
                    return
                self.fail("untracked value/callback: " + value.id, value)
            if isinstance(value, (ast.List, ast.Tuple)):
                for element in value.elts:
                    expression(element)
                return
            if isinstance(value, ast.Dict):
                if any(key is None for key in value.keys):
                    self.fail("dictionary unpacking needs a lifecycle summary", value)
                for element in [*value.keys, *value.values]:
                    expression(element)
                return
            if isinstance(value, ast.Attribute):
                if (isinstance(value.value, ast.Name) and value.value.id == receiver and value.attr in fields):
                    return
                if self.resolve(value, module, locals_) == ("constant",):
                    return
                self.fail("attribute value has no source-derived primitive-state summary: " + ast.unparse(value), value)
            if isinstance(value, ast.Call):
                if any(isinstance(argument, ast.Starred) for argument in value.args) or any(keyword.arg is None for keyword in value.keywords):
                    self.fail("call argument unpacking needs a lifecycle summary", value)
                for argument in [*value.args, *[keyword.value for keyword in value.keywords]]:
                    expression(argument)
                target = self.resolve(value.func, module, locals_)
                if (isinstance(value.func, ast.Name) and value.func.id in BUILTINS
                        and value.func.id not in locals_):
                    if value.func.id in ITERATORS and not iteration:
                        self.fail("iterator may escape the immediate checked loop and retain request state", value)
                    if value.func.id in EXCEPTIONS and not raising:
                        self.fail("exception object may escape the immediate checked raise", value)
                    summary = "python-primitive-builtin/1:" + value.func.id
                elif (owner and isinstance(value.func, ast.Attribute) and isinstance(value.func.value, ast.Name)
                        and value.func.value.id == receiver and value.func.attr in owner[1]):
                    self.function(module, owner[1][value.func.attr], owner, initializing, fields)
                    summary = RULE + ":" + owner[0] + "." + value.func.attr
                elif target and target[0] == "function":
                    self.function(target[1], target[2])
                    summary = RULE + ":" + target[1] + "." + target[2].name
                else:
                    self.fail("call has no exact source/framework lifecycle summary: " + ast.unparse(value.func), value)
                self.calls.append(dict(path=self.modules[module]["path"], line=value.lineno,
                                       target=ast.unparse(value.func), summary=summary))
                return
            if isinstance(value, (ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare, ast.IfExp, ast.Subscript, ast.Slice, ast.JoinedStr, ast.FormattedValue)):
                for child in ast.iter_child_nodes(value):
                    if isinstance(child, ast.expr):
                        expression(child)
                return
            if isinstance(value, (ast.ListComp, ast.DictComp)):
                for generator in value.generators:
                    if generator.is_async:
                        self.fail("async comprehension needs a lifecycle summary", value)
                    expression(generator.iter, iteration=True)
                    assignment(generator.target)
                    for condition in generator.ifs:
                        expression(condition)
                for member in [value.elt] if isinstance(value, ast.ListComp) else [value.key, value.value]:
                    expression(member)
                return
            self.fail("expression needs a lifecycle summary: " + type(value).__name__, value)

        def assignment(target):
            if isinstance(target, ast.Name) and target.id != receiver:
                locals_.add(target.id)
                return
            if isinstance(target, (ast.Tuple, ast.List)):
                for element in target.elts:
                    assignment(element)
                return
            if (initializing and isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)
                    and target.value.id == receiver and not target.attr.startswith("__")):
                fields.add(target.attr)
                return
            self.fail("request/shared state mutation needs an isolation summary", target)

        def statements(body):
            for statement in body:
                if isinstance(statement, (ast.Pass, ast.Break, ast.Continue)):
                    continue
                if isinstance(statement, (ast.Assign, ast.AnnAssign)):
                    if isinstance(statement, ast.AnnAssign):
                        self.annotation(statement.annotation)
                    if statement.value is None:
                        self.fail("uninitialized local is outside lifecycle subset", statement)
                    expression(statement.value)
                    for target in statement.targets if isinstance(statement, ast.Assign) else [statement.target]:
                        assignment(target)
                elif isinstance(statement, (ast.Return, ast.Expr)):
                    if statement.value is not None:
                        expression(statement.value)
                elif isinstance(statement, ast.If):
                    expression(statement.test)
                    statements(statement.body)
                    statements(statement.orelse)
                elif isinstance(statement, ast.For):
                    expression(statement.iter, iteration=True)
                    assignment(statement.target)
                    statements(statement.body)
                    statements(statement.orelse)
                elif isinstance(statement, ast.While):
                    self.fail("while loop needs a termination/lifetime summary", statement)
                elif isinstance(statement, ast.Raise) and statement.exc is not None and statement.cause is None:
                    expression(statement.exc, raising=True)
                elif isinstance(statement, ast.Assert):
                    expression(statement.test)
                    if statement.msg:
                        expression(statement.msg)
                else:
                    self.fail("statement needs a lifecycle/effect summary: " + type(statement).__name__, statement)
        statements(node.body)
        self.active.remove(key)
        self.checked.add(key)

    def entry(self, entry, check):
        if check["status"] != "READY" and not check.get("host_condition"):
            self.fail("native binding is not source checked")
        if entry.get("kind") not in {"function", "method"} or entry.get("unit", "file") != "file":
            self.fail("entry kind/source unit needs a lifecycle summary")
        if entry.get("binding_program") or entry.get("environment") or entry.get("resources"):
            self.fail("request construction/environment/resource lifecycle needs its own source summary")
        def primitive_port(schema):
            if "x-native-type" in schema:
                return False
            if "anyOf" in schema:
                return bool(schema["anyOf"]) and all(primitive_port(branch) for branch in schema["anyOf"])
            kind = schema.get("type")
            if kind in {"null", "boolean", "integer", "number", "string"}:
                return True
            if kind == "array":
                return isinstance(schema.get("items"), dict) and primitive_port(schema["items"])
            if kind == "object":
                return (isinstance(schema.get("additionalProperties"), dict)
                        and primitive_port(schema["additionalProperties"]))
            return False
        schema = check.get("input_schema") or {}
        if schema.get("additionalProperties") or any(not primitive_port(port)
                for port in schema.get("properties", {}).values()):
            self.fail("request port has no closed primitive-value lifecycle summary")
        for values in [entry.get("fixed_arguments", {}), (entry.get("constructor") or {}).get("arguments", {}),
                       *[item.get("arguments", {}) for item in entry.get("initializers", [])]]:
            _literal(ast.parse(repr(values), mode="eval").body)
        module = self.entry_module(entry["path"])
        namespace = self.modules[module]["namespace"]
        if entry["kind"] == "function":
            target = namespace.get(entry["symbol"])
            if not target or target[0] != "function":
                self.fail("entry is not one frozen top-level synchronous function")
            self.function(module, target[2])
            return
        parts = entry["symbol"].split(".")
        if len(parts) != 2 or not entry.get("constructor") or entry["constructor"].get("symbol") != parts[0]:
            self.fail("owner construction identity is not established")
        target = namespace.get(parts[0])
        if not target or target[0] != "class" or parts[1] not in target[2]:
            self.fail("entry is not one frozen direct owner method")
        owner, fields = (parts[0], target[2]), set()
        if "__init__" in owner[1]:
            self.function(module, owner[1]["__init__"], owner, True, fields)
        for initializer in entry.get("initializers", []):
            name = initializer["symbol"].split(".")[-1]
            if name not in owner[1] or name == "__init__":
                self.fail("initializer does not identify a checked original owner method")
            self.function(module, owner[1][name], owner, True, fields)
        self.function(module, owner[1][parts[1]], owner, False, fields)


def build_lifecycle_evidence(inspector, rows, members):
    """Build bounded evidence for *every* original group member, without imports.

    No caller-supplied lifecycle claim is read. Unsupported source gets a
    concrete missing condition and retains its partial frozen import coverage.
    """
    by_id, reports = {row["entry_id"]: row for row in rows}, []
    for identity in members:
        checker = ClosedSource(inspector)
        row = by_id[identity]
        result = dict(entry_id=identity, status="UNKNOWN", rule=RULE, reason=None)
        try:
            checker.entry(row["entry"], row["check"])
            result.update(status="SUPPORTED", scope="frozen synchronous primitive-valued source owner; request state is read-only")
        except (LifecycleGap, ValueError, KeyError, TypeError, SyntaxError) as error:
            result["reason"] = str(error)
        result.update(source_coverage=sorted(checker.coverage.values(), key=lambda value: value["path"]),
                      import_coverage=sorted((dict(module=name, path=value["path"], imports=value["imports"])
                                             for name, value in checker.modules.items()), key=lambda value: value["module"]),
                      call_coverage=checker.calls)
        reports.append(result)
    supported = bool(reports) and all(item["status"] == "SUPPORTED" for item in reports)
    evidence = dict(schema="source-lifecycle-evidence/1", rule=RULE, source_id=inspector.request["source_id"],
        source_version=inspector.request["source_version"], entries=list(members),
        binding_digest=_digest([by_id[identity]["entry"] for identity in members]), members=reports,
        checks={name: dict(status="SUPPORTED" if supported else "UNKNOWN", basis=RULE if supported else
            "full frozen source/import/call coverage is missing; see members", guards=["OWNER_LEASE_ENDED", "ROOTS_AND_PERMITS_DRAINED",
            "NO_UNKNOWN_CALLS", "PROCESS_AND_BACKEND_RELEASE_CONFIRMED"] if name in {"owner_end", "cleanup"} else [])
                for name in OBLIGATIONS})
    evidence["digest"] = _digest(evidence)
    return evidence
