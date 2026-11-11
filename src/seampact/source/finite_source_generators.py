"""Lazy, bounded *analysis* of original synchronous source generators.

The iterator below interprets frozen AST over the checker's abstract ordinary
values. It never compiles, imports or executes analyzed Python. Only immediate
complete consumption is supported; no source generator is materialized or
replaced in the original runtime.
"""
import ast
from dataclasses import dataclass, field

from seampact.source.call_value_conditions import FiniteIteration


class GeneratorUnknown(ValueError):
    pass


class _GeneratorReturn(Exception):
    pass


def own_nodes(node):
    for child in ast.iter_child_nodes(node):
        yield child
        if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            yield from own_nodes(child)


def is_generator(node):
    return any(isinstance(child, (ast.Yield, ast.YieldFrom)) for child in own_nodes(node))


@dataclass(eq=False)
class SourceGenerator:
    target: object
    environment: dict
    site: int
    sources: tuple
    watchers: tuple = ()
    defaults_used: tuple = ()
    consumed: bool = False
    steps: int = 0
    yields: int = 0
    failure: object = None
    fresh: dict = field(default_factory=dict)

    def stamp(self, checker):
        values = [*self.sources]
        for mapping, name, original in self.watchers:
            current = mapping.get(name)
            if type(original) is tuple and original[0] == 'identity':
                if current is not original[1]:
                    return None
                continue
            if original is None or checker.iteration_stamp(current) != original:
                return None
        stamps = [checker.iteration_stamp(value) for value in values]
        return repr(stamps) if all(value is not None for value in stamps) else None

    def iterate(self, checker):
        if self.consumed:
            raise GeneratorUnknown('source generator was already consumed or escaped')
        self.consumed = True
        if (not isinstance(self.target.node, ast.FunctionDef) or self.target.node.decorator_list
                or self.target.node.args.vararg or self.target.node.args.kwarg):
            raise GeneratorUnknown('source generator wrapper or variadic binding is outside the finite domain')
        initial = self.stamp(checker)
        if initial is None:
            raise GeneratorUnknown('source generator captures an unknown or polluted value')
        try:
            for value in self.block(checker, self.target.node.body):
                if self.stamp(checker) != initial:
                    raise GeneratorUnknown('source generator input or capture changed across a resume')
                self.yields += 1
                if self.yields > 64:
                    raise GeneratorUnknown('source generator exceeds finite yield count 64')
                yield value
                # Check before resuming, rather than only after the next yield.
                if self.stamp(checker) != initial:
                    raise GeneratorUnknown('source generator input or capture changed while suspended')
        except _GeneratorReturn:
            return

    def tick(self):
        self.steps += 1
        if self.steps > 1024:
            raise GeneratorUnknown('source generator exceeds finite analysis steps 1024')

    def expression(self, checker, node, *, mutate=False):
        self.tick()
        env, path, scope = self.environment, self.target.path, self.target.name
        if isinstance(node, ast.Name):
            value = env.get(node.id)
        elif isinstance(node, ast.Constant):
            value = checker.value(node, env, path, scope)
        elif isinstance(node, ast.Attribute):
            parent = checker.value(node.value, env, path, scope)
            attributes = checker.generator_fields(parent)
            if type(attributes) is not dict or node.attr not in attributes:
                raise GeneratorUnknown('source generator attribute read is not a closed ordinary field')
            value = attributes[node.attr]
            stamp = checker.iteration_stamp(value)
            if stamp is None:
                raise GeneratorUnknown('source generator field is not an exact ordinary value')
            watch = (attributes, node.attr, stamp)
            if not any(mapping is attributes and name == node.attr for mapping, name, _ in self.watchers):
                self.watchers += (watch,)
        elif isinstance(node, ast.List):
            if any(isinstance(part, ast.Starred) for part in node.elts):
                raise GeneratorUnknown('source generator expanded literal is not supported')
            value = [self.expression(checker, part) for part in node.elts]
            self.fresh[id(value)] = value
        elif isinstance(node, ast.Subscript):
            parent = self.expression(checker, node.value)
            if type(parent) is not list:
                raise GeneratorUnknown('source generator subscript receiver is not an exact list')
            if isinstance(node.slice, ast.Slice):
                bounds = [None if part is None else self.expression(checker, part)
                          for part in (node.slice.lower, node.slice.upper, node.slice.step)]
                if any(part is not None and type(part) is not int for part in bounds) or bounds[2] == 0:
                    raise GeneratorUnknown('source generator slice bounds have no proved normal result')
                value = parent[slice(*bounds)]
                self.fresh[id(value)] = value
            else:
                index = self.expression(checker, node.slice)
                if type(index) is not int or not -len(parent) <= index < len(parent):
                    raise GeneratorUnknown('source generator index has no proved normal result')
                value = parent[index]
        elif isinstance(node, ast.Compare) and len(node.ops) == 1:
            left = self.expression(checker, node.left)
            right = self.expression(checker, node.comparators[0])
            operation = node.ops[0]
            if type(left) is str and type(right) is str and isinstance(operation, (ast.Eq, ast.NotEq)):
                if not checker.ordinary_value_operation('str'):
                    raise GeneratorUnknown('source generator original string comparison binding is unknown')
                return left == right if isinstance(operation, ast.Eq) else left != right
            if type(left) is not int or type(right) is not int or not checker.ordinary_value_operation('int'):
                raise GeneratorUnknown('source generator comparison is not exact ordinary integer control')
            if isinstance(operation, ast.Lt): value = left < right
            elif isinstance(operation, ast.LtE): value = left <= right
            elif isinstance(operation, ast.Gt): value = left > right
            elif isinstance(operation, ast.GtE): value = left >= right
            elif isinstance(operation, ast.Eq): value = left == right
            elif isinstance(operation, ast.NotEq): value = left != right
            else: raise GeneratorUnknown('source generator comparison is outside finite control')
        elif isinstance(node, (ast.UnaryOp, ast.BinOp)):
            if isinstance(node, ast.UnaryOp) and not isinstance(node.op, (ast.UAdd, ast.USub)):
                raise GeneratorUnknown('source generator unary operation is outside finite control')
            if isinstance(node, ast.BinOp) and not isinstance(node.op, (ast.Add, ast.Sub)):
                raise GeneratorUnknown('source generator binary operation is outside finite control')
            for part in ast.iter_child_nodes(node):
                if isinstance(part, ast.expr): self.expression(checker, part)
            before = len(checker.negative_scope_gaps)
            value = checker.value(node, env, path, scope)
            if len(checker.negative_scope_gaps) != before:
                raise GeneratorUnknown('source generator primitive operation remains UNKNOWN')
        elif isinstance(node, ast.Call):
            if any(isinstance(part, ast.Starred) for part in node.args) or any(k.arg is None for k in node.keywords):
                raise GeneratorUnknown('source generator expanded call binding is not supported')
            mutation = False
            if isinstance(node.func, ast.Name):
                if node.func.id not in {'len', 'range', 'zip'} or not checker.ordinary_operation(node, env, path, node.func.id):
                    raise GeneratorUnknown('source generator call is outside captured ordinary operations')
            elif isinstance(node.func, ast.Attribute):
                receiver = self.expression(checker, node.func.value)
                mutation = type(receiver) is list and node.func.attr in {'append', 'extend'}
                if mutation:
                    if not mutate or self.fresh.get(id(receiver)) is not receiver:
                        raise GeneratorUnknown('source generator mutation is not confined to a fresh local list')
                elif type(receiver) is not str or node.func.attr not in {'split', 'join', 'replace', 'strip'}:
                    raise GeneratorUnknown('source generator method is outside original string operations')
            else:
                raise GeneratorUnknown('source generator dynamic call is not supported')
            for part in [*node.args, *(keyword.value for keyword in node.keywords)]:
                self.expression(checker, part)
            before = len(checker.negative_scope_gaps)
            value = checker.value(node, env, path, scope)
            if len(checker.negative_scope_gaps) != before:
                raise GeneratorUnknown('source generator operation binding or normal result remains UNKNOWN')
            if mutation:
                return value
            if type(value) is list and isinstance(node.func, ast.Attribute) and node.func.attr == 'split':
                self.fresh[id(value)] = value
        else:
            raise GeneratorUnknown('source generator expression is outside finite propagation: ' + type(node).__name__)
        if not checker.call_plain(value) and type(value) is not FiniteIteration:
            raise GeneratorUnknown('source generator value is not an exact finite ordinary value')
        if type(value) is str and len(value) > 65536:
            raise GeneratorUnknown('source generator string exceeds finite length 65536')
        return value

    def block(self, checker, statements):
        env, path, scope = self.environment, self.target.path, self.target.name
        for statement in statements:
            self.tick()
            if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Yield):
                node = statement.value
                if node.value is None:
                    raise GeneratorUnknown('bare yield has no ordinary transmitted value')
                value = self.expression(checker, node.value)
                if not checker.call_plain(value):
                    raise GeneratorUnknown('source generator yield is not an exact finite ordinary value')
                # Mutable yielded storage must be fresh and unaliased in the
                # suspended frame. Exact immutable leaves cannot mutate it.
                if type(value) is list and (self.fresh.get(id(value)) is not value
                        or any(item is value for item in env.values())
                        or any(type(item) in (list, dict) for item in value)):
                    raise GeneratorUnknown('source generator yield exposes retained mutable storage')
                yield value
            elif isinstance(statement, (ast.Assign, ast.AnnAssign)):
                targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
                if any(not isinstance(target, ast.Name) for target in targets):
                    raise GeneratorUnknown('source generator writes outside local names')
                value = self.expression(checker, statement.value)
                for target in targets: env[target.id] = value
            elif isinstance(statement, ast.AugAssign):
                if not isinstance(statement.target, ast.Name) or not isinstance(statement.op, (ast.Add, ast.Sub)):
                    raise GeneratorUnknown('source generator augmented mutation is outside local integer names')
                if type(env.get(statement.target.id)) is not int or type(self.expression(checker, statement.value)) is not int:
                    raise GeneratorUnknown('source generator augmented operands are not exact integers')
                expression = ast.copy_location(ast.BinOp(left=statement.target, op=statement.op, right=statement.value), statement)
                env[statement.target.id] = self.expression(checker, expression)
            elif isinstance(statement, ast.For):
                if not isinstance(statement.target, ast.Name):
                    raise GeneratorUnknown('source generator loop target is outside local names')
                values = self.expression(checker, statement.iter)
                items = checker.iteration_items(statement.iter, values)
                if items is None:
                    raise GeneratorUnknown('source generator loop has no finite exact iterable')
                stamp = checker.iteration_stamp(values)
                for item in items:
                    checker.bind(statement.target, item, env, path, scope)
                    yield from self.block(checker, statement.body)
                    if checker.iteration_stamp(values) != stamp:
                        raise GeneratorUnknown('source generator loop input was changed')
                yield from self.block(checker, statement.orelse)
            elif isinstance(statement, ast.If):
                value = self.expression(checker, statement.test)
                if not checker.call_plain(value):
                    raise GeneratorUnknown('source generator branch is not exact ordinary control')
                truth = checker.generator_truth(value)
                if truth is None:
                    raise GeneratorUnknown('source generator branch truth is not closed')
                yield from self.block(checker, statement.body if truth else statement.orelse)
            elif isinstance(statement, ast.Return):
                if statement.value is not None and not (isinstance(statement.value, ast.Constant) and statement.value.value is None):
                    raise GeneratorUnknown('source generator return value is outside the finite domain')
                raise _GeneratorReturn
            elif isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call):
                self.expression(checker, statement.value, mutate=True)
            elif isinstance(statement, ast.Pass) or (isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant)
                                                   and type(statement.value.value) is str):
                continue
            else:
                raise GeneratorUnknown('source generator statement is outside finite control: ' + type(statement).__name__)
