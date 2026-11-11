"""An explicit finite restriction of an original **kwargs request domain.

Values are accepted request literals, not defaults injected into native calls.
This restriction supplies no SDK-effect or lifecycle permission on its own.
"""
import copy
import json


def _literal(value, depth=0):
    if depth > 8:
        raise ValueError("keyword domain literal exceeds the finite depth")
    if value is None or type(value) in (str, int, float, bool):
        return
    if type(value) is list:
        for item in value:
            _literal(item, depth + 1)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for item in value.values():
            _literal(item, depth + 1)
        return
    raise ValueError("keyword domain values must be finite JSON literals")


def _key(value):
    _literal(value)
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":"))


def literal_equal(left, right):
    try:
        return _key(left) == _key(right)
    except (ValueError, TypeError, OverflowError):
        return False


def narrow_keywords(schema, node, domain, fixed=()):
    """Close only the original variadic keyword arm; never widen its signature."""
    if node.args.kwarg is None:
        raise ValueError("keyword_domain requires the original **kwargs parameter")
    if type(domain) is not dict or len(domain) > 32:
        raise ValueError("keyword_domain must be a finite object of additional keyword names")
    declared = {p.arg for p in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]}
    declared.update([node.args.kwarg.arg, *([node.args.vararg.arg] if node.args.vararg else [])])
    reserved = declared | set(fixed) | set(schema["properties"])
    result = copy.deepcopy(schema)
    for name, choices in domain.items():
        if (type(name) is not str or not name.isidentifier() or name.startswith("__")
                or name in reserved):
            raise ValueError("keyword_domain may restrict only additional original keyword names")
        if type(choices) is not list or not 1 <= len(choices) <= 16:
            raise ValueError("keyword_domain needs 1 to 16 explicit accepted literals per name")
        keys = [_key(value) for value in choices]
        if len(set(keys)) != len(keys):
            raise ValueError("keyword_domain contains duplicate accepted literals")
        result["properties"][name] = {"enum": copy.deepcopy(choices)}
    result["additionalProperties"] = False
    return result
