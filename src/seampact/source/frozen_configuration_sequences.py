"""Exact finite sequences descended from the original frozen YAML read.

These are source-analysis values, not copied runtime configuration objects.
The ledger retains their original analysis aliases and checked mutations.
It never changes the ordinary per-call value limit, native reader conditions,
target permission, exception behavior, or original iteration order.
"""
import ast
import hashlib
import json
import math
from dataclasses import dataclass

from seampact.source.frozen_configuration import frozen_bytes, plain_yaml_value
from seampact.source.import_only import _metadata


RULE = 'source-frozen-configuration-sequence/1'
UNHANDLED = object()
LIMIT = 1024


class ConfigurationSequenceUnknown(ValueError):
    pass


@dataclass(frozen=True)
class ConfigurationIteration:
    receiver: object
    operation: str
    items: tuple
    stamp: object
    ordinary: bool = False


def _ledger(checker):
    if not hasattr(checker, 'frozen_configuration_sequences'):
        checker.frozen_configuration_sequences = {}
    return checker.frozen_configuration_sequences


def _plain(value, count=None, active=None, depth=0):
    """Original reader's JSON domain, with its internal proved-None marker."""
    from seampact.source.dependency_closure import _LiteralNone
    count = [0] if count is None else count
    count[0] += 1
    if count[0] > LIMIT or depth > 64:
        raise ConfigurationSequenceUnknown('original frozen configuration exceeds its finite reader domain')
    kind = type(value)
    if kind is _LiteralNone:
        return None
    if kind in (str, int, bool) or kind is float and math.isfinite(value):
        return value
    if kind not in (list, dict):
        raise ConfigurationSequenceUnknown('frozen configuration contains an unproved object protocol')
    active = set() if active is None else active
    if id(value) in active:
        raise ConfigurationSequenceUnknown('cyclic frozen configuration value')
    active.add(id(value))
    try:
        if kind is list:
            return [_plain(item, count, active, depth + 1) for item in value]
        if any(type(key) is not str for key in value):
            raise ConfigurationSequenceUnknown('frozen configuration has an unproved key protocol')
        return {key: _plain(item, count, active, depth + 1) for key, item in value.items()}
    finally:
        active.remove(id(value))


def _stamp(value, active=None, depth=0):
    """Alias-sensitive snapshot; never invokes source-object equality/hooks."""
    from seampact.source.dependency_closure import _LiteralNone, _Instance
    if depth > 64:
        raise ConfigurationSequenceUnknown('frozen sequence alias depth exceeds its finite bound')
    kind = type(value)
    if kind is _LiteralNone:
        return ('none',)
    if value is None:
        # A tracer UNKNOWN is never promoted to a known YAML null.
        raise ConfigurationSequenceUnknown('frozen sequence contains an UNKNOWN value')
    if kind in (str, int, bool):
        return (kind.__name__, value)
    if kind is float and math.isfinite(value):
        return ('float', value)
    if kind not in (list, dict, _Instance):
        raise ConfigurationSequenceUnknown('frozen sequence contains an unsupported source object')
    active = set() if active is None else active
    if id(value) in active:
        raise ConfigurationSequenceUnknown('frozen sequence has a cyclic alias')
    active.add(id(value))
    try:
        if kind is list:
            if len(value) > LIMIT:
                raise ConfigurationSequenceUnknown('frozen sequence exceeds original reader bound')
            return ('list', id(value), tuple(_stamp(item, active, depth + 1) for item in value))
        if kind is dict:
            if len(value) > LIMIT or any(type(key) is not str for key in value):
                raise ConfigurationSequenceUnknown('frozen sequence mapping has an unknown key/size protocol')
            return ('dict', id(value), tuple((key, _stamp(item, active, depth + 1)) for key, item in value.items()))
        if not value.type_known or value.local_class is None:
            raise ConfigurationSequenceUnknown('frozen sequence source model has an unknown result type')
        return ('source-model', id(value), id(value.allocation), id(value.local_class),
                _stamp(value.attributes, active, depth + 1))
    finally:
        active.remove(id(value))


def _register(checker, value, origins, *, model=False, changed=()):
    from seampact.source.dependency_closure import _Instance
    ledger = _ledger(checker)
    pending, seen, updates = [value], set(), {}
    while pending:
        item = pending.pop()
        if id(item) in seen:
            continue
        seen.add(id(item))
        if type(item) not in (list, dict, _Instance):
            continue
        if type(item) is _Instance and not model:
            raise ConfigurationSequenceUnknown('unproved source model cannot acquire configuration provenance')
        stamp = _stamp(item)
        previous = ledger.get(id(item))
        if id(item) in checker.call_value_taints or (previous is not None and previous['value'] is item
                and previous['stamp'] != stamp and id(item) not in changed):
            raise ConfigurationSequenceUnknown('changed/contaminated configuration alias cannot regain provenance')
        updates[id(item)] = dict(value=item, origins=origins, stamp=stamp, model=model)
        pending.extend(item.values() if type(item) is dict else item if type(item) is list else item.attributes.values())
    ledger.update(updates)


def _record(checker, value):
    record = _ledger(checker).get(id(value))
    if record is None or record['value'] is not value:
        return None
    taints = checker.call_value_taints
    try:
        if id(value) in taints or record['stamp'] != _stamp(value):
            return None
        if any(origin not in checker.configuration_conditions for origin in record['origins']):
            return None
        # Nested aliases can be invalidated without mutating a parent first.
        pending, seen = [value], set()
        while pending:
            item = pending.pop()
            if id(item) in seen:
                continue
            seen.add(id(item))
            if id(item) in taints:
                return None
            if type(item) is list:
                pending.extend(item)
            elif type(item) is dict:
                pending.extend(item.values())
        return record
    except ConfigurationSequenceUnknown:
        return None


def register_reader_result(checker, result, condition, path, node):
    """Called only at the original safe_load source-summary return occurrence."""
    condition = _metadata(condition)
    if condition not in checker.configuration_conditions:
        raise ConfigurationSequenceUnknown('original frozen reader condition was not recorded by the source replay')
    filename, data = frozen_bytes(checker.inspector, condition.get('configuration_file'))
    original = plain_yaml_value(data)
    actual = _plain(result)
    value_digest = hashlib.sha256(json.dumps(original, sort_keys=True, allow_nan=False).encode()).hexdigest()
    if (condition.get('source_version') != checker.inspector.request.get('source_version') or
            condition.get('sha256') != checker.inspector.files[filename]['sha256'] or
            condition.get('original_reader') != 'yaml.safe_load' or
            condition.get('reader_identity', {}).get('installed_source') != 'MATCHED' or
            condition.get('source_reader_check', {}).get('status') != 'FINITE_FORMS_CHECKED' or
            condition.get('value_sha256') != value_digest or actual != original):
        raise ConfigurationSequenceUnknown('frozen configuration sequence differs from its original source read')
    _register(checker, result, [condition])
    checker.protocol(RULE, path, node, operation='original exact frozen reader result',
                     condition=condition, bound=LIMIT, execution_permission=False)


def configuration_plain(checker, value):
    if _record(checker, value) is None:
        return False
    try:
        _plain(value)
        return True
    except ConfigurationSequenceUnknown:
        return False


def configuration_items(checker, value):
    if type(value) is ConfigurationIteration:
        if value.ordinary:
            try:
                return value.items if checker.call_plain(value.receiver) and _stamp(value.receiver) == value.stamp else None
            except ConfigurationSequenceUnknown:
                return None
        record = _record(checker, value.receiver)
        if record is None or record['stamp'] != value.stamp:
            return None
        return value.items
    if type(value) is list and _record(checker, value) is not None:
        return tuple(value)
    return None


def configuration_truth(checker, value):
    if type(value) in (list, dict) and _record(checker, value) is not None:
        return bool(value)
    return None


def configuration_collect(checker, node, args, kwargs, env, path):
    """Original list(dict-view) at an immediate finite source consumption."""
    if (not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name) or node.func.id != 'list'
            or len(args) != 1 or kwargs or type(args[0]) is not ConfigurationIteration):
        return UNHANDLED
    if not checker.ordinary_operation(node, env, path, 'list'):
        checker.negative_gap(path, node, 'original frozen sequence list collector builtin binding is UNKNOWN')
        return None
    values = configuration_items(checker, args[0])
    if values is None:
        checker.negative_gap(path, node, 'original dictionary view changed before finite collection')
        return None
    result = list(values)
    record = _record(checker, args[0].receiver)
    if record is not None:
        try:
            _register(checker, result, record['origins'], model=record['model'])
        except ConfigurationSequenceUnknown as error:
            checker.negative_gap(path, node, str(error))
            return None
    return result


def configuration_model_result(checker, result, inputs):
    origins = []
    for value in inputs:
        record = _record(checker, value)
        if record is not None:
            for origin in record['origins']:
                if origin not in origins:
                    origins.append(origin)
    if origins:
        _register(checker, result, origins, model=True)


def configuration_method(checker, receiver, name, args, kwargs, path, node):
    """Native finite methods, with the original objects and mutation order."""
    record = _record(checker, receiver)
    if _ledger(checker).get(id(receiver)) is not None and record is None:
        return UNHANDLED
    if type(receiver) is dict and record is None and name in {'copy', 'keys', 'values', 'items'} and not args and not kwargs:
        if not checker.call_plain(receiver):
            return UNHANDLED
        if name == 'copy':
            return receiver.copy()  # Original shallow aliases, no reader origin added.
        values = tuple(receiver if name == 'keys' else receiver.values() if name == 'values' else receiver.items())
        return ConfigurationIteration(receiver, name, values, _stamp(receiver), ordinary=True)
    if type(receiver) is dict and name == 'setdefault' and 1 <= len(args) <= 2 and not kwargs and type(args[0]) is str:
        # Exact ordinary kwargs in original model_dump overrides; this does
        # not add configuration provenance to unrelated per-call values.
        from seampact.source.dependency_closure import LITERAL_NONE
        try:
            if len(receiver) > 64:
                return UNHANDLED
            _plain(receiver)
            if len(args) == 2:
                _plain(args[1])
        except ConfigurationSequenceUnknown:
            return UNHANDLED
        result = receiver.setdefault(args[0], args[1] if len(args) == 2 else LITERAL_NONE)
        if record is not None:
            _register(checker, receiver, record['origins'], changed=(id(receiver),))
        return result
    if type(receiver) is dict and record is not None and name in {'copy', 'keys', 'values', 'items'} and not args and not kwargs:
        if name == 'copy':
            result = receiver.copy()
            _register(checker, result, record['origins'], model=record['model'])
            return result
        values = tuple(receiver if name == 'keys' else receiver.values() if name == 'values' else receiver.items())
        return ConfigurationIteration(receiver, name, values, record['stamp'])
    if name == 'update' and type(receiver) is dict and len(args) == 1 and not kwargs and type(args[0]) is dict:
        incoming = _record(checker, args[0])
        if _ledger(checker).get(id(args[0])) is not None and incoming is None:
            return UNHANDLED
        if record is None and incoming is None:
            return UNHANDLED
        try:
            _plain(receiver)
            _plain(args[0])
        except ConfigurationSequenceUnknown:
            return UNHANDLED
        origins = list((record or incoming)['origins'])
        if incoming is not None:
            for origin in incoming['origins']:
                if origin not in origins:
                    origins.append(origin)
        receiver.update(args[0])
        _register(checker, receiver, origins, changed=(id(receiver),))
        return None
    if name in {'append', 'extend'} and type(receiver) is list and len(args) == 1 and not kwargs:
        values = [args[0]] if name == 'append' else configuration_items(checker, args[0])
        if values is None:
            return UNHANDLED
        origins = list(record['origins']) if record is not None else []
        for value in values:
            item = _record(checker, value)
            if item is not None:
                for origin in item['origins']:
                    if origin not in origins:
                        origins.append(origin)
        if not origins:
            return UNHANDLED
        try:
            _stamp(receiver)
            for value in values:
                if _record(checker, value) is None:
                    if _ledger(checker).get(id(value)) is not None:
                        raise ConfigurationSequenceUnknown('invalidated configuration item cannot regain provenance')
                    _plain(value)
            if len(receiver) + len(values) > LIMIT:
                raise ConfigurationSequenceUnknown('derived configuration sequence exceeds original finite bound')
        except ConfigurationSequenceUnknown:
            return UNHANDLED
        receiver.extend(values)
        _register(checker, receiver, origins, model=True, changed=(id(receiver),))
        return None
    return UNHANDLED


def configuration_loop(checker, node, value, env, path, scope):
    """Exact control for a finite source-backed original for/else loop.

    Unknown branches and mutation stop finite inference and retain the normal
    UNKNOWN failure path. Exceptions are observed as source control; they are
    never caught or converted into a successful configuration value here.
    """
    if type(node) is not ast.For:
        return UNHANDLED
    items = configuration_items(checker, value)
    if items is None:
        return UNHANDLED
    record = _record(checker, value.receiver if type(value) is ConfigurationIteration else value)
    if record is None:
        return UNHANDLED
    receiver = value.receiver if type(value) is ConfigurationIteration else value
    stamp = record['stamp']

    def block(statements):
        for statement in statements:
            if type(statement) is ast.Continue:
                return 'continue', None
            if type(statement) is ast.Break:
                return 'break', None
            if type(statement) is ast.Return:
                return 'return', checker.value(statement.value, env, path, scope)
            if type(statement) is ast.Raise:
                checker.value(statement.exc, env, path, scope)
                checker.negative_gap(path, statement, 'original frozen configuration loop raises; normal result remains UNKNOWN')
                return 'raise', None
            if type(statement) is ast.If:
                truth = checker.conditional_truth(statement.test, env, path, scope)
                if truth is None:
                    checker.statement(statement, env, path, scope)
                    checker.negative_gap(path, statement, 'original frozen configuration loop branch remains UNKNOWN')
                    return 'unknown', None
                checker.protocol('source-known-branch/1', path, statement.test,
                    expression=ast.unparse(statement.test), selected='body' if truth else 'orelse',
                    ast_sha256=hashlib.sha256(ast.dump(statement.test, include_attributes=False).encode()).hexdigest())
                status, result = block(statement.body if truth else statement.orelse)
                if status != 'normal':
                    return status, result
            else:
                returned, result = checker.statement(statement, env, path, scope)
                if returned:
                    return 'return', result
        return 'normal', None

    broken = False
    for item in items:
        if _record(checker, receiver) is None or _record(checker, receiver)['stamp'] != stamp:
            checker.negative_gap(path, node, 'original frozen sequence changed during ordered consumption')
            return False, None
        checker.bind(node.target, item, env, path, scope)
        status, result = block(node.body)
        if status in {'return', 'raise'}:
            return True, result
        if status == 'unknown':
            checker.bind(node.target, None, env, path, scope)
            return False, None
        if status == 'break':
            broken = True
            break
    if _record(checker, receiver) is None or _record(checker, receiver)['stamp'] != stamp:
        checker.negative_gap(path, node, 'original frozen sequence changed during ordered consumption')
        return False, None
    checker.protocol(RULE, path, node, operation='original exact ordered for/else consumption',
                     conditions=record['origins'], count=len(items), break_taken=broken,
                     execution_permission=False)
    if not broken:
        status, result = block(node.orelse)
        if status in {'return', 'raise'}:
            return True, result
    return False, None


def configuration_comprehension(checker, node, value, env, path, scope, *, setup=False):
    """Original single-generator list/dict comprehension over frozen values."""
    if setup or type(node) not in (ast.ListComp, ast.DictComp) or len(node.generators) != 1 or node.generators[0].is_async:
        return UNHANDLED
    receiver = value.receiver if type(value) is ConfigurationIteration else value
    record = _record(checker, receiver)
    items = configuration_items(checker, value)
    if record is None or items is None:
        return UNHANDLED
    generator, local = node.generators[0], env.copy()
    stamp = record['stamp']
    result = [] if type(node) is ast.ListComp else {}
    for item in items:
        checker.bind(generator.target, item, local, path, scope)
        selected = True
        for condition in generator.ifs:
            truth = checker.conditional_truth(condition, local, path, scope)
            if truth is None:
                checker.negative_gap(path, condition, 'original frozen configuration comprehension filter remains UNKNOWN')
                return None
            if not truth:
                selected = False
                break  # Preserve original filter short circuit and order.
        if selected:
            if type(node) is ast.ListComp:
                result.append(checker.value(node.elt, local, path, scope, setup))
            else:
                key = checker.value(node.key, local, path, scope, setup)
                if type(key) is not str:
                    checker.negative_gap(path, node.key, 'original frozen dictionary-comprehension key protocol is UNKNOWN')
                    return None
                result[key] = checker.value(node.value, local, path, scope, setup)
        fresh = _record(checker, receiver)
        if fresh is None or fresh['stamp'] != stamp:
            checker.negative_gap(path, node, 'original frozen sequence changed during comprehension consumption')
            return None
    try:
        _register(checker, result, record['origins'], model=record['model'])
    except ConfigurationSequenceUnknown as error:
        checker.negative_gap(path, node, str(error))
        return None
    checker.protocol(RULE, path, node, operation='original exact ordered comprehension',
                     conditions=record['origins'], count=len(items), result_count=len(result), execution_permission=False)
    return result
