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


@dataclass(frozen=True)
class _Function:
    path: str
    name: str
    node: object


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
        if not isinstance(value, (dict, list, _Instance)):
            return value
        if id(value) in memo:
            return memo[id(value)]
        result = _Instance(value.origin, {}) if isinstance(value, _Instance) else {} if isinstance(value, dict) else []
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
        self.steps, self.active = 0, set()
        self.calls, self.unknown, self.processes = [], [], []
        self.visited = set()
        self.cwd, self.script_contexts = '.', set()

    def tree(self, path):
        return ast.parse(dict(units(path, self.inspector.data(path)))["file"])

    def unknown_target(self, path, node, target, reason):
        row = dict(target=target, reason=reason, evidence=[_cite(path, node)])
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
        except (ValueError, SyntaxError, KeyError, OSError):
            self.unknown.append(dict(target=path, reason="frozen_source_unavailable", evidence=[]))
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
                        env[child.id] = None
                    if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute):
                        receiver = child.func.value
                        if isinstance(receiver, ast.Name) and isinstance(env.get(receiver.id), (dict, list)):
                            env[receiver.id] = None
        return env

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
                    if isinstance(parent, _Instance):
                        return parent.attributes.get(node.attr)
                return None
            parent = receiver(target.value)
            if isinstance(parent, _Instance):
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
                return self.module(parent.path).get(node.attr)
            if isinstance(parent, _External):
                if parent.name == "sys" and node.attr == "executable":
                    return "python"
                return _External(parent.name + "." + node.attr, parent.local)
            if isinstance(parent, _Instance):
                return parent.attributes.get(node.attr, _External(parent.origin + '.' + node.attr))
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
                if node.func.attr in {"submit", "map"} and args and isinstance(args[0], _Function) and not setup:
                    self.invoke(args[0], args[1:], {}, path, scope, node)
            if setup:
                for value in args + list(kwargs.values()):
                    if isinstance(value, (dict, list)):
                        value.clear()
                return _Instance(name + '()', {}) if isinstance(function, _External) and not function.local else None
            if isinstance(function, _Function):
                return self.invoke(function, args, kwargs, path, scope, node)
            if name in {"subprocess.run", "subprocess.Popen", "subprocess.call", "subprocess.check_call", "subprocess.check_output"}:
                self.process(path, scope, node, args[0] if args else kwargs.get("args"), kwargs)
            elif isinstance(function, _External) and function.local:
                self.unknown_target(path, node, function.name, "dynamic_call_target_unresolved" if
                                    function.name.startswith("<modified:") else "local_call_source_unavailable")
            elif function is None and isinstance(node.func, ast.Name) and (node.func.id not in _BUILTINS or node.func.id in env):
                self.unknown_target(path, node, node.func.id, "dynamic_call_target_unresolved")
            elif function is None and isinstance(node.func, ast.Attribute) and isinstance(
                    self.value(node.func.value, env, path, scope, setup), _Instance):
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
            if a is not None and b is not None:
                return (a == b) if isinstance(node.ops[0], ast.Eq) else a != b
        value = self.value(node, env, path, scope)
        return bool(value) if isinstance(value, (bool, int, str)) else None

    def statement(self, node, env, path, scope, setup=False):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            env[node.name] = _Function(path, node.name, node)
        elif isinstance(node, ast.ClassDef):
            env[node.name] = None
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                module = node.module or "" if isinstance(node, ast.ImportFrom) else alias.name
                target = self.module_path(module, path, getattr(node, "level", 0))
                bound = alias.asname or (alias.name if isinstance(node, ast.ImportFrom) else alias.name.split(".")[0])
                if target:
                    value = self.module(target).get(alias.name) if isinstance(node, ast.ImportFrom) else _Module(target)
                    env[bound] = value if value is not None else _External(module + "." + alias.name, True)
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
            result = self.statement(node, env, path, scope)
            if result[0]:
                return result
        return False, None

    def invoke(self, target, args, kwargs, path, scope, node):
        edge = dict(source_path=path, source_symbol=scope, target_path=target.path,
                    target_symbol=target.name, evidence=[_cite(path, node)])
        if edge not in self.calls:
            self.calls.append(edge)
        if target.node.decorator_list:
            module = self.module(target.path)
            decorators = [self.value(d, module, target.path, target.name, setup=True) for d in target.node.decorator_list]
            if any(not isinstance(d, _External) or d.name != "contextlib.contextmanager" for d in decorators):
                self.unknown_target(path, node, target.name, "decorated_call_target_unresolved")
                return None
        key = (target.path, target.name)
        self.visited.add(key)
        if key in self.active:
            return None
        if len(self.active) >= 16:
            self.unknown_target(path, node, target.name, "finite_call_depth")
            return None
        self.active.add(key)
        local = self.module(target.path).copy()
        parameters = target.node.args.posonlyargs + target.node.args.args
        defaults = dict(zip([p.arg for p in parameters[-len(target.node.args.defaults):]],
                            target.node.args.defaults)) if target.node.args.defaults else {}
        for index, parameter in enumerate(parameters):
            local[parameter.arg] = (args[index] if index < len(args) else kwargs[parameter.arg] if parameter.arg in kwargs
                                    else self.value(defaults.get(parameter.arg), local, target.path, target.name))
        for parameter in target.node.args.kwonlyargs:
            local[parameter.arg] = kwargs.get(parameter.arg)
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
            self.block(self.tree(path).body, env, path, "__main__")
        finally:
            self.cwd = previous

    def entry(self, entry):
        self.calls, self.unknown, self.processes, self.visited = [], [], [], set()
        self.modules, self.import_roots, self.steps = {}, {}, 0
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
        if self.unknown:
            issues.append(dict(issue_code="dependency_closure_unresolved", entry_id=entry["id"],
                field_paths=["entries"], evidence=[e for u in self.unknown for e in u["evidence"]],
                reason="Called local code or original subprocess target is not closed over frozen source.",
                repair_action="Provide missing frozen source or retain an unresolved host dependency scope; do not infer resource closure."))
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
