"""Finite original class worlds for native subclass enumeration.

Source creation coverage and existing target members are separate premises.
Neither this module nor a before/after snapshot grants a read/use interval;
the normal worker must also retain its original synchronous source lock and
close the complete call effects. Classes are retained only by original source
namespaces, never by an extra platform strong reference in the receipt.
"""
import ast
import builtins
import hashlib
import json
import types
import weakref
from dataclasses import dataclass

from seampact.source.class_factories import finite_class_graph
from seampact.source.frozen_inheritance import FrozenInheritance, InheritanceIssue
from seampact.source.abc_kernel import abc_objects
from seampact.source.import_only import compile_import_only_plan, _metadata


RULE = 'source-complete-finite-class-world/1'
_TYPE, _DICT, _STR, _LIST = type, dict, str, list
_OBJECT = object
_CLASS_DICT = type.__dict__['__dict__']
_MRO, _BASES, _NAME = (type.__dict__[name] for name in ('__mro__', '__bases__', '__name__'))
_SUBCLASSES = type.__dict__['__subclasses__']
_BUILD_CLASS = builtins.__build_class__
_IMPORT = builtins.__import__
_SET, _BUILTINS = set, builtins.__dict__
_REF, _MODULE = weakref.ReferenceType, types.ModuleType
_MODULE_DICT = types.ModuleType.__dict__['__dict__']
_WEAKREF_NAMESPACE = weakref.__dict__
_SET_PROTOCOLS = tuple((name, set.__dict__[name]) for name in ('__new__', '__init__', '__iter__', 'union'))
_OBJECT_PROTOCOLS = tuple((name, object.__dict__[name]) for name in ('__hash__', '__eq__', '__ne__'))
_TYPE_PROTOCOLS = tuple((name, type.__dict__[name]) for name in
    ('__getattribute__', '__call__', '__new__', '__init__', '__subclasses__', '__mro__', '__bases__', '__name__'))
_CLASS_HOOKS = {'__subclasses__', '__init_subclass__', '__set_name__', '__class_getitem__', '__mro_entries__'}
_DYNAMIC = {'type', 'exec', 'eval', '__import__', 'setattr', 'delattr'}


class RegistrationUnknown(ValueError):
    pass


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    allow_nan=False, separators=(',', ':')).encode()).hexdigest()


@dataclass(frozen=True)
class ClassWorld:
    """Source-derived IDs; no actual class object or execution permission."""
    source_version: str
    root: str
    rows: tuple
    _prefix_json: str

    def __post_init__(self):
        object.__setattr__(self, 'rows', tuple(types.MappingProxyType(dict(
            row, bases=tuple(row['bases']), mro=tuple(row['mro']))) for row in self.rows))

    @property
    def prefix(self):
        return json.loads(self._prefix_json)

    def row(self, identity):
        if type(identity) is not str:
            raise RegistrationUnknown('class reference has an unknown identity protocol')
        row = next((row for row in self.rows if row['identity'] == identity), None)
        if row is None:
            raise RegistrationUnknown('class reference is outside the complete original world')
        return row

    def direct_subclasses(self, identity):
        self.row(identity)
        return tuple(row['identity'] for row in self.rows if identity in row['bases'])

    def name(self, identity):
        return self.row(identity)['symbol']

    def descends(self, parent, child):
        if type(parent) is not str or type(child) is not str:
            raise RegistrationUnknown('class descent state has an unknown identity protocol')
        return parent != child and parent in self.row(child)['mro'][1:]

    def strict_descent(self, parents, children):
        """A recursive same-function state must decrease every class argument.

        Non-class parameters and other effects are still checked by the normal
        closure. It must not erase them from its active-call state.
        """
        try:
            return (type(parents) is tuple and type(children) is tuple and bool(parents)
                    and len(parents) == len(children)
                    and all(type(a) is str and type(b) is str and self.descends(a, b)
                            for a, b in zip(parents, children)))
        except RegistrationUnknown:
            return False

    def accepts_prefix(self, prefix):
        # A class/import introduced by the read/use call is not an already
        # created member of this module-only world.
        try:
            prefix = _metadata(prefix)
            return type(prefix) is dict and prefix == self.prefix and prefix.get('status') == 'CHECKED'
        except ValueError:
            return False

    def requirements(self):
        rows = [dict(row, bases=list(row['bases']), mro=list(row['mro'])) for row in self.rows]
        return dict(schema=RULE, source_version=self.source_version, root=self.root,
            classes=rows, activation_prefix=self.prefix, digest=_digest(dict(
                source_version=self.source_version, root=self.root, classes=rows, prefix=self.prefix)),
            target_status='REQUIRES_NORMAL_WORKER_CHECK', execution_permission=False,
            read_use_requirements=['complete original call effects and unchanged creation prefix',
                'original binding lock and native source context lock throughout the synchronous read/use',
                'no unknown callback, background creation/registration, class escape or suspension',
                'fresh original class/metaclass/native-subclass protocol and full ordered graph check'])


def compile_world(inspector, entry, candidate, root_path, root_symbol):
    """Recompute complete module creation coverage, not a supplied class list."""
    plan = compile_import_only_plan(inspector, entry, candidate, dict(requirements=[], bindings={}))
    if plan['status'] != 'CHECKED':
        raise RegistrationUnknown('complete original class import/definition prefix is UNKNOWN')
    resolver = FrozenInheritance(inspector)
    try:
        source = resolver._name(root_path, root_symbol)
        root = source.path + ':' + source.node.name
        records, definitions = [], {}
        paths = plan['modules_order']
        for path in paths:
            tree = resolver.tree(path)
            top = {id(node) for node in tree.body if isinstance(node, ast.ClassDef)}
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    if id(node) not in top:
                        raise RegistrationUnknown('local, conditional or nested class creation is outside the complete module world')
                    key = (path, node.lineno, node.end_lineno)
                    identity = path + ':' + node.name
                    if identity in definitions:
                        raise RegistrationUnknown('repeated original class binding cannot define one stable member')
                    definitions[identity] = node
                    records.append((key, identity, node))
                if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in _DYNAMIC:
                    raise RegistrationUnknown('dynamic class creation or mutation alias is outside the finite world')
                if isinstance(node, ast.Attribute) and node.attr in {'new_class', 'register', '__bases__', '__mro_entries__'}:
                    raise RegistrationUnknown('dynamic registration or inheritance mutation is outside the finite world')
                if isinstance(node, (ast.Global, ast.Nonlocal, ast.Delete)):
                    raise RegistrationUnknown('mutable class-binding lifetime is outside the complete module world')
        by_site = {key: (identity, node) for key, identity, node in records}
        rows, seen = [], set()
        for event in plan['activation_prefix']['events']:
            if event['kind'] != 'CLASS_CREATION':
                continue
            evidence = event.get('evidence')
            if event['status'] != 'SOURCE_CHECKED' or type(evidence) is not list or len(evidence) != 1:
                raise RegistrationUnknown('class creation occurrence is not mechanically closed')
            site = evidence[0]
            found = by_site.get((event['path'], site.get('start'), site.get('end')))
            if found is None or found[0] in seen:
                raise RegistrationUnknown('class creation occurrence is absent, ambiguous or repeated')
            identity, node = found
            graph = finite_class_graph(inspector, event['path'], node.name)
            if graph['source_status'] != 'SOURCE_CHECKED':
                raise RegistrationUnknown('original class definition, C3 or descriptor protocol is UNKNOWN')
            row = next(row for row in graph['classes'] if row['identity'] == identity)
            if any(member.name in _CLASS_HOOKS for member in node.body
                   if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))):
                raise RegistrationUnknown('original class shadows a native creation/subclass protocol')
            rows.append(dict(identity=identity, path=event['path'], symbol=node.name,
                file_sha256=inspector.files[event['path']]['sha256'],
                definition_ast_sha256=hashlib.sha256(ast.dump(node, include_attributes=False).encode()).hexdigest(),
                bases=row['bases'], mro=graph['mro']))
            seen.add(identity)
        if seen != set(definitions) or root not in seen or not 0 < len(rows) <= 256:
            raise RegistrationUnknown('complete original class creation coverage or finite bound is UNKNOWN')
        if any(base not in seen and base not in {'builtins.object', 'abc.ABC'}
               for row in rows for base in row['bases']):
            raise RegistrationUnknown('a class creation base is outside the complete original prefix')
        if any(sum(identity in row['bases'] for row in rows) > 256 for identity in seen):
            raise RegistrationUnknown('complete native subclass edge bound is exceeded')
        return ClassWorld(inspector.request['source_version'], root, tuple(rows), json.dumps(
            plan['activation_prefix'], sort_keys=True, ensure_ascii=False, allow_nan=False))
    except (InheritanceIssue, KeyError, SyntaxError, OSError) as error:
        raise RegistrationUnknown(str(error)) from error


def _namespace(value):
    if _TYPE(value) is not _DICT or any(_TYPE(key) is not _STR for key in value):
        raise RegistrationUnknown('original class namespace has unknown mapping/key protocols')
    return value


def _kernel():
    if _TYPE(builtins) is not _MODULE or _TYPE(weakref) is not _MODULE:
        raise RegistrationUnknown('original class/weak-reference kernel module protocol changed')
    namespace = _namespace(_MODULE_DICT.__get__(builtins, _MODULE))
    weak_namespace = _namespace(_MODULE_DICT.__get__(weakref, _MODULE))
    if (weak_namespace is not _WEAKREF_NAMESPACE or weak_namespace.get('ref') is not _REF or
            weak_namespace.get('ReferenceType') is not _REF):
        raise RegistrationUnknown('original passive weak-reference kernel changed')
    if (namespace is not _BUILTINS or namespace.get('__build_class__') is not _BUILD_CLASS or
            namespace.get('__import__') is not _IMPORT or namespace.get('type') is not _TYPE or
            namespace.get('object') is not _OBJECT or namespace.get('set') is not _SET or
            any(_TYPE.__dict__[name] is not value for name, value in _TYPE_PROTOCOLS) or
            any(_SET.__dict__[name] is not value for name, value in _SET_PROTOCOLS) or
            any(_OBJECT.__dict__[name] is not value for name, value in _OBJECT_PROTOCOLS)):
        raise RegistrationUnknown('original native class/subclass kernel changed')
    abc, meta = abc_objects()
    return abc, meta


def _module_namespaces(modules):
    _namespace(modules)
    namespaces = {}
    for path, module in modules.items():
        if _TYPE(module) is not _MODULE:
            raise RegistrationUnknown('original class module has an unknown object protocol')
        namespaces[path] = _namespace(_MODULE_DICT.__get__(module, _MODULE))
    return namespaces


def _targets(world, namespaces, references=None):
    """Read exact existing classes without owner construction or attr hooks."""
    _namespace(namespaces)
    abc, abc_meta = _kernel()
    if _SET(namespaces) != _SET(world.prefix['modules']):
        raise RegistrationUnknown('the complete original module world is not currently present')
    for namespace in namespaces.values():
        namespace = _namespace(namespace)
        if namespace.get('__builtins__') is not _BUILTINS or namespace.get('set', _SET) is not _SET:
            raise RegistrationUnknown('original native class-set operation binding changed')
    actual = {}
    for row in world.rows:
        namespace = _namespace(namespaces.get(row['path']))
        class_ = namespace.get(row['symbol'])
        if _TYPE(class_) is not _TYPE and _TYPE(class_) is not abc_meta:
            raise RegistrationUnknown('original class has an unknown metaclass or identity')
        if references is not None and references[row['identity']]() is not class_:
            raise RegistrationUnknown('original class member was lost or replaced')
        values = _CLASS_DICT.__get__(class_, _TYPE)
        if any(_TYPE(key) is not _STR for key in values) or any(name in values for name in _CLASS_HOOKS):
            raise RegistrationUnknown('original class has an unknown native subclass protocol')
        name = _NAME.__get__(class_, _TYPE)
        if _TYPE(name) is not _STR or name != row['symbol']:
            raise RegistrationUnknown('original class name changed')
        actual[row['identity']] = class_
    endpoints = {'builtins.object': _OBJECT, 'abc.ABC': abc}
    for row in world.rows:
        class_ = actual[row['identity']]
        expected_bases = [actual.get(name, endpoints.get(name)) for name in row['bases']]
        expected_mro = [actual.get(name, endpoints.get(name)) for name in row['mro']]
        for current, expected in ((_BASES.__get__(class_, _TYPE), expected_bases),
                                  (_MRO.__get__(class_, _TYPE), expected_mro)):
            if len(current) != len(expected) or any(a is not b for a, b in zip(current, expected)):
                raise RegistrationUnknown('original class bases or C3 order changed')
    # Enumerate only the requested root and its descendants. Unrelated classes
    # may share object/ABC; their existence does not make them root members.
    pending, visited = [world.root], set()
    while pending:
        identity = pending.pop(0)
        if identity in visited:
            continue
        visited.add(identity)
        expected_ids = world.direct_subclasses(identity)
        current = _SUBCLASSES(actual[identity])
        if _TYPE(current) is not _LIST or len(current) != len(expected_ids) or any(
                value is not actual[name] for value, name in zip(current, expected_ids)):
            raise RegistrationUnknown('complete original native subclass membership/order changed')
        pending.extend(expected_ids)
    return actual


def capture_world(world, modules):
    """Called only after the normal original controlled loader completed.

    This pure capture returns weak references, not a caller-selectable class
    whitelist or an exclusion/quiet flag. The worker must authenticate the
    loader occurrence and consume the full source/read/use premises itself.
    """
    namespaces = _module_namespaces(modules)
    actual = _targets(world, namespaces)
    return dict(schema=RULE, source_digest=world.requirements()['digest'],
                modules={path: _REF(module) for path, module in modules.items()},
                classes={name: _REF(value) for name, value in actual.items()})


def verify_world(world, receipt, modules):
    """Fresh member/kernel premise only; never claim an interference proof."""
    _kernel()
    _namespace(receipt)
    if (_TYPE(receipt.get('schema')) is not _STR or _TYPE(receipt.get('source_digest')) is not _STR or
            receipt['schema'] != RULE or receipt['source_digest'] != world.requirements()['digest']):
        raise RegistrationUnknown('original class world source requirements changed')
    module_refs = _namespace(receipt.get('modules'))
    _namespace(modules)
    if _SET(modules) != _SET(module_refs) or any(_TYPE(reference) is not _REF or
            reference.__callback__ is not None or modules[path] is not reference()
            for path, reference in module_refs.items()):
        raise RegistrationUnknown('original class module binding was lost or replaced')
    references = _namespace(receipt.get('classes'))
    if _SET(references) != {row['identity'] for row in world.rows} or any(
            _TYPE(value) is not _REF or value.__callback__ is not None
            for value in references.values()):
        raise RegistrationUnknown('original class identity evidence is not an exact passive weak reference')
    _targets(world, _module_namespaces(modules), references)
    return dict(status='TARGET_MEMBERS_CHECKED', schema=RULE, source_digest=receipt['source_digest'],
                execution_permission=False, read_use_region='REQUIRES_NORMAL_LOCKED_CALL_CONSUMER')
