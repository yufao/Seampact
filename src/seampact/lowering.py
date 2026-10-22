"""Finite sequential async-loop extraction with structural inverse checking.

Original iteration, per-item reads, mutable aliases and exceptions remain in
their original order. No batching or parallelization is implied by extraction.
"""

import ast
import copy
import hashlib

from .inspection import native_ast


class UnsupportedLowering(ValueError):
    pass


def locate(tree, symbol):
    parts = symbol.split(".")
    if len(parts) != 2:
        raise UnsupportedLowering("supported rule requires a top-level class async method")
    classes = [n for n in tree.body if isinstance(n,ast.ClassDef) and n.name==parts[0]]
    if len(classes)!=1:
        raise UnsupportedLowering("class source anchor is not unique")
    methods=[n for n in classes[0].body if isinstance(n,ast.AsyncFunctionDef) and n.name==parts[1]]
    if len(methods)!=1:
        raise UnsupportedLowering("async method source anchor is not unique")
    return classes[0],methods[0]


def names(nodes, context):
    return {n.id for node in nodes for n in ast.walk(node) if isinstance(n,ast.Name) and isinstance(n.ctx,context)}


def import_names(node):
    """Python imports bind aliases (or a dotted import's first component)."""
    if isinstance(node,ast.Import):
        return {alias.asname or alias.name.split(".")[0] for alias in node.names}
    if isinstance(node,ast.ImportFrom):
        return {alias.asname or alias.name for alias in node.names}
    return set()


def lexical_bindings(nodes):
    """Include local bindings which have no ast.Name(Store) representation.

    Nested scopes are rejected by the rule before this is used. Comprehension
    targets may conservatively add a local; an unproved incoming is rejected.
    """
    result=names(nodes,ast.Store) | names(nodes,ast.Del)
    for statement in nodes:
        for node in ast.walk(statement):
            result.update(import_names(node))
            if isinstance(node,(ast.ExceptHandler,ast.MatchAs,ast.MatchStar)) and node.name:
                result.add(node.name)
            elif isinstance(node,ast.MatchMapping) and node.rest:
                result.add(node.rest)
    return result


def analyze_loop(source, symbol, loop_index=0):
    tree=native_ast(source)
    cls,method=locate(tree,symbol)
    if any(isinstance(n,(ast.Global,ast.Nonlocal,ast.FunctionDef,ast.AsyncFunctionDef,
                         ast.ClassDef,ast.Lambda)) for statement in method.body for n in ast.walk(statement)):
        raise UnsupportedLowering("nested lexical scopes and global/nonlocal bindings unsupported")
    if not method.args.args or method.args.args[0].arg!="self":
        raise UnsupportedLowering("instance method with self required")
    if any(isinstance(d,ast.Name) and d.id in {"staticmethod","classmethod"} for d in method.decorator_list):
        raise UnsupportedLowering("static/class methods are not this rule")
    loops=[(i,n) for i,n in enumerate(method.body) if isinstance(n,ast.For)]
    if type(loop_index) is not int or not 0<=loop_index<len(loops):
        raise UnsupportedLowering("top-level loop anchor absent")
    position,loop=loops[loop_index]
    if not isinstance(loop.target,ast.Name) or loop.orelse:
        raise UnsupportedLowering("simple iterator target and no for-else required")
    forbidden=(ast.Break,ast.Continue,ast.Return,ast.Yield,ast.YieldFrom,ast.Try,ast.With,
               ast.AsyncWith,ast.AsyncFor,ast.For,ast.If,ast.Match,ast.FunctionDef,ast.AsyncFunctionDef,
               ast.ClassDef,ast.Lambda,ast.Global,ast.Nonlocal,ast.Delete,ast.NamedExpr,
               ast.ListComp,ast.SetComp,ast.DictComp,ast.GeneratorExp,ast.AugAssign)
    if not loop.body or any(not isinstance(n,(ast.Assign,ast.Expr,ast.Pass)) for n in loop.body):
        raise UnsupportedLowering("straight-line assign/expr loop body required")
    if any(isinstance(n,forbidden) for statement in loop.body for n in ast.walk(statement)):
        raise UnsupportedLowering("control transfer, nested scope or carried assignment unsupported")
    for node in ast.walk(method):
        if isinstance(node,ast.Call) and (
            isinstance(node.func,ast.Name) and node.func.id in {"locals","globals","vars","eval","exec","dir","super"}
            or isinstance(node.func,ast.Attribute) and node.func.attr in {"currentframe","_getframe","stack"}):
            raise UnsupportedLowering("frame/scope reflection is outside the extraction rule")
    for statement in loop.body:
        if isinstance(statement,ast.Assign) and (len(statement.targets)!=1 or not isinstance(statement.targets[0],ast.Name)):
            raise UnsupportedLowering("only single local assignments supported")
    parameters={n.arg for n in method.args.posonlyargs+method.args.args+method.args.kwonlyargs}
    if method.args.vararg: parameters.add(method.args.vararg.arg)
    if method.args.kwarg: parameters.add(method.args.kwarg.arg)
    method_locals=parameters | lexical_bindings(method.body)
    incoming=[]
    assigned=set()
    for statement in loop.body:
        read_node=statement.value if isinstance(statement,ast.Assign) else statement
        for node in ast.walk(read_node):
            if isinstance(node,ast.Name) and isinstance(node.ctx,ast.Load) and node.id in method_locals:
                if node.id not in assigned and node.id!="self" and node.id not in incoming:
                    incoming.append(node.id)
        if isinstance(statement,ast.Assign):
            assigned.add(statement.targets[0].id)
    if assigned.intersection(incoming):
        raise UnsupportedLowering("scalar loop carry requires a different return/state rule")
    if assigned.intersection({"self",loop.target.id}):
        raise UnsupportedLowering("self and the original iterator target cannot be rebound")
    after_reads=names(method.body[position+1:],ast.Load)
    if assigned.intersection(after_reads):
        raise UnsupportedLowering("body locals are live after the loop")
    # A read of an uninitialized method local used to fail in the loop body.
    # Moving that error to argument evaluation could change earlier effects.
    # Only direct assignments/imports establish definite initialization. A name
    # stored in an earlier if/try/loop does not prove it exists on this path.
    before_defs=set(parameters)
    for statement in method.body[:position]:
        if isinstance(statement,ast.Assign):
            before_defs.update(t.id for t in statement.targets if isinstance(t,ast.Name))
        elif isinstance(statement,ast.AnnAssign) and statement.value is not None and isinstance(statement.target,ast.Name):
            before_defs.add(statement.target.id)
        before_defs.update(import_names(statement))
        # Exception targets are automatically deleted at handler exit, including
        # a target which shadowed an earlier parameter/assignment/import.
        before_defs.difference_update(n.name for n in ast.walk(statement)
                                      if isinstance(n,ast.ExceptHandler) and n.name)
        if any(isinstance(n,ast.Delete) for n in ast.walk(statement)):
            raise UnsupportedLowering("deletion before the cutpoint needs a path-sensitive rule")
    before_defs.add(loop.target.id)
    if not set(incoming)<=before_defs:
        raise UnsupportedLowering("incoming local is not defined at the original entry cutpoint")
    helper="_seampact_body_"+hashlib.sha256((symbol+":"+str(loop_index)).encode()).hexdigest()[:12]
    if any(getattr(n,"name",None)==helper for n in cls.body):
        raise UnsupportedLowering("helper name collides with source class")
    return dict(rule="sequential-async-loop-body/1",symbol=symbol,loop_index=loop_index,
                loop_position=position,helper=helper,incoming=incoming,body_locals=sorted(assigned),
                source_line=loop.lineno,end_line=loop.end_lineno,
                assumptions=["no externally observed frame/local layout or added-method reflection",
                             "native dependency and effect summaries remain to be checked"],
                parallel_admitted=False)


def lower_loop(source,symbol,loop_index=0):
    recipe=analyze_loop(source,symbol,loop_index)
    tree=native_ast(source)
    cls,method=locate(tree,symbol)
    loop=method.body[recipe["loop_position"]]
    helper=ast.AsyncFunctionDef(name=recipe["helper"],
        args=ast.arguments(posonlyargs=[],args=[ast.arg(arg=name) for name in ["self"]+recipe["incoming"]],
                           vararg=None,kwarg=None,kwonlyargs=[],kw_defaults=[],defaults=[]),
        body=copy.deepcopy(loop.body),decorator_list=[],returns=None,type_comment=None,
        **({"type_params": []} if "type_params" in ast.AsyncFunctionDef._fields else {}))
    loop.body=[ast.Expr(value=ast.Await(value=ast.Call(
        func=ast.Attribute(value=ast.Name(id="self",ctx=ast.Load()),attr=recipe["helper"],ctx=ast.Load()),
        args=[ast.Name(id=name,ctx=ast.Load()) for name in recipe["incoming"]],keywords=[])))]
    cls.body.append(helper)
    patched=ast.unparse(ast.fix_missing_locations(tree))+"\n"
    report=verify_lowering(source,patched,recipe)
    if not report["structural_inverse"]:
        raise ValueError("lowering failed independent structural inverse")
    return dict(source=patched,recipe=recipe,verification=report)


def verify_lowering(original,patched,recipe):
    """Invert the submitted artifact and compare whole ASTs; don't regenerate it."""
    derived=analyze_loop(original,recipe["symbol"],recipe["loop_index"])
    if any(recipe.get(k)!=derived[k] for k in ("rule","symbol","loop_index","loop_position","helper","incoming","body_locals")):
        return dict(structural_inverse=False,reason="recipe disagrees with independent source analysis")
    before=native_ast(original)
    after=native_ast(patched)
    cls,method=locate(after,recipe["symbol"])
    candidates=[n for n in cls.body if isinstance(n,ast.AsyncFunctionDef) and n.name==recipe["helper"]]
    if len(candidates)!=1:
        return dict(structural_inverse=False,reason="helper absent or duplicated")
    helper=candidates[0]
    expected_args=["self"]+recipe["incoming"]
    if [a.arg for a in helper.args.args]!=expected_args or helper.decorator_list or helper.returns or (
        helper.args.posonlyargs or helper.args.kwonlyargs or helper.args.defaults or helper.args.kw_defaults or
        helper.args.vararg or helper.args.kwarg or getattr(helper,"type_comment",None) or
        getattr(helper,"type_params",None) or any(a.annotation or a.type_comment for a in helper.args.args)):
        return dict(structural_inverse=False,reason="helper interface altered")
    position=recipe["loop_position"]
    if position>=len(method.body) or not isinstance(method.body[position],ast.For):
        return dict(structural_inverse=False,reason="loop anchor altered")
    loop=method.body[position]
    expected=ast.parse("async def check(self):\n    await self."+recipe["helper"]+"("+", ".join(recipe["incoming"])+")").body[0].body
    if ast.dump(ast.Module(body=loop.body,type_ignores=[]))!=ast.dump(ast.Module(body=expected,type_ignores=[])):
        return dict(structural_inverse=False,reason="callsite altered")
    loop.body=copy.deepcopy(helper.body)
    cls.body.remove(helper)
    identical=ast.dump(before,include_attributes=False)==ast.dump(after,include_attributes=False)
    return dict(structural_inverse=identical,scope="whole code-unit AST with exact original body restored",
                sequential_conformance="NOT_RUN",effect_closure="UNKNOWN",parallel_admitted=False)
