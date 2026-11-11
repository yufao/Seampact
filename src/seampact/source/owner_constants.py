"""Frozen scalar owner fields used only to prune unreachable init-read checks."""

import ast


UNKNOWN = object()


def owner_constants(owner, entry, chain=None):
    if owner.bases and not chain:
        return {}  # An unexamined base can change ordinary field access.
    classes = [item.node for item in chain] if chain else [owner]
    constants = {}
    writes = {}
    method_names = {n.name for cls in classes for n in cls.body
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    for cls in classes:
        if cls.keywords or cls.decorator_list:
            return {}
        if any(not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Pass)) and not (
                isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str))
                for n in cls.body):
            return {}  # Class-level descriptors or dynamic initialization.
        for method in [n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            if method.decorator_list:
                return {}
            if method.name in {"__getattribute__", "__getattr__", "__setattr__", "__delattr__", "__new__"}:
                return {}
            args = method.args.posonlyargs + method.args.args
            if not args:
                continue
            receiver = args[0].arg
            parents = {id(child): parent for parent in ast.walk(method) for child in ast.iter_child_nodes(parent)}
            for node in ast.walk(method):
                if isinstance(node, ast.Name) and node.id == receiver:
                    parent = parents.get(id(node))
                    if not (isinstance(parent, ast.Attribute) and parent.value is node):
                        return {}  # Includes nested containers, return/yield and lexical aliases.
                if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == receiver:
                    if node.attr.startswith("__"):
                        return {}
                    if isinstance(node.ctx, (ast.Store, ast.Del)):
                        writes.setdefault(node.attr, []).append(method.name)
                elif isinstance(node, ast.Attribute) and isinstance(node.ctx, (ast.Store, ast.Del)):
                    return {}  # A class/foreign-object write has no constant-field effect summary.
                if isinstance(node, ast.Call) and any(isinstance(a, ast.Name) and a.id == receiver
                        for a in [*node.args, *(k.value for k in node.keywords)]):
                    return {}  # Owner escape / reflective mutation has no summary.
                if isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(node.value, ast.Name) and node.value.id == receiver:
                    return {}
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {
                        "eval", "exec", "globals", "locals", "vars", "__import__"}:
                    return {}
                if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Call)
                        and isinstance(node.value.func, ast.Name) and node.value.func.id == "super"
                        and node.attr not in method_names):
                    return {}  # No escaped inherited object hooks.

    def literal(node, env):
        if isinstance(node, ast.Name):
            return env.get(node.id, UNKNOWN)
        try:
            value = ast.literal_eval(node)
            return value if value is None or type(value) in (str, int, float, bool) else UNKNOWN
        except (ValueError, TypeError):
            return UNKNOWN

    def initialize(index, positional, keyword):
        if index >= len(classes):
            return
        methods = [n for n in classes[index].body if isinstance(n, ast.FunctionDef) and n.name == "__init__"]
        if not methods:
            initialize(index + 1, positional, keyword)
            return
        node = methods[0]
        args = node.args.posonlyargs + node.args.args
        if not args or node.decorator_list or node.args.vararg or node.args.kwarg:
            return
        receiver = args[0].arg
        defaults = dict(zip([p.arg for p in args[-len(node.args.defaults):]], node.args.defaults)) if node.args.defaults else {}
        defaults.update((p.arg, v) for p, v in zip(node.args.kwonlyargs, node.args.kw_defaults) if v is not None)
        env = {name:literal(value, {}) for name, value in defaults.items()}
        env.update(zip([p.arg for p in args[1:]], positional))
        env.update(keyword)
        for statement in node.body:
            if isinstance(statement, (ast.If, ast.Try, ast.For, ast.While, ast.With, ast.Return, ast.Raise, ast.Match)):
                break  # No initialization fact from a path not proven to run.
            if not isinstance(statement, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.Assert, ast.Expr, ast.Pass)):
                break
            assigned = {value.id for value in ast.walk(statement)
                        if isinstance(value, ast.Name) and isinstance(value.ctx, (ast.Store, ast.Del))}
            if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
                target = statement.targets[0]
                if isinstance(target, ast.Name):
                    env[target.id] = literal(statement.value, env)
                    assigned.discard(target.id)
                if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) and target.value.id == receiver:
                    value = literal(statement.value, env)
                    if value is not UNKNOWN:
                        constants[target.attr] = value
            if isinstance(statement, (ast.AugAssign, ast.AnnAssign)) and isinstance(statement.target, ast.Name):
                env[statement.target.id] = UNKNOWN
            for name in assigned:
                env[name] = UNKNOWN  # Unpacking / assignment expressions cannot retain a default.
            if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call):
                call = statement.value
                if (isinstance(call.func, ast.Attribute) and call.func.attr == "__init__"
                        and isinstance(call.func.value, ast.Call) and isinstance(call.func.value.func, ast.Name)
                        and call.func.value.func.id == "super" and not call.func.value.args and not call.func.value.keywords
                        and all(k.arg is not None for k in call.keywords) and not any(isinstance(a, ast.Starred) for a in call.args)):
                    initialize(index + 1, [literal(a, env) for a in call.args], {k.arg:literal(k.value, env) for k in call.keywords})
    configured = {k:v for k,v in entry.get("constructor", {}).get("arguments", {}).items()
                  if v is None or type(v) in (str, int, float, bool)}
    # Nonliteral configured arguments must shadow a literal default as UNKNOWN.
    configured.update((k,UNKNOWN) for k in entry.get("constructor", {}).get("arguments", {}) if k not in configured)
    initialize(0, [], configured)
    return {name:value for name,value in constants.items() if writes.get(name) == ["__init__"]}


def reachable_owner_nodes(method, constants):
    args = method.args.posonlyargs + method.args.args
    receiver = args[0].arg if args else None
    def truth(node):
        if isinstance(node, ast.Compare) and len(node.ops) == 1 and isinstance(node.ops[0], (ast.Eq, ast.NotEq)):
            left, right = node.left, node.comparators[0]
            if (isinstance(left, ast.Attribute) and isinstance(left.value, ast.Name) and left.value.id == receiver
                    and left.attr in constants and isinstance(right, ast.Constant)):
                equal = constants[left.attr] == right.value
                return equal if isinstance(node.ops[0], ast.Eq) else not equal
        return None
    def walk(node):
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            # Nested lexical receivers are not this owner. Keep them conservative.
            for child in ast.iter_child_nodes(node):
                yield from ast.walk(child)
        elif isinstance(node, ast.If) and truth(node.test) is not None:
            yield from walk(node.test)
            for child in node.body if truth(node.test) else node.orelse:
                yield from walk(child)
        else:
            for child in ast.iter_child_nodes(node):
                yield from walk(child)
    for statement in method.body:
        yield from walk(statement)
