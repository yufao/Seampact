"""Finite source class graphs and literal factories, without activating them.

Source facts, original construction effects and target identity requirements
are separate. A CONDITIONAL graph is never an execution or lifecycle permit.
"""

import ast
import hashlib
import math

from seampact.source.frozen_inheritance import FrozenInheritance, InheritanceIssue
from seampact.source.type_references import TypeReferences, _scoped


GRAPH_RULE = 'source-finite-class-graph/1'
FACTORY_RULE = 'source-literal-class-factory/1'
UNKNOWN = object()
_HOOKS = {'__new__', '__getattribute__', '__getattr__', '__setattr__', '__delattr__',
          '__init_subclass__', '__class_getitem__', '__mro_entries__', '__set_name__'}


def _site(path, node):
    return dict(path=path, start=node.lineno, end=node.end_lineno,
                column=node.col_offset, end_column=node.end_col_offset,
                ast_sha256=hashlib.sha256(ast.dump(node, include_attributes=False).encode()).hexdigest())


def _plain(value, depth=0):
    if depth > 8:
        return False
    if value is None or type(value) in (str, bool, int):
        return True
    if type(value) is float:
        return math.isfinite(value)
    if type(value) in (list, tuple):
        return len(value) <= 128 and all(_plain(item, depth + 1) for item in value)
    if type(value) is dict:
        return len(value) <= 128 and all(type(key) is str and _plain(item, depth + 1) for key, item in value.items())
    return False


def _literal(node):
    try:
        value = ast.literal_eval(node)
        return value if _plain(value) else UNKNOWN
    except (ValueError, TypeError, SyntaxError, RecursionError):
        return UNKNOWN


def _merge(sequences):
    """Python C3 merge over already finite, exact source class references."""
    pending, result = [list(sequence) for sequence in sequences if sequence], []
    while pending:
        head = next((sequence[0] for sequence in pending
                     if all(sequence[0] not in other[1:] for other in pending)), None)
        if head is None:
            raise InheritanceIssue('finite class graph has no consistent C3 method order')
        result.append(head)
        pending = [sequence[1:] if sequence[0] == head else sequence for sequence in pending]
        pending = [sequence for sequence in pending if sequence]
    return result


class _Graph:
    def __init__(self, inspector):
        self.inspector = inspector
        self.resolver = FrozenInheritance(inspector)
        self.references = TypeReferences(inspector, self.resolver.tree)
        self.classes, self.sources, self.orders, self.active = {}, {}, {}, set()
        self.unknowns, self.effects, self.endpoints = [], [], []

    def unknown(self, reason, path, node):
        self.unknowns.append(dict(reason=reason, evidence=_site(path, node)))

    def decorator(self, path, node, owner):
        root = node
        while isinstance(root, ast.Attribute):
            root = root.value
        if isinstance(root, ast.Name):
            for marker in _scoped(owner.body):
                if (isinstance(marker, ast.Name) and marker.id == root.id and isinstance(marker.ctx, (ast.Store, ast.Del))
                        or isinstance(marker, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and marker.name == root.id):
                    return None
        qualified = self.references.resolve(path, node, node)
        if qualified is not None or not isinstance(node, ast.Name) or node.id not in {'staticmethod', 'classmethod'}:
            return qualified
        frames = self.references._frames(path, node)
        if frames is None:
            return None
        for frame in frames:
            for marker in _scoped(frame.body):
                if (isinstance(marker, ast.Name) and marker.id == node.id and isinstance(marker.ctx, (ast.Store, ast.Del))
                        or isinstance(marker, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and marker.name == node.id
                        or isinstance(marker, (ast.Global, ast.Nonlocal)) and node.id in marker.names):
                    return None
                if isinstance(marker, (ast.Import, ast.ImportFrom)) and any(alias.name == '*'
                        or (alias.asname or (alias.name if isinstance(marker, ast.ImportFrom) else alias.name.split('.')[0])) == node.id
                        for alias in marker.names):
                    return None
        # Lexical source fact only. The target original builtin descriptor
        # identity and its definition-time effects remain explicit conditions.
        return 'builtins.' + node.id

    def endpoint(self, name):
        if name == 'builtins.object':
            return name
        if name != 'abc.ABC':
            raise InheritanceIssue('external inheritance protocol is outside the finite ABC endpoint: ' + name)
        if not self.endpoints:
            self.endpoints.append(dict(reference='abc.ABC', metaclass='abc.ABCMeta', status='CONDITIONAL',
                target_requirements=['exact original ABC and ABCMeta identities',
                    'original ABCMeta class creation and native abstract-state initialization protocols',
                    'original abstractmethod decorator identity and effects where used',
                    'no dynamic registration, cache mutation or unknown subclass/class hooks'],
                effects='original class creation and abstract-state initialization retained'))
        return name

    def visit(self, source):
        path, node = source.path, source.node
        identity = path + ':' + node.name
        if identity in self.active:
            raise InheritanceIssue('cyclic finite class graph')
        if identity in self.classes:
            return identity
        if len(self.classes) >= 32 or len(self.active) >= 16:
            raise InheritanceIssue('finite class graph bound exceeded')
        self.active.add(identity)
        row = dict(identity=identity, symbol=node.name, source=_site(path, node),
                   file_sha256=self.inspector.files[path]['sha256'], bases=[], attributes={}, methods=[])
        self.classes[identity], self.sources[identity] = row, source
        try:
            if node.decorator_list or node.keywords:
                self.unknown('class decorator or explicit metaclass protocol is unproved', path, node)
            for base in node.bases:
                if not isinstance(base, (ast.Name, ast.Attribute)):
                    self.unknown('dynamic base expression is outside the finite graph', path, base)
                    continue
                qualified = self.references.resolve(path, base, base)
                if qualified in {'builtins.object', 'abc.ABC'}:
                    row['bases'].append(self.endpoint(qualified))
                else:
                    try:
                        row['bases'].append(self.visit(self.resolver._name(path, ast.unparse(base))))
                    except InheritanceIssue as error:
                        self.unknown(str(error), path, base)
            if not node.bases:
                row['bases'].append('builtins.object')
            if len(set(row['bases'])) != len(row['bases']):
                self.unknown('duplicate finite class bases', path, node)
            for member in node.body:
                if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if member.name in _HOOKS:
                        self.unknown('custom allocation, attribute or class hook is unproved', path, member)
                    decorators = []
                    for decorator in member.decorator_list:
                        qualified = self.decorator(path, decorator, node)
                        if qualified not in {'abc.abstractmethod', 'builtins.staticmethod', 'builtins.classmethod'}:
                            self.unknown('method descriptor or decorator is outside the finite class graph', path, decorator)
                        decorators.append(qualified)
                    row['methods'].append(dict(name=member.name, source=_site(path, member), decorators=decorators))
                    # Preserve Python's final class-namespace binding. A later
                    # method shadows an earlier literal and an inherited value;
                    # factory attribute lookup must not read through it.
                    row['attributes'][member.name] = dict(status='METHOD', source=_site(path, member))
                    args = member.args
                    if member.name == '__init__':
                        self.effects.append(dict(kind='original_constructor', status='UNKNOWN', source=_site(path, member),
                            scope='original body remains separate; graph proof does not certify constructor effects'))
                    definitions = [*args.defaults, *(value for value in args.kw_defaults if value is not None)]
                    annotations = [argument.annotation for argument in [*args.posonlyargs, *args.args, *args.kwonlyargs]
                                   if argument.annotation is not None]
                    if member.returns is not None:
                        annotations.append(member.returns)
                    if definitions or annotations or decorators:
                        self.effects.append(dict(kind='original_method_definition', status='UNKNOWN', source=_site(path, member),
                            scope='original defaults, annotations and supported decorators retained; target prefix remains independent'))
                elif isinstance(member, (ast.Assign, ast.AnnAssign)):
                    targets = member.targets if isinstance(member, ast.Assign) else [member.target]
                    value = _literal(member.value)
                    for target in targets:
                        if not isinstance(target, ast.Name):
                            self.unknown('class attribute write is not a fixed name', path, member)
                        elif target.id in _HOOKS:
                            self.unknown('class assigns an unknown class hook', path, member)
                        else:
                            row['attributes'][target.id] = dict(status='SOURCE_LITERAL' if value is not UNKNOWN else 'UNKNOWN',
                                source=_site(path, member), **({'value': value} if value is not UNKNOWN else {}))
                    if value is UNKNOWN:
                        self.unknown('computed class attribute or descriptor is unproved', path, member)
                        self.effects.append(dict(kind='original_class_attribute_expression', status='UNKNOWN', source=_site(path, member)))
                elif not isinstance(member, ast.Pass) and not (isinstance(member, ast.Expr) and isinstance(member.value, ast.Constant)
                                                               and isinstance(member.value.value, str)):
                    self.unknown('class body statement is outside the finite graph', path, member)
            return identity
        finally:
            self.active.remove(identity)

    def order(self, identity, active=()):
        if identity in self.orders:
            return self.orders[identity]
        if identity in active:
            raise InheritanceIssue('cyclic finite class method order')
        if identity == 'builtins.object':
            order = [identity]
        elif identity == 'abc.ABC':
            order = [identity, 'builtins.object']
        else:
            bases = self.classes[identity]['bases']
            order = [identity, *_merge([*[self.order(base, (*active, identity)) for base in bases], bases])]
        self.orders[identity] = order
        return order


def finite_class_graph(inspector, path, symbol):
    """Describe a finite local class graph, with an explicit conditional ABC end.

    This API accepts original frozen source references, never an eligibility
    flag or an observed SDK object. Target identity and every recorded effect
    must be consumed independently by normal binding/worker checks.
    """
    graph = _Graph(inspector)
    result = dict(schema=GRAPH_RULE, status='UNKNOWN', source_status='UNKNOWN', classes=[], mro=[],
                  external_endpoints=[], target_requirements=[], construction_effects=[], unknowns=[],
                  target_status='UNKNOWN', execution_permission=False, lifecycle='UNKNOWN')
    try:
        identity = graph.visit(graph.resolver._name(path, symbol))
        order = graph.order(identity)
        graph.resolver._check_rewrites(list(graph.sources.values()))
        result.update(root=identity, mro=order)
        if not graph.unknowns:
            result.update(status='CONDITIONAL', source_status='SOURCE_CHECKED')
    except (InheritanceIssue, SyntaxError, ValueError, KeyError, OSError) as error:
        graph.unknowns.append(dict(reason=str(error)))
    result.update(classes=list(graph.classes.values()), external_endpoints=graph.endpoints,
                  construction_effects=graph.effects, unknowns=graph.unknowns,
                  target_requirements=['original unchanged source class identities, direct bases and exact computed MRO',
                    'original builtin class builder and corresponding metaclass identities/protocols',
                    'original builtin staticmethod/classmethod descriptor identities and effects where used',
                    'original class-definition prefix, defaults, annotations and constructor effects checked separately',
                    'no unproved descriptor, class/attribute hook or dynamic inheritance protocol'])
    return result


def literal_class_factory(inspector, path, node, key_attribute, *, filter_attribute=None, selection_key=UNKNOWN):
    """Read one original literal class collection and fixed class attributes.

    Only literal tuple/list members, literal string keys and literal boolean
    filters are supported. Computed availability (including module truth),
    dynamic registries and actual construction remain UNKNOWN.
    """
    result = dict(schema=FACTORY_RULE, status='UNKNOWN', source_members=[], ordered_members=[], mapping=[], selected=None,
                  target_status='UNKNOWN', construction_status='UNKNOWN', execution_permission=False,
                  target_requirements=['same original finite class objects and checked class/metaclass protocols',
                    'same fixed key/filter attributes without descriptor or unknown attribute hooks',
                    'same actual ordinary selection key and original evaluation/filter/construction order'], unknowns=[])
    resolver = FrozenInheritance(inspector)
    references = TypeReferences(inspector, resolver.tree)
    try:
        if not isinstance(node, ast.AST):
            raise InheritanceIssue('factory collection has no original source expression')
        original = next((candidate for candidate in ast.walk(resolver.tree(path))
                         if type(candidate) is type(node) and all(getattr(candidate, field, None) == getattr(node, field, None)
                             for field in ('lineno', 'col_offset', 'end_lineno', 'end_col_offset'))
                         and ast.dump(candidate, include_attributes=False) == ast.dump(node, include_attributes=False)), None)
        if original is None or not isinstance(original, (ast.Tuple, ast.List)):
            raise InheritanceIssue('factory collection is not an original frozen literal tuple/list')
        tree = resolver.tree(path)
        parents = {id(child): parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
        ancestor = parents.get(id(original))
        while ancestor is not None:
            if isinstance(ancestor, (ast.FunctionDef, ast.AsyncFunctionDef)):
                break
            if isinstance(ancestor, ast.ClassDef):
                raise InheritanceIssue('class-body factory bindings are outside this finite literal scope')
            ancestor = parents.get(id(ancestor))
        if (len(original.elts) > 32 or type(key_attribute) is not str or not key_attribute.isidentifier()
                or filter_attribute is not None and (type(filter_attribute) is not str or not filter_attribute.isidentifier())):
            raise InheritanceIssue('literal factory bound or fixed attribute name is unsupported')
        keys, blocked = set(), False
        for member in original.elts:
            if not isinstance(member, (ast.Name, ast.Attribute)) or references.resolve(path, member, member) is None:
                raise InheritanceIssue('factory member binding is not an exact original class reference')
            source = resolver._name(path, ast.unparse(member))
            graph = finite_class_graph(inspector, source.path, source.node.name)
            identity = source.path + ':' + source.node.name
            result['source_members'].append(dict(identity=identity, source=_site(path, member), graph=graph))
            if graph['source_status'] != 'SOURCE_CHECKED':
                result['unknowns'].append(dict(reason='factory member class graph is unproved', identity=identity))
                blocked = True
            classes = {row['identity']: row for row in graph['classes']}
            def attribute(name):
                for owner in graph['mro']:
                    row = classes.get(owner, {}).get('attributes', {}).get(name)
                    if row is not None:
                        return row.get('value', UNKNOWN) if row['status'] == 'SOURCE_LITERAL' else UNKNOWN
                return UNKNOWN
            key = attribute(key_attribute)
            enabled = attribute(filter_attribute) if filter_attribute is not None else True
            result['ordered_members'].append(dict(identity=identity, key=key if type(key) is str else None,
                filter=enabled if type(enabled) is bool else None))
            if type(key) is not str or type(enabled) is not bool:
                result['unknowns'].append(dict(reason='factory key/filter is not a proved literal ordinary attribute', identity=identity))
                blocked = True
                continue
            if enabled:
                if key in keys:
                    raise InheritanceIssue('literal factory contains duplicate selected keys')
                keys.add(key)
                result['mapping'].append(dict(key=key, identity=identity))
        if not blocked:
            result['status'] = 'CONDITIONAL'
            if type(selection_key) is str:
                result['selected'] = next((row['identity'] for row in result['mapping'] if row['key'] == selection_key), None)
                result['selection_status'] = 'CONDITIONAL_SOURCE_SELECTION' if result['selected'] is not None else 'CONDITIONAL_LOOKUP_FAILURE'
            else:
                result['selection_status'] = 'UNKNOWN'
    except (InheritanceIssue, SyntaxError, ValueError, KeyError, OSError) as error:
        result['unknowns'].append(dict(reason=str(error)))
    return result
