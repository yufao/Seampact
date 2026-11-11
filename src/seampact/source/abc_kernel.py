"""Read-only checks of the core's early-captured original ABC kernel.

Import this core module before importing inspected source. These checks read
existing class state; they never construct an owner, register a subclass or
clear ABC state. Source identities and ordinary constructor effects remain
the normal binder and closure checker's responsibility.
"""

import abc
import _abc
import ast
import builtins
import math
import sys
import types


_TYPE, _OBJECT = type, object
_FUNCTION, _STATIC, _CLASS = types.FunctionType, staticmethod, classmethod
_MODULE, _DICT, _TUPLE, _FROZEN, _SET, _LIST = types.ModuleType, dict, tuple, frozenset, set, list
_BUILTIN_FUNCTION = types.BuiltinFunctionType
_STR, _INT, _FLOAT, _BOOL = str, int, float, bool
_LEN, _ALL, _ANY, _ZIP, _ISINSTANCE, _ENUMERATE = len, all, any, zip, isinstance, enumerate
_ISFINITE = math.isfinite
_MODULE_DICT = _MODULE.__dict__['__dict__']
_CLASS_DICT = _TYPE.__dict__['__dict__']
_MRO = _TYPE.__dict__['__mro__']
_BASES = _TYPE.__dict__['__bases__']
_ABC, _META = abc.ABC, abc.ABCMeta
_ABC_NS = _MODULE_DICT.__get__(abc, _MODULE)
_C_NS = _MODULE_DICT.__get__(_abc, _MODULE)
_BUILTINS_NS = _MODULE_DICT.__get__(builtins, _MODULE)
_ABC_GLOBALS = dict(_ABC_NS)
_C_GLOBALS = dict(_C_NS)
_ABC_CLASS = dict(_CLASS_DICT.__get__(_ABC, _TYPE))
_META_CLASS = dict(_CLASS_DICT.__get__(_META, _TYPE))
_BUILTINS = {name: _BUILTINS_NS[name] for name in ('__build_class__', '__import__', 'type', 'object',
    'super', 'staticmethod', 'classmethod', 'str', 'int', 'float', 'bool', 'list', 'dict', 'tuple', 'set', 'frozenset',
    'len', 'all', 'any', 'zip', 'isinstance', 'enumerate')}
_GET_DUMP = _C_NS['_get_dump']
_ABC_DATA = _TYPE(_ABC_CLASS['_abc_impl'])
_HOOKS = {'__new__', '__getattribute__', '__getattr__', '__setattr__', '__delattr__',
          '__init_subclass__', '__class_getitem__', '__mro_entries__', '__set_name__', '__del__'}


def _function(value):
    kind = _TYPE(value)
    return value.__func__ if kind is _STATIC or kind is _CLASS else value


_FUNCTIONS = [(function, function.__code__, function.__globals__, function.__defaults__, function.__kwdefaults__,
               function.__closure__, _TUPLE(cell.cell_contents for cell in function.__closure__) if function.__closure__ else ())
              for value in [*_META_CLASS.values(), _ABC_NS['abstractmethod']]
              if _TYPE(function := _function(value)) is _FUNCTION]


def _same_namespace(actual, expected):
    return (_ALL(_TYPE(name) is _STR for name in actual) and _ALL(_TYPE(name) is _STR for name in expected)
            and _LEN(actual) == _LEN(expected) and _ALL(name in actual and actual[name] is value for name, value in expected.items()))


def check_abc_kernel():
    """Check current identities against the trusted core bootstrap capture."""
    result = dict(schema='target-original-abc-kernel/1', status='UNKNOWN', source_import=False,
                  owner_construction=False, registry_mutation=False)
    try:
        sys_namespace = _MODULE_DICT.__get__(sys, _MODULE)
        if not _ALL(_TYPE(name) is _STR for name in sys_namespace):
            return dict(result, reason='kernel module namespace has a nonordinary key')
        modules = sys_namespace['modules']
        if (_TYPE(modules) is not _DICT or not _ALL(_TYPE(name) is _STR for name in modules)
                or not _ALL(_TYPE(name) is _STR for name in _BUILTINS_NS)):
            return dict(result, reason='kernel module table or builtins namespace has a nonordinary key')
        if (modules.get('abc') is not abc or modules.get('_abc') is not _abc
                or modules.get('builtins') is not builtins or _TYPE(abc) is not _MODULE or _TYPE(_abc) is not _MODULE
                or _TYPE(builtins) is not _MODULE):
            return dict(result, reason='original kernel module identities changed')
        if (not _same_namespace(_ABC_NS, _ABC_GLOBALS) or not _same_namespace(_C_NS, _C_GLOBALS)
                or not _ALL(_BUILTINS_NS.get(name) is value for name, value in _BUILTINS.items())):
            return dict(result, reason='original kernel globals or builtin identities changed')
        meta_bases, abc_bases = _BASES.__get__(_META, _TYPE), _BASES.__get__(_ABC, _TYPE)
        if (_TYPE(_META) is not _TYPE or _TYPE(_ABC) is not _META
                or _LEN(meta_bases) != 1 or meta_bases[0] is not _TYPE
                or _LEN(abc_bases) != 1 or abc_bases[0] is not _OBJECT
                or not _same_namespace(_CLASS_DICT.__get__(_META, _TYPE), _META_CLASS)
                or not _same_namespace(_CLASS_DICT.__get__(_ABC, _TYPE), _ABC_CLASS)):
            return dict(result, reason='original ABC class or metaclass protocol changed')
        for function, code, globals_, defaults, kwdefaults, closure, contents in _FUNCTIONS:
            if (_TYPE(function) is not _FUNCTION or function.__code__ is not code or function.__globals__ is not globals_
                    or function.__defaults__ is not defaults or function.__kwdefaults__ is not kwdefaults
                    or function.__closure__ is not closure
                    or closure is not None and _ANY(cell.cell_contents is not expected for cell, expected in _ZIP(closure, contents))):
                return dict(result, reason='original ABC Python function code or globals changed')
        if _TYPE(_GET_DUMP) is not _BUILTIN_FUNCTION or _GET_DUMP.__module__ != '_abc':
            return dict(result, reason='original native ABC state reader changed')
        state = _GET_DUMP(_ABC)
        if _TYPE(state) is not _TUPLE or _LEN(state) != 4 or _TYPE(state[0]) is not _SET or state[0]:
            return dict(result, reason='original ABC has an unproved virtual subclass registry')
        return dict(result, status='CHECKED', metaclass='EXACT_ORIGINAL_ABC_META',
                    scope='original ABC class creation kernel and existing empty virtual registry only')
    except (AttributeError, KeyError, TypeError, ValueError, RuntimeError):
        return dict(result, reason='original ABC kernel state is not readable in the finite protocol')


def abc_objects():
    """Return core-owned references only after a fresh actual kernel check."""
    if check_abc_kernel()['status'] != 'CHECKED':
        raise ValueError('original ABC kernel conditions are not checked')
    return _ABC, _META


def _literal(node):
    try:
        value = ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError):
        raise ValueError('nonliteral source default or class attribute') from None
    if _TYPE(value) is _FLOAT and not _ISFINITE(value):
        raise ValueError('nonfinite source floating literal')
    if value is None or _TYPE(value) is _STR or _TYPE(value) is _INT or _TYPE(value) is _FLOAT or _TYPE(value) is _BOOL:
        return value
    if _TYPE(value) is _TUPLE and _ALL(item is None or _TYPE(item) is _STR or _TYPE(item) is _INT or _TYPE(item) is _BOOL
                                     or _TYPE(item) is _FLOAT and _ISFINITE(item) for item in value):
        return value
    raise ValueError('source literal is outside the immutable ABC prefix subset')


def _equal_plain(actual, expected):
    if _TYPE(actual) is not _TYPE(expected):
        return False
    if _TYPE(expected) is _TUPLE:
        return _LEN(actual) == _LEN(expected) and _ALL(_equal_plain(a, b) for a, b in _ZIP(actual, expected))
    return actual == expected


def _annotation(node):
    if _ISINSTANCE(node, ast.Name) and node.id in {'str', 'int', 'float', 'bool', 'list', 'dict', 'tuple', 'set', 'object'}:
        return _BUILTINS[node.id]
    return _literal(node)


def check_abc_source(inspector, chain):
    """Prove a small original definition prefix; trace method bodies normally.

    The first executable subset permits a same-module local single chain,
    original ABC import, immutable class literals and ordinary definitions.
    External imports, deferred/dynamic annotations and abstract decorators
    remain outside this subset, rather than being assumed harmless.
    """
    from seampact.source.inspection import native_ast
    result = dict(schema='source-original-abc-prefix/1', status='UNKNOWN', kernel=check_abc_kernel(),
                  constructor_effects='REQUIRES_NORMAL_CLOSURE_TRACE')
    if result['kernel']['status'] != 'CHECKED':
        return result
    try:
        if _LEN(chain) > 16:
            return dict(result, reason='ABC local chain exceeds its finite class bound')
        paths = {source.path for source in chain}
        if _LEN(paths) != 1:
            return dict(result, reason='ABC prefix imports across source modules are outside this finite subset')
        path = next(iter(paths))
        from pathlib import PurePosixPath
        for parent in PurePosixPath(path).parents:
            package = (parent / '__init__.py').as_posix()
            if package == path or package not in inspector.files:
                continue
            if _ANY(not (_ISINSTANCE(node, ast.Pass) or _ISINSTANCE(node, ast.Expr)
                        and _ISINSTANCE(node.value, ast.Constant) and _TYPE(node.value.value) is _STR)
                    for node in native_ast(inspector.data(package)).body):
                return dict(result, reason='parent package initialization is outside the pure ABC prefix')
        declared = {source.node.name for source in chain}
        for node in native_ast(inspector.data(path)).body:
            if _ISINSTANCE(node, ast.ImportFrom) and node.module == 'abc' and not node.level:
                if _ANY(alias.name != 'ABC' for alias in node.names):
                    return dict(result, reason='ABC prefix import is outside the original ABC class binding')
                continue
            if _ISINSTANCE(node, ast.Import) and _ALL(alias.name == 'abc' for alias in node.names):
                continue
            if _ISINSTANCE(node, ast.Pass) or (_ISINSTANCE(node, ast.Expr) and _ISINSTANCE(node.value, ast.Constant)
                                              and _TYPE(node.value.value) is _STR):
                continue
            if not _ISINSTANCE(node, ast.ClassDef) or node.name not in declared or node.decorator_list or node.keywords:
                return dict(result, reason='module prefix has an unproved class creation or effect')
            for member in node.body:
                if _ISINSTANCE(member, ast.Pass) or (_ISINSTANCE(member, ast.Expr) and _ISINSTANCE(member.value, ast.Constant)
                                                    and _TYPE(member.value.value) is _STR):
                    continue
                if _ISINSTANCE(member, ast.Assign):
                    _literal(member.value)
                    if _ANY(not _ISINSTANCE(target, ast.Name) or target.id.startswith('__') or target.id in
                            ({'_abc_impl'} | _SET(_BUILTINS)) for target in member.targets):
                        return dict(result, reason='class assignment can alter an ABC or descriptor protocol')
                    continue
                if not _ISINSTANCE(member, ast.FunctionDef):
                    return dict(result, reason='class definition is outside the ordinary ABC prefix')
                if member.name in _HOOKS or member.name in _BUILTINS:
                    return dict(result, reason='method definition can alter an ABC or builtin definition protocol')
                if _ANY(not _ISINSTANCE(decorator, ast.Name) or decorator.id not in {'staticmethod', 'classmethod'}
                        for decorator in member.decorator_list) or _LEN(member.decorator_list) > 1:
                    return dict(result, reason='method decorator needs a separate ABC definition protocol')
                for default in [*member.args.defaults, *(value for value in member.args.kw_defaults if value is not None)]:
                    _literal(default)
                for argument in [*member.args.posonlyargs, *member.args.args, *member.args.kwonlyargs,
                        *([member.args.vararg] if member.args.vararg else []), *([member.args.kwarg] if member.args.kwarg else [])]:
                    if argument.annotation is not None:
                        _annotation(argument.annotation)
                if member.returns is not None:
                    _annotation(member.returns)
        return dict(result, status='CHECKED', definition_effects='ORIGINAL_FINITE_DEFINITIONS_CHECKED',
                    scope='original ABC endpoint and finite definition prefix only; ordinary call effects remain separate')
    except (AttributeError, KeyError, TypeError, ValueError, SyntaxError, OSError):
        return dict(result, reason='ABC prefix has an unproved default, annotation, literal or source operation')


def check_abc_target(class_, local_classes, source_classes):
    """Check shape/state/defaults; caller still authenticates each source class.

    `local_classes` must come from the normal binding's original namespaces,
    not candidate flags. This helper does not authenticate filenames or create
    an owner. The normal callable/source checks remain required.
    """
    result = dict(schema='target-finite-abc-owner/1', status='UNKNOWN', kernel=check_abc_kernel(),
                  source_identity='REQUIRES_NORMAL_BINDER_CHECK', owner_construction=False)
    if result['kernel']['status'] != 'CHECKED':
        return result
    try:
        if ((_TYPE(local_classes) is not _LIST and _TYPE(local_classes) is not _TUPLE)
                or (_TYPE(source_classes) is not _LIST and _TYPE(source_classes) is not _TUPLE)
                or not local_classes or _LEN(local_classes) != _LEN(source_classes) or class_ is not local_classes[0]):
            return dict(result, reason='original local ABC chain is incomplete')
        from seampact.source.frozen_inheritance import ClassSource
        if not _ALL(_TYPE(source) is ClassSource and _TYPE(source.node) is ast.ClassDef for source in source_classes):
            return dict(result, reason='source class records are not from the normal frozen binder')
        expected_mro = [*local_classes, _ABC, _OBJECT]
        actual_mro = _MRO.__get__(class_, _TYPE)
        if _LEN(actual_mro) != _LEN(expected_mro) or _ANY(a is not b for a, b in _ZIP(actual_mro, expected_mro)):
            return dict(result, reason='actual ABC method order differs from the frozen local chain')
        for index, (original, source) in _ENUMERATE(_ZIP(local_classes, source_classes)):
            if _TYPE(original) is not _META:
                return dict(result, reason='actual ABC metaclass or direct base changed')
            bases = _BASES.__get__(original, _TYPE)
            if _LEN(bases) != 1 or bases[0] is not expected_mro[index + 1]:
                return dict(result, reason='actual ABC metaclass or direct base changed')
            namespace = _CLASS_DICT.__get__(original, _TYPE)
            if _ANY(_TYPE(name) is not _STR for name in namespace) or _ANY(name in namespace for name in _HOOKS):
                return dict(result, reason='actual ABC class exposes an unknown class or attribute protocol')
            abstract = namespace.get('__abstractmethods__')
            if (_TYPE(abstract) is not _FROZEN or abstract or _TYPE(namespace.get('_abc_impl')) is not _ABC_DATA
                    or _GET_DUMP(original)[0]):
                return dict(result, reason='actual ABC abstract state or registry is outside the concrete empty subset')
            declared = {member.name for member in source.node.body if _ISINSTANCE(member, ast.FunctionDef)}
            for member in source.node.body:
                if _ISINSTANCE(member, ast.Assign):
                    expected = _literal(member.value)
                    for target in member.targets:
                        if not _ISINSTANCE(target, ast.Name) or not _equal_plain(namespace.get(target.id), expected):
                            return dict(result, reason='actual fixed class attribute changed')
                        declared.add(target.id)
                elif _ISINSTANCE(member, ast.FunctionDef):
                    descriptor = namespace.get(member.name)
                    if not member.decorator_list:
                        expected_descriptor = _FUNCTION
                    elif (_LEN(member.decorator_list) == 1 and _ISINSTANCE(member.decorator_list[0], ast.Name)
                            and member.decorator_list[0].id in {'staticmethod', 'classmethod'}):
                        expected_descriptor = _STATIC if member.decorator_list[0].id == 'staticmethod' else _CLASS
                    else:
                        return dict(result, reason='source method descriptor needs a separate protocol')
                    if _TYPE(descriptor) is not expected_descriptor:
                        return dict(result, reason='actual method descriptor differs from its original source binding')
                    function = _function(descriptor)
                    if _TYPE(function) is not _FUNCTION:
                        return dict(result, reason='actual original method descriptor is not supported')
                    if function.__code__.co_freevars:
                        closure = function.__closure__
                        if (function.__code__.co_freevars != ('__class__',) or closure is None or _LEN(closure) != 1
                                or closure[0].cell_contents is not original):
                            return dict(result, reason='actual original class method closure changed')
                    globals_ = function.__globals__
                    if _TYPE(globals_) is not _DICT or not _ALL(_TYPE(name) is _STR for name in globals_):
                        return dict(result, reason='actual method global namespace has unknown keys or mapping protocols')
                    namespace_builtins = globals_.get('__builtins__')
                    if namespace_builtins is not _BUILTINS_NS and namespace_builtins is not builtins:
                        return dict(result, reason='actual method uses an unproved builtin namespace')
                    defaults = _TUPLE(_literal(value) for value in member.args.defaults) or None
                    actual_defaults = function.__defaults__
                    if ((defaults is None) != (actual_defaults is None) or defaults is not None and
                            (_TYPE(actual_defaults) is not _TUPLE or _LEN(defaults) != _LEN(actual_defaults)
                             or _ANY(not _equal_plain(a, b) for a, b in _ZIP(actual_defaults, defaults)))):
                        return dict(result, reason='actual original method defaults changed')
                    expected_kw = {argument.arg: _literal(value) for argument, value in
                                   _ZIP(member.args.kwonlyargs, member.args.kw_defaults) if value is not None}
                    actual_kw = function.__kwdefaults__
                    actual_kw = {} if actual_kw is None else actual_kw
                    if (_TYPE(actual_kw) is not _DICT or not _ALL(_TYPE(name) is _STR for name in actual_kw)
                            or _SET(actual_kw) != _SET(expected_kw)
                            or _ANY(not _equal_plain(actual_kw[name], value) for name, value in expected_kw.items())):
                        return dict(result, reason='actual original keyword defaults changed')
                    parameters = [*member.args.posonlyargs, *member.args.args, *member.args.kwonlyargs,
                        *([member.args.vararg] if member.args.vararg else []), *([member.args.kwarg] if member.args.kwarg else [])]
                    expected_annotations = {argument.arg: _annotation(argument.annotation) for argument in parameters
                                            if argument.annotation is not None}
                    if member.returns is not None:
                        expected_annotations['return'] = _annotation(member.returns)
                    actual_annotations = function.__annotations__
                    if (_TYPE(actual_annotations) is not _DICT or not _ALL(_TYPE(name) is _STR for name in actual_annotations)
                            or _SET(actual_annotations) != _SET(expected_annotations)
                            or _ANY(actual_annotations[name] is not value and not
                                (value is None or _TYPE(value) is _STR or _TYPE(value) is _INT or _TYPE(value) is _FLOAT
                                 or _TYPE(value) is _BOOL or _TYPE(value) is _TUPLE)
                                or actual_annotations[name] is not value and not _equal_plain(actual_annotations[name], value)
                                for name, value in expected_annotations.items())):
                        return dict(result, reason='actual original annotations changed')
            automatic = {'__module__', '__doc__', '__dict__', '__weakref__', '__abstractmethods__', '_abc_impl'}
            if _SET(namespace) != declared | (_SET(namespace) & automatic):
                return dict(result, reason='actual ABC class has additional unproved bindings')
        return dict(result, status='CHECKED', scope='existing original concrete ABC chain shape/state only')
    except (AttributeError, KeyError, TypeError, ValueError):
        return dict(result, reason='actual ABC target state is outside the finite check')
