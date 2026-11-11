"""Finite AST coverage over ordinary value envelopes, never source execution.

Unknown predicates check both original arms. Unknown finite list lengths check
every possible iteration count with the full element domain on every resume.
This proves an envelope and coverage, not an invented sequence or execution
trace. Original SDK identities, effects and helper targets remain independent
normal-worker obligations.
"""
import ast
import copy
import hashlib
from dataclasses import dataclass, field

from seampact.source.call_value_conditions import finite_split, ordinary, CallUnknown
from seampact.source.sdk_bounded_values import (
    BoundedUnknown, BoundedString, BoundedInteger, BoundedBoolean, BoundedList, BoundedTuple,
    is_bounded, value_kind, abstract, append_list, compare, extend_list, integer,
    length, merge, possible_truth, slice_list, string_operation, truth,
)

RULE = 'source-bounded-value-flow/1'
TARGET_SCHEMA = 'source-bounded-value-target-requirements/1'
ITERATION_RULE = 'source-bounded-iteration-consumption/1'


@dataclass(frozen=True)
class BoundedIteration:
    domain: BoundedList
    site: int
    requirements: tuple = ()


@dataclass(frozen=True)
class FlowResult:
    value: object
    generator: bool
    requirements: tuple


def _domain(value):
    from seampact.source.dependency_closure import _LiteralNone
    from seampact.source.sdk_value_summaries import SDKListDomain
    if type(value) is _LiteralNone:
        return None
    if type(value) is SDKListDomain:
        if type(value.max_item_length) is not int or type(value.max_length) is not int:
            raise BoundedUnknown('SDK result has no finite ordinary domain')
        return BoundedList(BoundedString(value.max_item_length), value.max_length)
    if type(value) is BoundedIteration:
        return value.domain
    return value


def _iteration_domain(value, node):
    if type(value) is BoundedIteration and value.site != id(node):
        raise BoundedUnknown('bounded generator escapes its immediate original consumption')
    return _domain(value)


def contains_bounded(value, seen=None):
    from seampact.source.sdk_value_summaries import SDKListDomain
    if is_bounded(value) or type(value) in (SDKListDomain, BoundedIteration):
        return True
    seen = set() if seen is None else seen
    if id(value) in seen:
        return False
    seen.add(id(value))
    if type(value) is list or type(value) is dict:
        return any(contains_bounded(item, seen) for item in (value.values() if type(value) is dict else value))
    return False


@dataclass
class _State:
    env: dict
    owned: set = field(default_factory=set)
    output: object = None
    returned: bool = False
    result: object = None

    def clone(self):
        # All mutable operations below replace fresh local list envelopes;
        # borrowed lists and source owners are never mutated by analysis.
        return _State(dict(self.env), set(self.owned), self.output, self.returned, self.result)


class _Flow:
    def __init__(self, checker, path, scope, *, generator=False):
        self.checker, self.path, self.scope = checker, path, scope
        self.generator = generator
        self.steps = 0
        self.requirements = []
        self.owner_fields = []

    def tick(self):
        self.steps += 1
        self.checker.steps += 1
        if self.steps > 16000 or self.checker.steps > 16000:
            raise BoundedUnknown('bounded flow exceeds finite analysis steps 16000')

    def _native(self, kind):
        if not self.checker.ordinary_value_operation(kind):
            raise BoundedUnknown('original native operation binding is not closed')

    def bind_target(self,target,value,state):
        if isinstance(target,ast.Name):
            state.env[target.id] = value
            state.owned.discard(target.id)
            return
        if isinstance(target,(ast.Tuple,ast.List)) and type(value) is BoundedTuple:
            if len(target.elts) != len(value.items) or any(isinstance(item,ast.Starred) for item in target.elts):
                raise BoundedUnknown('original tuple unpacking arity is not closed')
            for child,item in zip(target.elts,value.items):
                self.bind_target(child,item,state)
            return
        raise BoundedUnknown('bounded iteration target is outside native local tuple unpacking')

    @staticmethod
    def reference(node):
        while isinstance(node, ast.Attribute):
            node = node.value
        return isinstance(node, ast.Name)

    def _join(self, left, right):
        if left.returned != right.returned:
            raise BoundedUnknown('branches mix termination and continuation')
        result = {}
        for name in left.env.keys() | right.env.keys():
            if name not in left.env or name not in right.env:
                raise BoundedUnknown('branch leaves a possibly unbound local')
            a, b = left.env[name], right.env[name]
            result[name] = a if a is b else merge(_domain(a), _domain(b))
        output = (right.output if left.output is None else left.output if right.output is None
                  else merge(left.output, right.output))
        if left.output is None and right.output is not None:
            output = merge([], right.output)
        elif right.output is None and left.output is not None:
            output = merge(left.output, [])
        returned = None
        if left.returned:
            returned = left.result if left.result is right.result else merge(_domain(left.result), _domain(right.result))
        return _State(result, left.owned & right.owned, output, left.returned, returned)

    def _join_at(self, left, right, continuation):
        missing = left.env.keys() ^ right.env.keys()
        loaded = {item.id for statement in continuation for item in ast.walk(statement)
                  if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Load)}
        if missing & loaded:
            raise BoundedUnknown('possibly unbound local is read after a bounded branch/loop')
        for name in missing:
            left.env.pop(name, None)
            right.env.pop(name, None)
            left.owned.discard(name)
            right.owned.discard(name)
        return self._join(left, right)

    def expression(self, node, state):
        self.tick()
        env = state.env
        if isinstance(node, ast.Constant):
            if node.value is not None and type(node.value) not in (str, int, bool):
                raise BoundedUnknown('literal is outside the bounded ordinary domain')
            return node.value
        if isinstance(node, ast.Name):
            if node.id not in env:
                raise BoundedUnknown('bounded source name is unresolved')
            if type(env[node.id]) is BoundedIteration:
                raise BoundedUnknown('stored bounded generator has no immediate consumption proof')
            return _domain(env[node.id])
        if isinstance(node, ast.List):
            if any(isinstance(item, ast.Starred) for item in node.elts):
                raise BoundedUnknown('expanded list literal is outside bounded flow')
            values = [self.expression(item, state) for item in node.elts]
            abstract(values)
            return values
        if isinstance(node,ast.Tuple):
            if any(isinstance(item,ast.Starred) for item in node.elts):
                raise BoundedUnknown('expanded tuple literal is outside bounded flow')
            return BoundedTuple(tuple(self.expression(item,state) for item in node.elts))
        if isinstance(node, ast.Attribute):
            if not self.reference(node):
                raise BoundedUnknown('bounded field receiver is not a passive source reference')
            owner = self.checker.value(node.value, env, self.path, self.scope)
            fields = self.checker.generator_fields(owner)
            if fields is None:
                from seampact.source.sdk_transformer_protocols import is_sdk_value
                if is_sdk_value(owner):
                    value = self.checker.attribute_value(node,owner,self.path,self.scope)
                    abstract(_domain(value))
                    return _domain(value)
            if type(fields) is not dict or node.attr not in fields or not self.checker.call_plain(fields[node.attr]):
                raise BoundedUnknown('owner field is not a closed exact ordinary source value')
            value = fields[node.attr]
            try:
                copied = ordinary(value)
            except CallUnknown as error:
                raise BoundedUnknown('owner field value has no original ordinary proof') from error
            fact = dict(name=node.attr,value=copied,**self.checker.source_owner_path(owner))
            if fact not in self.owner_fields:
                self.owner_fields.append(fact)
            return value
        if isinstance(node, ast.Subscript):
            value = self.expression(node.value, state)
            if isinstance(node.slice, ast.Slice):
                bounds = [None if item is None else self.expression(item, state)
                          for item in (node.slice.lower, node.slice.upper, node.slice.step)]
                if type(value) is list and all(item is None or type(item) is int for item in bounds) and bounds[2] != 0:
                    return value[slice(*bounds)]
                if (type(value) is BoundedList and bounds[2] in (None,1)
                        and all(type(item) is int or type(item) is BoundedInteger for item in bounds[:2])):
                    low,high = abstract(bounds[0]),abstract(bounds[1])
                    if low.lower < 0 or high.lower < 0:
                        raise BoundedUnknown('symbolic slice requires nonnegative original bounds')
                    maximum = min(value.max_length,max(0,high.upper-low.lower))
                    if (isinstance(node.slice.lower,ast.Name) and isinstance(node.slice.upper,ast.BinOp)
                            and isinstance(node.slice.upper.op,ast.Add) and isinstance(node.slice.upper.left,ast.Name)
                            and node.slice.upper.left.id == node.slice.lower.id):
                        width_node = node.slice.upper.right
                        width = (width_node.value if isinstance(width_node,ast.Constant) else
                                 state.env.get(width_node.id) if isinstance(width_node,ast.Name) else None)
                        if type(width) is int and width >= 0:
                            maximum = min(maximum,width)
                    minimum = max(0,min(value.min_length,high.lower)-min(value.max_length,low.upper))
                    return BoundedList(value.element,maximum,min(minimum,maximum))
                return slice_list(value, *bounds)
            index = self.expression(node.slice, state)
            if type(value) is list and type(index) is int and -len(value) <= index < len(value):
                return value[index]
            raise BoundedUnknown('unknown index has no all-path normal-result proof')
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult):
            left, right = self.expression(node.left, state), self.expression(node.right, state)
            if value_kind(left) != 'list' or value_kind(right) != 'integer':
                raise BoundedUnknown('repetition is outside finite native list/integer values')
            self._native('list')
            self._native('int')
            source, count = abstract(left), abstract(right)
            return BoundedList(source.element, source.max_length * max(0, count.upper),
                               source.min_length * max(0, count.lower))
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub)):
            left,right = self.expression(node.left,state),self.expression(node.right,state)
            if isinstance(node.op,ast.Add) and value_kind(left)==value_kind(right)=='string':
                self._native('str')
                a,b = abstract(left),abstract(right)
                return BoundedString(a.max_length+b.max_length,a.min_length+b.min_length)
            self._native('int')
            return integer('add' if isinstance(node.op, ast.Add) else 'subtract',
                           left,right)
        if isinstance(node, ast.UnaryOp):
            value = self.expression(node.operand, state)
            if isinstance(node.op, ast.Not):
                result = truth(value)
                return BoundedBoolean() if result is None else not result
            if isinstance(node.op, (ast.UAdd, ast.USub)):
                self._native('int')
                return integer('positive' if isinstance(node.op, ast.UAdd) else 'negative', value)
        if isinstance(node, ast.Compare) and len(node.ops) == 1:
            operation = {ast.Eq:'eq', ast.NotEq:'ne', ast.Lt:'lt', ast.LtE:'le', ast.Gt:'gt', ast.GtE:'ge'}.get(type(node.ops[0]))
            if operation is None:
                raise BoundedUnknown('comparison protocol is outside bounded flow')
            left, right = self.expression(node.left, state), self.expression(node.comparators[0], state)
            self._native('str' if value_kind(left) == 'string' else 'int')
            return compare(operation, left, right)
        if isinstance(node, ast.BoolOp):
            value = self.expression(node.values[0], state)
            for item in node.values[1:]:
                test = truth(value)
                select_right = test is False if isinstance(node.op, ast.Or) else test is True
                if select_right:
                    value = self.expression(item, state)
                elif test is None:
                    value = merge(value, self.expression(item, state))
            return value
        if isinstance(node, ast.IfExp):
            predicate = truth(self.expression(node.test, state))
            if predicate is not None:
                return self.expression(node.body if predicate else node.orelse, state)
            return merge(self.expression(node.body, state.clone()), self.expression(node.orelse, state.clone()))
        if isinstance(node, ast.ListComp):
            if len(node.generators) != 1 or node.generators[0].is_async:
                raise BoundedUnknown('comprehension is outside one finite synchronous list domain')
            generator = node.generators[0]
            if not isinstance(generator.target, (ast.Name,ast.Tuple,ast.List)):
                raise BoundedUnknown('comprehension target is not a local name')
            values = _iteration_domain(self.expression(generator.iter, state), generator.iter)
            if type(values) is list:
                # Exact original ordinary input order/cardinality is retained;
                # only each SDK result's own unknown domain is propagated.
                results, optional = [], False
                for value in values:
                    local = state.clone()
                    self.bind_target(generator.target,value,local)
                    keep = True
                    for condition in generator.ifs:
                        outcome = truth(self.expression(condition, local))
                        if outcome is False:
                            keep = False
                            break
                        if outcome is None:
                            keep = None
                    if keep is not False:
                        results.append(self.expression(node.elt, local))
                        optional |= keep is None
                if not optional:
                    abstract(results)
                    return results
                domain = abstract(results)
                return BoundedList(domain.element, domain.max_length)
            domain = abstract(_domain(values))
            if type(domain) is not BoundedList:
                raise BoundedUnknown('comprehension input is not a finite ordinary list')
            if domain.max_length > 64:
                raise BoundedUnknown('symbolic comprehension exceeds explicit all-path iteration bound 64')
            if not domain.max_length:
                return []
            local = state.clone()
            self.bind_target(generator.target,domain.element,local)
            keep = True
            for condition in generator.ifs:
                outcome = truth(self.expression(condition, local))
                if outcome is False:
                    return []
                if outcome is None:
                    keep = None
            element = self.expression(node.elt, local)
            return BoundedList(abstract(element), domain.max_length, domain.min_length if keep is True else 0)
        if isinstance(node, ast.Call):
            return self.call(node, state)
        raise BoundedUnknown('expression is outside bounded flow: ' + type(node).__name__)

    def call(self, node, state):
        if any(isinstance(item, ast.Starred) for item in node.args) or any(item.arg is None for item in node.keywords):
            raise BoundedUnknown('expanded call binding is outside bounded flow')
        env = state.env
        if (isinstance(node.func,ast.Name) and node.func.id in ('zip','range') and node.func.id not in env):
            operation = node.func.id
            if not self.checker.ordinary_operation(node,env,self.path,operation) or node.keywords:
                raise BoundedUnknown('bounded iterator original builtin binding is not closed')
            values = [self.expression(item,state) for item in node.args]
            if operation == 'zip':
                if len(values) > 64:
                    raise BoundedUnknown('bounded zip arity exceeds finite native domain')
                domains = [abstract(_iteration_domain(value,argument)) for value,argument in zip(values,node.args)]
                if any(type(value) is not BoundedList for value in domains):
                    raise BoundedUnknown('bounded zip input is not an original finite ordinary sequence')
                maximum = min((value.max_length for value in domains),default=0)
                minimum = min((value.min_length for value in domains),default=0)
                element = BoundedTuple(tuple(value.element for value in domains)) if maximum else None
                return BoundedIteration(BoundedList(element,maximum,minimum),id(node))
            if not 1 <= len(values) <= 3:
                raise BoundedUnknown('bounded range arity is unsupported')
            start,stop,step = (0,values[0],1) if len(values)==1 else (
                values[0],values[1],1 if len(values)==2 else values[2])
            if type(start) is not int or type(step) is not int or start < 0 or step <= 0 or value_kind(stop)!='integer':
                raise BoundedUnknown('bounded range requires exact nonnegative start and positive original step')
            end = abstract(stop)
            minimum = max(0,(end.lower-start+step-1)//step)
            maximum = max(0,(end.upper-start+step-1)//step)
            if maximum > 64:
                raise BoundedUnknown('bounded range exceeds explicit all-path iteration bound 64')
            element = BoundedInteger(start,start+(maximum-1)*step) if maximum else None
            return BoundedIteration(BoundedList(element,maximum,minimum),id(node))
        if isinstance(node.func, ast.Name) and node.func.id == 'isinstance' and node.func.id not in env:
            # The operand is an envelope of exact ordinary builtins; an SDK
            # type name or a custom metaclass is never a type identity proof.
            if (len(node.args) != 2 or node.keywords
                    or self.checker.sdk_source.type_references.builtin(self.path, node)['status'] != 'SOURCE_CHECKED'):
                raise BoundedUnknown('bounded isinstance original operation is not closed')
            from seampact.source.call_value_conditions import builtin_state
            if not builtin_state():
                raise BoundedUnknown('bounded isinstance native target changed')
            qualified = self.checker.sdk_source.type_references.resolve(self.path, node.args[1], node)
            expected = {'builtins.str':'string', 'builtins.int':'integer', 'builtins.bool':'boolean',
                        'builtins.list':'list', 'builtins.dict':'dictionary'}.get(qualified)
            if expected is None:
                raise BoundedUnknown('bounded isinstance type is not an exact native ordinary class')
            kind = value_kind(self.expression(node.args[0], state))
            return kind == expected or kind == 'boolean' and expected == 'integer'
        args = [self.expression(item, state) for item in node.args]
        kwargs = {item.arg:self.expression(item.value, state) for item in node.keywords}
        if isinstance(node.func, ast.Name) and node.func.id == 'len' and node.func.id not in env:
            if not self.checker.ordinary_operation(node, env, self.path, 'len') or len(args) != 1 or kwargs:
                raise BoundedUnknown('original len binding is not closed')
            return length(args[0])
        if isinstance(node.func, ast.Attribute) and node.func.attr in ('split', 'join', 'replace', 'strip'):
            receiver = self.expression(node.func.value, state)
            self._native('str')
            if type(receiver) is str and node.func.attr == 'split':
                try:
                    return finite_split(receiver, args, kwargs)
                except CallUnknown as error:
                    raise BoundedUnknown(str(error)) from error
            return string_operation(node.func.attr, receiver, args, kwargs)
        from seampact.source.dependency_closure import _External, _Function, _BoundMethod
        if not self.reference(node.func):
            raise BoundedUnknown('bounded call target is not a passive source reference')
        function = self.checker.value(node.func, env, self.path, self.scope)
        if type(function) is _External and not function.local:
            from seampact.source.sdk_value_summaries import summarize_call
            result = summarize_call(self.checker, function.name, args, kwargs, self.path, node)
            if result is None or result[0] is None:
                raise BoundedUnknown('external call has no supported finite ordinary result')
            value, requirement = result
            self.requirements.append(requirement)
            return _domain(value)
        if type(function) in (_Function, _BoundMethod):
            # Bounded arguments force the normal closure into this interpreter;
            # its failure never falls through to the ordinary mutable engine.
            # Check only genuinely feasible arms here: a source owner's exact
            # original field can exclude an unsupported alternative branch.
            if not any(contains_bounded(item) for item in [*args, *kwargs.values()]):
                raise BoundedUnknown('nested source helper has no bounded pure-flow dispatch premise')
            before = len(self.checker.negative_scope_gaps)
            result = self.checker.call_value(function, args, kwargs, self.path, self.scope, node)
            from seampact.source.sdk_value_summaries import GAP as SDK_GAP
            if (result is None or any(item.get('reason') != SDK_GAP
                                    for item in self.checker.negative_scope_gaps[before:])):
                raise BoundedUnknown('nested source call has an unclosed path/effect')
            return result if type(result) is BoundedIteration else _domain(result)
        raise BoundedUnknown('call target/effect is outside bounded ordinary flow')

    def mutation(self, node, state):
        if (not isinstance(node.func, ast.Attribute) or not isinstance(node.func.value, ast.Name)
                or node.func.attr not in ('append', 'extend') or len(node.args) != 1 or node.keywords):
            raise BoundedUnknown('statement call is not one fresh local list mutation')
        name = node.func.value.id
        if name not in state.owned or name not in state.env:
            raise BoundedUnknown('list mutation can reach a borrowed value')
        receiver = state.env[name]
        if any(other != name and value is receiver for other, value in state.env.items()):
            raise BoundedUnknown('fresh local mutation has an unresolved alias')
        self._native('list')
        value = self.expression(node.args[0], state)
        state.env[name] = append_list(receiver, value) if node.func.attr == 'append' else extend_list(receiver, _domain(value))

    def loop(self, node, values, state, continuation):
        """The same all-length rule for helper and normal source consumers."""
        if not isinstance(node.target, (ast.Name,ast.Tuple,ast.List)):
            raise BoundedUnknown('bounded loop target is not a local name')
        values = _iteration_domain(values, node.iter)
        if type(values) is list:
            for value in values:
                self.bind_target(node.target,value,state)
                state = self.block(node.body, state)
                if state.returned:
                    raise BoundedUnknown('loop body terminates before complete finite consumption')
            return self.block(node.orelse, state)
        domain = abstract(values)
        if type(domain) is not BoundedList:
            raise BoundedUnknown('loop has no finite native ordinary list domain')
        if domain.max_length > 64:
            raise BoundedUnknown('symbolic loop exceeds explicit all-path iteration bound 64')
        # Each state is the entire possible prefix after that many original
        # resumes. Reusing a proved prefix does not select a runtime length.
        possibilities = []
        current = state.clone()
        for count in range(domain.max_length + 1):
            if count >= domain.min_length:
                possibilities.append(current.clone())
            if count == domain.max_length:
                break
            self.bind_target(node.target,domain.element,current)
            current = self.block(node.body, current)
            if current.returned:
                raise BoundedUnknown('loop body terminates before complete bounded consumption')
        result = possibilities[0]
        for alternative in possibilities[1:]:
            result = self._join_at(result, alternative, [*node.orelse, *continuation])
        return self.block(node.orelse, result)

    def block(self, statements, state):
        for position, node in enumerate(statements):
            self.tick()
            if state.returned:
                break
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and type(node.value.value) is str:
                continue
            if isinstance(node, ast.Pass):
                continue
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if len(targets) != 1 or not isinstance(targets[0], ast.Name) or node.value is None:
                    raise BoundedUnknown('assignment escapes finite local names')
                name = targets[0].id
                value = self.expression(node.value, state)
                if type(value) is BoundedIteration:
                    raise BoundedUnknown('bounded generator cannot be stored as an ordinary list')
                state.env[name] = value
                state.owned.discard(name)
                if isinstance(node.value, (ast.List, ast.ListComp)):
                    state.owned.add(name)
            elif isinstance(node, ast.AugAssign):
                if not isinstance(node.target, ast.Name) or not isinstance(node.op, (ast.Add, ast.Sub)):
                    raise BoundedUnknown('augmented assignment is not local finite integer arithmetic')
                self._native('int')
                state.env[node.target.id] = integer('add' if isinstance(node.op, ast.Add) else 'subtract',
                    state.env.get(node.target.id), self.expression(node.value, state))
            elif isinstance(node, ast.If):
                outcome = truth(self.expression(node.test, state))
                if outcome is None:
                    state = self._join_at(self.block(node.body, state.clone()), self.block(node.orelse, state.clone()),
                                          statements[position+1:])
                else:
                    state = self.block(node.body if outcome else node.orelse, state)
            elif isinstance(node, ast.For):
                state = self.loop(node,self.expression(node.iter,state),state,statements[position+1:])
            elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Yield):
                if not self.generator or node.value.value is None:
                    raise BoundedUnknown('yield is outside the supported original generator')
                value = self.expression(node.value.value, state)
                abstract(value)
                if value_kind(value) == 'list':
                    expression = node.value.value
                    fresh = isinstance(expression,ast.List) or isinstance(expression,ast.Subscript) and isinstance(expression.slice,ast.Slice)
                    domain = abstract(value)
                    def immutable(item):
                        kind = value_kind(item)
                        return kind not in ('list','tuple') or type(item) is BoundedTuple and all(immutable(part) for part in item.items)
                    if not fresh or domain.max_length and not immutable(domain.element):
                        raise BoundedUnknown('bounded generator exposes unresolved mutable yield storage')
                state.output = append_list([] if state.output is None else state.output, value)
            elif isinstance(node, ast.Return):
                if self.generator and node.value is not None and not (isinstance(node.value, ast.Constant) and node.value.value is None):
                    raise BoundedUnknown('generator return value is outside finite ordinary flow')
                state.returned = True
                state.result = None if node.value is None else self.expression(node.value, state)
            elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                self.mutation(node.value, state)
            else:
                raise BoundedUnknown('statement/control is outside bounded flow: ' + type(node).__name__)
        return state


def _fresh_normal_accumulators(target, node, env, checker):
    """Only original empty local lists with no intervening read can be owned.

    This deliberately does not infer freshness from a local variable name or
    from an ordinary-looking borrowed list. The flow engine replaces these
    envelopes; it never mutates the caller's analysis containers.
    """
    before = target.node.body[:target.node.body.index(node)]
    candidates = {}
    for position, statement in enumerate(before):
        writes = {item.id for item in ast.walk(statement)
                  if isinstance(item, ast.Name) and isinstance(item.ctx, (ast.Store,ast.Del))}
        for name in writes:
            candidates.pop(name,None)
        if (type(statement) is ast.Assign and len(statement.targets) == 1
                and type(statement.targets[0]) is ast.Name
                and type(statement.value) is ast.List and not statement.value.elts):
            candidates[statement.targets[0].id] = position
    owned = set()
    for name, position in candidates.items():
        value = env.get(name)
        if type(value) is not list or value or id(value) in checker.call_value_taints:
            continue
        if any(type(item) is ast.Name and isinstance(item.ctx,ast.Load) and item.id == name
               for statement in before[position+1:] for item in ast.walk(statement)):
            continue
        # Direct aliases are the only aliases a never-read fresh allocation
        # could have acquired in this frame; a borrowed/closed-over source
        # allocation cannot satisfy its original local construction above.
        if any(other != name and item is value for other,item in env.items()):
            continue
        owned.add(name)
    return owned


def consume_normal_for(checker, node, value, env, path, scope):
    """Consume one immediate bounded iterator using the existing finite flow.

    SDK objects are never added to the ordinary domain here. A body needing an
    unproved effect, SDK-result merge, borrowed mutation or unknown control
    fails before any state is published. All original target gaps survive.
    """
    if type(value) is not BoundedIteration:
        return None
    if type(node) is not ast.For or value.site != id(node.iter):
        raise BoundedUnknown('bounded iterator is not at its original immediate For site')
    domain = value.domain
    if type(domain) is not BoundedList:
        raise BoundedUnknown('bounded iterator has no original finite list domain')
    if domain.max_length > 64:
        raise BoundedUnknown('normal bounded For exceeds explicit all-path iteration bound 64')
    frames = checker.registration_frames
    target = frames[-1].get('target') if frames else None
    if target is None or node not in target.node.body:
        raise BoundedUnknown('normal bounded For has no direct original source frame')
    from seampact.source.finite_source_generators import own_nodes
    if any(isinstance(item,(ast.Global,ast.Nonlocal)) for item in own_nodes(target.node)):
        raise BoundedUnknown('normal bounded For source frame has a nonlocal writer')
    forbidden = (ast.Break,ast.Continue,ast.Return,ast.Raise,ast.Yield,ast.YieldFrom,ast.Await,
                 ast.AsyncFor,ast.AsyncWith,ast.With,ast.Try,ast.While,
                 ast.Import,ast.ImportFrom,ast.Global,ast.Nonlocal,ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef)
    if any(isinstance(item,forbidden) for statement in [*node.body,*node.orelse]
           for item in ast.walk(statement)):
        raise BoundedUnknown('normal bounded For has unproved termination, binding or exceptional control')
    local = dict(env)
    before = target.node.body[:target.node.body.index(node)]
    parameters = {item.arg for item in [*target.node.args.posonlyargs,*target.node.args.args,
                                        *target.node.args.kwonlyargs]}
    prior_stores = {item.id for statement in before for item in ast.walk(statement)
                    if type(item) is ast.Name and isinstance(item.ctx,ast.Store)}
    # The normal tracer seeds future locals with UNKNOWN. A variable whose
    # first original binding is in this loop is genuinely unbound on its zero
    # arm; it must not be joined as if UNKNOWN were a runtime None value.
    future_stores = {item.id for statement in target.node.body[target.node.body.index(node):]
                     for item in ast.walk(statement) if type(item) is ast.Name and isinstance(item.ctx,ast.Store)}
    for name in future_stores - prior_stores - parameters:
        if local.get(name) is None:
            local.pop(name,None)
    state = _State(local,owned=_fresh_normal_accumulators(target,node,env,checker))
    flow = _Flow(checker,path,scope)
    continuation = target.node.body[target.node.body.index(node)+1:]
    previous = checker.in_bounded_flow_attempt
    checker.in_bounded_flow_attempt = True
    try:
        state = flow.loop(node,value,state,continuation)
    finally:
        checker.in_bounded_flow_attempt = previous
    requirement = _target_requirement(checker,target,dict(env),(),flow.owner_fields)
    # Publish only a fully checked ordinary state. No representative element,
    # synthetic iterator or partly evaluated sibling branch enters the frame.
    env.clear()
    env.update(state.env)
    checker.protocol(RULE,path,node,target_requirements=requirement,
        runtime='original For order and for-else retained; all finite lengths checked')
    checker.protocol(ITERATION_RULE,path,node,
        minimum_length=domain.min_length,maximum_length=domain.max_length,
        coverage='ALL_FEASIBLE_LENGTHS_WITH_FULL_ORIGINAL_ELEMENT_DOMAIN',
        original_sequence='not invented; conditional original resume order retained',
        effects='all original helper/SDK target prerequisites remain independent',
        execution_permission=False)
    return False,None


def _target_requirement(checker, target, local, defaults_used, owner_fields):
    from seampact.source.finite_source_generators import SourceGenerator
    proxy = SourceGenerator(target, local, 0, (), defaults_used=tuple(defaults_used))
    requirement = checker.generator_target_requirements(proxy)
    requirement.update(schema=TARGET_SCHEMA, owner_fields=copy.deepcopy(owner_fields),
        coverage='ALL_FEASIBLE_ORIGINAL_BRANCHES_AND_FINITE_LENGTHS',
        result='ordinary envelope only; exact values/order/count not invented',
        required=[*requirement['required'], 'ALL_ORIGINAL_BOUND_VALUE_READS_AND_OWNER_FIELDS'])
    parameters = {item.arg for item in (*target.node.args.posonlyargs, *target.node.args.args,
                                       *target.node.args.kwonlyargs)}
    assigned = {item.id for item in ast.walk(target.node) if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Store)}
    loaded = {item.id for item in ast.walk(target.node) if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Load)}
    requirement['abstract_value_domains'] = [dict(name=name, domain=_describe(local[name]))
        for name in sorted(loaded & local.keys()) if contains_bounded(local[name])]
    module = checker.module(target.path)
    for name in sorted(loaded - parameters - assigned):
        if name not in local:
            continue
        value = local[name]
        if contains_bounded(value):
            requirement['same_call_captures'].append(dict(name=name, relation='FRESH_PARENT_SAME_CALL',
                domain=_describe(value), target_status='UNKNOWN'))
        elif checker.call_plain(value):
            fact = dict(name=name, value=ordinary(_domain(value)))
            if name in module and module[name] is value:
                requirement['source_globals'].append(fact)
            elif target.closure is not None and name in target.closure:
                requirement['same_call_captures'].append(dict(**fact, relation='FRESH_PARENT_SAME_CALL',
                    target_status='REQUIRES_NORMAL_WORKER_CHECK'))
        else:
            from seampact.source.dependency_closure import _Function, _External
            if type(value) not in (_Function, _External):
                requirement['same_call_captures'].append(dict(name=name, relation='ORIGINAL_SOURCE_OWNER',
                    target_status='UNKNOWN'))
    return requirement


def _describe(value):
    """Native JSON domain metadata; no guessed value or source data string."""
    value = abstract(_domain(value))
    if type(value) is BoundedString:
        return dict(kind='string', min_length=value.min_length, max_length=value.max_length)
    if type(value) is BoundedInteger:
        return dict(kind='integer', lower=value.lower, upper=value.upper)
    if type(value) is BoundedBoolean:
        return dict(kind='boolean', may_false=value.may_false, may_true=value.may_true)
    if type(value) is BoundedList:
        return dict(kind='list', min_length=value.min_length, max_length=value.max_length,
                    element=None if not value.max_length else _describe(value.element))
    if type(value) is BoundedTuple:
        return dict(kind='tuple',items=[_describe(item) for item in value.items])
    if value is None:
        return dict(kind='null')
    raise BoundedUnknown('bounded domain has no native JSON description')


def _sdk_body(checker, target, local):
    from seampact.source.dependency_closure import _External
    from seampact.source.sdk_value_summaries import _definition
    def external(node):
        # Applicability scanning must not interpret a call/getter/subscript or
        # change owner state before the bounded rule has been selected.
        if isinstance(node, ast.Name):
            value = local.get(node.id)
            return value if type(value) is _External else None
        if isinstance(node, ast.Attribute):
            parent = external(node.value)
            return _External(parent.name + '.' + node.attr, parent.local) if parent is not None else None
        return None
    for child in ast.walk(target.node):
        if isinstance(child, ast.Call):
            function = external(child.func)
            definition = _definition(function.name) if type(function) is _External and not function.local else None
            if definition is not None and definition['id'] == 'finite-original-sentence-slices/1':
                return True
    return False


def try_flow(checker, target, local, site, defaults_used=()):
    """Return None outside this rule; never use a partial failure as a result."""
    if not any(contains_bounded(value) for value in local.values()) and not _sdk_body(checker, target, local):
        return None
    from seampact.source.finite_source_generators import is_generator
    node = target.node
    if (not isinstance(node, ast.FunctionDef) or node.decorator_list or node.args.vararg or node.args.kwarg
            or node.returns is not None or any(parameter.annotation is not None
                for parameter in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs))):
        raise BoundedUnknown('bounded helper definition/wrapper is outside the finite domain')
    generator = is_generator(node)
    if any(type(value) is BoundedIteration for value in local.values()):
        raise BoundedUnknown('bounded generator argument/capture escapes immediate consumption')
    flow = _Flow(checker, target.path, target.name, generator=generator)
    parameters = {item.arg for item in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)}
    from seampact.source.finite_source_generators import own_nodes
    assigned = {item.id for item in own_nodes(node) if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Store)}
    environment = {name:_domain(value) for name,value in local.items()
                   if not (value is None and name in assigned - parameters)}
    result = flow.block(node.body, _State(environment))
    requirement = _target_requirement(checker, target, local, defaults_used, flow.owner_fields)
    requirements = (requirement, *flow.requirements)
    if generator:
        domain = abstract([] if result.output is None else result.output)
        value = BoundedIteration(domain, id(site), requirements)
    else:
        if not result.returned:
            raise BoundedUnknown('bounded helper has no proved explicit ordinary return')
        value = result.result
        abstract(value)
    return FlowResult(value, generator, requirements)


def evaluate_expression(checker, node, env, path, scope):
    """Check an expression over envelopes without changing its caller's locals."""
    return _Flow(checker, path, scope).expression(node, _State(dict(env)))


