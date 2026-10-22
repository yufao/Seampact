"""Finite R-INPLACE lowering of original synchronous/direct-await calls.

Only the callee expression changes. Arguments, their unpacking, the surrounding
frame, branches, loops, handlers and timeout scopes remain in the original code.
The injected factory must not wait: its returned proxy waits only when called,
after Python has evaluated the original arguments. Structural inverse checking
does not establish state stability, effect closure or deadline equivalence.
"""

import ast
import copy
import hashlib

from .lowering import UnsupportedLowering, lexical_bindings
from .inspection import native_ast


RULE = "inplace-callsite/1"


def _anchor(tree, symbol):
    parts = symbol.split(".")
    owner = None
    scope = tree.body
    if len(parts) == 2:
        owners = [node for node in scope if isinstance(node, ast.ClassDef) and node.name == parts[0]]
        if len(owners) != 1:
            raise UnsupportedLowering("original callsite owner is absent or ambiguous")
        owner, scope = owners[0], owners[0].body
    elif len(parts) != 1:
        raise UnsupportedLowering("callsite needs a top-level function or direct class method")
    matches = [node for node in scope if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
               and node.name == parts[-1]]
    if len(matches) != 1:
        raise UnsupportedLowering("original controller source anchor is absent or ambiguous")
    return matches[0], owner


def _walk(node, path=()):
    yield node, path
    for field, value in ast.iter_fields(node):
        if isinstance(value, ast.AST):
            yield from _walk(value, path + (field,))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                if isinstance(child, ast.AST):
                    yield from _walk(child, path + (field, index))


def _at(tree, path):
    node = tree
    for part in path:
        node = node[part] if isinstance(part, int) else getattr(node, part)
    return node


def _position(node):
    return dict(line=node.lineno, column=node.col_offset,
                end_line=node.end_lineno, end_column=node.end_col_offset)


def _source_offset(source, line, column):
    # Python AST columns are UTF-8 byte offsets, even for a decoded text source.
    lines = source.encode("utf-8").splitlines(keepends=True)
    if not 1 <= line <= len(lines) or not 0 <= column <= len(lines[line - 1]):
        raise UnsupportedLowering("callsite source span is outside the frozen code unit")
    return sum(len(value) for value in lines[:line - 1]) + column


def _target(tree, controller, owner, call):
    if isinstance(call.func, ast.Name):
        parameters = {argument.arg for argument in (controller.args.posonlyargs + controller.args.args
                                                     + controller.args.kwonlyargs)}
        if controller.args.vararg:
            parameters.add(controller.args.vararg.arg)
        if controller.args.kwarg:
            parameters.add(controller.args.kwarg.arg)
        local_names = parameters | lexical_bindings(controller.body)
        if call.func.id in local_names:
            raise UnsupportedLowering("callee is a local/parameter value; its actual target needs a host binding")
        matches = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and node.name == call.func.id]
        symbol = call.func.id
    elif (owner is not None and isinstance(call.func, ast.Attribute)
          and isinstance(call.func.value, ast.Name) and call.func.value.id == "self"):
        positional = controller.args.posonlyargs + controller.args.args
        if not positional or positional[0].arg != "self":
            raise UnsupportedLowering("self method call does not have its original instance receiver")
        if owner.bases or owner.keywords or owner.decorator_list:
            raise UnsupportedLowering("inherited/decorated owner dispatch needs a fixed native host summary")
        matches = [node for node in owner.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and node.name == call.func.attr]
        if any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in
               {"__getattribute__", "__getattr__", "__setattr__"} for node in owner.body):
            raise UnsupportedLowering("custom instance attribute dispatch needs its native host rule")
        symbol = owner.name + "." + call.func.attr
    else:
        raise UnsupportedLowering("call target is not a same-unit native function or declared self method")
    if len(matches) != 1:
        raise UnsupportedLowering("actual callee source is absent or ambiguous; imports/SDK dispatch need a host rule")
    target = matches[0]
    if target.decorator_list:
        raise UnsupportedLowering("decorated callee dispatch needs its actual effective-callable binding")
    if any(isinstance(node, (ast.Yield, ast.YieldFrom)) for node in ast.walk(target)):
        raise UnsupportedLowering("generator callee needs a streaming/resume lifecycle rule")
    return target, symbol


def analyze_callsite(source, symbol, callsite):
    if not isinstance(callsite, dict) or any(type(callsite.get(key)) is not int
            for key in ("line", "column", "end_line", "end_column")):
        raise UnsupportedLowering("callsite needs exact line/column/end_line/end_column source coordinates")
    tree = native_ast(source)
    controller, owner = _anchor(tree, symbol)
    if controller.decorator_list:
        raise UnsupportedLowering("decorated controller needs an effective native callable binding")
    if any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda,
                              ast.Yield, ast.YieldFrom, ast.Global, ast.Nonlocal))
           for statement in controller.body for node in ast.walk(statement)):
        raise UnsupportedLowering("nested scope, generator and global/nonlocal controller bindings are unsupported")
    for node in ast.walk(controller):
        if isinstance(node, ast.Call) and (
            isinstance(node.func, ast.Name) and node.func.id in
            {"locals", "globals", "vars", "eval", "exec", "dir", "super", "getattr", "setattr", "delattr"}
            or isinstance(node.func, ast.Attribute) and node.func.attr in {"currentframe", "_getframe", "stack"}):
            raise UnsupportedLowering("frame reflection or dynamic attribute mutation needs its own host rule")
    found = [(node, path) for node, path in _walk(tree) if isinstance(node, ast.Call)
             and _position(node) == callsite and any(node is member for member in ast.walk(controller))]
    if len(found) != 1:
        raise UnsupportedLowering("callsite does not identify exactly one original controller Call")
    call, path = found[0]
    target, target_symbol = _target(tree, controller, owner, call)
    parent = _at(tree, path[:-1]) if path and isinstance(path[-1], str) else None
    direct_await = isinstance(parent, ast.Await) and parent.value is call
    asynchronous = isinstance(target, ast.AsyncFunctionDef)
    if asynchronous != direct_await:
        raise UnsupportedLowering("only synchronous calls or direct await of a known async call are supported; "
                                  "creating an awaitable for later use is a different rule")
    if direct_await and not isinstance(controller, ast.AsyncFunctionDef):
        raise UnsupportedLowering("direct await callsite must remain in its original async controller")
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == target.name and isinstance(node.ctx, (ast.Store, ast.Del)):
            raise UnsupportedLowering("callee name can be reassigned in the frozen unit")
        if (owner is not None and isinstance(call.func, ast.Attribute) and isinstance(node, ast.Attribute)
                and node.attr == target.name and isinstance(node.ctx, (ast.Store, ast.Del))):
            raise UnsupportedLowering("selected method can be replaced in the frozen unit")
    helper = "_seampact_callsite_" + hashlib.sha256((symbol + repr(sorted(callsite.items()))).encode()).hexdigest()[:16]
    if any(isinstance(node, ast.Name) and node.id == helper or getattr(node, "name", None) == helper
           for node in ast.walk(tree)):
        raise UnsupportedLowering("generated callsite factory name collides with original source")
    return dict(rule=RULE, symbol=symbol, callsite=dict(callsite), helper=helper, node_path=list(path),
                callee=ast.unparse(call.func), target_symbol=target_symbol, effective_symbol=target_symbol,
                target_line=target.lineno, asynchronous=asynchronous,
                callee_span=_position(call.func), argument_evaluation="ORIGINAL_ONCE_BEFORE_GATE",
                control="ORIGINAL_FRAME_AND_CONTROL", timeout="ORIGINAL_SCOPES_QUEUE_COUNTS_INSIDE_ONLY",
                independent_export=False, parallel_admitted=False,
                unresolved=["stability of original argument references while waiting",
                            "native effects, retirement, locks and added-reference lifetime",
                            "external mutation of the resolved callable or its receiver",
                            "deadline crossing and observable added proxy/traceback frames"])


def lower_callsites(source, specifications):
    if not isinstance(specifications, list) or not specifications:
        raise UnsupportedLowering("at least one original callsite specification is required")
    recipes = [analyze_callsite(source, specification["symbol"], specification["callsite"])
               for specification in specifications]
    if len({tuple(recipe["node_path"]) for recipe in recipes}) != len(recipes):
        raise UnsupportedLowering("one original callsite cannot have multiple active proxies")
    replacements = []
    raw = source.encode("utf-8")
    for recipe in recipes:
        span = recipe["callee_span"]
        begin = _source_offset(source, span["line"], span["column"])
        end = _source_offset(source, span["end_line"], span["end_column"])
        replacement = recipe["helper"].encode() + b"(" + raw[begin:end] + b")"
        replacements.append((begin, end, replacement))
    replacements.sort(reverse=True)
    last_begin = len(raw)
    for begin, end, replacement in replacements:
        if end > last_begin:
            raise UnsupportedLowering("overlapping callee replacements need a different boundary")
        raw = raw[:begin] + replacement + raw[end:]
        last_begin = begin
    patched = raw.decode("utf-8")
    verification = verify_callsites(source, patched, recipes)
    if not verification["structural_inverse"]:
        raise UnsupportedLowering("in-place lowering failed its independent structural inverse")
    return dict(source=patched, recipes=recipes, verification=verification)


def lower_callsite(source, symbol, callsite):
    result = lower_callsites(source, [dict(symbol=symbol, callsite=callsite)])
    return dict(source=result["source"], recipe=result["recipes"][0], verification=result["verification"])


def verify_callsites(original, patched, recipes):
    before, after = native_ast(original), native_ast(patched)
    paths = set()
    for recipe in recipes:
        derived = analyze_callsite(original, recipe["symbol"], recipe["callsite"])
        if recipe != derived or tuple(derived["node_path"]) in paths:
            return dict(structural_inverse=False, reason="recipe differs from independent original-source analysis")
        paths.add(tuple(derived["node_path"]))
        try:
            original_call = _at(before, derived["node_path"])
            call = _at(after, derived["node_path"])
        except (AttributeError, IndexError, TypeError):
            return dict(structural_inverse=False, reason="original callsite structure changed")
        expected = ast.Call(func=ast.Name(id=derived["helper"], ctx=ast.Load()),
                            args=[copy.deepcopy(original_call.func)], keywords=[])
        if (not isinstance(call, ast.Call)
                or ast.dump(call.func, include_attributes=False) != ast.dump(expected, include_attributes=False)):
            return dict(structural_inverse=False, reason="callsite factory or original callee changed")
        call.func = copy.deepcopy(original_call.func)
    identical = ast.dump(before, include_attributes=False) == ast.dump(after, include_attributes=False)
    return dict(structural_inverse=identical, scope="whole code-unit AST; only original callee expressions restored",
                state_stability="UNKNOWN", effect_closure="UNKNOWN", deadline_equivalence="UNKNOWN",
                original_timeout_scopes=True, queue_counts_inside_original_timeout=True,
                independent_export=False, parallel_admitted=False)


def verify_callsite(original, patched, recipe):
    return verify_callsites(original, patched, [recipe])
