"""Finite type-operand non-escape facts, without importing source or an SDK.

An external name, source digest or same-named loaded class never certifies the
original SDK object. Observations of the check process and target conditions
are separate. Only a unique frozen ordinary empty class has a source identity
proof here; this removes one type-reference escape, not the operation's other
operand protocols, handler effects, execution or lifecycle obligations.
"""

import ast
import builtins
import sys
import types


RULE = "source-finite-type-reference/1"
_TYPE = type
_BASE_EXCEPTION = BaseException
_EXCEPTION = Exception
_OBJECT = object
_ISINSTANCE = isinstance
_MODULE_TYPE = types.ModuleType
_MAPPING_PROXY_TYPE = types.MappingProxyType
_BUILTIN_FUNCTION_TYPE = types.BuiltinFunctionType
_DICT_TYPE = dict
_STR_TYPE = str
_TYPE_DICT = _TYPE.__dict__['__dict__']
_TYPE_MRO = _TYPE.__dict__['__mro__']
_TYPE_QUALNAME = _TYPE.__dict__['__qualname__']
_MODULE_DICT = _MODULE_TYPE.__dict__['__dict__']


def _scoped(nodes):
    for node in nodes:
        yield node
        if not _ISINSTANCE(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            yield from _scoped(ast.iter_child_nodes(node))


class TypeReferences:
    def __init__(self, inspector, tree):
        self.inspector, self.tree = inspector, tree
        # This object belongs to one frozen source replay. Cache source facts
        # only; the loaded builtin bindings below are checked on every use.
        self._builtins_source_closed = None
        self.source_scope_gaps = []
        self.activation_scope = None
        self._scope_used = False
        self._require_scope = None

    def bind_activation(self, scope, *, require_scope=None):
        # Only ActivationScope itself decides whether its independent upper
        # bound is complete. Serialized path lists/permission flags are not an
        # alternate way to narrow the source mutation domain.
        from seampact.source.activation_scope import ActivationScope
        if _TYPE(scope) is not ActivationScope or scope.inspector is not self.inspector:
            raise ValueError('original independent activation scope required')
        self.activation_scope = scope
        self._scope_used, self._require_scope = False, require_scope
        self._builtins_source_closed = None
        self.source_scope_gaps.clear()

    def source_paths(self):
        return (self.activation_scope.paths() if self._scope_used else
                tuple(path for path in self.inspector.files if path.endswith('.py')))

    def _frames(self, path, node):
        tree = self.tree(path)
        parents = {id(c): p for p in ast.walk(tree) for c in ast.iter_child_nodes(p)}
        frames, inside_function, inside_comprehension = [], False, False
        current = parents.get(id(node))
        while current is not None:
            if _ISINSTANCE(current, (ast.SetComp, ast.DictComp, ast.GeneratorExp, ast.Lambda)):
                return None
            if _ISINSTANCE(current, ast.ListComp):
                frames.append(current)
                inside_comprehension = True
            elif _ISINSTANCE(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
                inside_function = True
                frames.append(current)
            elif _ISINSTANCE(current, ast.ClassDef) and not inside_function:
                if inside_comprehension:
                    # The outer iterable and comprehension body have different
                    # access to class locals. That prefix is not in this subset.
                    return None
                frames.append(current)
            current = parents.get(id(current))
        return [*frames, tree]

    def resolve(self, path, expression, site, seen=()):
        if _ISINSTANCE(expression, ast.Attribute):
            parent = self.resolve(path, expression.value, site, seen)
            return parent + "." + expression.attr if parent else None
        if not _ISINSTANCE(expression, ast.Name) or expression.id in seen:
            return None
        name = expression.id
        frames = self._frames(path, site)
        if frames is None:
            return None
        for frame in frames:
            if _ISINSTANCE(frame, ast.ListComp):
                # A comprehension owns its targets. Only an unbound name can
                # continue to the enclosing lexical frames. Treat the first
                # iterable conservatively too; no new iteration/effect fact is
                # inferred by this lexical lookup.
                if any(generator.is_async for generator in frame.generators):
                    return None
                for generator in frame.generators:
                    targets = list(ast.walk(generator.target))
                    if any(not _ISINSTANCE(node, (ast.Name, ast.Tuple, ast.List, ast.Store)) for node in targets):
                        return None
                    if any(_ISINSTANCE(node, ast.Name) and node.id == name for node in targets):
                        return None
                if any(_ISINSTANCE(node, ast.NamedExpr) for node in ast.walk(frame)):
                    return None
                continue
            nodes = list(_scoped(frame.body))
            if _ISINSTANCE(frame, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = frame.args
                if name in {a.arg for a in [*args.posonlyargs, *args.args, *args.kwonlyargs,
                    *([args.vararg] if args.vararg else []), *([args.kwarg] if args.kwarg else [])]}:
                    return None
            bindings = []
            for node in nodes:
                if _ISINSTANCE(node, (ast.Global, ast.Nonlocal)) and name in node.names:
                    return None
                if _ISINSTANCE(node, (ast.Import, ast.ImportFrom)):
                    for alias in node.names:
                        local = alias.asname or (alias.name if _ISINSTANCE(node, ast.ImportFrom) else alias.name.split('.')[0])
                        if alias.name == '*':
                            return None
                        if local == name:
                            qualified = ((node.module or '') + '.' + alias.name if _ISINSTANCE(node, ast.ImportFrom)
                                         else alias.name if alias.asname else alias.name.split('.')[0])
                            bindings.append((node, qualified if not getattr(node, 'level', 0) else None))
                elif _ISINSTANCE(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == name:
                    bindings.append((node, '@source:' + path + ':' + name if _ISINSTANCE(node, ast.ClassDef) else None))
                elif _ISINSTANCE(node, ast.Name) and node.id == name and _ISINSTANCE(node.ctx, (ast.Store, ast.Del)):
                    bindings.append((node, None))
                elif _ISINSTANCE(node, ast.ExceptHandler) and node.name == name:
                    bindings.append((node, None))
            if bindings:
                if len(bindings) != 1 or bindings[0][0] not in frame.body or bindings[0][0].lineno >= site.lineno:
                    return None
                return bindings[0][1]
        return 'builtins.' + name if name in {'isinstance', 'object', 'Exception', 'BaseException',
                                            'list', 'dict', 'str', 'int', 'float', 'bool', 'len'} else None

    def _aliases(self, path):
        aliases = {}
        tree = self.tree(path)
        for node in ast.walk(tree):
            if _ISINSTANCE(node, ast.Import):
                for item in node.names:
                    aliases.setdefault(item.asname or item.name.split('.')[0], set()).add(item.name if item.asname else item.name.split('.')[0])
            elif _ISINSTANCE(node, ast.ImportFrom) and not node.level:
                for item in node.names:
                    aliases.setdefault(item.asname or item.name, set()).add((node.module or '') + '.' + item.name)
        def names(node):
            if _ISINSTANCE(node, ast.Name):
                return aliases.get(node.id, set())
            if _ISINSTANCE(node, ast.Attribute):
                return {n + '.' + node.attr for n in names(node.value)}
            if _ISINSTANCE(node, ast.Subscript):
                return names(node.value)
            return set()
        return tree, aliases, names

    def _source_builtins_closed(self, paths=None):
        for source in self.source_paths() if paths is None else paths:
            if not source.endswith('.py'):
                continue
            try:
                tree, _, names = self._aliases(source)
            except (SyntaxError, ValueError, KeyError, OSError) as error:
                # A frozen repository may contain non-importable fragments.
                # They prevent this full-scope proof; never silently skip them
                # or abort the complete candidate with an uncaught parse error.
                self.source_scope_gaps.append(dict(path=source, status='UNKNOWN',
                    reason='frozen_source_not_parseable_or_available',
                    line=getattr(error, 'lineno', None)))
                return False
            for node in ast.walk(tree):
                if _ISINSTANCE(node, ast.Name) and node.id == '__builtins__':
                    return False
                targets = node.targets if _ISINSTANCE(node, (ast.Assign, ast.Delete)) else [node.target] if _ISINSTANCE(node, (ast.AnnAssign, ast.AugAssign)) else []
                if any(any(n == 'builtins' or n.startswith('builtins.') for n in names(t))
                       for t in targets if _ISINSTANCE(t, (ast.Attribute, ast.Subscript))):
                    return False
                if _ISINSTANCE(node, ast.Call) and any('builtins' in names(arg) or 'builtins.isinstance' in names(arg)
                    for arg in [*node.args, *(k.value for k in node.keywords)]):
                    return False
        return True

    def builtins_closed(self):
        if _TYPE(builtins) is not _MODULE_TYPE:
            return False
        if self._builtins_source_closed is None:
            paths = tuple(path for path in self.inspector.files if path.endswith('.py'))
            self._builtins_source_closed = self._source_builtins_closed(paths)
            if (not self._builtins_source_closed and self.activation_scope is not None
                    and self.activation_scope.closed and set(self.activation_scope.paths()) != set(paths)):
                full_gaps = list(self.source_scope_gaps)
                self.source_scope_gaps.clear()
                self._builtins_source_closed = self._source_builtins_closed(self.activation_scope.paths())
                if self._builtins_source_closed:
                    self._scope_used = True
                    if self._require_scope is not None:
                        self._require_scope()
                else:
                    self.source_scope_gaps[:] = full_gaps
        if not self._builtins_source_closed:
            return False
        namespace = _MODULE_DICT.__get__(builtins, _MODULE_TYPE)
        if _TYPE(namespace) is not _DICT_TYPE:
            return False
        builder = namespace.get('__build_class__')
        if (_TYPE(builder) is not _BUILTIN_FUNCTION_TYPE or builder.__name__ != '__build_class__'
                or builder.__module__ != 'builtins' or builder.__self__ is not builtins):
            return False
        return all(namespace.get(name) is original for name, original in (
            ('object', _OBJECT), ('type', _TYPE), ('Exception', _EXCEPTION), ('BaseException', _BASE_EXCEPTION)))

    def builtin(self, path, call):
        result = dict(status="UNKNOWN", operation="isinstance", lexical_binding="UNKNOWN",
                      target_binding="UNKNOWN", check_process_observation=dict(status='OBSERVATION_ONLY', binding='UNKNOWN'))
        if self.activation_scope is not None and self.activation_scope.closed and path not in self.activation_scope.modules:
            return result
        if (not _ISINSTANCE(call, ast.Call) or len(call.args) != 2 or call.keywords
                or any(_ISINSTANCE(arg, ast.Starred) for arg in call.args)):
            return result
        try:
            if self.resolve(path, call.func, call) != 'builtins.isinstance' or not self.builtins_closed():
                return result
        except (SyntaxError, ValueError, KeyError, OSError):
            return result
        result['lexical_binding'] = 'CHECKED'
        value = _MODULE_DICT.__get__(builtins, _MODULE_TYPE).get('isinstance')
        if (_TYPE(value) is not _BUILTIN_FUNCTION_TYPE or value.__name__ != 'isinstance'
                or value.__module__ != 'builtins' or value.__self__ is not builtins):
            return result
        result.update(status='SOURCE_CHECKED', check_process_observation=dict(status='OBSERVATION_ONLY', binding='OBSERVED_NATIVE_BUILTIN'),
                      scope='frozen source operation identity; target runtime remains independent')
        return result

    def _source_class(self, qualified):
        if qualified.startswith('@source:'):
            path, name = qualified[len('@source:'):].rsplit(':', 1)
        else:
            module, _, name = qualified.rpartition('.')
            choices = [module.replace('.', '/') + '.py', module.replace('.', '/') + '/__init__.py']
            path = next((p for p in choices if p in self.inspector.files), None)
        if path is None or path not in self.inspector.files:
            return None
        tree = self.tree(path)
        bindings = [n for n in _scoped(tree.body) if _ISINSTANCE(n, ast.ClassDef) and n.name == name
                    or _ISINSTANCE(n, ast.Name) and n.id == name and _ISINSTANCE(n.ctx, (ast.Store, ast.Del))
                    or _ISINSTANCE(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name]
        if len(bindings) != 1 or not _ISINSTANCE(bindings[0], ast.ClassDef) or bindings[0] not in tree.body:
            return None
        cls = bindings[0]
        if cls.decorator_list or cls.keywords or len(cls.bases) > 1:
            return None
        if any(not _ISINSTANCE(n, ast.Pass) and not (_ISINSTANCE(n, ast.Expr) and _ISINSTANCE(n.value, ast.Constant)
                   and _ISINSTANCE(n.value.value, str)) for n in cls.body):
            return None
        base = self.resolve(path, cls.bases[0], cls) if cls.bases else 'builtins.object'
        if base not in {'builtins.object', 'builtins.Exception', 'builtins.BaseException'}:
            return None
        return path, cls, base != 'builtins.object'

    def pure_declaration_profile(self):
        """Deliberately small complete frozen prefix, including parent packages.

        No opaque import/call, dynamic code, implicit control/value protocol or
        definition-time expression is accepted as an ordinary class origin.
        This is a source-profile fact, never a target activation observation.
        """
        forbidden = (ast.If, ast.IfExp, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith,
            ast.BoolOp, ast.Compare, ast.BinOp, ast.UnaryOp, ast.Subscript, ast.Lambda,
            ast.ListComp, ast.DictComp, ast.SetComp, ast.GeneratorExp, ast.Await, ast.Yield,
            ast.YieldFrom, ast.NamedExpr, ast.Global, ast.Nonlocal, ast.Delete, ast.AugAssign)
        for path in self.source_paths():
            if not path.endswith('.py'):
                continue
            tree = self.tree(path)
            parents = {id(c): p for p in ast.walk(tree) for c in ast.iter_child_nodes(p)}
            for node in ast.walk(tree):
                if _ISINSTANCE(node, forbidden):
                    return False
                if _ISINSTANCE(node, (ast.Import, ast.ImportFrom)):
                    modules = [a.name for a in node.names] if _ISINSTANCE(node, ast.Import) else [node.module or '']
                    if getattr(node, 'level', 0):
                        return False
                    for module in modules:
                        if module == 'builtins':
                            continue
                        prefix = module.split('.')
                        if not any(module.replace('.', '/') + suffix in self.inspector.files for suffix in ('.py', '/__init__.py')):
                            return False
                        if any('/'.join(prefix[:end]) + '/__init__.py' not in self.inspector.files for end in range(1, len(prefix))):
                            return False
                if _ISINSTANCE(node, ast.ClassDef) and self._source_class('@source:' + path + ':' + node.name) is None:
                    return False
                if _ISINSTANCE(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    args = node.args
                    parameters = [*args.posonlyargs, *args.args, *args.kwonlyargs,
                        *([args.vararg] if args.vararg else []), *([args.kwarg] if args.kwarg else [])]
                    if (node.name in {'__getattr__', '__getattribute__'} or node.decorator_list or node.returns is not None or any(a.annotation is not None for a in parameters)
                            or any(not _ISINSTANCE(n, ast.Constant) for n in [*args.defaults, *(n for n in args.kw_defaults if n is not None)])):
                        return False
                if _ISINSTANCE(node, (ast.Assign, ast.AnnAssign)):
                    targets = node.targets if _ISINSTANCE(node, ast.Assign) else [node.target]
                    if any(not _ISINSTANCE(t, ast.Name) for t in targets):
                        return False
                    try:
                        ast.literal_eval(node.value)
                    except (ValueError, TypeError, SyntaxError):
                        return False
                if _ISINSTANCE(node, ast.Call):
                    target = self.resolve(path, node.func, node)
                    if target == 'builtins.isinstance':
                        if (len(node.args) != 2 or node.keywords
                                or not _ISINSTANCE(node.args[0], (ast.Name, ast.Constant))
                                or not _ISINSTANCE(node.args[1], (ast.Name, ast.Attribute))):
                            return False
                    elif target is None or self._source_class(target) is None or node.keywords or any(
                            not _ISINSTANCE(a, (ast.Name, ast.Constant)) for a in node.args):
                        return False
                if _ISINSTANCE(node, ast.Attribute):
                    parent = parents.get(id(node))
                    if _ISINSTANCE(parent, ast.Attribute) and parent.value is node:
                        continue
                    resolved = self.resolve(path, node, node)
                    if resolved != 'builtins.isinstance' and self._source_class(resolved or '') is None and resolved not in {
                            'builtins.object', 'builtins.Exception', 'builtins.BaseException'}:
                        return False
        return True

    def _class_uses_closed(self, qualified, source_path, cls):
        canonical = qualified if not qualified.startswith('@source:') else source_path[:-3].replace('/', '.') + '.' + cls.name
        module = canonical.rpartition('.')[0]
        for path in self.source_paths():
            if not path.endswith('.py'):
                continue
            tree, aliases, names = self._aliases(path)
            if path == source_path:
                aliases.setdefault(cls.name, set()).add(canonical)
            parents = {id(c): p for p in ast.walk(tree) for c in ast.iter_child_nodes(p)}
            for node in ast.walk(tree):
                targets = node.targets if _ISINSTANCE(node, (ast.Assign, ast.Delete)) else [node.target] if _ISINSTANCE(node, (ast.AnnAssign, ast.AugAssign)) else []
                if any(canonical in names(t) or any(n.startswith(canonical + '.') or n == module + '.__dict__' for n in names(t)) for t in targets):
                    return False
                if not _ISINSTANCE(node, (ast.Name, ast.Attribute)) or canonical not in names(node):
                    if _ISINSTANCE(node, ast.Call) and any(module in names(arg) for arg in [*node.args, *(k.value for k in node.keywords)]):
                        return False
                    continue
                parent = parents.get(id(node))
                if _ISINSTANCE(parent, ast.Attribute) and parent.value is node:
                    return False
                if _ISINSTANCE(parent, ast.Call) and parent.func is node:
                    continue  # Original construction keeps its independent effects.
                if (_ISINSTANCE(parent, ast.Call) and len(parent.args) == 2 and parent.args[1] is node
                        and self.resolve(path, parent.func, parent) == 'builtins.isinstance' and not parent.keywords):
                    continue
                if _ISINSTANCE(parent, ast.ExceptHandler) and parent.type is node:
                    continue
                return False
        return True

    @staticmethod
    def loaded(qualified, exception=False):
        result = dict(status='OBSERVATION_ONLY', loaded_binding='NOT_OBSERVED',
                      installed_source='UNKNOWN', metaclass='UNKNOWN')
        if _TYPE(sys) is not _MODULE_TYPE:
            return result
        modules = _MODULE_DICT.__get__(sys, _MODULE_TYPE).get('modules')
        if _TYPE(modules) is not _DICT_TYPE:
            return result
        parts = qualified.split('.')
        value = None
        for end in range(len(parts), 0, -1):
            module = modules.get('.'.join(parts[:end]))
            if _TYPE(module) is not _MODULE_TYPE:
                continue
            value = module
            for name in parts[end:]:
                if _TYPE(value) is _MODULE_TYPE:
                    namespace = _MODULE_DICT.__get__(value, _MODULE_TYPE)
                    if _TYPE(namespace) is not _DICT_TYPE:
                        return result
                    value = namespace.get(name)
                elif _TYPE(value) is _TYPE:
                    namespace = _TYPE_DICT.__get__(value, _TYPE)
                    if _TYPE(namespace) is not _MAPPING_PROXY_TYPE:
                        return result
                    value = namespace.get(name)
                else:
                    value = None
                    break
            break
        if value is None:
            return result
        if _TYPE(value) is not _TYPE:
            result['loaded_binding'] = 'NOT_AN_EXACT_ORDINARY_CLASS'
            return result
        namespace = _TYPE_DICT.__get__(value, _TYPE)
        if _TYPE(namespace) is not _MAPPING_PROXY_TYPE:
            return result
        owner = namespace.get('__module__')
        name = _TYPE_QUALNAME.__get__(value, _TYPE)
        if _TYPE(owner) is not _STR_TYPE or _TYPE(name) is not _STR_TYPE or '.' in name or name != parts[-1]:
            return result
        result.update(loaded_binding='OBSERVED_ORDINARY_CLASS_OBJECT', metaclass='OBSERVED_EXACT_BUILTIN_TYPE')
        if exception and not any(base is _BASE_EXCEPTION for base in _TYPE_MRO.__get__(value, _TYPE)):
            result['loaded_binding'] = 'OBSERVED_NON_EXCEPTION_CLASS'
            return result
        # __file__ and same-named metadata have no authenticated SDK origin.
        # Do not open paths supplied by a loaded module/class object.
        result['installed_source'] = 'NOT_VERIFIED_NO_AUTHENTICATED_SDK_ORIGIN'
        return result

    def check(self, path, operation, operand, qualified=None):
        kind = 'isinstance_type_operand' if _ISINSTANCE(operation, ast.Call) else 'except_type_reference'
        fact = dict(rule=RULE, status='UNKNOWN', kind=kind, path=path, line=operation.lineno,
                    evidence=[dict(path=path, unit='file', start=operand.lineno, end=operand.end_lineno)],
                    scope='one frozen type-reference occurrence only; not operation effects or execution permission',
                    first_operand_protocol='UNKNOWN', handler_effects='UNKNOWN', lifecycle='UNKNOWN', target_state='UNKNOWN')
        if self.activation_scope is not None and self.activation_scope.closed and path not in self.activation_scope.modules:
            return fact
        if not _ISINSTANCE(operand, (ast.Name, ast.Attribute)):
            return fact
        try:
            resolved = self.resolve(path, operand, operation)
        except (SyntaxError, ValueError, KeyError, OSError):
            return fact
        if resolved is None or qualified is not None and qualified != resolved:
            return fact
        fact['reference'] = resolved
        if _ISINSTANCE(operation, ast.Call):
            fact['builtin'] = self.builtin(path, operation)
            fact['check_process_observation'] = dict(builtin=fact['builtin'].pop('check_process_observation'))
            if len(operation.args) != 2 or operation.args[1] is not operand or fact['builtin']['status'] != 'SOURCE_CHECKED':
                return fact
        elif not _ISINSTANCE(operation, ast.ExceptHandler) or operation.type is not operand:
            return fact
        try:
            source = self._source_class(resolved)
            if source is not None:
                definition_path, cls, is_exception = source
                if kind == 'except_type_reference' and not is_exception:
                    return fact
                if self.builtins_closed() and self.pure_declaration_profile() and self._class_uses_closed(resolved, definition_path, cls):
                    fact.update(status='SOURCE_CHECKED', identity=dict(kind='unique_frozen_ordinary_class', path=definition_path,
                        symbol=cls.name, sha256=self.inspector.files[definition_path]['sha256']),
                        metaclass='SOURCE_PROVED_BUILTIN_TYPE', exception_class=is_exception,
                        source_profile_preconditions=['ordinary Python source profile', 'normal original import of the complete frozen pure declaration prefix',
                            'original builtin class builder and bases', 'no external namespace rebinding before this occurrence'],
                        activation_status='UNKNOWN', boolean_guard_permission=False)
                    return fact
        except (SyntaxError, ValueError, KeyError, OSError):
            return fact
        if not resolved.startswith('@source:'):
            fact['external_class_conditions'] = dict(status='UNKNOWN', original_class_identity='UNKNOWN', target_state='UNKNOWN',
                conditional_non_escape=dict(status='CONDITIONAL', required=[
                    'this exact operand is the original unchanged class object at the original operation',
                    'original builtin operation and matching metaclass protocol at that runtime point']))
            fact.setdefault('check_process_observation', {})['sdk_class'] = self.loaded(resolved, kind == 'except_type_reference')
        return fact
