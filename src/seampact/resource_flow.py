"""Finite, source-only parameter forwarding to known resource consumers.

This proves a parameter/use relationship, not branch selection, compatibility,
execution, object lifetime, or permission to split. No source is imported.
"""

import ast
import copy
from dataclasses import dataclass, field as data_field
from pathlib import PurePosixPath

from .inspection import units


@dataclass
class Target:
    path: str
    node: object
    owner: object = None

    def __deepcopy__(self, memo):
        return self  # Frozen AST identity is shared by branch environments.


@dataclass
class External:
    name: str


@dataclass
class Instance:
    target: Target
    attributes: dict = data_field(default_factory=dict)


RESOURCE = "RESOURCE_ORIGIN"


def _contains(value, seen=()):
    if id(value) in seen:
        return False
    seen = seen + (id(value),)
    return (value == RESOURCE if isinstance(value, str) else
            any(_contains(v, seen) for v in value.attributes.values()) if isinstance(value, Instance) else
            any(_contains(v, seen) for v in value.values()) if isinstance(value, dict) else False)


def _scope_nodes(node):
    for statement in getattr(node, "body", []):
        stack = [statement]
        while stack:
            current = stack.pop()
            yield current
            if not isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                stack.extend(ast.iter_child_nodes(current))


def _writes(nodes, name):
    return sum(isinstance(n, ast.Name) and n.id == name and isinstance(n.ctx, (ast.Store, ast.Del))
        or isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name == name
        or isinstance(n, (ast.Import, ast.ImportFrom)) and any(
            (a.asname or a.name.split(".")[0]) == name for a in n.names)
        for n in nodes)


def _truth(node):
    try:
        return bool(ast.literal_eval(node))
    except (ValueError, TypeError):
        return None


class ParameterFlow:
    def __init__(self, inspector, evidence, field, diagnostic=False):
        self.inspector, self.evidence, self.field = inspector, evidence, field
        self.trees, self.proof, self.unresolved = {}, [], []
        self.active = set()
        self.resolving, self.invalid_external = set(), set()
        self.invalid_classes = set()
        self.diagnostic, self.requirements = diagnostic, []

    def tree(self, path):
        if path not in self.trees:
            self.trees[path] = ast.parse(dict(units(path, self.inspector.data(path)))["file"])
            aliases = {}
            for n in ast.walk(self.trees[path]):
                if isinstance(n, (ast.Import, ast.ImportFrom)) and not getattr(n, "level", 0):
                    for alias in n.names:
                        origin = ((n.module + "." + alias.name) if isinstance(n, ast.ImportFrom) else
                                  alias.name if alias.asname else alias.name.split(".")[0])
                        aliases.setdefault(alias.asname or alias.name.split(".")[0], set()).add(origin)
            for n in ast.walk(self.trees[path]):
                if isinstance(n, ast.Attribute) and isinstance(n.ctx, (ast.Store, ast.Del)):
                    text = ast.unparse(n)
                    root, _, suffix = text.partition(".")
                    for origin in aliases.get(root, []):
                        self.invalid_external.add(origin + "." + suffix)
        return self.trees[path]

    def covered(self, path, node):
        if self.diagnostic:
            return True
        return any(e["path"] == path and e.get("unit", "file") == "file" and
                   e["start"] <= node.lineno and node.end_lineno <= e["end"] for e in self.evidence)

    def cite(self, path, node, kind):
        return dict(path=path, unit="file", start=node.lineno, end=node.end_lineno, kind=kind)

    def require(self, path, node, kind):
        citation = self.cite(path, node, kind)
        if citation not in self.requirements:
            self.requirements.append(citation)
        if self.covered(path, node):
            return True
        self.unresolved.append(dict(issue_code="evidence_incomplete", **self.cite(path, node, kind)))
        return False

    def module_path(self, module, path, level=0):
        if level:
            parts = list(PurePosixPath(path).parent.parts)
            if level - 1 > len(parts):
                return None
            prefix = parts[:len(parts) - level + 1]
            base = "/".join(prefix + (module.split(".") if module else []))
            choices = [base + ".py", base + "/__init__.py"]
        else:
            base = module.replace(".", "/")
            choices = [base + ".py", base + "/__init__.py", "src/" + base + ".py",
                       "src/" + base + "/__init__.py"]
        found = [p for p in choices if p in self.inspector.files]
        return found[0] if len(found) == 1 else None

    def imported(self, statement, path):
        result = {}
        for alias in statement.names:
            name = alias.asname or alias.name.split(".")[0]
            if isinstance(statement, ast.ImportFrom):
                target_path = self.module_path(statement.module or "", path, statement.level)
                if target_path:
                    result[name] = self.module_member(target_path, alias.name)
                elif not statement.level:
                    result[name] = External((statement.module or "") + "." + alias.name)
            else:
                bound_module = alias.name if alias.asname else alias.name.split(".")[0]
                target_path = self.module_path(bound_module, path)
                result[name] = (External("source:" + target_path) if target_path else
                                External(bound_module))
        return result

    def module_member(self, path, name):
        tree = self.tree(path)
        if _writes(list(_scope_nodes(tree)), name) != 1 or (path, name) in self.resolving:
            return None
        self.resolving.add((path, name))
        try:
            for statement in tree.body:
                if isinstance(statement, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and statement.name == name:
                    return Target(path, statement)
                if isinstance(statement, (ast.Import, ast.ImportFrom)):
                    value = self.imported(statement, path).get(name)
                    if value is not None:
                        return value
        finally:
            self.resolving.remove((path, name))
        return None

    def class_supported(self, target, seen=()):
        node = target.node
        if ((target.path, node.lineno) in self.invalid_classes or node in seen or
                node.decorator_list or node.keywords or len(node.bases) > 1):
            return False
        if any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and
               n.name in {"__new__", "__getattribute__", "__getattr__", "__setattr__", "__delattr__", "__init_subclass__"}
               for n in node.body):
            return False
        if any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
                not isinstance(d, ast.Name) or d.id not in {"staticmethod", "classmethod"} for d in n.decorator_list)
               for n in node.body):
            return False
        for base in node.bases:
            if isinstance(base, ast.Name) and base.id == "object" and self.module_member(target.path, "object") is None:
                continue
            resolved = self.module_member(target.path, base.id) if isinstance(base, ast.Name) else None
            if not isinstance(resolved, Target) or not isinstance(resolved.node, ast.ClassDef) or not self.class_supported(resolved, seen + (node,)):
                return False
        return True

    def invalidate(self, value):
        if isinstance(value, dict):
            value.clear()
        elif isinstance(value, Instance):
            value.attributes.clear()
        elif isinstance(value, External):
            self.invalid_external.add(value.name)
        elif isinstance(value, Target) and isinstance(value.node, ast.ClassDef):
            self.invalid_classes.add((value.path, value.node.lineno))

    def clear_destination(self, destination, env, target):
        if isinstance(destination, ast.Name):
            env[destination.id] = None
        elif isinstance(destination, ast.Attribute):
            receiver = self.resolve(destination.value, env, target)
            if isinstance(receiver, Instance):
                receiver.attributes[destination.attr] = None
            elif isinstance(receiver, External):
                self.invalid_external.add(receiver.name + "." + destination.attr)
        elif isinstance(destination, ast.Subscript):
            mapping = self.resolve(destination.value, env, target)
            if isinstance(mapping, dict):
                if isinstance(destination.slice, ast.Constant):
                    mapping.pop(destination.slice.value, None)
                else:
                    mapping.clear()

    def resolve(self, expression, env, target):
        if isinstance(expression, ast.Name):
            if expression.id in env:
                return env[expression.id]
            args = target.node.args.posonlyargs + target.node.args.args + target.node.args.kwonlyargs
            if any(a.arg == expression.id for a in args) or _writes(list(_scope_nodes(target.node)), expression.id):
                return None
            return self.module_member(target.path, expression.id)
        if isinstance(expression, ast.Attribute):
            parent = self.resolve(expression.value, env, target)
            if isinstance(parent, External):
                if parent.name.startswith("source:"):
                    return self.module_member(parent.name[7:], expression.attr)
                name = parent.name + "." + expression.attr
                if any(name == changed or name.startswith(changed + ".") for changed in self.invalid_external):
                    return None
                return External(name)
            if isinstance(parent, Instance):
                if expression.attr in parent.attributes:
                    return parent.attributes[expression.attr]
                methods = [n for n in parent.target.node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                           and n.name == expression.attr]
                if len(methods) == 1 and not methods[0].decorator_list:
                    return (Target(parent.target.path, methods[0], parent.target.node), parent)
        return None

    def sink(self, function, args, keywords):
        if not isinstance(function, External):
            return False
        name, field = function.name, self.field
        if any(name == changed or name.startswith(changed + ".") for changed in self.invalid_external):
            return False
        matches = lambda value: isinstance(value, str) and value == RESOURCE
        if field == "path":
            return ((name.startswith("transformers.") and name.endswith(".from_pretrained") or
                     name in {"torch.load", "joblib.load", "safetensors.torch.load_file", "safetensors.safe_open",
                              "vllm.LLM", "vllm.AsyncLLMEngine.from_engine_args"}) and
                    any(matches(v) for v in args[:1] + [v for k, v in keywords.items() if k in
                        {"pretrained_model_name_or_path", "model_name_or_path", "path", "filename", "f", "model"}]))
        if field == "base_url" and name.split(".")[0] in {"requests", "httpx", "aiohttp"}:
            method = name.rsplit(".", 1)[-1]
            if method in {"get", "post", "put", "delete", "head", "patch", "options", "stream", "request"}:
                position = 1 if method in {"stream", "request"} else 0
                if len(args) > position and matches(args[position]) or matches(keywords.get("url")):
                    return True
        if name in {"litellm.completion", "litellm.acompletion", "litellm.batch_completion"}:
            keys = {"base_url", "api_base"} if field == "base_url" else {"api_key"} if field == "credential" else {field}
        elif name.startswith(("openai.OpenAI", "openai.AsyncOpenAI", "openai.AzureOpenAI", "openai.AsyncAzureOpenAI")):
            keys = {"api_key"} if field == "credential" else {field}
        else:
            return False
        return any(matches(keywords.get(k)) for k in keys)

    def expression(self, value, env, target, trail, depth):
        if isinstance(value, (ast.Lambda, ast.GeneratorExp, ast.ListComp, ast.SetComp, ast.DictComp)):
            return None  # Deferred or separately scoped bodies are not witnesses.
        if isinstance(value, ast.Attribute) and not isinstance(value.value, (ast.Name, ast.Attribute)):
            self.expression(value.value, env, target, trail, depth)
            return None
        if isinstance(value, (ast.Name, ast.Attribute)):
            return self.resolve(value, env, target)
        if isinstance(value, ast.Dict):
            items = [self.expression(v, env, target, trail, depth) for v in value.values]
            if any(not isinstance(k, ast.Constant) or not isinstance(k.value, str) for k in value.keys):
                return None
            return {k.value: item for k, item in zip(value.keys, items)}
        if isinstance(value, ast.IfExp):
            # A resource may be conditionally used. This records the original
            # path, and does not certify that any specific request takes it.
            a = self.expression(value.body, env, target, trail, depth)
            b = self.expression(value.orelse, env, target, trail, depth)
            return a if a == b else a if isinstance(value.orelse, ast.Constant) and value.orelse.value is None else None
        if not isinstance(value, ast.Call):
            for child in ast.iter_child_nodes(value):
                if isinstance(child, ast.expr):
                    self.expression(child, env, target, trail, depth)
            return None
        if any(isinstance(arg, ast.Starred) for arg in value.args):
            return None  # Positional expansion needs its own binding proof.
        function = self.resolve(value.func, env, target)
        args = [self.expression(a, env, target, trail, depth) for a in value.args]
        keywords = {}
        for k in value.keywords:
            item = self.expression(k.value, env, target, trail, depth)
            if k.arg is None:
                # An unknown **kwargs cannot replace an explicit keyword in
                # Python: duplicates raise. It supplies no origin proof itself.
                if item is None:
                    continue
                if not isinstance(item, dict) or set(item) & set(keywords):
                    return None
                keywords.update(item)
            elif k.arg in keywords:
                return None
            else:
                keywords[k.arg] = item
        if self.sink(function, args, keywords) and self.require(target.path, value, "resource_consumer"):
            self.proof = trail + [self.cite(target.path, value, "resource_consumer")]
        if isinstance(function, External):
            if function.name in {"openai.OpenAI", "openai.AsyncOpenAI", "openai.AzureOpenAI", "openai.AsyncAzureOpenAI",
                                 "httpx.Client", "httpx.AsyncClient"}:
                return function
        receiver = None
        if isinstance(function, tuple):
            function, receiver = function
        if isinstance(function, Target) and (any(_contains(v) for v in args + list(keywords.values())) or _contains(receiver)):
            if not self.require(target.path, value, "original_call"):
                return None
            next_trail = trail + [self.cite(target.path, value, "original_call")]
            return self.invoke(function, args, keywords, receiver, next_trail, depth + 1)
        # Preserve literal-key dictionary edits, but never infer dynamic keys.
        if isinstance(value.func, ast.Attribute):
            mapping = self.resolve(value.func.value, env, target)
            if isinstance(mapping, dict):
                if value.func.attr == "update" and len(args) == 1 and isinstance(args[0], dict):
                    mapping.update(args[0])
                elif value.func.attr == "pop" and value.args and isinstance(value.args[0], ast.Constant):
                    mapping.pop(value.args[0].value, None)
                else:
                    mapping.clear()
                return None
        for item in args + list(keywords.values()):
            self.invalidate(item)
        return None

    def invoke(self, target, args, keywords, receiver, trail, depth=0):
        if depth > 8 or (target.path, target.node.lineno) in self.active:
            return None
        if isinstance(target.node, ast.ClassDef):
            if not self.class_supported(target):
                return None
            init = [n for n in target.node.body if isinstance(n, ast.FunctionDef) and n.name == "__init__"]
            if len(init) != 1:
                return None
            receiver = Instance(target)
            self.invoke(Target(target.path, init[0], target.node), args, keywords, receiver, trail, depth + 1)
            return receiver
        node = target.node
        if node.decorator_list:
            return None
        if receiver and not self.class_supported(receiver.target):
            return None
        parameters = node.args.posonlyargs + node.args.args
        values = ([receiver] if receiver else []) + args
        if len(values) > len(parameters) or any(isinstance(a, ast.Starred) for a in args):
            return None
        env = {p.arg: v for p, v in zip(parameters, values)}
        for key, item in keywords.items():
            if key in env or key not in {a.arg for a in parameters + node.args.kwonlyargs}:
                return None
            env[key] = item
        for parameter in parameters + node.args.kwonlyargs:
            if _contains(env.get(parameter.arg)) and (not self.require(target.path, parameter, "original_parameter") or
                                                      _writes(list(_scope_nodes(node)), parameter.arg)):
                return None
        self.active.add((target.path, node.lineno))
        try:
            returns = []
            self.statements(node.body, env, target, trail, depth, returns)
            if receiver and parameters:
                final_receiver = env.get(parameters[0].arg)
                if isinstance(final_receiver, Instance):
                    receiver.attributes = final_receiver.attributes
            return returns[0] if returns and all(v == returns[0] for v in returns) else None
        finally:
            self.active.remove((target.path, node.lineno))

    def statements(self, body, env, target, trail, depth, returns):
        for statement in body:
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                env[statement.name] = None
            elif isinstance(statement, (ast.Import, ast.ImportFrom)):
                for name, value in self.imported(statement, target.path).items():
                    env[name] = value if _writes(list(_scope_nodes(target.node)), name) == 1 else None
            elif isinstance(statement, (ast.Assign, ast.AnnAssign)):
                value = self.expression(statement.value, env, target, trail, depth) if statement.value else None
                targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
                for destination in targets:
                    if isinstance(destination, ast.Name):
                        env[destination.id] = value if _writes(list(_scope_nodes(target.node)), destination.id) == 1 else None
                    elif isinstance(destination, ast.Attribute):
                        receiver = self.resolve(destination.value, env, target)
                        if isinstance(receiver, Instance):
                            writes = [n for n in ast.walk(receiver.target.node) if isinstance(n, ast.Attribute) and
                                      isinstance(n.ctx, (ast.Store, ast.Del)) and n.attr == destination.attr]
                            receiver.attributes[destination.attr] = value if len(writes) == 1 and value is not receiver else None
                        elif isinstance(receiver, External):
                            self.invalid_external.add(receiver.name + "." + destination.attr)
                        elif isinstance(receiver, Target):
                            self.invalidate(receiver)
                    elif isinstance(destination, ast.Subscript):
                        mapping = self.resolve(destination.value, env, target)
                        if isinstance(mapping, dict):
                            if isinstance(destination.slice, ast.Constant) and isinstance(destination.slice.value, str):
                                mapping[destination.slice.value] = value
                            else:
                                mapping.clear()
                if _contains(value) and not self.require(target.path, statement, "original_assignment"):
                    for destination in targets:
                        if isinstance(destination, ast.Name):
                            env[destination.id] = None
                        elif isinstance(destination, ast.Attribute):
                            receiver = self.resolve(destination.value, env, target)
                            if isinstance(receiver, Instance):
                                receiver.attributes[destination.attr] = None
            elif isinstance(statement, ast.Return):
                returns.append(self.expression(statement.value, env, target, trail, depth) if statement.value else None)
                return True
            elif isinstance(statement, ast.Raise):
                return True
            elif isinstance(statement, (ast.Delete, ast.AugAssign)):
                targets = statement.targets if isinstance(statement, ast.Delete) else [statement.target]
                for destination in targets:
                    self.clear_destination(destination, env, target)
            elif isinstance(statement, ast.If):
                self.expression(statement.test, env, target, trail, depth)
                truth = _truth(statement.test)
                if truth is not None:
                    if self.statements(statement.body if truth else statement.orelse, env, target, trail, depth, returns):
                        return True
                    continue
                branches = [copy.deepcopy(env), copy.deepcopy(env)]
                terminated = []
                for branch, statements in zip(branches, (statement.body, statement.orelse)):
                    terminated.append(self.statements(statements, branch, target, trail, depth, returns))
                if all(terminated):
                    return True
                for key in list(env):
                    if any(terminated):
                        env[key] = branches[0 if terminated[1] else 1].get(key)
                        continue
                    a, b = branches[0].get(key), branches[1].get(key)
                    env[key] = ({k: v for k, v in a.items() if k in b and v == b[k]}
                                if isinstance(a, dict) and isinstance(b, dict) else a if a == b else None)
            elif isinstance(statement, (ast.For, ast.AsyncFor, ast.While, ast.Try, ast.With, ast.AsyncWith)):
                if isinstance(statement, ast.While) and _truth(statement.test) is False:
                    continue
                if isinstance(statement, (ast.For, ast.AsyncFor)):
                    if isinstance(statement.iter, (ast.List, ast.Tuple, ast.Set)) and not statement.iter.elts:
                        continue
                    for destination in ast.walk(statement.target):
                        if isinstance(destination, (ast.Name, ast.Attribute, ast.Subscript)):
                            self.clear_destination(destination, env, target)
                # Calls within original paths may use an established parameter;
                # assignments inside them do not establish an outer alias.
                for member in ("body", "orelse", "finalbody"):
                    self.statements(getattr(statement, member, []), copy.deepcopy(env), target, trail, depth, returns)
                for handler in getattr(statement, "handlers", []):
                    self.statements(handler.body, copy.deepcopy(env), target, trail, depth, returns)
                for key in list(env):
                    if any(isinstance(n, ast.Name) and n.id == key and isinstance(n.ctx, (ast.Store, ast.Del))
                           for n in ast.walk(statement)):
                        env[key] = None
            else:
                for child in ast.iter_child_nodes(statement):
                    if isinstance(child, ast.expr):
                        self.expression(child, env, target, trail, depth)


def parameter_flow(inspector, node, path, cell, parameter, field, evidence, entry, _diagnostic=False):
    if cell != "file":
        return dict(proved=False, trace=[], missing=[])
    flow = ParameterFlow(inspector, evidence, field, _diagnostic)
    tree = flow.tree(path)
    owners = [n for n in tree.body if isinstance(n, ast.ClassDef) and any(
        isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)) and
        member.lineno == node.lineno and member.name == node.name for member in n.body)]
    owner = owners[0] if len(owners) == 1 else None
    target = Target(path, node, owner)
    receiver = Instance(Target(path, owner)) if owner else None
    flow.invoke(target, [], {parameter: RESOURCE}, receiver, [])
    if not flow.proof and receiver and node.name == "__init__":
        method = entry.get("symbol", "").split(".")[-1]
        methods = [n for n in owner.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == method]
        if len(methods) == 1:
            flow.invoke(Target(path, methods[0], owner), [], {}, receiver, [])
    trace = flow.requirements if flow.proof else []
    missing = flow.unresolved
    if not flow.proof and missing and not _diagnostic:
        # Diagnose the rest of the same frozen chain in one pass. These ranges
        # are requests for evidence, never a replacement for submitted evidence.
        diagnostic = parameter_flow(inspector, node, path, cell, parameter, field, evidence, entry, True)
        if diagnostic["proved"]:
            missing = [dict(ref, issue_code="evidence_incomplete") for ref in diagnostic["trace"] if not any(
                e["path"] == ref["path"] and e.get("unit", "file") == ref["unit"] and
                e["start"] <= ref["start"] and ref["end"] <= e["end"] for e in evidence)]
    return dict(proved=bool(flow.proof), trace=trace, missing=missing)
