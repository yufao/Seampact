"""Finite source-only configuration flow; never import a source or change its environment.

The interpreter follows original calls, not configuration field names. Unknown
merges and subprocess forwarding remain unsupported and are reported as facts.
"""

import ast
from dataclasses import dataclass
from pathlib import PurePosixPath

from .resource_flow import ParameterFlow, Target, External, Instance, RESOURCE, _contains, _scope_nodes, _writes
from .inspection import units


@dataclass(frozen=True)
class Literal:
    value: object


class ConfigurationFlow(ParameterFlow):
    def __init__(self, inspector, evidence, field, parameter, entry):
        super().__init__(inspector, evidence, field)
        self.parameter, self.entry = parameter, entry
        self.module_active, self.reads = set(), []
        self.steps = 0
        self.environment_invalid = False
        self.source_gaps = []
        self.checked_modules = set()
        self.operations, self.limit_reached = 0, False

    def within_limit(self):
        self.operations += 1
        if self.operations > 5000:
            self.limit_reached = True
            return False
        return True

    def imported(self, statement, path):
        result = super().imported(statement, path)
        if isinstance(statement, ast.ImportFrom):
            module = statement.module or ''
            script_root = str(PurePosixPath(self.entry['path']).parent)
            local_root = module.split('.')[0]
            prefix = '' if script_root == '.' else script_root + '/'
            local_package = any(p.startswith(prefix + local_root + '/') for p in self.inspector.files)
            if (statement.level or local_package) and not self.module_path(module, path, statement.level):
                for alias in statement.names:
                    result[alias.asname or alias.name] = External('source-unavailable:' + module + '.' + alias.name)
        return result

    def require(self, path, node, kind):
        # Keep interpreting to report the complete missing chain in one pass.
        super().require(path, node, kind)
        return True

    def module_path(self, module, path, level=0):
        result = super().module_path(module, path, level)
        if result or level:
            return result
        # A script's own directory is Python's original import root. Do not
        # search arbitrary suffixes or infer sys.path changes in other scripts.
        root = str(PurePosixPath(self.entry['path']).parent)
        prefix = '' if root == '.' else root + '/'
        base = prefix + module.replace('.', '/')
        choices = [p for p in (base + '.py', base + '/__init__.py') if p in self.inspector.files]
        return choices[0] if len(choices) == 1 else None

    def module_member(self, path, name):
        if 'source:' + path + '.' + name in self.invalid_external:
            return None
        # Imported module bodies can overwrite a process variable before their
        # factory/constant is used. Never skip such a write when resolving a
        # member lazily. Dynamic module writes remain conservatively unknown.
        tree = self.tree(path)
        aliases = {}
        for statement in tree.body:
            if isinstance(statement, ast.Import):
                aliases.update({a.asname or a.name: a.name for a in statement.names if a.name == 'os'})
            elif isinstance(statement, ast.ImportFrom) and statement.module == 'os' and not statement.level:
                aliases.update({a.asname or a.name: 'os.' + a.name for a in statement.names})
        def canonical(node):
            text = ast.unparse(node)
            root, dot, suffix = text.partition('.')
            return aliases.get(root, root) + (dot + suffix if dot else '')
        nodes = _scope_nodes(tree) if path not in self.checked_modules else ()
        self.checked_modules.add(path)
        for node in nodes:
            destination = node.value if isinstance(node, ast.Subscript) and isinstance(node.ctx, (ast.Store, ast.Del)) else None
            if destination is not None and canonical(destination) == 'os.environ':
                if not isinstance(node.slice, ast.Constant) or node.slice.value == self.parameter:
                    self.environment_invalid = True
            if isinstance(node, ast.Call) and canonical(node.func) in {'os.putenv', 'os.unsetenv', 'os.environ.update', 'os.environ.clear', 'os.environ.pop'}:
                if not node.args or not isinstance(node.args[0], ast.Constant) or node.args[0].value == self.parameter or canonical(node.func) in {'os.environ.update', 'os.environ.clear'}:
                    self.environment_invalid = True
        result = super().module_member(path, name)
        if result is not None:
            return result
        tree = self.tree(path)
        if _writes(list(_scope_nodes(tree)), name) != 1 or (path, name) in self.module_active:
            return None
        statements = [n for n in tree.body if isinstance(n, (ast.Assign, ast.AnnAssign)) and any(
            isinstance(t, ast.Name) and t.id == name for t in (n.targets if isinstance(n, ast.Assign) else [n.target]))]
        if len(statements) != 1:
            return None
        self.module_active.add((path, name))
        try:
            statement = statements[0]
            value = self.expression(statement.value, {}, Target(path, tree), [], 0)
            if _contains(value):
                self.require(path, statement, 'configuration_assignment')
            return value
        finally:
            self.module_active.remove((path, name))

    def resolve(self, expression, env, target):
        if not self.within_limit():
            return None
        if isinstance(expression, ast.Name) and isinstance(target.node, ast.Module):
            return env.get(expression.id) if expression.id in env else self.module_member(target.path, expression.id)
        result = super().resolve(expression, env, target)
        # Import identity is part of the proof, including SDK aliases.
        if isinstance(expression, ast.Name) and result is not None:
            for statement in self.tree(target.path).body:
                if isinstance(statement, (ast.Import, ast.ImportFrom)) and any(
                        (a.asname or a.name.split('.')[0]) == expression.id for a in statement.names):
                    self.require(target.path, statement, 'original_import')
        return result

    def dataclass_fields(self, target):
        node = target.node
        if (target.path, getattr(node, 'lineno', 0)) in self.invalid_classes:
            return None
        if not isinstance(node, ast.ClassDef) or node.bases or node.keywords or len(node.decorator_list) != 1:
            return None
        decorator = self.resolve(node.decorator_list[0], {}, Target(target.path, self.tree(target.path)))
        if not isinstance(decorator, External) or decorator.name != 'dataclasses.dataclass':
            return None
        if any(not isinstance(n, (ast.AnnAssign, ast.Pass, ast.Expr)) for n in node.body):
            return None
        fields = [n for n in node.body if isinstance(n, ast.AnnAssign)]
        if any(not isinstance(n.target, ast.Name) for n in fields):
            return None
        return fields

    def class_supported(self, target, seen=()):
        return self.dataclass_fields(target) is not None or super().class_supported(target, seen)

    def invoke(self, target, args, keywords, receiver, trail, depth=0):
        self.steps += 1
        if self.steps > 500 or depth > 12:
            return None
        fields = self.dataclass_fields(target) if isinstance(target.node, ast.ClassDef) else None
        if fields is not None:
            names = [n.target.id for n in fields]
            if len(args) > len(names) or any(k not in names or k in names[:len(args)] for k in keywords):
                return None
            values = dict(zip(names, args))
            values.update(keywords)
            for n in fields:
                values.setdefault(n.target.id, self.expression(n.value, {}, target, trail, depth) if n.value else None)
                if _contains(values[n.target.id]):
                    self.require(target.path, n, 'configuration_field')
            return Instance(target, values)
        return super().invoke(target, args, keywords, receiver, trail, depth)

    def expression(self, value, env, target, trail, depth):
        if not self.within_limit():
            return None
        if isinstance(value, ast.Constant):
            return Literal(value.value)
        if isinstance(value, ast.BoolOp) and isinstance(value.op, ast.Or):
            # getenv defaults/optional overrides keep conditional origin; no
            # selected branch or runtime success is asserted by this proof.
            values = []
            for part in value.values:
                item = self.expression(part, env, target, trail, depth)
                if isinstance(item, Literal):
                    if item.value:
                        return values[0] if values and all(v == values[0] for v in values) else item if not values else None
                    continue
                values.append(item)
            origins = [v for v in values if _contains(v)]
            return origins[0] if origins and all(v == origins[0] for v in origins) else None
        if isinstance(value, ast.BinOp) and isinstance(value.op, ast.Add) and self.field == 'base_url':
            left = self.expression(value.left, env, target, trail, depth)
            if left == RESOURCE and isinstance(value.right, ast.Constant) and isinstance(value.right.value, str):
                return RESOURCE
            return None
        if isinstance(value, ast.JoinedStr) and self.field == 'credential':
            if len(value.values) == 2 and isinstance(value.values[0], ast.Constant) and value.values[0].value == 'Bearer ':
                part = value.values[1]
                if isinstance(part, ast.FormattedValue) and part.conversion == -1 and not part.format_spec:
                    return self.expression(part.value, env, target, trail, depth)
            return None
        if not isinstance(value, ast.Call):
            return super().expression(value, env, target, trail, depth)
        function = self.resolve(value.func, env, target)
        name = function.name if isinstance(function, External) else None
        if name in {'os.putenv', 'os.unsetenv', 'os.environ.clear', 'os.environ.update', 'os.environ.pop', 'os.environ.__setitem__'}:
            key = self.expression(value.args[0], env, target, trail, depth) if value.args else None
            if name in {'os.environ.clear', 'os.environ.update'} or not isinstance(key, Literal) or key.value == self.parameter:
                self.environment_invalid = True
            return None
        if name in {'os.getenv', 'os.environ.get'}:
            key = self.expression(value.args[0], env, target, trail, depth) if value.args else None
            if key == Literal(self.parameter) and not self.environment_invalid:
                self.require(target.path, value, 'environment_read')
                self.reads.append(self.cite(target.path, value, 'environment_read'))
                return RESOURCE
            return None
        if name and name.startswith('source-unavailable:'):
            self.source_gaps.append(dict(self.cite(target.path, value, 'configuration_source_unavailable'),
                target=name[len('source-unavailable:'):], status='SOURCE_UNAVAILABLE'))
            return None
        if isinstance(value.func, ast.Attribute) and value.func.attr in {'strip', 'rstrip'}:
            receiver = self.expression(value.func.value, env, target, trail, depth)
            if receiver == RESOURCE and all(isinstance(a, ast.Constant) and a.value in {'/', ' '} for a in value.args) and not value.keywords:
                return RESOURCE
        # Original local calls are visited even before they acquire an origin:
        # the environment read can be inside the unique configuration factory.
        original, receiver = function, None
        if isinstance(function, tuple):
            original, receiver = function
        if isinstance(original, Target):
            if any(isinstance(a, ast.Starred) for a in value.args) or any(k.arg is None for k in value.keywords):
                return None
            args = [self.expression(a, env, target, trail, depth) for a in value.args]
            keywords = {k.arg: self.expression(k.value, env, target, trail, depth) for k in value.keywords}
            self.require(target.path, value, 'original_call')
            return self.invoke(original, args, keywords, receiver, trail + [self.cite(target.path, value, 'original_call')], depth + 1)
        if name == 'json.dumps' and value.args:
            payload = self.expression(value.args[0], env, target, trail, depth)
            return {'serialized_model': RESOURCE} if self.field == 'model' and isinstance(payload, dict) and payload.get('model') == RESOURCE else None
        if isinstance(value.func, ast.Attribute) and value.func.attr == 'encode':
            payload = self.expression(value.func.value, env, target, trail, depth)
            if isinstance(payload, dict) and payload.get('serialized_model') == RESOURCE:
                return payload
        if name == 'urllib.request.Request':
            args = [self.expression(a, env, target, trail, depth) for a in value.args]
            kw = {k.arg: self.expression(k.value, env, target, trail, depth) for k in value.keywords}
            url = kw.get('url', args[0] if args else None)
            headers, data = kw.get('headers'), kw.get('data')
            matches = (self.field == 'base_url' and url == RESOURCE or
                       self.field == 'model' and isinstance(data, dict) and data.get('serialized_model') == RESOURCE or
                       self.field == 'credential' and isinstance(headers, dict) and headers.get('Authorization') == RESOURCE)
            if matches:
                self.require(target.path, value, 'http_request_field')
                return {'urllib_request': RESOURCE}
            return None
        if name == 'urllib.request.urlopen':
            request = self.expression(value.args[0], env, target, trail, depth) if value.args else None
            if isinstance(request, dict) and request.get('urllib_request') == RESOURCE:
                self.require(target.path, value, 'resource_consumer')
                self.proof = trail + [self.cite(target.path, value, 'resource_consumer')]
            return None
        return super().expression(value, env, target, trail, depth)

    def statements(self, body, env, target, trail, depth, returns):
        for statement in body:
            if self.limit_reached:
                return None
            # Environment setup often normalizes a fixed, disjoint path map.
            # Expand only literal .items() maps; an unknown key still poisons
            # subsequent reads rather than assuming it cannot overwrite them.
            if (isinstance(statement, ast.For) and isinstance(statement.iter, ast.Call) and
                    isinstance(statement.iter.func, ast.Attribute) and statement.iter.func.attr == 'items' and
                    not statement.iter.args and not statement.iter.keywords and
                    isinstance(statement.target, (ast.Tuple, ast.List)) and len(statement.target.elts) == 2 and
                    all(isinstance(n, ast.Name) for n in statement.target.elts)):
                mapping = self.resolve(statement.iter.func.value, env, target)
                if isinstance(mapping, dict) and len(mapping) <= 32 and all(isinstance(v, Literal) for v in mapping.values()):
                    for key, item in mapping.items():
                        branch = env.copy()
                        branch[statement.target.elts[0].id] = Literal(key)
                        branch[statement.target.elts[1].id] = item
                        self.statements(statement.body, branch, target, trail, depth, returns)
                    continue
            destinations = (statement.targets if isinstance(statement, (ast.Assign, ast.Delete)) else
                            [statement.target] if isinstance(statement, (ast.AnnAssign, ast.AugAssign)) else [])
            for destination in destinations:
                if isinstance(destination, ast.Subscript):
                    receiver = self.resolve(destination.value, env, target)
                    if isinstance(receiver, External) and receiver.name == 'os.environ':
                        key = self.expression(destination.slice, env, target, trail, depth)
                        if not isinstance(key, Literal) or key.value == self.parameter:
                            self.environment_invalid = True
                elif isinstance(destination, ast.Attribute):
                    receiver = self.resolve(destination.value, env, target)
                    if isinstance(receiver, External) and receiver.name == 'os' and destination.attr == 'environ':
                        self.environment_invalid = True
            if isinstance(target.node, ast.Module) and isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                env[statement.name] = Target(target.path, statement) if _writes(list(_scope_nodes(target.node)), statement.name) == 1 else None
            elif isinstance(statement, ast.If) and isinstance(statement.test, ast.Compare) and ast.unparse(statement.test) in {"__name__ == '__main__'", "'__main__' == __name__"}:
                if self.statements(statement.body, env, target, trail, depth, returns):
                    return True
            elif isinstance(statement, (ast.With, ast.AsyncWith)):
                for item in statement.items:
                    self.expression(item.context_expr, env, target, trail, depth)
                if self.statements(statement.body, env.copy(), target, trail, depth, returns):
                    return True
            elif super().statements([statement], env, target, trail, depth, returns):
                return True


def environment_flow(inspector, entry, parameter, field, evidence):
    if entry.get('unit', 'file') != 'file':
        return dict(proved=False, trace=[], missing=[], issue_code='configuration_flow_unsupported')
    flow = ConfigurationFlow(inspector, evidence, field, parameter, entry)
    tree = flow.tree(entry['path'])
    flow.statements(tree.body, {}, Target(entry['path'], tree), [], 0, [])
    missing = flow.unresolved if flow.proof else []
    if flow.limit_reached:
        flow.source_gaps.append(dict(kind='configuration_flow_limit', status='UNRESOLVED', operations=5000))
    return dict(proved=bool(flow.proof) and not missing and not flow.limit_reached, trace=flow.requirements if flow.proof else [],
                missing=missing, rule='finite_environment_configuration_calls',
                issue_code='evidence_incomplete' if missing else 'configuration_source_unavailable' if any(
                    f['kind'] == 'configuration_source_unavailable' for f in flow.source_gaps) else 'configuration_flow_unsupported',
                facts=flow.reads + [r for r in flow.requirements if r['kind'] in {
                    'configuration_field', 'original_assignment', 'original_parameter', 'configuration_assignment'}] + flow.source_gaps,
                conditional_use=True)


def configuration_facts(inspector, evidence):
    """Report original merge order and loader use, without claiming a resolved value."""
    facts = []
    for path in dict.fromkeys(e['path'] for e in evidence):
        if not path.endswith('.py'):
            if path.endswith(('.yaml', '.yml')):
                for ref in evidence:
                    if ref['path'] == path:
                        facts.append(dict(ref, kind='configuration_source', source_format='yaml', status='SOURCE_SIGNAL', selected_value='UNRESOLVED'))
            continue
        try:
            tree = ast.parse(dict(units(path, inspector.data(path)))['file'])
        except (ValueError, SyntaxError, KeyError):
            continue
        for node in sorted(ast.walk(tree), key=lambda n: getattr(n, 'lineno', 0)):
            if not isinstance(node, ast.Call):
                continue
            name = ast.unparse(node.func)
            kind = ('configuration_merge' if isinstance(node.func, ast.Attribute) and node.func.attr in {'merge_from_file', 'merge_from_list'} else
                    'configuration_loader_use' if name == 'torch.load' else None)
            if kind:
                guards = [ast.unparse(n.test) for n in ast.walk(tree) if isinstance(n, ast.If) and
                          n.lineno <= node.lineno <= n.end_lineno]
                facts.append(dict(path=path, unit='file', start=node.lineno, end=node.end_lineno, kind=kind,
                    operation=name, expression=ast.unparse(node.args[0]) if node.args else None,
                    conditions=guards, status='SOURCE_SIGNAL', selected_value='UNRESOLVED', override_order='source_order; conditional branches preserved'))
        # Backward one-step aliases of actual loader arguments identify which
        # configuration attribute is read; the value and active branch stay unknown.
        arguments = {n.args[0].id for n in ast.walk(tree) if isinstance(n, ast.Call) and
                     ast.unparse(n.func) == 'torch.load' and n.args and isinstance(n.args[0], ast.Name)}
        for statement in ast.walk(tree):
            if isinstance(statement, ast.Assign) and any(isinstance(t, ast.Name) and t.id in arguments for t in statement.targets):
                facts.append(dict(path=path, unit='file', start=statement.lineno, end=statement.end_lineno,
                    kind='configuration_field_use', status='SOURCE_SIGNAL', expression=ast.unparse(statement.value), selected_value='UNRESOLVED'))
    return facts
