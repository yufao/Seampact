"""Actual original targets for the finite registration read/use consumer.

These helpers do not import candidate code or call source functions. The
normal worker captures them after its checked original loader and repeats the
source and target checks inside its original synchronous invocation context.
"""
import ast
import hashlib
import json
import sys
import types
import weakref

from seampact.source.call_value_conditions import ordinary
from seampact.source.finite_registration import (
    RULE, RegistrationUnknown, compile_world, capture_world, verify_world, _kernel,
)
from seampact.source.frozen_inheritance import FrozenInheritance
from seampact.source.import_only import _metadata


_TYPE, _DICT, _STR, _FUNCTION = type, dict, str, types.FunctionType
_STATIC, _CLASS = staticmethod, classmethod
_MODULE, _REF = types.ModuleType, weakref.ReferenceType
_MODULE_DICT = types.ModuleType.__dict__['__dict__']
_CLASS_DICT = type.__dict__['__dict__']
_MRO = type.__dict__['__mro__']
_MISSING = object()
_PURE_RULES = {RULE, 'source-known-branch/1', 'source-literal-membership/1', 'source-exact-local-instance/1'}
_NATIVE_CALLS = {'original ordered native direct subclass enumeration',
    'original native set of exact class references; iteration order remains UNKNOWN',
    'original native class set union; iteration order remains UNKNOWN'}


def _site(path, value):
    return (path, value.get('start'), value.get('end'), value.get('col'), value.get('endcol'),
            value.get('ast_sha256'), value.get('kind'))


def _call_site(path, node):
    return (path, node.lineno, node.end_lineno, node.col_offset, node.end_col_offset,
            hashlib.sha256(ast.dump(node, include_attributes=False).encode()).hexdigest(), type(node).__name__)


def _dictionary(value):
    if _TYPE(value) is not _DICT or any(_TYPE(key) is not _STR for key in value):
        raise RegistrationUnknown('registration target evidence has an unknown mapping/key protocol')
    return value


def required_worlds(inspector, entry, candidate, trace, *, complete_call):
    """Recompute every original world; a protocol record alone is insufficient."""
    _kernel()
    trace = _metadata(trace)
    requirements = [row.get('target_requirements') for row in trace.get('external_protocols', [])
                    if row.get('rule') == RULE]
    if not requirements:
        return {}
    if (trace.get('activation_prefix', {}).get('status') != 'CHECKED' or complete_call and (
            trace.get('status') != 'CHECKED' or trace.get('unresolved_targets') or
            trace.get('negative_scope_status') != 'CHECKED' or trace.get('calls') or
            any(row.get('rule') not in _PURE_RULES for row in trace.get('external_protocols', [])))):
        raise RegistrationUnknown('complete synchronous registration read/use effects remain UNKNOWN')
    worlds = {}
    for requirement in requirements:
        if (_TYPE(requirement) is not _DICT or requirement.get('schema') != RULE or
                requirement.get('target_status') != 'REQUIRES_NORMAL_WORKER_CHECK' or
                requirement.get('execution_permission') is not False or _TYPE(requirement.get('root')) is not _STR):
            raise RegistrationUnknown('original registration source requirement is absent or malformed')
        path, separator, symbol = requirement['root'].rpartition(':')
        if not separator or not path or not symbol.isidentifier():
            raise RegistrationUnknown('original registration class reference is malformed')
        world = compile_world(inspector, entry, candidate, path, symbol)
        if world.requirements() != requirement or not world.accepts_prefix(trace['activation_prefix']):
            raise RegistrationUnknown('original complete class creation/read-use prefix changed')
        worlds[requirement['digest']] = world
    if complete_call:
        sites = trace.get('invoked_callables')
        if _TYPE(sites) is not list or not sites:
            raise RegistrationUnknown('original registration call targets are not fully located')
        original_calls = {_site(row['source_path'], row['call_site']) for row in trace.get('invoked_calls', [])
                          if row.get('call_site', {}).get('kind') == 'Call'}
        native_calls = {_site(row['evidence'][0]['path'], row['call_site'])
                        for row in trace.get('external_protocols', []) if row.get('rule') == RULE
                        and row.get('operation') in _NATIVE_CALLS and row.get('call_site', {}).get('kind') == 'Call'}
        for site in sites:
            node, _ = _source_function(inspector, site)
            if isinstance(node, ast.AsyncFunctionDef) or any(isinstance(child, (ast.Await, ast.Yield, ast.YieldFrom))
                                                          for child in ast.walk(node)):
                raise RegistrationUnknown('registration read/use may suspend outside the original synchronous region')
            if any(_call_site(site['path'], child) not in original_calls | native_calls
                   for child in ast.walk(node) if isinstance(child, ast.Call)):
                raise RegistrationUnknown('registration read/use contains an unproved call, builtin IO or callback effect')
    return worlds


def module_targets(binding, worlds, *, in_context=False):
    """Obtain modules from the actual normal binding, never from a proof flag."""
    paths = {path for world in worlds.values() for path in world.prefix['modules']}
    records = binding.modules
    if in_context:
        records = sys.modules
    _dictionary(records)
    result = {}
    for path in paths:
        names = [name for name, record in binding.module_paths.items()
                 if record[0] == path and not name.startswith('src.')]
        if len(names) != 1 or _TYPE(records.get(names[0])) is not _MODULE:
            raise RegistrationUnknown('complete original registration source module is not currently bound')
        result[path] = records[names[0]]
    return result


def _source_function(inspector, site):
    site = _metadata(site)
    path, symbol = site.get('path'), site.get('symbol')
    if _TYPE(path) is not _STR or _TYPE(symbol) is not _STR:
        raise RegistrationUnknown('registration source callable anchor is malformed')
    parts = symbol.split('.')
    tree = FrozenInheritance(inspector).tree(path)
    body = tree.body
    if len(parts) == 2:
        owners = [node for node in body if isinstance(node, ast.ClassDef) and node.name == parts[0]]
        if len(owners) != 1:
            raise RegistrationUnknown('registration source method owner is ambiguous')
        body = owners[0].body
    elif len(parts) != 1:
        raise RegistrationUnknown('nested registration callable closures are outside this read/use subset')
    found = [node for node in body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == parts[-1]]
    if len(found) != 1:
        raise RegistrationUnknown('registration source callable is absent or repeated')
    node = found[0]
    expected = dict(path=path, unit='file', start=node.lineno, end=node.end_lineno, symbol=symbol,
        ast_sha256=hashlib.sha256(ast.dump(node, include_attributes=False).encode()).hexdigest())
    if expected != site:
        raise RegistrationUnknown('registration callable source occurrence changed')
    return node, parts


def _function(inspector, site, modules, entry_function=None):
    node, parts = _source_function(inspector, site)
    module = _dictionary(modules).get(site['path'])
    if _TYPE(module) is not _MODULE:
        raise RegistrationUnknown('registration callable source module is absent')
    namespace = _dictionary(_MODULE_DICT.__get__(module, _MODULE))
    if len(parts) == 1:
        if node.decorator_list:
            raise RegistrationUnknown('registration function wrapper is outside the read/use subset')
        function = namespace.get(parts[0]) if entry_function is None else entry_function
    else:
        class_ = namespace.get(parts[0])
        # Each class/metaclass was checked by the complete world first.
        values = _CLASS_DICT.__get__(class_, _TYPE)
        if any(_TYPE(key) is not _STR for key in values):
            raise RegistrationUnknown('registration method owner has unknown key protocols')
        descriptor = values.get(parts[1])
        decorators = [ast.unparse(value) for value in node.decorator_list]
        expected = _STATIC if decorators == ['staticmethod'] else _CLASS if decorators == ['classmethod'] else _FUNCTION if not decorators else None
        if _TYPE(descriptor) is not expected:
            raise RegistrationUnknown('registration method descriptor differs from its original declaration')
        function = descriptor.__func__ if expected is _STATIC or expected is _CLASS else descriptor
    if _TYPE(function) is not _FUNCTION or function.__globals__ is not namespace:
        raise RegistrationUnknown('registration callable has an unknown original function/global binding')
    first = min([node.lineno, *(value.lineno for value in node.decorator_list)])
    if (function.__code__.co_firstlineno != first or _TYPE(function.__code__.co_filename) is not _STR or
            function.__code__.co_filename != str(inspector.root / site['path']) or
            _TYPE(function.__qualname__) is not _STR or function.__qualname__ != site['symbol']):
        raise RegistrationUnknown('registration callable differs from its original source anchor')
    return function


def _function_state(function):
    attributes = _dictionary(function.__dict__)
    if len(attributes) or function.__closure__ is not None:
        raise RegistrationUnknown('registration callable wrapper or closure effects are outside the finite subset')
    positional, keyword = function.__defaults__, function.__kwdefaults__
    if positional is not None and _TYPE(positional) is not tuple:
        raise RegistrationUnknown('registration defaults have an unknown tuple protocol')
    if keyword is not None:
        _dictionary(keyword)
    defaults = dict(positional=None if positional is None else [ordinary(value) for value in positional],
                    keyword=None if keyword is None else ordinary(keyword))
    return function.__code__, json.dumps(defaults, sort_keys=True, ensure_ascii=False, allow_nan=False)


def _class_attribute(class_, name):
    for base in _MRO.__get__(class_, _TYPE):
        values = _CLASS_DICT.__get__(base, _TYPE)
        if any(_TYPE(key) is not _STR for key in values):
            raise RegistrationUnknown('registration class reference has unknown key protocols')
        if name in values:
            return values[name]
    return _MISSING


def _capture_binding(value, names, active=(), depth=0):
    """Snapshot source read bindings without retaining classes or their globals."""
    if depth > 8 or id(value) in active:
        raise RegistrationUnknown('cyclic or deep registration reference binding is outside this subset')
    if value is _MISSING:
        return ('missing',)
    kind = _TYPE(value)
    if kind is _FUNCTION:
        return ('function', _REF(value))
    if kind is _STATIC or kind is _CLASS:
        if _TYPE(value.__func__) is not _FUNCTION:
            raise RegistrationUnknown('registration descriptor has an unknown function protocol')
        return ('descriptor', kind, _REF(value.__func__))
    if kind is _TYPE or kind is _kernel()[1]:
        attributes = {name: _capture_binding(_class_attribute(value, name), (), (*active, id(value)), depth + 1)
                      for name in names}
        return ('class', _REF(value), attributes)
    # Current supported read/use bindings are exact ordinary data or original
    # class/function references. Unknown SDK/module object protocols stay out.
    plain = ordinary(value)
    return ('plain', json.dumps(plain, sort_keys=True, ensure_ascii=False, allow_nan=False))


def _verify_binding(value, snapshot):
    tag = snapshot[0]
    if tag == 'missing':
        if value is not _MISSING:
            raise RegistrationUnknown('original registration global binding changed')
    elif tag == 'function' or tag == 'class':
        if value is not snapshot[1]():
            raise RegistrationUnknown('original registration class/function reference changed')
        if tag == 'class':
            for name, expected in snapshot[2].items():
                _verify_binding(_class_attribute(value, name), expected)
    elif tag == 'descriptor':
        if _TYPE(value) is not snapshot[1] or value.__func__ is not snapshot[2]():
            raise RegistrationUnknown('original registration descriptor reference changed')
    elif tag == 'plain':
        actual = json.dumps(ordinary(value), sort_keys=True, ensure_ascii=False, allow_nan=False)
        if actual != snapshot[1]:
            raise RegistrationUnknown('original registration ordinary global state changed')
    else:
        raise RegistrationUnknown('original registration reference evidence is malformed')


def _capture_globals(function):
    namespace = _dictionary(function.__globals__)
    names = tuple(function.__code__.co_names)
    return {name: _capture_binding(namespace.get(name, _MISSING), names) for name in names}


def _verify_globals(function, expected):
    namespace = _dictionary(function.__globals__)
    for name, snapshot in expected.items():
        _verify_binding(namespace.get(name, _MISSING), snapshot)


def capture_targets(inspector, entry, candidate, trace, modules):
    """Capture after the checked original loader, without calling any helper."""
    worlds = required_worlds(inspector, entry, candidate, trace, complete_call=False)
    result = dict(worlds={}, functions={})
    for identity, world in worlds.items():
        result['worlds'][identity] = capture_world(world, modules)
    resolver = FrozenInheritance(inspector)
    for path in {path for world in worlds.values() for path in world.prefix['modules']}:
        for parent in resolver.tree(path).body:
            definitions = [(parent, parent.name)] if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)) else [
                (node, parent.name + '.' + node.name) for node in parent.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))] if isinstance(parent, ast.ClassDef) else []
            for node, symbol in definitions:
                site = dict(path=path, unit='file', start=node.lineno, end=node.end_lineno, symbol=symbol,
                    ast_sha256=hashlib.sha256(ast.dump(node, include_attributes=False).encode()).hexdigest())
                try:
                    function = _function(inspector, site, modules)
                    code, defaults = _function_state(function)
                    globals_ = _capture_globals(function)
                except (RegistrationUnknown, ValueError, TypeError):
                    continue  # A later call to this unproved target is rejected.
                result['functions'][path + ':' + symbol] = dict(function=_REF(function), code=code,
                                                              defaults=defaults, globals=globals_)
    return result


def verify_targets(inspector, entry, candidate, trace, receipt, modules, entry_function=None):
    """Source/target premises only; the caller must hold the original locks."""
    worlds = required_worlds(inspector, entry, candidate, trace, complete_call=True)
    _dictionary(receipt)
    old_worlds, old_functions = _dictionary(receipt.get('worlds')), _dictionary(receipt.get('functions'))
    if set(worlds) != set(old_worlds):
        raise RegistrationUnknown('actual call requires a different complete original class world')
    for identity, world in worlds.items():
        verify_world(world, old_worlds[identity], modules)
    for site in trace['invoked_callables']:
        prior = _dictionary(old_functions.get(site['path'] + ':' + site['symbol']))
        function = _function(inspector, site, modules, entry_function if site['path'] == entry['path']
                             and site['symbol'] == entry['symbol'] and entry['kind'] == 'function' else None)
        reference = prior.get('function')
        if _TYPE(reference) is not _REF or reference.__callback__ is not None or reference() is not function:
            raise RegistrationUnknown('original registration callable was lost or replaced')
        code, defaults = _function_state(function)
        if code is not prior.get('code') or _TYPE(prior.get('defaults')) is not _STR or defaults != prior['defaults']:
            raise RegistrationUnknown('original registration callable code/default state changed')
        _verify_globals(function, prior['globals'])
    return dict(status='SOURCE_AND_TARGET_CHECKED', read_use_region='REQUIRES_ORIGINAL_NORMAL_LOCKED_CONSUMER',
                execution_permission=False)
