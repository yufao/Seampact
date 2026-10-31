"""Finite source call reachability for already discovered dependencies.

This is a candidate consistency check, never an execution or split permission.
Only frozen Python is read. Unknown commands stay unknown; dictionary values
are not all promoted to possible commands and uncalled definitions are skipped.
"""

import ast
import builtins
import copy
import hashlib
import json
import posixpath
from dataclasses import dataclass
from pathlib import PurePosixPath

from .inspection import units
from .execution_context import process_context


@dataclass(eq=False)
class _Function:
    path: str
    name: str
    node: object
    closure: object = None
    method_kind: str = ""
    defaults: object = None
    owner: object = None


@dataclass(eq=False)
class _Class:
    path: str
    name: str
    node: object
    attributes: dict
    bases: list
    fields: dict
    dataclass_init: bool = False
    problem: str = ""


@dataclass(frozen=True)
class _Unsupported:
    reason: str
    path: str
    node: object


@dataclass(frozen=True)
class _BoundMethod:
    function: _Function
    receiver: object


@dataclass(frozen=True)
class _Super:
    cls: _Class
    receiver: object


@dataclass(frozen=True)
class _Field:
    default: object = None
    factory: object = None
    init: bool = True
    kw_only: bool = False


@dataclass(frozen=True)
class _Module:
    path: str


@dataclass(frozen=True)
class _External:
    name: str
    local: bool = False


@dataclass
class _Instance:
    """Opaque external result, with only explicit original attribute writes."""
    origin: str
    attributes: dict
    local_class: object = None


@dataclass(frozen=True)
class _Path:
    value: str


_BUILTINS = frozenset(dir(builtins)) - {"eval", "exec", "__import__"}


def _cite(path, node, kind="call"):
    return dict(path=path, unit="file", start=node.lineno, end=node.end_lineno, kind=kind)


def _branch_environment(env):
    # Preserve aliases within a branch, without one branch's list/dict mutation
    # becoming evidence in its sibling. Frozen callable ASTs are shared.
    memo = {}
    def clone(value):
        if isinstance(value, _Function) and value.closure is None:
            return value
        if not isinstance(value, (dict, list, _Instance, _Function, _BoundMethod)):
            return value
        if id(value) in memo:
            return memo[id(value)]
        if isinstance(value, _Function):
            result = _Function(value.path, value.name, value.node, None, value.method_kind, value.defaults, value.owner)
            memo[id(value)] = result
            result.closure = clone(value.closure) if value.closure is not None else None
            return result
        if isinstance(value, _BoundMethod):
            return _BoundMethod(clone(value.function), clone(value.receiver))
        result = _Instance(value.origin, {}, value.local_class) if isinstance(value, _Instance) else {} if isinstance(value, dict) else []
        memo[id(value)] = result
        if isinstance(value, _Instance):
            result.attributes.update((k, clone(v)) for k, v in value.attributes.items())
        elif isinstance(value, dict):
            result.update((k, clone(v)) for k, v in value.items())
        else:
            result.extend(clone(v) for v in value)
        return result
    return clone(env)


class _Closure:
    def __init__(self, inspector, candidate):
        self.inspector, self.candidate = inspector, candidate
        self.modules, self.loading = {}, set()
        self.unavailable_modules = set()
        self.steps, self.active = 0, set()
        self.calls, self.unknown, self.processes = [], [], []
        self.visited = set()
        self.cwd, self.script_contexts = '.', set()

    def tree(self, path):
        return ast.parse(dict(units(path, self.inspector.data(path)))["file"])

    def unknown_target(self, path, node, target, reason):
        code = ("dependency_closure_limit" if reason.startswith("finite_") else
                "dependency_closure_unresolved" if reason in {"frozen_source_unavailable", "local_call_source_unavailable",
                                                               "subprocess_source_unavailable", "local_base_source_unavailable"} else
                "dependency_closure_unsupported")
        row = dict(target=target, reason=reason, issue_code=code,
                   scope="source" if code == "dependency_closure_unresolved" else "rule", evidence=[_cite(path, node)])
        if row not in self.unknown:
            self.unknown.append(row)

    def roots(self, path):
        # Frozen, explicit script-root additions. Values are evaluated only from
        # pathlib/__file__ and preceding immutable module assignments.
        parent = PurePosixPath(path).parent
        roots = [parent.as_posix()]
        package = parent
        while (package / "__init__.py").as_posix() in self.inspector.files:
            package = package.parent
        if package != parent:
            roots.append(package.as_posix())
        return list(dict.fromkeys(roots + [".", "src"]))

    def module_path(self, module, path, level=0):
        if level:
            base = PurePosixPath(path).parent
            for _ in range(level - 1):
                base = base.parent
            names = [str(base / module.replace(".", "/"))]
        else:
            names = [posixpath.normpath(posixpath.join(root, module.replace(".", "/")))
                     for root in self.import_roots.get(path, self.roots(path))]
        choices = [name + suffix for name in names for suffix in (".py", "/__init__.py")]
        found = list(dict.fromkeys(p for p in choices if p in self.inspector.files))
        # Earlier explicit sys.path roots have original Python precedence.
        return found[0] if found else None

    def local_module(self, module, path, level=0):
        if level:
            return True
        prefix = module.split(".")[0]
        return any(p.startswith(posixpath.normpath(posixpath.join(root, prefix)) + "/") or
                   p == posixpath.normpath(posixpath.join(root, prefix)) + ".py"
                   for root in self.import_roots.get(path, self.roots(path)) for p in self.inspector.files)

    def module(self, path):
        if path in self.modules:
            return self.modules[path]
        env = {"__file__": _Path(path), "__name__": "__imported__"}
        self.modules[path] = env
        self.import_roots.setdefault(path, self.roots(path))
        try:
            tree = self.tree(path)
        except SyntaxError as error:
            location = ast.Pass(lineno=error.lineno or 1, end_lineno=error.end_lineno or error.lineno or 1)
            self.unknown_target(path, location, path, "source_syntax_rule_unsupported")
            self.unavailable_modules.add(path)
            return env
        except (ValueError, KeyError, OSError):
            self.unavailable_modules.add(path)
            self.unknown.append(dict(target=path, reason="frozen_source_unavailable",
                                     issue_code="dependency_closure_unresolved", scope="source", evidence=[]))
            return env
        # Module setup imports/definitions/constants, never unused bodies.
        for node in tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
                                 ast.Assign, ast.AnnAssign)):
                self.statement(node, env, path, "<module>", setup=True)
            elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                call = node.value
                if ast.unparse(call.func).endswith(".path.insert") and len(call.args) == 2:
                    value = self.value(call.args[1], env, path, "<module>")
                    if isinstance(value, (str, _Path)):
                        root = value.value if isinstance(value, _Path) else value
                        self.import_roots[path].insert(0, root)
                else:
                    self.value(call, env, path, "<module>", setup=True)
            elif isinstance(node, (ast.If, ast.Try, ast.For, ast.While, ast.With)):
                # Module branch/configuration evaluation is outside this finite
                # rule. Invalidate affected values rather than use stale constants.
                for child in ast.walk(node):
                    if isinstance(child, ast.Name) and isinstance(child.ctx, (ast.Store, ast.Del)):
                        env[child.id] = _Unsupported("module_binding_control_flow_unsupported", path, child)
                    if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute):
                        receiver = child.func.value
                        if isinstance(receiver, ast.Name) and isinstance(env.get(receiver.id), (dict, list)):
                            env[receiver.id] = None
        return env

    def class_attribute(self, cls, name):
        if cls.problem:
            return _Unsupported(cls.problem, cls.path, cls.node)
        if name in cls.attributes:
            return cls.attributes[name]
        if cls.bases:
            base = cls.bases[0]
            if isinstance(base, _Class):
                return self.class_attribute(base, name)
            if isinstance(base, _External) and not base.local:
                # Opaque third-party base: do not read or enumerate its methods.
                return _External(base.name + "." + name)
            return _Unsupported("dynamic_inheritance_unsupported", cls.path, cls.node)
        return None

    def class_definition(self, node, env, path, scope):
        cls = _Class(path, node.name, node, {}, [], {})
        # Resolve only explicit bases; never inspect unrelated external classes.
        cls.bases = [_External("builtins.object") if isinstance(base, ast.Name) and base.id == "object" and "object" not in env else
                     self.value(base, env, path, scope, setup=True) for base in node.bases]
        if len(cls.bases) > 1 or node.keywords or any(not isinstance(b, (_Class, _External)) for b in cls.bases):
            cls.problem = "dynamic_inheritance_unsupported"
        if any(isinstance(b, _External) and b.local for b in cls.bases):
            cls.problem = "local_base_source_unavailable"
        if any(isinstance(b, _Class) and b.problem for b in cls.bases):
            cls.problem = "inherited_class_protocol_unsupported"
        dataclass_options = None
        for decorator in node.decorator_list:
            target = decorator.func if isinstance(decorator, ast.Call) else decorator
            resolved = self.value(target, env, path, scope, setup=True)
            if isinstance(resolved, _External) and resolved.name == "dataclasses.dataclass":
                options = {k.arg: k.value.value for k in decorator.keywords if isinstance(k.value, ast.Constant)} if isinstance(decorator, ast.Call) else {}
                if isinstance(decorator, ast.Call) and (decorator.args or len(options) != len(decorator.keywords)):
                    cls.problem = "dataclass_options_unsupported"
                elif any(k not in {"init", "repr", "eq", "order", "unsafe_hash", "frozen", "match_args", "kw_only", "slots", "weakref_slot"} or not isinstance(v, bool) for k, v in options.items()):
                    cls.problem = "dataclass_options_unsupported"
                dataclass_options = options
            else:
                cls.problem = "decorated_class_target_unsupported"
        if dataclass_options is not None and cls.bases and isinstance(cls.bases[0], _Class):
            cls.fields.update(cls.bases[0].fields)
        class_env = env.copy()
        for child in node.body:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                kind = "unsupported" if child.decorator_list else "instance"
                if len(child.decorator_list) == 1:
                    decorator = child.decorator_list[0]
                    if isinstance(decorator, ast.Name) and decorator.id in {"staticmethod", "classmethod"} and decorator.id not in env:
                        kind = decorator.id
                function = _Function(path, node.name + "." + child.name, child,
                                     env if scope != "<module>" else None, kind)
                function.owner = cls
                function.defaults = self.function_defaults(child, class_env, path, scope)
                cls.attributes[child.name] = class_env[child.name] = function
                if child.name in {"__new__", "__getattr__", "__getattribute__", "__setattr__"}:
                    cls.problem = "custom_class_protocol_unsupported"
            elif isinstance(child, (ast.Assign, ast.AnnAssign)):
                value = None
                field = None
                if isinstance(child, ast.AnnAssign) and dataclass_options is not None:
                    field = _Field(kw_only=dataclass_options.get("kw_only", False))
                    call = child.value
                    callee = self.value(call.func, class_env, path, scope, setup=True) if isinstance(call, ast.Call) else None
                    if isinstance(callee, _External) and callee.name == "dataclasses.field":
                        options = {k.arg: self.value(k.value, class_env, path, scope, setup=True) for k in call.keywords}
                        if call.args or any(k not in {"default", "default_factory", "init", "kw_only", "repr", "compare", "hash", "metadata"} for k in options):
                            cls.problem = "dataclass_field_unsupported"
                        field = _Field(options.get("default"), options.get("default_factory"), options.get("init", True), options.get("kw_only", field.kw_only))
                        value = field.default
                    else:
                        value = self.value(child.value, class_env, path, scope, setup=True)
                        field = _Field(value, kw_only=field.kw_only)
                    if isinstance(child.target, ast.Name):
                        # ClassVar/InitVar need different generated constructor semantics.
                        annotation = ast.unparse(child.annotation)
                        if "ClassVar" in annotation or "InitVar" in annotation:
                            cls.problem = "dataclass_annotation_unsupported"
                        cls.fields[child.target.id] = field
                else:
                    value = self.value(child.value, class_env, path, scope, setup=True)
                for target in child.targets if isinstance(child, ast.Assign) else [child.target]:
                    self.bind(target, value, class_env)
                    if isinstance(target, ast.Name):
                        cls.attributes[target.id] = value
            elif not isinstance(child, ast.Pass) and not (isinstance(child, ast.Expr) and isinstance(child.value, ast.Constant)):
                cls.problem = "class_body_rule_unsupported"
        cls.dataclass_init = dataclass_options is not None and dataclass_options.get("init", True) and "__init__" not in cls.attributes
        return cls

    def construct(self, cls, args, kwargs, path, scope, node):
        if cls.problem:
            self.unknown_target(path, node, cls.name, cls.problem)
            self.unknown_target(cls.path, cls.node, cls.name, cls.problem)
            return _Unsupported(cls.problem, cls.path, cls.node)
        edge = dict(source_path=path, source_symbol=scope, target_path=cls.path,
                    target_symbol=cls.name, evidence=[_cite(path, node)])
        if edge not in self.calls:
            self.calls.append(edge)
        self.visited.add((cls.path, cls.name))
        instance = _Instance(cls.name + "()", {}, cls)
        if cls.dataclass_init:
            position = 0
            for name, field in cls.fields.items():
                value = field.default
                if field.init and name in kwargs:
                    value = kwargs[name]
                elif field.init and not field.kw_only and position < len(args):
                    value = args[position]
                    position += 1
                elif field.factory is not None:
                    value = self.call_value(field.factory, [], {}, path, scope, node)
                instance.attributes[name] = value
            post = self.class_attribute(cls, "__post_init__")
            if isinstance(post, _Function):
                self.invoke(post, [instance], {}, path, scope, node)
        else:
            init = self.class_attribute(cls, "__init__")
            if isinstance(init, _Function):
                self.invoke(init, [instance] + args, kwargs, path, scope, node)
            elif isinstance(init, _Unsupported):
                self.unknown_target(path, node, cls.name, init.reason)
        return instance

    def call_value(self, function, args, kwargs, path, scope, node):
        if isinstance(function, _BoundMethod):
            return self.invoke(function.function, [function.receiver] + args, kwargs, path, scope, node)
        if isinstance(function, _Function):
            return self.invoke(function, args, kwargs, path, scope, node)
        if isinstance(function, _Class):
            return self.construct(function, args, kwargs, path, scope, node)
        if isinstance(function, _Instance) and function.local_class is not None:
            method = self.class_attribute(function.local_class, "__call__")
            if isinstance(method, _Function):
                return self.invoke(method, [function] + args, kwargs, path, scope, node)
            self.unknown_target(path, node, function.origin, "instance_call_protocol_unsupported")
        if isinstance(function, _Unsupported):
            self.unknown_target(path, node, ast.unparse(node.func) if isinstance(node, ast.Call) else scope, function.reason)
            self.unknown_target(function.path, function.node, scope, function.reason)
        elif isinstance(function, _External) and function.local:
            self.unknown_target(path, node, function.name, "local_call_source_unavailable")
        elif function is None:
            self.unknown_target(path, node, scope, "dynamic_call_target_unresolved")
        return None

    def function_defaults(self, node, env, path, scope):
        parameters = node.args.posonlyargs + node.args.args
        defaults = dict(zip([p.arg for p in parameters[-len(node.args.defaults):]], node.args.defaults)) if node.args.defaults else {}
        defaults.update((p.arg, value) for p, value in zip(node.args.kwonlyargs, node.args.kw_defaults) if value is not None)
        return {name: self.value(value, env, path, scope, setup=True) for name, value in defaults.items()}

    def bind(self, target, value, env):
        if isinstance(target, ast.Name):
            env[target.id] = value
        elif isinstance(target, (ast.Tuple, ast.List)):
            for index, item in enumerate(target.elts):
                self.bind(item, value[index] if isinstance(value, (tuple, list)) and index < len(value) else None, env)
        elif isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name):
            mapping = env.get(target.value.id)
            key = target.slice.value if isinstance(target.slice, ast.Constant) else None
            if isinstance(mapping, dict) and key is not None:
                mapping[key] = value
            else:
                env[target.value.id] = None
        elif isinstance(target, ast.Attribute):
            def receiver(node):
                if isinstance(node, ast.Name):
                    return env.get(node.id)
                if isinstance(node, ast.Attribute):
                    parent = receiver(node.value)
                    if isinstance(parent, (_Instance, _Class)):
                        return parent.attributes.get(node.attr)
                return None
            parent = receiver(target.value)
            if isinstance(parent, (_Instance, _Class)):
                parent.attributes[target.attr] = value
                return
            root = target.value
            while isinstance(root, ast.Attribute):
                root = root.value
            if isinstance(root, ast.Name) and isinstance(env.get(root.id), _External):
                env[root.id] = _External("<modified:" + env[root.id].name + ">", True)

    def value(self, node, env, path, scope, setup=False):
        if node is None:
            return None
        self.steps += 1
        if self.steps > 16000:
            if not any(u["reason"] == "finite_analysis_limit" for u in self.unknown):
                self.unknown_target(path, node, scope, "finite_analysis_limit")
            return None
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            return env.get(node.id)
        if isinstance(node, ast.Attribute):
            parent = self.value(node.value, env, path, scope, setup)
            if isinstance(parent, _Module):
                module = self.module(parent.path)
                if parent.path in self.unavailable_modules:
                    return _External(parent.path + "." + node.attr, True)
                return module.get(node.attr, _Unsupported("source_export_rule_unsupported", path, node))
            if isinstance(parent, _External):
                if parent.name == "sys" and node.attr == "executable":
                    return "python"
                return _External(parent.name + "." + node.attr, parent.local)
            if isinstance(parent, _Unsupported):
                return parent
            if isinstance(parent, _Super):
                base = parent.cls.bases[0] if parent.cls.bases else None
                if isinstance(base, _Class):
                    member = self.class_attribute(base, node.attr)
                    return _BoundMethod(member, parent.receiver) if isinstance(member, _Function) else member
                if isinstance(base, _External):
                    return _External(base.name + "." + node.attr, base.local)
                return _Unsupported("super_dispatch_unsupported", path, node)
            if isinstance(parent, _Instance):
                if node.attr in parent.attributes:
                    return parent.attributes[node.attr]
                if parent.local_class is None:
                    return _External(parent.origin + '.' + node.attr)
                member = self.class_attribute(parent.local_class, node.attr)
                if isinstance(member, _Function):
                    if member.method_kind == "unsupported":
                        self.unknown_target(path, node, member.name, "decorated_method_target_unsupported")
                        return _Unsupported("decorated_method_target_unsupported", member.path, member.node)
                    return member if member.method_kind == "staticmethod" else _BoundMethod(
                        member, parent.local_class if member.method_kind == "classmethod" else parent)
                return member if member is not None else _Unsupported("local_attribute_target_unsupported", path, node)
            if isinstance(parent, _Class):
                member = self.class_attribute(parent, node.attr)
                return (_BoundMethod(member, parent) if isinstance(member, _Function) and member.method_kind == "classmethod" else
                        member if member is not None else _Unsupported("local_attribute_target_unsupported", path, node))
            if isinstance(parent, _Path):
                if node.attr == "parent":
                    return _Path(posixpath.dirname(parent.value) or ".")
                if node.attr == "parents":
                    result, current = [], parent.value
                    for _ in range(8):
                        current = posixpath.dirname(current) or "."
                        result.append(_Path(current))
                    return result
            if isinstance(parent, dict):
                return parent.get(node.attr)
            return None
        if isinstance(node, (ast.List, ast.Tuple)):
            result = []
            for item in node.elts:
                if isinstance(item, ast.Starred):
                    value = self.value(item.value, env, path, scope, setup)
                    if not isinstance(value, (list, tuple)):
                        return None
                    result.extend(value)
                else:
                    result.append(self.value(item, env, path, scope, setup))
            return result
        if isinstance(node, ast.Dict):
            result = {}
            for key, item in zip(node.keys, node.values):
                if key is None:
                    other = self.value(item, env, path, scope, setup)
                    if isinstance(other, dict):
                        result.update(other)
                    else:
                        return None
                else:
                    key = self.value(key, env, path, scope, setup)
                    if isinstance(key, (str, int)):
                        result[key] = self.value(item, env, path, scope, setup)
            return result
        if isinstance(node, ast.Subscript):
            parent, key = self.value(node.value, env, path, scope, setup), self.value(node.slice, env, path, scope, setup)
            try:
                return parent[key] if isinstance(parent, (dict, list, tuple)) and key is not None else None
            except (KeyError, IndexError, TypeError):
                return None
        if isinstance(node, ast.BinOp):
            left, right = self.value(node.left, env, path, scope, setup), self.value(node.right, env, path, scope, setup)
            if isinstance(node.op, ast.Div) and isinstance(left, _Path) and isinstance(right, (str, _Path)):
                return _Path(posixpath.normpath(posixpath.join(left.value, right.value if isinstance(right, _Path) else right)))
            if isinstance(node.op, ast.Add) and isinstance(left, (str, list)) and type(left) is type(right):
                return left + right
            return None
        if isinstance(node, ast.IfExp):
            truth = self.truth(node.test, env, path, scope)
            if truth is not None:
                return self.value(node.body if truth else node.orelse, env, path, scope, setup)
            return None
        if isinstance(node, ast.Call):
            function = self.value(node.func, env, path, scope, setup)
            args = [self.value(a, env, path, scope, setup) for a in node.args]
            kwargs = {k.arg: self.value(k.value, env, path, scope, setup) for k in node.keywords}
            name = function.name if isinstance(function, _External) else ast.unparse(node.func)
            if function is None and name == "super" and "super" not in env:
                cls = args[0] if len(args) == 2 else env.get("<method_class>")
                receiver = args[1] if len(args) == 2 else env.get("<method_self>")
                if isinstance(cls, _Class) and receiver is not None:
                    return _Super(cls, receiver)
                return _Unsupported("super_dispatch_unsupported", path, node)
            if args and ((isinstance(function, _External) and name == "pathlib.Path") or
                         (function is None and name == "str" and "str" not in env)):
                if name == "str":
                    return args[0].value if isinstance(args[0], _Path) else args[0] if isinstance(args[0], str) else None
                return args[0] if isinstance(args[0], _Path) else _Path(args[0]) if isinstance(args[0], str) else None
            if name == "sys.executable":
                return "python"
            if isinstance(node.func, ast.Attribute):
                receiver = self.value(node.func.value, env, path, scope, setup)
                if isinstance(receiver, _Path) and node.func.attr in {"resolve", "absolute", "expanduser"}:
                    return receiver
                if isinstance(receiver, list) and node.func.attr in {"append", "extend"} and args:
                    receiver.extend(args[0] if node.func.attr == "extend" and isinstance(args[0], list) else [args[0]])
                    return None
                if isinstance(receiver, dict) and node.func.attr not in {"get", "keys", "values", "items", "copy"}:
                    # Do not preserve a frozen command map after an unmodelled
                    # update/delete/escape to a method.
                    receiver.clear()
                # Standard executor callback registration is a real call edge.
                if node.func.attr in {"submit", "map"} and args and isinstance(args[0], (_Function, _BoundMethod)) and not setup:
                    self.call_value(args[0], args[1:], {}, path, scope, node)
            if setup:
                for value in args + list(kwargs.values()):
                    if isinstance(value, (dict, list)):
                        value.clear()
                if isinstance(function, _Class):
                    # A setup-created local object must not masquerade as missing
                    # source; analysing arbitrary module initialization is outside this rule.
                    return _Unsupported("module_constructor_unsupported", path, node)
                return _Instance(name + '()', {}) if isinstance(function, _External) and not function.local else None
            if isinstance(function, (_Function, _BoundMethod, _Class, _Unsupported)) or isinstance(function, _Instance) and function.local_class is not None:
                return self.call_value(function, args, kwargs, path, scope, node)
            if name in {"subprocess.run", "subprocess.Popen", "subprocess.call", "subprocess.check_call", "subprocess.check_output"}:
                self.process(path, scope, node, args[0] if args else kwargs.get("args"), kwargs)
            elif isinstance(function, _External) and function.local:
                self.unknown_target(path, node, function.name, "dynamic_call_target_unresolved" if
                                    function.name.startswith("<modified:") else "local_call_source_unavailable")
            elif function is None and isinstance(node.func, ast.Name) and (node.func.id not in _BUILTINS or node.func.id in env):
                self.unknown_target(path, node, node.func.id, "dynamic_call_target_unresolved")
            elif function is None and isinstance(node.func, ast.Attribute) and isinstance(
                    self.value(node.func.value, env, path, scope, setup), (_Instance, _Class)):
                self.unknown_target(path, node, ast.unparse(node.func), "dynamic_call_target_unresolved")
            if name not in {"subprocess.run", "subprocess.Popen", "subprocess.call", "subprocess.check_call", "subprocess.check_output"}:
                for value in args + list(kwargs.values()):
                    if isinstance(value, (dict, list)):
                        value.clear()
            return _Instance(name + '()', {}) if isinstance(function, _External) and not function.local else None
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
            local = env.copy()
            for generator in node.generators:
                values = self.value(generator.iter, local, path, scope, setup)
                self.bind(generator.target, values[0] if isinstance(values, list) and values else None, local)
            for child in ([node.key, node.value] if isinstance(node, ast.DictComp) else [node.elt]):
                self.value(child, local, path, scope, setup)
        elif not isinstance(node, (ast.Lambda, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.expr):
                    self.value(child, env, path, scope, setup)
        return None

    def truth(self, node, env, path, scope):
        if isinstance(node, ast.Compare) and len(node.ops) == 1 and isinstance(node.ops[0], (ast.Eq, ast.NotEq)):
            a, b = self.value(node.left, env, path, scope), self.value(node.comparators[0], env, path, scope)
            # Abstract source values are not the original runtime objects.
            # Their dataclass equality cannot prove a branch false (or true).
            if isinstance(a, (str, int, float, bool)) and isinstance(b, (str, int, float, bool)):
                return (a == b) if isinstance(node.ops[0], ast.Eq) else a != b
        value = self.value(node, env, path, scope)
        return bool(value) if isinstance(value, (bool, int, str)) else None

    def statement(self, node, env, path, scope, setup=False):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            function = _Function(path, node.name if scope in {"<module>", "__main__"} else scope + ".<locals>." + node.name,
                                 node, env if scope not in {"<module>", "__main__"} else None)
            function.defaults = self.function_defaults(node, env, path, scope)
            env[node.name] = function
        elif isinstance(node, ast.ClassDef):
            env[node.name] = self.class_definition(node, env, path, scope)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                module = node.module or "" if isinstance(node, ast.ImportFrom) else alias.name
                target = self.module_path(module, path, getattr(node, "level", 0))
                bound = alias.asname or (alias.name if isinstance(node, ast.ImportFrom) else alias.name.split(".")[0])
                if target:
                    value = self.module(target).get(alias.name) if isinstance(node, ast.ImportFrom) else _Module(target)
                    env[bound] = (value if value is not None else _External(module + "." + alias.name, True)
                                  if target in self.unavailable_modules else _Unsupported("source_export_rule_unsupported", path, node))
                else:
                    name = module + "." + alias.name if isinstance(node, ast.ImportFrom) else module
                    env[bound] = _External(name, self.local_module(module, path, getattr(node, "level", 0)))
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = self.value(node.value, env, path, scope, setup)
            for target in node.targets if isinstance(node, ast.Assign) else [node.target]:
                self.bind(target, value, env)
        elif isinstance(node, ast.AugAssign):
            left, right = self.value(node.target, env, path, scope), self.value(node.value, env, path, scope)
            self.bind(node.target, left + right if isinstance(node.op, ast.Add) and isinstance(left, list) and isinstance(right, list) else None, env)
        elif isinstance(node, ast.Return):
            return True, self.value(node.value, env, path, scope)
        elif isinstance(node, ast.Raise):
            self.value(node.exc, env, path, scope)
            return True, None
        elif isinstance(node, (ast.Break, ast.Continue)):
            return True, None
        elif isinstance(node, ast.If):
            truth = self.truth(node.test, env, path, scope)
            branches = [node.body] if truth is True else [node.orelse] if truth is False else [node.body, node.orelse]
            results, states = [], []
            for branch in branches:
                local = _branch_environment(env)
                results.append(self.block(branch, local, path, scope))
                states.append(local)
            for name in set().union(*(set(s) for s in states)):
                values = [s.get(name) for s in states]
                env[name] = values[0] if all(v == values[0] for v in values) else None
            if results and all(r[0] for r in results):
                values = [r[1] for r in results]
                return True, values[0] if all(v == values[0] for v in values) else None
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            values = self.value(node.iter, env, path, scope)
            if isinstance(values, list) and len(values) <= 64:
                for value in values:
                    self.bind(node.target, value, env)
                    self.block(node.body, env, path, scope)
            else:
                self.bind(node.target, None, env)
                self.block(node.body, env, path, scope)
            self.block(node.orelse, env, path, scope)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                value = self.value(item.context_expr, env, path, scope)
                if item.optional_vars:
                    self.bind(item.optional_vars, value, env)
            return self.block(node.body, env, path, scope)
        elif isinstance(node, ast.Try):
            result = self.block(node.body, env, path, scope)
            for handler in node.handlers:
                self.block(handler.body, env.copy(), path, scope)
            self.block(node.orelse, env, path, scope)
            self.block(node.finalbody, env, path, scope)
            return result
        elif isinstance(node, ast.While):
            if self.truth(node.test, env, path, scope) is not False:
                self.block(node.body, env, path, scope)
        elif isinstance(node, ast.Expr):
            self.value(node.value, env, path, scope, setup)
        return False, None

    def block(self, statements, env, path, scope):
        for node in statements:
            if self.steps > 16000:
                return False, None
            result = self.statement(node, env, path, scope)
            if result[0]:
                return result
        return False, None

    def invoke(self, target, args, kwargs, path, scope, node):
        edge = dict(source_path=path, source_symbol=scope, target_path=target.path,
                    target_symbol=target.name, evidence=[_cite(path, node)])
        if edge not in self.calls:
            self.calls.append(edge)
        if target.node.decorator_list and target.method_kind not in {"staticmethod", "classmethod"}:
            module = self.module(target.path)
            decorators = [self.value(d, module, target.path, target.name, setup=True) for d in target.node.decorator_list]
            if any(not isinstance(d, _External) or d.name != "contextlib.contextmanager" for d in decorators):
                self.unknown_target(path, node, target.name, "decorated_call_target_unresolved")
                return None
        key = (target.path, target.name)
        self.visited.add(key)
        if key in self.active:
            self.unknown_target(path, node, target.name, "finite_recursive_call")
            return None
        if len(self.active) >= 16:
            self.unknown_target(path, node, target.name, "finite_call_depth")
            return None
        self.active.add(key)
        local = self.module(target.path).copy()
        if target.closure is not None:
            local.update(target.closure)
        # Python resolves a function's own locals lexically, including names
        # assigned after a call. Do not fall through to a stale global/capture.
        assigned, declarations = set(), {}
        def bindings(statements):
            for child in statements:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    assigned.add(child.name)
                    continue
                if isinstance(child, (ast.Global, ast.Nonlocal)):
                    declarations.update((name, child) for name in child.names)
                elif isinstance(child, ast.Name) and isinstance(child.ctx, (ast.Store, ast.Del)):
                    assigned.add(child.id)
                elif isinstance(child, (ast.Import, ast.ImportFrom)):
                    assigned.update(a.asname or (a.name if isinstance(child, ast.ImportFrom) else a.name.split('.')[0]) for a in child.names)
                bindings(list(ast.iter_child_nodes(child)))
        bindings(target.node.body)
        for name in assigned - declarations.keys():
            local[name] = None
        for name in assigned & declarations.keys():
            declaration = declarations[name]
            reason = "lexical_binding_mutation_unsupported"
            self.unknown_target(target.path, declaration, name, reason)
            marker = _Unsupported(reason, target.path, declaration)
            local[name] = marker
            # Do not let a later closure call recover a pre-mutation binding.
            frame = target.closure if isinstance(declaration, ast.Nonlocal) else self.module(target.path)
            if frame is not None:
                frame[name] = marker
        if target.owner is not None:
            local["<method_class>"] = target.owner
            local["<method_self>"] = args[0] if args else None
        parameters = target.node.args.posonlyargs + target.node.args.args
        defaults = target.defaults or {}
        for index, parameter in enumerate(parameters):
            local[parameter.arg] = (args[index] if index < len(args) else kwargs[parameter.arg] if parameter.arg in kwargs
                                    else defaults.get(parameter.arg))
        for parameter in target.node.args.kwonlyargs:
            local[parameter.arg] = kwargs.get(parameter.arg, defaults.get(parameter.arg))
        if target.node.args.vararg:
            local[target.node.args.vararg.arg] = args[len(parameters):]
        if target.node.args.kwarg:
            names = {p.arg for p in parameters + target.node.args.kwonlyargs}
            local[target.node.args.kwarg.arg] = {k: v for k, v in kwargs.items() if k not in names}
        try:
            return self.block(target.node.body, local, target.path, target.name)[1]
        finally:
            self.active.remove(key)

    def process(self, path, scope, node, argv, kwargs):
        target = None
        cwd_value = kwargs.get("cwd")
        cwd_node = next((k.value for k in node.keywords if k.arg == 'cwd'), None)
        if isinstance(cwd_value, _Path):
            cwd = cwd_value.value
        elif isinstance(cwd_value, str) and self.cwd is not None:
            cwd = posixpath.normpath(posixpath.join(self.cwd, cwd_value))
        else:
            cwd = self.cwd if "cwd" not in kwargs or isinstance(cwd_node, ast.Constant) and cwd_node.value is None else None
        if cwd is not None and (cwd.startswith('/') or cwd == '..' or cwd.startswith('../') or ':' in cwd or '\\' in cwd):
            cwd = None
        if isinstance(argv, list) and ("shell" not in kwargs or kwargs["shell"] is False):
            words = [v.value if isinstance(v, _Path) else v for v in argv]
            # Python script or module invocation, without shell interpretation.
            if words and isinstance(words[0], str) and (PurePosixPath(words[0]).name.startswith("python") or words[0] == "<python>"):
                if len(words) > 2 and words[1] == "-m" and isinstance(words[2], str):
                    target = self.module_path(words[2], path)
                elif len(words) > 1 and isinstance(words[1], str) and words[1].endswith(".py"):
                    target = posixpath.normpath(words[1]) if isinstance(argv[1], _Path) else posixpath.normpath(posixpath.join(cwd, words[1])) if cwd is not None else None
            elif words and isinstance(words[0], str) and words[0].endswith(".py"):
                target = posixpath.normpath(words[0]) if isinstance(argv[0], _Path) else posixpath.normpath(posixpath.join(cwd, words[0])) if cwd is not None else None
        if target is not None and (target.startswith('/') or target == '..' or target.startswith('../') or ':' in target or '\\' in target):
            target = None
        env_node = next((k.value for k in node.keywords if k.arg == "env"), None)
        edge = dict(source_path=path, source_symbol=scope, target_path=target,
                    cwd=cwd,
                    argv=[v if isinstance(v, (str, int, float, bool)) else None for v in argv] if isinstance(argv, list) else None,
                    environment=dict(mode="inherit" if env_node is None else "explicit", expression=ast.unparse(env_node) if env_node else None),
                    evidence=[_cite(path, node, "subprocess")])
        if edge not in self.processes:
            self.processes.append(edge)
        if target is None:
            self.unknown_target(path, node, ast.unparse(node), "subprocess_target_unresolved")
        elif target not in self.inspector.files:
            self.unknown_target(path, node, target, "subprocess_source_unavailable")
        else:
            self.script(target, cwd)

    def script(self, path, cwd):
        key = (path, "__main__")
        if (path, cwd) in self.script_contexts:
            return
        self.script_contexts.add((path, cwd))
        self.visited.add(key)
        previous = self.cwd
        self.cwd = cwd
        try:
            env = self.module(path).copy()
            env["__name__"] = "__main__"
            if path in self.unavailable_modules:
                return
            self.block(self.tree(path).body, env, path, "__main__")
        finally:
            self.cwd = previous

    def entry(self, entry):
        self.calls, self.unknown, self.processes, self.visited = [], [], [], set()
        self.modules, self.import_roots, self.steps, self.active = {}, {}, 0, set()
        self.unavailable_modules = set()
        self.cwd, self.script_contexts = '.', set()
        path, symbol = entry.get("path"), entry.get("symbol", "__main__")
        def not_covered(reason):
            return dict(entry_id=entry["id"], status="NOT_COVERED", scope_covered=False,
                        reachable_entries=[], dependency_ids=[], missing_dependency_ids=[], call_edges=[], process_edges=[],
                        evidence=[], unresolved_targets=[dict(target=symbol, reason=reason, evidence=[])], issues=[])
        if path not in self.inspector.files or not path.endswith(".py"):
            return not_covered("entry_source_scope_unsupported")
        if entry.get("kind") == "cli":
            self.script(path, process_context(self.inspector, entry)['cwd'])
        else:
            target = self.module(path).get(symbol)
            if isinstance(target, _Function):
                self.invoke(target, [], copy.deepcopy(entry.get("fixed_arguments", {})), path, "<entry>", target.node)
            else:
                return not_covered("callable_scope_unsupported")
        reachable = []
        for other in self.candidate.get("entries", []):
            key = (other.get("path"), "__main__" if other.get("kind") == "cli" else other.get("symbol"))
            if other.get("id") != entry.get("id") and key in self.visited:
                reachable.append(other["id"])
        dependencies, missing = [], []
        for dep in self.candidate.get("dependencies", []):
            if set(dep.get("entry_ids", [])) & set(reachable):
                dependencies.append(dep["id"])
                if entry["id"] not in dep.get("entry_ids", []):
                    missing.append(dep["id"])
        evidence = [item for edge in self.calls + self.processes for item in edge["evidence"]]
        issues = []
        for identity in missing:
            index = next(i for i, d in enumerate(self.candidate["dependencies"]) if d["id"] == identity)
            issues.append(dict(issue_code="host_dependency_missing", entry_id=entry["id"], dependency_id=identity,
                field_paths=[f"dependencies[{index}].entry_ids"], evidence=evidence,
                reason="Original host reaches an entry with this discovered dependency; host membership and mapping are missing.",
                repair_action="Include this host in the affected dependency and prove its original parameter/environment transfer."))
        descriptions = {
            "dependency_closure_unresolved": ("Called local code or original subprocess target lacks frozen source.",
                "Provide the specifically missing frozen source or retain an unresolved host scope."),
            "dependency_closure_unsupported": ("Available source uses a call or binding outside the finite dependency rules.",
                "Resolve the cited unsupported rule or retain unknown scope; rereading available source cannot establish closure."),
            "dependency_closure_limit": ("Dependency investigation reached its finite analysis bound.",
                "Retain unknown scope and inspect the cited bound; do not request repeated source reads or infer closure.")}
        for code, (reason, repair) in descriptions.items():
            targets = [u for u in self.unknown if u["issue_code"] == code]
            if targets:
                issues.append(dict(issue_code=code, scope="source" if code == "dependency_closure_unresolved" else "rule",
                    repair_scope="evidence" if code == "dependency_closure_unresolved" else "rule",
                    model_repairable=code == "dependency_closure_unresolved",
                    entry_id=entry["id"], field_paths=["entries"], evidence=[e for u in targets for e in u["evidence"]],
                    reason=reason, repair_action=repair))
        return dict(entry_id=entry["id"], status="MISSING" if issues else "CHECKED", scope_covered=True,
                    reachable_entries=reachable, dependency_ids=dependencies, missing_dependency_ids=missing,
                    call_edges=self.calls, process_edges=self.processes, evidence=evidence,
                    unresolved_targets=self.unknown, issues=issues)


def dependency_closure(inspector, candidate):
    """Return source reachability facts and blocking candidate consistency issues.

    Cache is on the inspector and includes the entire candidate and frozen source
    digests. Callers receive copies so diagnostics cannot poison a later check.
    CHECKED means this finite audit found no issue, not complete discovery.
    """
    key = hashlib.sha256(json.dumps(dict(candidate=candidate, files=inspector.files), sort_keys=True,
                                   default=str, ensure_ascii=False).encode()).hexdigest()
    cached = getattr(inspector, "_dependency_closure_cache", None)
    if cached and cached[0] == key:
        return copy.deepcopy(cached[1])
    checker = _Closure(inspector, candidate)
    entries = {}
    for entry in candidate.get("entries", []):
        result = checker.entry(entry)
        if result is not None:
            entries[entry["id"]] = result
    result = dict(schema="dependency-closure/1", entries=entries,
                  issues=[i for row in entries.values() for i in row["issues"]])
    inspector._dependency_closure_cache = (key, copy.deepcopy(result))
    return result


def entry_dependency_issues(inspector, candidate, entry):
    return dependency_closure(inspector, candidate)["entries"].get(entry["id"], {}).get("issues", [])
