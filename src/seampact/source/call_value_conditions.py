"""Finite, per-invocation value evidence; never a candidate permission flag.

Only the ordinary arm of the existing group-port/3 is consumed.  Compilation
describes a required check; it is not an actual request or a resource proof.
"""
import ast
import builtins
import copy
import hashlib
import json
import math
import sys
from dataclasses import dataclass
from types import FunctionType, MethodType

RULE = 'source-per-call-values/1'
GAP = 'source type-reference fact does not prove target activation or operand effects'
_TYPE, _DICT, _LIST = type, dict, list
_SCALARS = (str, bool, int)
_FLOAT = float
_PLAIN = (*_SCALARS, _FLOAT, _LIST, _DICT)
_MODULE_TYPE = _TYPE(builtins)
_MODULE_DICT = _MODULE_TYPE.__dict__['__dict__']
_ORIGINALS = {name: getattr(builtins, name) for name in
              ('isinstance', 'list', 'dict', 'str', 'int', 'float', 'bool', 'len', 'range', 'zip',
               'staticmethod', 'classmethod', '__build_class__', '__import__')}
_STR_SPLIT = str.__dict__['split']
_STR_OPERATIONS = {name: str.__dict__[name] for name in ('join', 'replace', 'strip')}
_STR_COUNT = str.__dict__['count']
_INT_OPERATIONS = {name: int.__dict__[method] for name, method in
                   (('positive', '__pos__'), ('negative', '__neg__'), ('add', '__add__'), ('subtract', '__sub__'))}
_INT_BIT_LENGTH = int.__dict__['bit_length']
_VALUE_INTEGER_BITS = 4096
_VALUE_STRING_LENGTH = 65536
_SSIZE_MAX = sys.maxsize


class CallUnknown(ValueError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        allow_nan=False, separators=(',', ':')).encode()).hexdigest()


def ordinary(value, depth=0, active=None):
    """Copy actual plain transport values, without consulting object hooks."""
    if depth > 32:
        raise CallUnknown('ordinary call value exceeds finite depth 32')
    kind = _TYPE(value)
    if value is None or any(kind is native for native in _SCALARS):
        return value
    if kind is _FLOAT and math.isfinite(value):
        return value
    active = set() if active is None else active
    if id(value) in active:
        raise CallUnknown('cyclic call values have no ordinary transport proof')
    if kind is not _LIST and kind is not _DICT:
        raise CallUnknown('call value is not an exact finite ordinary value')
    active.add(id(value))
    try:
        if kind is _LIST:
            return [ordinary(v, depth + 1, active) for v in value]
        if any(_TYPE(k) is not _SCALARS[0] or k.startswith('$') for k in value):
            raise CallUnknown('ordinary call dictionary has an unsupported key or object marker')
        return {k: ordinary(v, depth + 1, active) for k, v in value.items()}
    finally:
        active.remove(id(value))


@dataclass(frozen=True)
class Shape:
    """Only a successful expression's outer type; no length/element proof."""
    kind: str


@dataclass(frozen=True)
class FiniteIteration:
    """An immediate finite consumption proof, never an ordinary list value."""
    items: tuple
    site: int
    sources: tuple = ()


@dataclass
class Context:
    values: object = None
    compiling: bool = False
    initialization_only: bool = False
    # Internal actual-worker receipts only. A source plan or serialized
    # candidate cannot populate this tuple to authorize an SDK operation.
    runtime_verified: tuple = ()
    # Source-derived facts from normal fixed resources, never execution rights.
    # Actual worker SDK/resource targets still require independent verification.
    sdk_profiles: tuple = ()


def contextual(inspector, context):
    from seampact.source.inspection import Inspector
    result = Inspector(inspector.root, inspector.snapshots, copy.deepcopy(inspector.request))
    result._call_value_context = context
    return result


def context(inspector):
    value = getattr(inspector, '_call_value_context', None)
    return value if _TYPE(value) is Context else None


def builtin_state():
    # Captured platform primitives, not an SDK name/hash or source assertion.
    if _TYPE(builtins) is not _MODULE_TYPE:
        return False
    namespace = _MODULE_DICT.__get__(builtins, _MODULE_TYPE)
    if _TYPE(namespace) is not _DICT or any(_TYPE(key) is not _SCALARS[0] for key in namespace):
        return False
    return (all(namespace.get(name) is value for name, value in _ORIGINALS.items())
            and _SCALARS[0].__dict__['split'] is _STR_SPLIT
            and all(_SCALARS[0].__dict__[name] is method for name, method in _STR_OPERATIONS.items())
            and _SCALARS[0].__dict__['count'] is _STR_COUNT
            and all(_SCALARS[2].__dict__[method] is _INT_OPERATIONS[name] for name, method in
                    (('positive', '__pos__'), ('negative', '__neg__'), ('add', '__add__'), ('subtract', '__sub__')))
            and _SCALARS[2].__dict__['bit_length'] is _INT_BIT_LENGTH)


def finite_integer(operation, arguments):
    """Captured native int operations, with no conversion or numeric hooks."""
    if (_TYPE(operation) is not _SCALARS[0] or operation not in _INT_OPERATIONS
            or _TYPE(arguments) is not _LIST
            or len(arguments) != (1 if operation in ('positive', 'negative') else 2)
            or any(_TYPE(value) is not _SCALARS[2] or _INT_BIT_LENGTH(value) > _VALUE_INTEGER_BITS
                   for value in arguments)):
        raise CallUnknown('integer operands or finite operation are not closed')
    result = _INT_OPERATIONS[operation](*arguments)
    if _INT_BIT_LENGTH(result) > _VALUE_INTEGER_BITS:
        raise CallUnknown('integer result exceeds finite bit length 4096')
    return result


def finite_string(operation, receiver, arguments, keywords):
    """Captured str descriptors on bounded exact values, never iterators.

    Only positionally bound forms are proved. The output length is checked
    before join/replace allocate it; strip cannot grow its bounded receiver.
    """
    if (_TYPE(operation) is not _SCALARS[0] or operation not in _STR_OPERATIONS
            or _TYPE(receiver) is not _SCALARS[0] or len(receiver) > _VALUE_STRING_LENGTH
            or _TYPE(arguments) is not _LIST or _TYPE(keywords) is not _DICT or keywords):
        raise CallUnknown('string operation binding or finite receiver is not closed')
    if operation == 'join':
        if (len(arguments) != 1 or _TYPE(arguments[0]) is not _LIST or len(arguments[0]) > 64
                or any(_TYPE(item) is not _SCALARS[0] or len(item) > _VALUE_STRING_LENGTH
                       for item in arguments[0])):
            raise CallUnknown('join operand is not a finite exact string list')
        pieces = arguments[0]
        length = 0
        for item in pieces:
            length += len(item)
        if pieces:
            length += len(receiver) * (len(pieces) - 1)
        if length > _VALUE_STRING_LENGTH:
            raise CallUnknown('join result exceeds finite length 65536')
    elif operation == 'replace':
        if (len(arguments) not in (2, 3)
                or any(_TYPE(value) is not _SCALARS[0] or len(value) > _VALUE_STRING_LENGTH
                       for value in arguments[:2])):
            raise CallUnknown('replace text operands are not bounded exact strings')
        count = arguments[2] if len(arguments) == 3 else -1
        if _TYPE(count) is not _SCALARS[2] or not -_SSIZE_MAX - 1 <= count <= _SSIZE_MAX:
            raise CallUnknown('replace count has no proved native normal result')
        occurrences = _STR_COUNT(receiver, arguments[0])
        if count >= 0 and occurrences > count:
            occurrences = count
        length = len(receiver) + occurrences * (len(arguments[1]) - len(arguments[0]))
        if length > _VALUE_STRING_LENGTH:
            raise CallUnknown('replace result exceeds finite length 65536')
    elif (len(arguments) > 1 or arguments and arguments[0] is not None
          and (_TYPE(arguments[0]) is not _SCALARS[0] or len(arguments[0]) > _VALUE_STRING_LENGTH)):
        raise CallUnknown('strip chars are not a bounded exact string or literal None')
    return _STR_OPERATIONS[operation](receiver, *arguments)


def finite_range(arguments):
    """Captured native range on exact integers, with no __index__ dispatch."""
    if (_TYPE(arguments) is not _LIST or not 1 <= len(arguments) <= 3
            or any(_TYPE(v) is not int for v in arguments)):
        raise CallUnknown('range arguments are not exact integers')
    try:
        result = _ORIGINALS['range'](*arguments)
        if len(result) > 64:
            raise CallUnknown('immediate iteration exceeds finite length 64')
        return tuple(result)
    except (ValueError, OverflowError) as error:
        raise CallUnknown('range does not have a proved finite normal result') from error


def finite_split(receiver, arguments, keywords):
    """Captured str descriptor; only exact ordinary arguments are accepted."""
    if (_TYPE(receiver) is not str or _TYPE(arguments) is not _LIST or _TYPE(keywords) is not _DICT
            or len(receiver) > 65536 or len(arguments) > 2
            or any(k not in ('sep', 'maxsplit') for k in keywords)
            or any(k in keywords for k in ('sep', 'maxsplit')[:len(arguments)])):
        raise CallUnknown('split binding or finite arguments are not closed')
    sep = arguments[0] if arguments else keywords.get('sep')
    maximum = arguments[1] if len(arguments) == 2 else keywords.get('maxsplit', -1)
    if (sep is not None and _TYPE(sep) is not str) or _TYPE(maximum) is not int:
        raise CallUnknown('split arguments are not exact ordinary values')
    if not -_SSIZE_MAX - 1 <= maximum <= _SSIZE_MAX:
        raise CallUnknown('split argument has no proved native normal result')
    try:
        # At most 65 pieces are needed to reject an over-limit result. When
        # there are at most 64 pieces this probe equals the original split.
        result = _STR_SPLIT(receiver, sep, 64 if maximum < 0 or maximum > 64 else maximum)
    except (TypeError, ValueError, OverflowError) as error:
        raise CallUnknown('split does not have a proved finite normal result') from error
    if len(result) > 64:
        raise CallUnknown('split result exceeds finite length 64')
    return result


def plain_truth(value):
    if value is None or _TYPE(value) is Shape:
        return None  # None is the tracer's UNKNOWN, not original literal None.
    if any(_TYPE(value) is native for native in _PLAIN):
        return bool(value)
    return None


def literal_default(node):
    if not isinstance(node, ast.Constant) or (node.value is not None and
            not any(_TYPE(node.value) is native for native in (*_SCALARS, _FLOAT))):
        raise CallUnknown('omitted original default is not a proved immutable literal')
    return ordinary(node.value)


def actual_defaults(callable_, names):
    """Only native function/method dictionaries; never unwrap unknown hooks."""
    if _TYPE(names) is not _LIST or any(_TYPE(name) is not _SCALARS[0] for name in names):
        raise CallUnknown('original parameter names are not exact ordinary strings')
    seen = set()
    for _ in range(16):
        if id(callable_) in seen:
            raise CallUnknown('original callable wrapper cycle')
        seen.add(id(callable_))
        if _TYPE(callable_) is MethodType:
            callable_ = callable_.__func__
        elif _TYPE(callable_) is FunctionType:
            attributes = callable_.__dict__
            if _TYPE(attributes) is not _DICT or any(_TYPE(name) is not _SCALARS[0] for name in attributes):
                raise CallUnknown('original callable metadata has an unknown key protocol')
            wrapped = attributes.get('__wrapped__')
            if wrapped is None:
                defaults = callable_.__defaults__
                keyword = callable_.__kwdefaults__
                defaults = () if defaults is None else defaults
                keyword = {} if keyword is None else keyword
                if (_TYPE(defaults) is not tuple or _TYPE(keyword) is not _DICT
                        or any(_TYPE(name) is not _SCALARS[0] for name in keyword)):
                    raise CallUnknown('original default storage is not native')
                return dict(zip(names[-len(defaults):], defaults), **keyword) if defaults else dict(keyword)
            callable_ = wrapped
        else:
            raise CallUnknown('original callable default identity is unavailable')
    raise CallUnknown('original callable wrapper exceeds finite depth')


_RUNTIME_PROTOCOLS = {
    'source-complete-finite-class-world/1': 'source-complete-finite-class-world/1',
    'source-finite-sdk-result/1': 'source-finite-sdk-result/1',
    'source-bounded-value-flow/1': 'source-bounded-value-target-requirements/1',
    'source-finite-generator/1': 'source-generator-target-requirements/1',
    'source-finite-configuration-model/1': 'source-finite-configuration-model/1',
    'source-finite-transformer-sdk-protocol/1': 'source-finite-transformer-sdk-protocol/1',
    'source-complete-finite-activation-scope/1': 'source-complete-finite-activation-scope/1',
    'source-sdk-import-region/1': 'source-sdk-import-region/1',
    'source-sdk-original-resource-find/1': 'source-sdk-original-resource-find/1',
}


def runtime_requirements(*traces):
    """Retain every source-derived conditional target, including guarded ones."""
    requirements = []
    for trace in traces:
        for protocol in trace.get('external_protocols', []):
            schema = _RUNTIME_PROTOCOLS.get(protocol.get('rule'))
            if schema is None:
                continue
            requirement = protocol.get('target_requirements')
            if (protocol.get('rule') == 'source-complete-finite-activation-scope/1' and
                    _TYPE(requirement) is _DICT and requirement.get('source_status') != 'STRUCTURE_CHECKED'):
                continue
            if (_TYPE(requirement) is not _DICT or requirement.get('schema') != schema
                    or requirement.get('execution_permission') is not False
                    or requirement.get('target_status') != 'REQUIRES_NORMAL_WORKER_CHECK'):
                raise CallUnknown('original runtime target requirements are absent or malformed')
            if requirement not in requirements:
                requirements.append(copy.deepcopy(requirement))
    return requirements


def compile_condition(inspector, entry, candidate):
    """Replay the exact generic and guarded traces; retain generic UNKNOWN."""
    from seampact.source.dependency_closure import resolve_resource_calls
    if context(inspector) is not None or entry.get('kind') not in ('function', 'method'):
        return None
    trace = resolve_resource_calls(inspector, entry, candidate)
    gaps = trace.get('negative_scope_gaps', [])
    from seampact.source.sdk_value_summaries import GAP as SDK_GAP
    from seampact.source.configuration_model_flow import GAP as MODEL_GAP
    from seampact.source.sdk_transformer_protocols import GAP as TRANSFORMER_GAP
    from seampact.source.activation_scope import GAP as ACTIVATION_GAP
    from seampact.source.sdk_import_summaries import GAP as IMPORT_GAP
    target_gaps = {SDK_GAP, MODEL_GAP, TRANSFORMER_GAP, ACTIVATION_GAP, IMPORT_GAP}
    try:
        requirements = runtime_requirements(trace)
    except CallUnknown:
        return None
    if (trace['status'] != 'CHECKED' or trace['unresolved_targets'] or not (gaps or requirements)
            or any(gap.get('reason') not in target_gaps | {GAP} for gap in gaps)):
        return None
    shadow = contextual(inspector, Context(compiling=True))
    guarded = resolve_resource_calls(shadow, entry, candidate)
    try:
        requirements = runtime_requirements(trace, guarded)
    except CallUnknown:
        return None
    remaining = guarded.get('negative_scope_gaps', [])
    if (guarded['status'] != 'CHECKED' or guarded['unresolved_targets']
            or guarded['negative_scope_status'] != 'CHECKED' and (
                not requirements or not remaining or any(gap.get('reason') not in target_gaps for gap in remaining))):
        return None
    prefix = resolve_resource_calls(contextual(inspector, Context(compiling=True, initialization_only=True)), entry, candidate)
    body = dict(schema=RULE, status='REQUIRES_ACTUAL_CALL',
        source_version=inspector.request['source_version'], entry_id=entry['id'],
        entry_digest=digest(entry), candidate_digest=digest(candidate),
        sites=copy.deepcopy(gaps), global_negative_scope='UNKNOWN',
        initialization=dict(status='CHECKED' if prefix['status'] == 'CHECKED' and not prefix['unresolved_targets']
                            and prefix['negative_scope_status'] == 'CHECKED'
                            and prefix.get('activation_prefix', {}).get('status') == 'CHECKED'
                            and not any(p.get('rule') in _RUNTIME_PROTOCOLS
                                        for p in prefix.get('external_protocols', [])) else 'UNKNOWN',
                            activation_prefix=copy.deepcopy(prefix.get('activation_prefix')),
                            scope='original module/constructor/initializers only; entry body excluded'),
        required=['EXACT_GROUP_PORT_3_ORDINARY_VALUES', 'ACTUAL_SUPPLIED_AND_OMITTED_ARGUMENTS',
                  'ORIGINAL_OPERATION_AND_DEFAULT_BINDINGS', 'COMPLETE_SAME_CALL_TRACE',
                  'CURRENT_RESOURCE_AND_HOST_CONDITIONS'],
        scope='one actual call only; no global resource non-use, SDK identity or lifecycle permission')
    if requirements:
        body.update(runtime_protocol_requirements=requirements,
                    global_negative_scope=trace['negative_scope_status'])
        if any(row['schema'] == 'source-complete-finite-class-world/1' for row in requirements):
            body['required'].append('ACTUAL_COMPLETE_FINITE_CLASS_WORLD_AND_READ_USE_CONDITIONS')
        if any(row['schema'] != 'source-complete-finite-class-world/1' for row in requirements):
            body['required'].append('ACTUAL_ORIGINAL_SDK_MODEL_AND_HELPER_TARGET_CONDITIONS')
    return dict(body, digest=digest(body))


def verify_recipe(inspector, entry, candidate, recipe):
    return recipe is not None and compile_condition(inspector, entry, candidate) == recipe


def call_context(arguments):
    if _TYPE(arguments) is not _DICT:
        raise CallUnknown('actual call arguments are not yet available')
    values = ordinary(arguments)
    return Context(values=values), digest(values)
