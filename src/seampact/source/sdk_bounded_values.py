"""Finite ordinary-value envelopes used only by the source interpreter.

These are sets of possible native values, not representative runtime values.
They cannot be converted to bool, iterated, or passed to original code.  The
caller separately proves each original operation, all feasible control paths,
effects, termination and SDK target conditions; an envelope grants none of
those permissions.
"""
from dataclasses import dataclass


_TYPE, _INT, _STR, _BOOL, _LIST = type, int, str, bool, list
MAX_STRING = 65536
MAX_LIST = 64
MAX_SYMBOLIC_LIST = 65537
MAX_INTEGER_BITS = 4096


class BoundedUnknown(ValueError):
    pass


class _NoTruth:
    def __bool__(self):
        raise BoundedUnknown('an abstract value is not an actual branch result')


def _integer(value):
    if _TYPE(value) is not _INT or value.bit_length() > MAX_INTEGER_BITS:
        raise BoundedUnknown('integer endpoint exceeds the exact finite domain')
    return value


def _size(value, maximum):
    if _TYPE(value) is not _INT or not 0 <= value <= maximum:
        raise BoundedUnknown('size exceeds the exact finite domain')
    return value


@dataclass(frozen=True)
class BoundedString(_NoTruth):
    max_length: int
    min_length: int = 0

    def __post_init__(self):
        _size(self.max_length, MAX_STRING)
        _size(self.min_length, self.max_length)


@dataclass(frozen=True)
class BoundedInteger(_NoTruth):
    lower: int
    upper: int

    def __post_init__(self):
        if _integer(self.lower) > _integer(self.upper):
            raise BoundedUnknown('integer interval is empty')


@dataclass(frozen=True)
class BoundedBoolean(_NoTruth):
    may_false: bool = True
    may_true: bool = True

    def __post_init__(self):
        if (_TYPE(self.may_false) is not _BOOL or _TYPE(self.may_true) is not _BOOL
                or not (self.may_false or self.may_true)):
            raise BoundedUnknown('boolean possibility set is empty or non-native')


@dataclass(frozen=True)
class BoundedTuple(_NoTruth):
    """A fixed original tuple shape, used only inside finite source flow."""
    items: tuple

    def __post_init__(self):
        if _TYPE(self.items) is not tuple or len(self.items) > MAX_LIST:
            raise BoundedUnknown('internal tuple shape is outside its finite native domain')
        for value in self.items:
            _validate(value,depth=1)


@dataclass(frozen=True)
class BoundedList(_NoTruth):
    element: object
    max_length: int
    min_length: int = 0

    def __post_init__(self):
        _size(self.max_length, MAX_SYMBOLIC_LIST)
        _size(self.min_length, self.max_length)
        # Empty lists need no element fact. Nonempty lists must have a closed
        # ordinary leaf/envelope, never a callback or an SDK object.
        if self.max_length:
            _validate(self.element, depth=1)


_KINDS = (BoundedString, BoundedInteger, BoundedBoolean, BoundedList, BoundedTuple)


def is_bounded(value):
    return any(_TYPE(value) is kind for kind in _KINDS)


def _validate(value, depth=0):
    if depth > 32:
        raise BoundedUnknown('bounded ordinary value exceeds finite depth 32')
    kind = _TYPE(value)
    if value is None or kind is _BOOL:
        return
    if kind is _STR:
        _size(len(value), MAX_STRING)
    elif kind is _INT:
        _integer(value)
    elif kind is BoundedString:
        _size(value.max_length, MAX_STRING)
        _size(value.min_length, value.max_length)
    elif kind is BoundedInteger:
        if _integer(value.lower) > _integer(value.upper):
            raise BoundedUnknown('integer interval is empty')
    elif kind is BoundedBoolean:
        if (_TYPE(value.may_false) is not _BOOL or _TYPE(value.may_true) is not _BOOL
                or not (value.may_false or value.may_true)):
            raise BoundedUnknown('boolean possibility set is empty or non-native')
    elif kind is BoundedTuple:
        if _TYPE(value.items) is not tuple or len(value.items) > MAX_LIST:
            raise BoundedUnknown('internal tuple shape is outside its finite native domain')
        for item in value.items:
            _validate(item,depth+1)
    elif kind is BoundedList:
        _size(value.max_length, MAX_SYMBOLIC_LIST)
        _size(value.min_length, value.max_length)
        if value.max_length:
            _validate(value.element, depth + 1)
    elif kind is _LIST:
        _size(len(value), MAX_LIST)
        for item in value:
            _validate(item, depth + 1)
    else:
        raise BoundedUnknown('value is outside the finite native ordinary domain')


def value_kind(value):
    _validate(value)
    kind = _TYPE(value)
    if kind is _STR or kind is BoundedString:
        return 'string'
    if kind is _INT or kind is BoundedInteger:
        return 'integer'
    if kind is _BOOL or kind is BoundedBoolean:
        return 'boolean'
    if kind is _LIST or kind is BoundedList:
        return 'list'
    if kind is BoundedTuple:
        return 'tuple'
    return 'null'


def abstract(value):
    """Produce an immutable envelope; never sample a possible element."""
    _validate(value)
    kind = _TYPE(value)
    if is_bounded(value) or value is None:
        return value
    if kind is _STR:
        return BoundedString(len(value), len(value))
    if kind is _INT:
        return BoundedInteger(value, value)
    if kind is _BOOL:
        return BoundedBoolean(not value, value)
    if kind is _LIST:
        element = None
        for index, item in enumerate(value):
            element = abstract(item) if index == 0 else merge(element, item)
        return BoundedList(element, len(value), len(value))
    raise BoundedUnknown('value has no finite envelope')


def merge(left, right):
    """A sound hull of both branch values, with no cross-type coercion."""
    left_kind, right_kind = value_kind(left), value_kind(right)
    if left_kind != right_kind:
        raise BoundedUnknown('branches have incompatible ordinary value kinds')
    if not is_bounded(left) and not is_bounded(right) and _TYPE(left) is not _LIST and left == right:
        return left
    if left_kind == 'null':
        return None
    a, b = abstract(left), abstract(right)
    if left_kind == 'string':
        return BoundedString(max(a.max_length, b.max_length), min(a.min_length, b.min_length))
    if left_kind == 'integer':
        return BoundedInteger(min(a.lower, b.lower), max(a.upper, b.upper))
    if left_kind == 'boolean':
        return BoundedBoolean(a.may_false or b.may_false, a.may_true or b.may_true)
    if left_kind == 'list':
        element = (b.element if not a.max_length else a.element if not b.max_length
                   else merge(a.element, b.element))
        return BoundedList(element, max(a.max_length, b.max_length), min(a.min_length, b.min_length))
    if left_kind == 'tuple':
        if len(a.items) != len(b.items):
            raise BoundedUnknown('tuple branches have different native arities')
        return BoundedTuple(tuple(merge(x,y) for x,y in zip(a.items,b.items)))
    raise BoundedUnknown('branch hull is outside the finite ordinary domain')


def length(value):
    kind = value_kind(value)
    if kind == 'tuple':
        return len(value.items)
    if kind not in ('string', 'list'):
        raise BoundedUnknown('length receiver is not a native string/list')
    if not is_bounded(value):
        return len(value)
    return BoundedInteger(value.min_length, value.max_length)


def truth(value):
    """Return a proof of truth, or None when both original branches exist."""
    kind = value_kind(value)
    if not is_bounded(value):
        return _BOOL(value)
    if kind == 'boolean':
        return True if not value.may_false else False if not value.may_true else None
    if kind == 'tuple':
        return bool(value.items)
    if kind in ('string', 'list'):
        return False if value.max_length == 0 else True if value.min_length > 0 else None
    if kind == 'integer':
        return False if value.lower == value.upper == 0 else True if value.lower > 0 or value.upper < 0 else None
    return False


def possible_truth(value):
    result = truth(value)
    return (False, True) if result is None else (result,)


def integer(operation, left, right=None):
    if _TYPE(operation) is not _STR or operation not in ('add', 'subtract', 'positive', 'negative'):
        raise BoundedUnknown('integer operation is outside the finite domain')
    if value_kind(left) != 'integer' or (operation in ('add', 'subtract') and value_kind(right) != 'integer'):
        raise BoundedUnknown('integer operands are not exact integer envelopes')
    a = abstract(left)
    if operation in ('positive', 'negative'):
        low, high = (a.lower, a.upper) if operation == 'positive' else (-a.upper, -a.lower)
    else:
        b = abstract(right)
        low, high = ((a.lower + b.lower, a.upper + b.upper) if operation == 'add'
                     else (a.lower - b.upper, a.upper - b.lower))
    result = BoundedInteger(low, high)
    return low if not is_bounded(left) and (right is None or not is_bounded(right)) else result


def compare(operation, left, right):
    if _TYPE(operation) is not _STR or operation not in ('eq', 'ne', 'lt', 'le', 'gt', 'ge'):
        raise BoundedUnknown('comparison operation is outside the finite domain')
    kind = value_kind(left)
    if kind != value_kind(right) or kind not in ('integer', 'string'):
        raise BoundedUnknown('comparison operands have no supported equal native kind')
    if not is_bounded(left) and not is_bounded(right):
        if operation == 'eq': return left == right
        if operation == 'ne': return left != right
        if operation == 'lt': return left < right
        if operation == 'le': return left <= right
        if operation == 'gt': return left > right
        return left >= right
    a, b = abstract(left), abstract(right)
    if kind == 'string':
        if operation not in ('eq', 'ne'):
            raise BoundedUnknown('unknown string order is outside the finite control domain')
        unequal = a.max_length < b.min_length or b.max_length < a.min_length
        equal = a.max_length == b.max_length == 0
    else:
        unequal = a.upper < b.lower or b.upper < a.lower
        equal = a.lower == a.upper == b.lower == b.upper
        if operation in ('lt', 'le', 'gt', 'ge'):
            if operation == 'lt': yes, no = a.upper < b.lower, a.lower >= b.upper
            elif operation == 'le': yes, no = a.upper <= b.lower, a.lower > b.upper
            elif operation == 'gt': yes, no = a.lower > b.upper, a.upper <= b.lower
            else: yes, no = a.lower >= b.upper, a.upper < b.lower
            return True if yes else False if no else BoundedBoolean()
    if operation == 'eq':
        return False if unequal else True if equal else BoundedBoolean()
    return True if unequal else False if equal else BoundedBoolean()


def _string(value):
    if value_kind(value) != 'string':
        raise BoundedUnknown('receiver/element is not a finite ordinary string')
    return abstract(value)


def string_operation(operation, receiver, arguments=(), keywords=None):
    """Bound successful original string results; exceptional paths stay caller-owned."""
    if (_TYPE(operation) is not _STR or operation not in ('split', 'join', 'strip', 'replace')
            or _TYPE(arguments) not in (tuple, _LIST)
            or keywords is not None and (_TYPE(keywords) is not dict or keywords)):
        raise BoundedUnknown('string operation or argument binding is outside the finite domain')
    source = _string(receiver)
    if operation == 'split':
        if len(arguments) > 2:
            raise BoundedUnknown('split arguments are not covered')
        separator = arguments[0] if arguments else None
        maximum = arguments[1] if len(arguments) == 2 else -1
        if separator is not None and (_TYPE(separator) is not _STR or not separator):
            raise BoundedUnknown('split separator is not an exact nonempty string/None')
        if separator is not None:
            _size(len(separator), MAX_STRING)
        _integer(maximum)
        upper = source.max_length if separator is None else source.max_length // len(separator) + 1
        if maximum >= 0:
            upper = min(upper, maximum + 1)
        lower = 0 if separator is None else 1
        return BoundedList(BoundedString(source.max_length), upper, lower)
    if operation == 'strip':
        if len(arguments) > 1 or arguments and arguments[0] is not None and _TYPE(arguments[0]) is not _STR:
            raise BoundedUnknown('strip argument is not exact string/None')
        if arguments and arguments[0] is not None:
            _size(len(arguments[0]), MAX_STRING)
        return BoundedString(source.max_length)
    if operation == 'replace':
        if (not 2 <= len(arguments) <= 3 or _TYPE(arguments[0]) is not _STR
                or _TYPE(arguments[1]) is not _STR):
            raise BoundedUnknown('replace old/new are not exact strings')
        old, new = arguments[:2]
        _size(len(old), MAX_STRING)
        _size(len(new), MAX_STRING)
        maximum = arguments[2] if len(arguments) == 3 else -1
        _integer(maximum)
        occurrences = source.max_length // len(old) if old else source.max_length + 1
        if maximum >= 0:
            occurrences = min(occurrences, maximum)
        return BoundedString(source.max_length + occurrences * max(0, len(new) - len(old)))
    if len(arguments) != 1 or value_kind(arguments[0]) != 'list':
        raise BoundedUnknown('join input is not one finite native list')
    values = arguments[0]
    if _TYPE(values) is _LIST:
        pieces = [_string(item) for item in values]
        upper = sum(item.max_length for item in pieces) + max(0, len(pieces) - 1) * source.max_length
        lower = sum(item.min_length for item in pieces) + max(0, len(pieces) - 1) * source.min_length
    else:
        element = _string(values.element) if values.max_length else BoundedString(0)
        upper = values.max_length * element.max_length + max(0, values.max_length - 1) * source.max_length
        lower = values.min_length * element.min_length + max(0, values.min_length - 1) * source.min_length
    return BoundedString(upper, lower)


def slice_list(value, lower=None, upper=None, step=None):
    if value_kind(value) != 'list' or any(item is not None and _TYPE(item) is not _INT for item in (lower, upper, step)) or step == 0:
        raise BoundedUnknown('list slice bounds are not exact native nonzero-step values')
    for item in (lower, upper, step):
        if item is not None: _integer(item)
    source = abstract(value)
    if source.max_length > MAX_LIST:
        if step not in (None, 1) or lower not in (None, 0) or upper is not None and upper >= 0:
            raise BoundedUnknown('large symbolic list slice is outside proved prefix/truncation forms')
        if upper is None:
            return source
        return BoundedList(source.element, max(0, source.max_length + upper), max(0, source.min_length + upper))
    # This enumerates at most 65 possible lengths, never possible list values.
    lengths = [len(range(count)[slice(lower, upper, step)])
               for count in range(source.min_length, source.max_length + 1)]
    return BoundedList(source.element, max(lengths), min(lengths))


def append_list(value, item):
    if value_kind(value) != 'list':
        raise BoundedUnknown('append receiver is not a finite native list')
    source = abstract(value)
    element = abstract(item) if not source.max_length else merge(source.element, item)
    return BoundedList(element, source.max_length + 1, source.min_length + 1)


def extend_list(value, items):
    if value_kind(value) != 'list' or value_kind(items) != 'list':
        raise BoundedUnknown('extend operands are not finite native lists')
    left, right = abstract(value), abstract(items)
    element = (right.element if not left.max_length else left.element if not right.max_length
               else merge(left.element, right.element))
    return BoundedList(element, left.max_length + right.max_length, left.min_length + right.min_length)
