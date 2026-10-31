"""Finite original Python subprocess environment forwarding, without execution.

Only a reached, identified subprocess call can establish a process edge. The
child is read from the same frozen inspector, never from a candidate entry or a
similarly named source. This is a conditional use proof, not execution success.
"""

import ast
import posixpath
from dataclasses import dataclass

from .configuration_flow import ConfigurationFlow, Literal, environment_flow
from .execution_context import process_context
from .inspection import units
from .resource_flow import External, Target, _contains, _scope_nodes, _writes


@dataclass
class _Environment:
    intact: bool

    def __deepcopy__(self, memo):
        # Branch exploration may mutate an alias passed into an original
        # function. Its possible key destruction must also reach the caller.
        # Validity only decreases; explicit dict/copy expressions create a new
        # snapshot instead of using this branch identity.
        return self


class _Argv(list):
    """A sequence witness whose validity follows every mutable alias."""

    def __init__(self, values):
        super().__init__(values)
        self.intact = True

    def __deepcopy__(self, memo):
        return self


class _ProcessFlow(ConfigurationFlow):
    def __init__(self, inspector, evidence, field, parameter, entry, cwd, chain):
        super().__init__(inspector, evidence, field, parameter, entry)
        self.cwd, self.chain = cwd, chain
        self.results, self.process_facts = [], []
        self.module_environments, self.loading_modules = {}, set()

    def fact(self, path, node, kind, **values):
        item = dict(self.cite(path, node, kind), **values)
        if item not in self.process_facts:
            self.process_facts.append(item)
        return item

    def imported(self, statement, path):
        result = super().imported(statement, path)
        for alias in statement.names:
            module = statement.module or '' if isinstance(statement, ast.ImportFrom) else alias.name
            local = self.module_path(module, path, getattr(statement, 'level', 0))
            if local:
                self.require(path, statement, 'original_import')
                exports = self.load_module(local)
                bound = alias.asname or (alias.name if isinstance(statement, ast.ImportFrom) else alias.name.split('.')[0])
                result[bound] = exports.get(alias.name) if isinstance(statement, ast.ImportFrom) else External('source:' + local)
        return result

    def load_module(self, path):
        if path in self.module_environments:
            return self.module_environments[path]
        if path in self.loading_modules:
            self.source_gaps.append(dict(kind='process_import_cycle', path=path, status='UNRESOLVED'))
            return {}
        self.loading_modules.add(path)
        try:
            tree = self.tree(path)
            exports = {}
            self.statements(tree.body, exports, Target(path, tree), [], 0, [])
            self.module_environments[path] = exports
            return exports
        finally:
            self.loading_modules.remove(path)

    def module_member(self, path, name):
        # Real imported module bodies execute once, including environment writes.
        if path in self.module_environments:
            return self.module_environments[path].get(name)
        return super().module_member(path, name)

    def resolve(self, node, env, target):
        result = super().resolve(node, env, target)
        if isinstance(node, ast.Name) and result is not None:
            scopes = [self.tree(target.path)]
            if not isinstance(target.node, ast.Module):
                scopes.append(target.node)
            for scope in scopes:
                for statement in _scope_nodes(scope):
                    if isinstance(statement, (ast.Import, ast.ImportFrom)) and any(
                            (a.asname or a.name.split('.')[0]) == node.id for a in statement.names):
                        self.require(target.path, statement, 'original_import')
        return result

    def builtin(self, name, env, target):
        return name not in env and not _writes(list(_scope_nodes(target.node)), name) and not _writes(
            list(_scope_nodes(self.tree(target.path))), name)

    def environment(self, node, env, target, trail, depth):
        value = self.expression(node, env, target, trail, depth)
        if isinstance(value, External) and value.name == 'os.environ':
            return _Environment(not self.environment_invalid)
        return value

    def invalidate(self, value):
        if isinstance(value, (_Environment, _Argv)):
            value.intact = False
            if isinstance(value, _Argv):
                for item in value:
                    self.invalidate(item)
        elif isinstance(value, (dict, list, tuple)):
            for item in (value.values() if isinstance(value, dict) else value):
                self.invalidate(item)
            super().invalidate(value)
        elif isinstance(value, External) and value.name == 'os.environ':
            self.environment_invalid = True
        else:
            super().invalidate(value)

    def expression(self, node, env, target, trail, depth):
        if node is None:
            return None
        reflective = {'exec', 'eval', 'compile', 'globals', 'locals', 'vars', 'setattr', 'delattr'}
        if isinstance(node, ast.Name) and node.id in reflective and self.builtin(node.id, env, target):
            return External('builtins.' + node.id)
        if isinstance(node, (ast.List, ast.Tuple)):
            if any(isinstance(n, ast.Starred) for n in node.elts):
                return None
            return _Argv(self.expression(n, env, target, trail, depth) for n in node.elts)
        if isinstance(node, ast.Attribute):
            identity = self.resolve(node, env, target)
            if isinstance(identity, External) and identity.name == 'sys.executable':
                return Literal('python')
            receiver = self.resolve(node.value, env, target)
            if isinstance(receiver, (_Environment, _Argv)):
                # A detached method can later mutate through a different name.
                self.invalidate(receiver)
                return None
        if isinstance(node, ast.Subscript):
            receiver = self.expression(node.value, env, target, trail, depth)
            key = self.expression(node.slice, env, target, trail, depth)
            if isinstance(key, Literal):
                if isinstance(receiver, dict):
                    return receiver.get(key.value) if isinstance(key.value, str) else None
                if isinstance(receiver, _Argv) and receiver.intact and isinstance(key.value, int) and -len(receiver) <= key.value < len(receiver):
                    return receiver[key.value]
            self.invalidate(receiver)
            return None
        if isinstance(node, ast.Dict) and any(k is None for k in node.keys):
            intact, inherited = True, False
            for key, value in zip(node.keys, node.values):
                item = self.environment(value, env, target, trail, depth)
                if key is None:
                    if not isinstance(item, _Environment):
                        intact = False
                    else:
                        intact = intact and item.intact
                        inherited = True
                elif not isinstance(key, ast.Constant) or key.value == self.parameter or not isinstance(item, Literal):
                    intact = False
            self.require(target.path, node, 'subprocess_environment_copy')
            return _Environment(intact and inherited)
        if isinstance(node, ast.Call):
            function = self.resolve(node.func, env, target)
            name = function.name if isinstance(function, External) else None
            if isinstance(node.func, ast.Name) and node.func.id in reflective and self.builtin(node.func.id, env, target):
                name = 'builtins.' + node.func.id
            if name in {'builtins.' + n for n in reflective} or name == 'importlib.import_module':
                self.source_gaps.append(self.fact(target.path, node, 'process_dynamic_namespace', status='UNRESOLVED'))
                return None
            if name in {'subprocess.run', 'subprocess.Popen', 'subprocess.call',
                        'subprocess.check_call', 'subprocess.check_output'}:
                self.process(node, env, target, trail, depth)
                return None
            if name == 'os.chdir':
                self.cwd = None  # A dynamic process directory is not guessed.
                self.require(target.path, node, 'process_directory_change')
                self.fact(target.path, node, 'process_directory_change', status='UNRESOLVED')
                return None
            receiver = (self.environment(node.func.value, env, target, trail, depth)
                        if isinstance(node.func, ast.Attribute) else None)
            if isinstance(receiver, _Argv):
                self.invalidate(receiver)
            if isinstance(receiver, _Environment) and node.func.attr == 'copy' and not node.args and not node.keywords:
                self.require(target.path, node, 'subprocess_environment_copy')
                return _Environment(receiver.intact)
            if name == 'os.environ.copy' and not node.args and not node.keywords:
                self.require(target.path, node, 'subprocess_environment_copy')
                return _Environment(not self.environment_invalid)
            if isinstance(node.func, ast.Name) and node.func.id == 'dict' and self.builtin('dict', env, target):
                if len(node.args) == 1 and not node.keywords:
                    original = self.environment(node.args[0], env, target, trail, depth)
                    if isinstance(original, _Environment):
                        self.require(target.path, node, 'subprocess_environment_copy')
                        return _Environment(original.intact)
            if isinstance(receiver, _Environment):
                args = [self.expression(a, env, target, trail, depth) for a in node.args]
                method = node.func.attr
                safe = method in {'pop', '__delitem__', '__setitem__', 'setdefault'} and args and isinstance(
                    args[0], Literal) and args[0].value != self.parameter
                if method == 'update' and len(node.args) == 1 and not node.keywords and isinstance(node.args[0], ast.Dict):
                    safe = all(isinstance(k, ast.Constant) and isinstance(k.value, str) and k.value != self.parameter
                               for k in node.args[0].keys)
                receiver.intact = receiver.intact and bool(safe)
                if not safe:
                    for value in args + [self.expression(k.value, env, target, trail, depth) for k in node.keywords]:
                        self.invalidate(value)
                if name and name.startswith('os.environ.'):
                    self.environment_invalid = self.environment_invalid or not receiver.intact
                self.require(target.path, node, 'subprocess_environment_operation')
                self.fact(target.path, node, 'subprocess_environment_operation', preserved=receiver.intact)
                return None
            if name in {'os.putenv', 'os.unsetenv', 'os.environ.clear', 'os.environ.update', 'os.environ.pop',
                        'os.environ.__setitem__', 'os.environ.setdefault', 'os.environ.__delitem__'}:
                # ConfigurationFlow keeps process-global invalidation. Explicit
                # snapshot maps above retain their independent earlier origin.
                safe = bool(node.args) and isinstance(node.args[0], ast.Constant) and node.args[0].value != self.parameter
                if name == 'os.environ.update' and len(node.args) == 1 and isinstance(node.args[0], ast.Dict):
                    safe = all(isinstance(k, ast.Constant) and isinstance(k.value, str) and k.value != self.parameter
                               for k in node.args[0].keys) and not node.keywords
                if name.endswith('.clear'):
                    safe = False
                self.environment_invalid = self.environment_invalid or not safe
                self.require(target.path, node, 'process_environment_operation')
                self.fact(target.path, node, 'process_environment_operation', preserved=not self.environment_invalid)
                for argument in node.args:
                    self.expression(argument, env, target, trail, depth)
                return None
            # Passing a mutable inherited map to arbitrary code forfeits its
            # identity proof. Original local functions are interpreted below.
            original = function[0] if isinstance(function, tuple) else function
            if (not isinstance(original, Target) or any(isinstance(a, ast.Starred) for a in node.args)
                    or any(k.arg is None for k in node.keywords)):
                for argument in node.args + [k.value for k in node.keywords]:
                    for part in ast.walk(argument):
                        if isinstance(part, (ast.Name, ast.Attribute)):
                            escaped = self.resolve(part, env, target)
                            self.invalidate(escaped)
                self.invalidate(receiver)
        return super().expression(node, env, target, trail, depth)

    def invoke(self, target, args, keywords, receiver, trail, depth=0):
        parameters = target.node.args.posonlyargs + target.node.args.args if isinstance(
            target.node, (ast.FunctionDef, ast.AsyncFunctionDef)) else []
        if (depth > 8 or self.steps >= 500 or (target.path, target.node.lineno) in self.active or
                isinstance(target.node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
                    target.node.decorator_list or len(args) + bool(receiver) > len(parameters) or
                    any(k not in {p.arg for p in parameters + target.node.args.kwonlyargs} or
                        k in {p.arg for p in parameters[:len(args)]} for k in keywords))):
            for value in args + list(keywords.values()):
                self.invalidate(value)
            return None
        if isinstance(target.node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            values = dict(zip((p.arg for p in parameters), args))
            values.update(keywords)
            for parameter in parameters + target.node.args.kwonlyargs:
                if isinstance(values.get(parameter.arg), (_Environment, _Argv)):
                    self.require(target.path, parameter, 'original_parameter')
        return super().invoke(target, args, keywords, receiver, trail, depth)

    def statements(self, body, env, target, trail, depth, returns):
        for statement in body:
            if (target.path != self.entry['path'] and isinstance(statement, ast.If) and
                    isinstance(statement.test, ast.Compare) and ast.unparse(statement.test) in {
                        "__name__ == '__main__'", "'__main__' == __name__"}):
                self.statements(statement.orelse, env, target, trail, depth, returns)
                continue
            destinations = (statement.targets if isinstance(statement, (ast.Assign, ast.Delete)) else
                            [statement.target] if isinstance(statement, (ast.AnnAssign, ast.AugAssign)) else [])
            if isinstance(statement, ast.AugAssign):
                mapping = self.resolve(statement.target, env, target)
                self.expression(statement.value, env, target, trail, depth)
                if isinstance(mapping, (_Environment, _Argv)) or isinstance(mapping, External) and mapping.name == 'os.environ':
                    safe = (isinstance(mapping, (_Environment, External)) and isinstance(statement.op, ast.BitOr)
                            and isinstance(statement.value, ast.Dict) and all(
                                isinstance(k, ast.Constant) and isinstance(k.value, str) and k.value != self.parameter
                                for k in statement.value.keys))
                    if isinstance(mapping, (_Environment, _Argv)):
                        mapping.intact = mapping.intact and safe
                    else:
                        self.environment_invalid = self.environment_invalid or not safe
                    self.require(target.path, statement, 'subprocess_environment_operation')
                    self.fact(target.path, statement, 'subprocess_environment_operation', preserved=bool(safe))
                    continue
            destinations = [node for destination in destinations for node in ast.walk(destination)
                            if isinstance(node, (ast.Subscript, ast.Attribute)) and isinstance(node.ctx, (ast.Store, ast.Del))]
            for destination in destinations:
                if isinstance(destination, ast.Subscript):
                    mapping = self.resolve(destination.value, env, target)
                    if isinstance(mapping, _Argv):
                        self.invalidate(mapping)
                    elif mapping is None:
                        # Nested/unknown indexing does not hide the original
                        # mutable value behind an unmodelled container access.
                        for part in ast.walk(destination.value):
                            if isinstance(part, (ast.Name, ast.Attribute)):
                                self.invalidate(self.resolve(part, env, target))
                    if isinstance(mapping, _Environment):
                        key = self.expression(destination.slice, env, target, trail, depth)
                        mapping.intact = mapping.intact and isinstance(key, Literal) and key.value != self.parameter
                        self.require(target.path, statement, 'subprocess_environment_operation')
                        self.fact(target.path, statement, 'subprocess_environment_operation', preserved=mapping.intact)
                    if isinstance(mapping, External) and mapping.name == 'os.environ':
                        self.require(target.path, statement, 'process_environment_operation')
                        self.fact(target.path, statement, 'process_environment_operation')
                elif isinstance(destination, ast.Attribute):
                    mapping = self.resolve(destination.value, env, target)
                    if isinstance(mapping, (_Environment, _Argv)):
                        self.invalidate(mapping)
                    if isinstance(mapping, External) and mapping.name == 'os' and destination.attr == 'environ':
                        self.require(target.path, statement, 'process_environment_operation')
            if isinstance(statement, (ast.Assign, ast.AnnAssign)):
                # Evidence must also include the argv and environment aliases,
                # which carry no scalar RESOURCE marker in ConfigurationFlow.
                self.require(target.path, statement, 'subprocess_original_assignment')
                names = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
                if all(isinstance(n, ast.Name) for n in names):
                    value = self.expression(statement.value, env, target, trail, depth) if statement.value else None
                    for name in names:
                        # A later augmented assignment still receives this
                        # exact object; static write counts cannot erase the
                        # alias before its possible mutation is examined.
                        tracked = isinstance(value, (_Environment, _Argv)) or isinstance(value, External) and value.name == 'os.environ'
                        env[name.id] = value if tracked or _writes(
                            list(_scope_nodes(target.node)), name.id) == 1 else None
                    if _contains(value):
                        self.require(target.path, statement, 'original_assignment')
                    continue
            if super().statements([statement], env, target, trail, depth, returns):
                return True

    def process(self, node, env, target, trail, depth):
        self.require(target.path, node, 'original_subprocess_call')
        if len(node.args) > 1 or any(k.arg is None for k in node.keywords):
            self.fact(target.path, node, 'subprocess_flow_unsupported', status='UNRESOLVED')
            return
        keywords = {k.arg: k.value for k in node.keywords}
        if set(keywords) - {'args', 'env', 'cwd', 'shell', 'check', 'capture_output', 'text', 'encoding',
                            'errors', 'timeout', 'stdout', 'stderr', 'stdin', 'universal_newlines', 'bufsize',
                            'close_fds', 'start_new_session'}:
            self.fact(target.path, node, 'subprocess_options_unsupported', status='UNRESOLVED')
            return
        if node.args and 'args' in keywords:
            self.fact(target.path, node, 'subprocess_options_unsupported', status='UNRESOLVED')
            return
        argv = self.expression(node.args[0], env, target, trail, depth) if node.args else None
        # Python evaluates all keyword expressions in their original order.
        # A later option can mutate the parent after an explicit env snapshot,
        # or before the implicit default environment is inherited.
        values = {k.arg: (self.environment(k.value, env, target, trail, depth) if k.arg == 'env' else
                          self.expression(k.value, env, target, trail, depth)) for k in node.keywords}
        if not node.args:
            argv = values.get('args')
        if not isinstance(argv, _Argv) or not argv.intact or len(argv) < 2 or not all(isinstance(v, Literal) and isinstance(v.value, str) for v in argv):
            self.fact(target.path, node, 'subprocess_argv_unresolved', status='UNRESOLVED')
            return
        argv = [v.value for v in argv]
        if argv[0] not in {'python', 'python3', 'python.exe'} or argv[1].startswith('-'):
            self.fact(target.path, node, 'subprocess_argv_unresolved', status='UNRESOLVED')
            return
        if 'shell' in values and values['shell'] != Literal(False):
            self.fact(target.path, node, 'subprocess_shell_unsupported', status='UNRESOLVED')
            return
        cwd = self.cwd
        if 'cwd' in values:
            value = values['cwd']
            if value != Literal(None):
                cwd = self.relative_path(cwd, value.value) if isinstance(value, Literal) and isinstance(value.value, str) else None
        child = self.relative_path(cwd, argv[1])
        if child is None or not child.endswith('.py'):
            self.fact(target.path, node, 'subprocess_target_unresolved', status='UNRESOLVED')
            return
        if child not in self.inspector.files:
            self.fact(target.path, node, 'subprocess_child_source_unavailable', target_path=child, status='SOURCE_UNAVAILABLE')
            return
        environment = values.get('env', Literal(None))
        intact = not self.environment_invalid if environment == Literal(None) else isinstance(environment, _Environment) and environment.intact
        if not intact:
            self.fact(target.path, node, 'subprocess_environment_overridden', target_path=child, status='UNRESOLVED')
            return
        if self.source_gaps:
            return
        edge = self.fact(target.path, node, 'subprocess_environment_inheritance', target_path=child,
                         parameter=self.parameter, mode='inherit' if environment == Literal(None) else 'copy', cwd=cwd,
                         status='SOURCE_PROVEN')
        child_entry = dict(path=child, kind='cli', unit='file', command=argv)
        result = environment_flow(self.inspector, child_entry, self.parameter, self.field, self.evidence)
        if result['proved']:
            # The scalar configuration interpreter resolves import identities;
            # explicitly cover those module imports at this process boundary.
            # This does not infer a child from the imports or from a candidate.
            for path in dict.fromkeys(r['path'] for r in result['trace']):
                # Parsing the child must not add its monkeypatches to this
                # parent's interpreter state: they are distinct processes.
                child_tree = ast.parse(dict(units(path, self.inspector.data(path)))['file'])
                for statement in child_tree.body:
                    if isinstance(statement, (ast.Import, ast.ImportFrom)):
                        self.require(path, statement, 'original_import')
        if not result['proved'] and child not in self.chain and len(self.chain) < 8:
            forwarded = _process_environment_flow(self.inspector, child_entry, self.parameter, self.field,
                                                  self.evidence, cwd, self.chain + (child,))
            if forwarded['proved'] or forwarded.get('facts'):
                result = forwarded
        result = dict(result)
        result['trace'] = list(self.requirements) + [edge] + result.get('trace', [])
        result['missing'] = list(self.unresolved) + result.get('missing', [])
        result['proved'] = result['proved'] and not self.unresolved and not self.limit_reached
        self.results.append(result)

    @staticmethod
    def relative_path(cwd, value):
        if cwd is None or not isinstance(value, str) or not value or '\\' in value or ':' in value or value.startswith('/'):
            return None
        value = posixpath.normpath(posixpath.join(cwd, value))
        return None if value == '..' or value.startswith('../') else value


def _process_environment_flow(inspector, entry, parameter, field, evidence, cwd, chain):
    flow = _ProcessFlow(inspector, evidence, field, parameter, entry, cwd, chain)
    try:
        tree = flow.tree(entry['path'])
        module_env = {}
        flow.module_environments[entry['path']] = module_env
        flow.statements(tree.body, module_env, Target(entry['path'], tree), [], 0, [])
    except (ValueError, SyntaxError, KeyError, OSError):
        return dict(proved=False, trace=[], missing=[], facts=[dict(kind='subprocess_source_unavailable',
                    path=entry['path'], status='SOURCE_UNAVAILABLE')], issue_code='configuration_source_unavailable')
    selected = next((r for r in flow.results if r['proved']), None)
    facts = flow.process_facts + flow.source_gaps + [f for r in flow.results for f in r.get('facts', [])]
    missing = selected.get('missing', []) if selected else [m for r in flow.results for m in r.get('missing', [])] + flow.unresolved
    unavailable = any(f.get('status') == 'SOURCE_UNAVAILABLE' for f in facts)
    return dict(proved=selected is not None, trace=selected['trace'] if selected else [], missing=missing,
                facts=facts, rule='finite_original_subprocess_environment_calls', conditional_use=True,
                issue_code='evidence_incomplete' if missing else 'configuration_source_unavailable' if unavailable
                else 'configuration_flow_unsupported')


def process_environment_flow(inspector, entry, parameter, field, evidence, closure=None):
    """Prove inheritance to an actual child consumer in this frozen source tree.

    ``closure`` may be supplied by a caller sharing a host reachability audit;
    reachability alone never substitutes for evaluating the original env and
    argv expressions here. No candidate child entry is required or trusted.
    """
    if entry.get('kind') != 'cli' or entry.get('unit', 'file') != 'file':
        return dict(proved=False, trace=[], missing=[], facts=[], issue_code='configuration_flow_unsupported')
    try:
        context = process_context(inspector, entry)
    except ValueError:
        return dict(proved=False, trace=[], missing=[], facts=[], issue_code='execution_context_unproven')
    return _process_environment_flow(inspector, entry, parameter, field, evidence, context['cwd'], (entry['path'],))
