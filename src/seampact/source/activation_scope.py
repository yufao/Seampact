"""Independent finite upper bounds for original source activation.

This deliberately over-approximates: every body in an imported source module
is included, including dormant functions and every branch.  It never asks the
dependency tracer, a negative mutation check or a candidate permission flag
which edges to remove.  An opaque import/call/implicit callback prevents a
smaller source domain.  A source upper bound is not an execution permission;
the original worker's object and read/use checks remain separate.
"""

import ast
import hashlib
import json
import posixpath
import sys
import types
import builtins
from pathlib import PurePosixPath

from seampact.source.inspection import units, native_syntax_issues


RULE = 'source-complete-finite-activation-scope/1'
GAP = 'finite activation scope requires original worker object and synchronous read-use checks'
LIMIT = 2048
_GETTRACE, _GETPROFILE = sys.gettrace, sys.getprofile


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                    ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _literal(node):
    if node is None:
        return True
    if type(node) is ast.Constant:
        return type(node.value) in (type(None), bool, int, float, str, bytes)
    if type(node) in (ast.Tuple, ast.List, ast.Set):
        return all(_literal(item) for item in node.elts)
    if type(node) is ast.Dict:
        return all(key is not None and _literal(key) and _literal(value)
                   for key, value in zip(node.keys, node.values))
    return False


class ActivationScope:
    """A bounded source plan; no source or SDK import occurs here."""

    def __init__(self, inspector, entry, candidate=None):
        self.inspector = inspector
        self.entry = entry
        self.modules, self.frontiers, self.edges, self.callables = {}, [], [], []
        self._trees, self._pending = {}, []
        path = entry.get('path') if type(entry) is dict else None
        if (type(path) is not str or path not in inspector.files or not path.endswith('.py')
                or entry.get('unit', 'file') != 'file'):
            self._gap(path, None, 'entry_source_not_a_frozen_python_module')
        elif entry.get('binding_program'):
            self._gap(path, None, 'entry_binding_program_activation_not_closed')
        else:
            self._add(path, None, 'ENTRY')
            while self._pending and len(self.modules) <= LIMIT:
                self._scan(self._pending.pop(0))
            if self._pending or len(self.modules) > LIMIT:
                self._gap(path, None, 'finite_source_activation_module_bound_exceeded')
        self.closed = bool(self.modules) and not self.frontiers

    def _gap(self, path, node, reason, **facts):
        row = dict(path=path, line=getattr(node, 'lineno', None), reason=reason, **facts)
        if row not in self.frontiers:
            self.frontiers.append(row)

    def _tree(self, path):
        if path not in self._trees:
            self._trees[path] = ast.parse(dict(units(path, self.inspector.data(path)))['file'])
        return self._trees[path]

    def _roots(self, path):
        parent = PurePosixPath(path).parent
        roots, package = [parent.as_posix()], parent
        while package.parts and (package / '__init__.py').as_posix() in self.inspector.files:
            package = package.parent
        if package != parent:
            roots.append(package.as_posix())
        return list(dict.fromkeys([*roots, '.', 'src']))

    def _resolve(self, path, module, level=0):
        if level:
            root = PurePosixPath(path).parent
            for _ in range(level - 1):
                root = root.parent
            names = [posixpath.normpath(str(root / module.replace('.', '/')))]
        else:
            names = [posixpath.normpath(posixpath.join(root, module.replace('.', '/')))
                     for root in self._roots(path)]
        choices = [name + suffix for name in names for suffix in ('.py', '/__init__.py')]
        return next((name for name in choices if name in self.inspector.files), None)

    def _add(self, path, origin, operation):
        if path not in self.modules:
            # Every original parent initializer can execute before the child.
            parents = []
            parent = PurePosixPath(path).parent
            while parent.parts and parent.name.isidentifier():
                if parent.as_posix() == 'src':
                    break  # This is an original supported import root, not a package.
                initializer = (parent / '__init__.py').as_posix()
                if initializer != path:
                    if initializer not in self.inspector.files:
                        self._gap(path, None, 'source_parent_initializer_or_namespace_path_not_closed',
                                  parent=parent.as_posix())
                    else:
                        parents.append(initializer)
                parent = parent.parent
            # Mark first to terminate original cyclic/partially initialized imports.
            self.modules[path] = self.inspector.files[path]['sha256']
            for initializer in reversed(parents):
                self._add(initializer, path, 'PARENT_INITIALIZER')
            self._pending.append(path)
        edge = dict(source=origin, target=path, operation=operation)
        if edge not in self.edges:
            self.edges.append(edge)

    def _scan(self, path):
        try:
            tree = self._tree(path)
        except (SyntaxError, ValueError, OSError, KeyError) as error:
            self._gap(path, None, 'activated_source_not_parseable_or_available',
                      detail=type(error).__name__)
            return
        for issue in native_syntax_issues(tree):
            self._gap(path, None, 'activated_native_syntax_not_covered', detail=issue)
        aliases, source_aliases, definitions = {}, {}, set()
        # Include all lexical scopes. Ambiguous bindings are conservatively rejected
        # rather than assuming that a same-spelled local is the imported object.
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                definitions.add(node.name)
                if not isinstance(node, ast.ClassDef):
                    self.callables.append(dict(path=path, symbol=node.name, line=node.lineno,
                        end=node.end_lineno, coverage='ENTIRE_BODY_ALL_BRANCHES'))
            if isinstance(node, ast.Import):
                for item in node.names:
                    target = self._resolve(path, item.name)
                    local = item.asname or item.name.split('.')[0]
                    aliases.setdefault(local, set()).add(item.name)
                    if target is None:
                        self._gap(path, node, 'external_import_activation_or_callback_frontier',
                                  module=item.name)
                    else:
                        source_aliases.setdefault(local, set()).add(target)
                        self._add(target, path, 'STATIC_IMPORT_ALL_BODIES')
            elif isinstance(node, ast.ImportFrom):
                if any(item.name == '*' for item in node.names):
                    self._gap(path, node, 'star_import_export_and_activation_not_closed')
                target = self._resolve(path, node.module or '', node.level)
                if target is None:
                    self._gap(path, node, 'import_target_activation_not_frozen_or_external',
                              module=node.module, level=node.level)
                else:
                    self._add(target, path, 'STATIC_FROM_IMPORT_ALL_BODIES')
                for item in node.names:
                    local = item.asname or item.name
                    aliases.setdefault(local, set()).add((node.module or '') + '.' + item.name)
                    child_module = ((node.module + '.') if node.module else '') + item.name
                    child = self._resolve(path, child_module, node.level)
                    if child is not None:
                        self._add(child, path, 'POSSIBLE_FROM_CHILD_IMPORT')
                        source_aliases.setdefault(local, set()).add(child)
        repeated = {name for name in definitions if sum(
            isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name == name
            for n in ast.walk(tree)) > 1}
        arguments = {arg.arg for node in ast.walk(tree)
                     if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
                     for arg in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs,
                                 *([node.args.vararg] if node.args.vararg else []),
                                 *([node.args.kwarg] if node.args.kwarg else [])]}
        stores = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
                  and isinstance(node.ctx, (ast.Store, ast.Del))}
        for node in ast.walk(tree):
            if isinstance(node, (ast.AsyncFunctionDef, ast.Await, ast.Yield, ast.YieldFrom,
                                 ast.AsyncFor, ast.AsyncWith)):
                self._gap(path, node, 'suspension_or_concurrent_callback_not_closed')
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.decorator_list:
                    self._gap(path, node, 'definition_decorator_callback_not_closed')
                if any(not _literal(value) for value in [*node.args.defaults, *node.args.kw_defaults]):
                    self._gap(path, node, 'definition_default_activation_not_closed')
                annotations = [a.annotation for a in [*node.args.posonlyargs, *node.args.args,
                    *node.args.kwonlyargs, *([node.args.vararg] if node.args.vararg else []),
                    *([node.args.kwarg] if node.args.kwarg else [])]] + [node.returns]
                if any(value is not None and not _literal(value) for value in annotations):
                    self._gap(path, node, 'definition_annotation_activation_not_closed')
            if isinstance(node, ast.ClassDef):
                if (node.decorator_list or node.keywords or any(
                        not isinstance(base, ast.Name) or base.id != 'object'
                        or 'object' in arguments | stores | definitions | aliases.keys()
                        for base in node.bases)):
                    self._gap(path, node, 'class_base_metaclass_or_decorator_callback_not_closed')
                if any(isinstance(child, ast.FunctionDef) and child.name.startswith('__')
                       and child.name.endswith('__') for child in node.body):
                    self._gap(path, node, 'custom_class_implicit_protocol_not_closed')
            if isinstance(node, ast.Call):
                if any(item.arg is None for item in node.keywords):
                    self._gap(path, node, 'call_keyword_mapping_expansion_not_closed')
                # A call to an unshadowed local definition cannot introduce source
                # absent from this upper bound: all its body is already included.
                if (isinstance(node.func, ast.Name) and node.func.id in definitions
                        and node.func.id not in repeated | arguments | stores
                        and node.func.id not in aliases):
                    continue
                # This is an independent finite native premise, not a call to
                # TypeReferences.builtins_closed. Literal native operands and
                # an original builtin type have no user instance/metaclass
                # callback; the worker separately checks the actual builtins.
                if (isinstance(node.func, ast.Name) and node.func.id == 'isinstance'
                        and 'isinstance' not in definitions | arguments | stores | aliases.keys()
                        and len(node.args) == 2 and not node.keywords and _literal(node.args[0])
                        and isinstance(node.args[1], ast.Name)
                        and node.args[1].id in {'str', 'int', 'float', 'bool', 'list', 'dict'}
                        and node.args[1].id not in definitions | arguments | stores | aliases.keys()):
                    continue
                self._gap(path, node, 'call_target_callback_or_dynamic_import_not_closed',
                          expression=ast.unparse(node.func))
            if isinstance(node, ast.Attribute):
                root = node.value
                while isinstance(root, ast.Attribute):
                    root = root.value
                if (not isinstance(root, ast.Name) or root.id not in source_aliases
                        or root.id in arguments | stores or len(source_aliases[root.id]) != 1):
                    self._gap(path, node, 'attribute_descriptor_or_reflective_access_not_closed',
                              expression=ast.unparse(node))
            if isinstance(node, (ast.Subscript, ast.BinOp, ast.UnaryOp, ast.Compare,
                                 ast.BoolOp, ast.With, ast.For, ast.ListComp, ast.SetComp,
                                 ast.DictComp, ast.GeneratorExp, ast.NamedExpr,
                                 ast.JoinedStr, ast.Starred, ast.Raise, ast.Match, ast.AugAssign)):
                self._gap(path, node, 'implicit_value_or_control_callback_not_closed',
                          expression=type(node).__name__)
            if isinstance(node, (ast.If, ast.While, ast.IfExp, ast.Assert)) and not _literal(node.test):
                self._gap(path, node, 'implicit_truth_callback_not_closed')
            if isinstance(node, ast.AnnAssign) and not _literal(node.annotation):
                self._gap(path, node, 'assignment_annotation_activation_not_closed')
            if isinstance(node, ast.Dict) and any(key is None for key in node.keys):
                self._gap(path, node, 'mapping_expansion_protocol_not_closed')
            if (isinstance(node, ast.Dict) and any(key is not None and not _literal(key) for key in node.keys)
                    or isinstance(node, ast.Set) and any(not _literal(item) for item in node.elts)):
                self._gap(path, node, 'collection_key_hash_or_equality_callback_not_closed')
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if any(isinstance(target, (ast.Tuple, ast.List)) for target in targets):
                    self._gap(path, node, 'assignment_unpacking_protocol_not_closed')
            if isinstance(node, ast.ExceptHandler) and node.type is not None:
                self._gap(path, node, 'exception_type_match_protocol_not_closed')
            if isinstance(node, ast.Name) and node.id in {'__builtins__', '__loader__', '__spec__'}:
                self._gap(path, node, 'reflective_loader_or_builtin_namespace_access_not_closed')

    def paths(self):
        """Only a complete upper bound can replace the old full-source scan."""
        return tuple(sorted(self.modules)) if self.closed else tuple(sorted(
            path for path in self.inspector.files if path.endswith('.py')))

    def requirements(self):
        plan = dict(schema=RULE, source_version=self.inspector.request.get('source_version'),
            entry={name: self.entry.get(name) for name in ('id', 'path', 'unit', 'kind', 'symbol',
                                                         'constructor', 'initializers', 'fixed_arguments')},
            source_status='STRUCTURE_CHECKED' if self.closed else 'UNKNOWN',
            target_status='REQUIRES_NORMAL_WORKER_CHECK', execution_permission=False,
            modules=dict(sorted(self.modules.items())), import_edges=self.edges,
            callable_coverage=self.callables, frontiers=self.frontiers,
            coverage='all original module/definition expressions and every callable body/branch',
            required=['original loaded modules/functions/classes/import aliases match this source plan',
                      'no unknown dynamic import, callback, plugin or concurrent writer',
                      'same original binding owner and synchronous lock at every read/use',
                      'independent SDK/native effects remain checked separately'])
        plan['digest'] = _digest(plan)
        return plan


def _same_immutable(actual, expected, depth=0):
    if depth > 32 or type(actual) is not type(expected):
        return False
    if type(expected) in (type(None), bool, int, str, bytes):
        return actual == expected
    if type(expected) is float:
        return float.hex(actual) == float.hex(expected)
    return (type(expected) is tuple and len(actual) == len(expected)
            and all(_same_immutable(a, b, depth + 1) for a, b in zip(actual, expected)))


def _namespace(module):
    if type(module) is not types.ModuleType:
        raise ValueError('original source module is not an exact module')
    namespace = types.ModuleType.__dict__['__dict__'].__get__(module, types.ModuleType)
    if type(namespace) is not dict or any(type(name) is not str for name in namespace):
        raise ValueError('original source namespace has an unknown key protocol')
    return namespace


def verify_activation_targets(inspector, entry, candidate, trace, *, binding=None, receipt=None):
    """Consume original import-only receipts inside their normal source lock.

    The finite positive domain has source modules, top-level native functions,
    static source imports and immutable globals.  No import, callable, property
    or model is executed for this check. Classes, nested closures, mutable
    globals and every opaque frontier remain UNKNOWN.
    """
    scope = ActivationScope(inspector, entry, candidate)
    plan = scope.requirements()
    try:
        from seampact.source.import_only import _metadata
        trace = _metadata(trace)
        requirements = [row.get('target_requirements') for row in trace.get('external_protocols', [])
                        if row.get('rule') == RULE]
        matched = bool(requirements) and all(value == plan for value in requirements)
    except (ValueError, TypeError, KeyError):
        matched = False
    result = dict(schema=RULE, status='UNKNOWN', allowed=False, execution_permission=False,
        source_status=plan['source_status'], requirement_matches_recomputed_source=matched,
        frontiers=plan['frontiers'], requirement_digests=[])
    try:
        if not matched or not scope.closed:
            raise ValueError('independent complete source activation upper bound is not proved')
        if (trace.get('status') != 'CHECKED' or trace.get('unresolved_targets')
                or trace.get('activation_prefix', {}).get('status') != 'CHECKED'
                or any(row.get('reason') != GAP for row in trace.get('negative_scope_gaps', []))):
            raise ValueError('independent source activation/call effects remain UNKNOWN')
        # Imports below are platform definitions, not source/SDK execution.
        from seampact.units import (NativeBindings, _native_context_lock,
            _verify_native_callable_snapshot, _plain_callable_defaults)
        from seampact.source.call_value_conditions import digest, _ORIGINALS
        if type(binding) is not NativeBindings or not _native_context_lock._is_owned():
            raise ValueError('original native binding and synchronous read/use lock are absent')
        if (type(receipt) is not dict or not any(value is receipt for value in binding.import_only_entries.values())
                or type(receipt.get('instance_identity')) is not dict or binding.detached):
            raise ValueError('original normal import-only occurrence/worker owner is absent')
        if binding.inspector.request.get('source_version') != plan['source_version']:
            raise ValueError('original binding source version differs from activation plan')
        # These native hooks would be independent unknown callbacks, even when
        # no corresponding call expression appears in frozen source.
        if _GETTRACE() is not None or _GETPROFILE() is not None:
            raise ValueError('unknown process tracing/profiling callback is active')
        if any(builtins.__dict__.get(name) is not original for name, original in _ORIGINALS.items()):
            raise ValueError('original native builtin operation binding changed')
        binding._verify_import_only_target(entry, receipt)
        if type(sys.modules) is not dict or type(binding.modules) is not dict:
            raise ValueError('original source module owner table is not exact')
        modules, module_names = {}, {}
        for path in scope.modules:
            names = [name for name, record in binding.module_paths.items()
                     if record[0] == path and not name.startswith('src.')]
            if len(names) != 1:
                raise ValueError('original source module import name is ambiguous')
            original = binding.modules.get(names[0])
            if original is None or sys.modules.get(names[0]) is not original:
                raise ValueError('complete original source module owner/current import identity is absent')
            modules[path] = original
            module_names[path] = names[0]
        helpers = receipt.get('helpers')
        if type(helpers) is not dict:
            raise ValueError('original imported source function receipts are absent')
        bookkeeping = {'__name__', '__doc__', '__package__', '__loader__', '__spec__',
                       '__file__', '__cached__', '__builtins__', '__path__', '__annotations__'}
        for path, module in modules.items():
            namespace = _namespace(module)
            if namespace.get('__builtins__') is not builtins.__dict__:
                raise ValueError('original source builtin namespace association changed')
            declarations = set()
            for node in scope._tree(path).body:
                if isinstance(node, ast.FunctionDef):
                    if (node.decorator_list or any(isinstance(child, (ast.FunctionDef, ast.ClassDef, ast.Lambda))
                            for statement in node.body for child in ast.walk(statement))):
                        raise ValueError('nested source creation requires a separate original target protocol')
                    item = helpers.get((path, node.name))
                    if (type(item) is not dict or item.get('source_sha256') != scope.modules[path]
                            or item.get('definition_ast_sha256') != hashlib.sha256(
                                ast.dump(node, include_attributes=False).encode()).hexdigest()
                            or item.get('namespace') is not namespace):
                        raise ValueError('original imported helper source/namespace receipt changed')
                    function = item.get('function')
                    if type(function) is not types.FunctionType or function.__globals__ is not namespace:
                        raise ValueError('original source helper function/global association changed')
                    current = namespace.get(node.name)
                    if current is not function:
                        if (function is not receipt.get('function') or current is not receipt.get('approved_proxy')
                                or current is None):
                            raise ValueError('original source helper binding changed')
                        _verify_native_callable_snapshot(current, receipt['approved_proxy_snapshot'])
                    _verify_native_callable_snapshot(function, item['snapshot'])
                    if _plain_callable_defaults(function) != item['defaults']:
                        raise ValueError('original source helper default content changed')
                    declarations.add(node.name)
                elif isinstance(node, (ast.Import, ast.ImportFrom)):
                    for alias in node.names:
                        local = alias.asname or (alias.name.split('.')[0] if isinstance(node, ast.Import) else alias.name)
                        if isinstance(node, ast.Import):
                            target = scope._resolve(path, alias.name if alias.asname else alias.name.split('.')[0])
                            expected = modules.get(target)
                        else:
                            target = scope._resolve(path, node.module or '', node.level)
                            parent = modules.get(target)
                            expected = _namespace(parent).get(alias.name)
                        if expected is None or namespace.get(local) is not expected:
                            raise ValueError('original static source import alias changed')
                        declarations.add(local)
                elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    if not all(isinstance(target, ast.Name) for target in targets):
                        raise ValueError('source global store target requires an unproved object protocol')
                    try:
                        expected = ast.literal_eval(node.value)
                    except (ValueError, TypeError, SyntaxError):
                        raise ValueError('source global is not an original immutable literal')
                    if not _same_immutable(expected, expected):
                        raise ValueError('mutable source global has no finite no-writer proof')
                    for target in targets:
                        if target.id not in namespace or not _same_immutable(namespace[target.id], expected):
                            raise ValueError('original immutable source global changed')
                        declarations.add(target.id)
                elif isinstance(node, ast.Pass) or (isinstance(node, ast.Expr)
                        and isinstance(node.value, ast.Constant) and type(node.value.value) is str):
                    continue
                else:
                    raise ValueError('original top-level creation/control protocol is outside the finite actual domain')
            # Python's original import machinery also binds an imported child
            # on its parent package. This is the same source-owned module,
            # not an arbitrary additional export or an SDK-created object.
            parent_name = module_names[path]
            for child_path, child in modules.items():
                child_name = module_names[child_path]
                prefix = parent_name + '.'
                if child_name.startswith(prefix) and '.' not in child_name[len(prefix):]:
                    local = child_name[len(prefix):]
                    if namespace.get(local) is child:
                        declarations.add(local)
            if set(namespace) - declarations - bookkeeping:
                raise ValueError('original source namespace has an unexplained additional binding')
        result.update(status='CHECKED', allowed=True, requirement_digests=[digest(plan)],
            modules_checked=sorted(modules),
            scope='original plain-source import occurrence and same-lock binding read/use only; SDK effects remain separate')
    except (ValueError, KeyError, TypeError, RuntimeError) as error:
        result['reason'] = str(error)
    return result
