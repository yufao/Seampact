"""Finite source call reachability for already discovered dependencies.

This is a candidate consistency check, never an execution or split permission.
Only frozen Python is read. Unknown commands stay unknown; dictionary values
are not all promoted to possible commands and uncalled definitions are skipped.
"""

import ast
import builtins
import copy
import hashlib
import json
import math
import posixpath
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from seampact.source.inspection import units
from seampact.source.execution_context import process_context
from seampact.source.frozen_inheritance import FrozenInheritance, InheritanceIssue
from seampact.source.external_source_protocols import ExternalSourceProtocols, TORCH_NO_GRAD, SPACY_LOAD, SPACY_CALL
from seampact.source.frozen_configuration import (
    ConfigurationUnknown, RULE as CONFIGURATION_RULE, frozen_bytes, plain_yaml_value,
    reader_identity, reader_source_check,
)
from seampact.source.call_value_conditions import (
    context as call_value_context, Shape as _PlainShape, GAP as _CALL_TYPE_GAP,
    plain_truth, literal_default, CallUnknown, builtin_state, finite_range, finite_split,
    finite_integer, finite_string,
    FiniteIteration as _FiniteIteration,
)
from seampact.source.finite_source_generators import SourceGenerator, GeneratorUnknown, is_generator, own_nodes
from seampact.source.finite_registration import compile_world, RegistrationUnknown, RULE as REGISTRATION_RULE
from seampact.source.sdk_value_summaries import SDKIteration, SDKListDomain, summarize_call as summarize_sdk_call
from seampact.source import sdk_bounded_values as bounded
from seampact.source.sdk_bounded_flow import BoundedIteration
from seampact.source.frozen_configuration_sequences import (
    UNHANDLED as CONFIG_UNHANDLED, ConfigurationSequenceUnknown, ConfigurationIteration,
    register_reader_result, configuration_plain, configuration_items, configuration_truth,
    configuration_method, configuration_loop, configuration_comprehension, configuration_collect,
)
from seampact.source.configuration_model_flow import (
    UNHANDLED as MODEL_UNHANDLED, ModelDump,
    model_definition, model_construct, model_attribute, model_call,
)
from seampact.source.sdk_transformer_protocols import (
    UNHANDLED as TRANSFORMER_UNHANDLED, SDKValue, SDKMethod, is_sdk_value,
    summarize_factory as summarize_transformer_factory, sdk_attribute, sdk_call, sdk_subscript,
)
from seampact.source.stdlib_definition_prefix import (
    RULE as STDLIB_PREFIX_RULE, import_fact as stdlib_import_fact, annotation_fact as stdlib_annotation_fact,
)
from seampact.source.sdk_import_summaries import (
    RULE as SDK_IMPORT_RULE, FINDER as SDK_FIND_RULE,
    import_requirement as sdk_import_requirement,
    attribute_requirement as sdk_attribute_requirement,
    finder_requirement as sdk_finder_requirement, finder_normal_exit,
)

_REGISTRATION_SET = set


@dataclass(eq=False)
class _Function:
    path: str
    name: str
    node: object
    closure: object = None
    method_kind: str = ""
    defaults: object = None
    owner: object = None


@dataclass(eq=False)
class _Class:
    path: str
    name: str
    node: object
    attributes: dict
    bases: list
    fields: dict
    dataclass_init: bool = False
    problem: str = ""


@dataclass(frozen=True)
class _Unsupported:
    reason: str
    path: str
    node: object


@dataclass(frozen=True)
class _BoundMethod:
    function: _Function
    receiver: object


@dataclass(frozen=True)
class _Super:
    cls: _Class
    receiver: object


@dataclass(frozen=True)
class _Field:
    default: object = None
    factory: object = None
    init: bool = True
    kw_only: bool = False


@dataclass(frozen=True)
class _Module:
    path: str


@dataclass(frozen=True)
class _External:
    name: str
    local: bool = False


@dataclass(frozen=True)
class _ExternalCallable:
    factory: str
    resource: str


@dataclass(frozen=True)
class _ClassSequence:
    items: tuple
    world: object
    ordered: bool = True


@dataclass(frozen=True)
class _ClassSet:
    items: tuple
    world: object = None


@dataclass(frozen=True)
class _ClassOperation:
    name: str
    receiver: object


@dataclass(frozen=True)
class _UnorderedClassTarget:
    items: tuple


@dataclass
class _Instance:
    """Opaque external result, with only explicit original attribute writes."""
    origin: str
    attributes: dict
    local_class: object = None
    allocation: object = field(default_factory=object, compare=False)
    type_known: bool = True


@dataclass(frozen=True)
class _Path:
    value: object
    frozen_anchor: bool = False
    original: object = None


@dataclass(eq=False)
class _ConfigurationStream:
    path: str
    encoding: object = None
    active: bool = False
    closed: bool = False
    consumed: bool = False
    invalid: bool = False


@dataclass(frozen=True)
class _OptionalPath:
    """An explicit source choice between a Path and literal None."""
    pass


@dataclass(frozen=True)
class _Alternatives:
    values: tuple


@dataclass(frozen=True)
class _LiteralNone:
    """A proven source/configuration None, distinct from an unknown value."""
    pass


LITERAL_NONE = _LiteralNone()


def _configuration_value(value):
    if value is None:
        return LITERAL_NONE
    if isinstance(value, dict):
        if set(value) == {"$dependency"}:
            return None  # A resource reference is not its eventual runtime value.
        return {k: _configuration_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_configuration_value(v) for v in value]
    return value


_BUILTINS = frozenset(dir(builtins)) - {"eval", "exec", "__import__"}


def _cite(path, node, kind="call"):
    return dict(path=path, unit="file", start=node.lineno, end=node.end_lineno, kind=kind)


def _branch_environment(env):
    # Preserve aliases within a branch, without one branch's list/dict mutation
    # becoming evidence in its sibling. Frozen callable ASTs are shared.
    memo = {}
    def clone(value):
        if isinstance(value, _Alternatives):
            return _Alternatives(tuple(clone(item) for item in value.values))
        if isinstance(value, _Function) and value.closure is None:
            return value
        if not isinstance(value, (dict, list, _Instance, _Function, _BoundMethod)):
            return value
        if id(value) in memo:
            return memo[id(value)]
        if isinstance(value, _Function):
            result = _Function(value.path, value.name, value.node, None, value.method_kind, value.defaults, value.owner)
            memo[id(value)] = result
            result.closure = clone(value.closure) if value.closure is not None else None
            return result
        if isinstance(value, _BoundMethod):
            return _BoundMethod(clone(value.function), clone(value.receiver))
        result = _Instance(value.origin, {}, value.local_class, value.allocation, value.type_known) if isinstance(value, _Instance) else {} if isinstance(value, dict) else []
        memo[id(value)] = result
        if isinstance(value, _Instance):
            result.attributes.update((k, clone(v)) for k, v in value.attributes.items())
        elif isinstance(value, dict):
            result.update((k, clone(v)) for k, v in value.items())
        else:
            result.extend(clone(v) for v in value)
        return result
    return clone(env)


def _join_branches(original, states, path, node):
    """Join mutations of the same abstract allocation, retaining its aliases.

    Different construction occurrences never become the same receiver merely
    because they have the same class/name. A changed field becomes unknown;
    independent unchanged fields and the exact source method binding survive.
    """
    memo = {}
    def captures_receiver(value, seen=None):
        seen = set() if seen is None else seen
        if id(value) in seen:
            return False
        seen.add(id(value))
        if isinstance(value, (_Instance, _BoundMethod)):
            return True
        if isinstance(value, _Function):
            return value.closure is not None and captures_receiver(value.closure, seen)
        if isinstance(value, _Alternatives):
            return any(captures_receiver(item, seen) for item in value.values)
        if isinstance(value, (dict, list, tuple)):
            return any(captures_receiver(item, seen) for item in (value.values() if isinstance(value, dict) else value))
        return False
    def join(values, prior=None):
        if all(value is values[0] for value in values):
            return values[0]
        if all(isinstance(value, _Instance) for value in values):
            first = values[0]
            if all(value.allocation is first.allocation and value.local_class is first.local_class for value in values):
                key = tuple(id(value) for value in values)
                if key in memo:
                    return memo[key]
                result = (prior if isinstance(prior, _Instance) and prior.allocation is first.allocation else
                          _Instance(first.origin, {}, first.local_class, first.allocation))
                memo[key] = result
                before = dict(result.attributes)
                result.type_known = all(value.type_known for value in values)
                result.attributes = {name: join([value.attributes.get(name) for value in values], before.get(name))
                    for name in set().union(*(set(value.attributes) for value in values))}
                return result
            return _Unsupported("branch_receiver_binding_unsupported", path, node)
        if any(isinstance(value, _Instance) for value in values):
            return _Unsupported("branch_receiver_binding_unsupported", path, node)
        if any(captures_receiver(value) for value in values):
            # Bound methods and containers/closures retaining receivers require
            # a separate alias-preserving merge. Structural dataclass equality
            # cannot identify their creation occurrences or future mutations.
            return _Unsupported("branch_receiver_alias_unsupported", path, node)
        if all(value == values[0] for value in values):
            return values[0]
        if all(isinstance(value, _Path) for value in values):
            return _Path(None)
        if all(isinstance(value, (_Function, _BoundMethod, _Class, _Unsupported, _Alternatives)) or value is None for value in values):
            return _Alternatives(tuple(item for value in values
                for item in (value.values if isinstance(value, _Alternatives) else (value,))))
        return None
    return {name: join([state.get(name) for state in states], original.get(name))
            for name in set().union(*(set(state) for state in states))}


class _Closure:
    def __init__(self, inspector, candidate, result_observer=None):
        self.inspector, self.candidate = inspector, candidate
        self.result_observer = result_observer
        self.modules, self.loading = {}, set()
        self.unavailable_modules = set()
        self.steps, self.active = 0, set()
        self.calls, self.unknown, self.processes, self.invoked_calls = [], [], [], []
        self.visited = set()
        self.cwd, self.script_contexts = '.', set()
        self.sdk_source = ExternalSourceProtocols(inspector)
        self.protocols = []
        self.call_summaries = {}
        self.summary_hits = 0
        self.external_calls, self.branch_stack = [], []
        self.negative_scope_gaps = []
        self.callable_sites, self.import_sites = [], []
        self.configuration_conditions = []
        self.call_context = call_value_context(inspector)
        self.call_value_taints = {}
        self.inheritance_rejection = None
        self.activation_events, self.activation_gaps = [], []
        self.module_progress = {}
        self.entry_receiver = None
        self.registration_entry = None
        self.registration_worlds, self.registration_frames = {}, []
        self.registration_world_failures = {}
        self.in_bounded_expression = False
        self.in_bounded_flow_attempt = False

    @staticmethod
    def bounded_value(value, seen=None):
        if bounded.is_bounded(value) or type(value) in (SDKListDomain, BoundedIteration):
            return True
        seen = set() if seen is None else seen
        if id(value) in seen:
            return False
        seen.add(id(value))
        if type(value) is list or type(value) is dict:
            return any(_Closure.bounded_value(item, seen) for item in
                       (value.values() if type(value) is dict else value))
        return False

    def call_state(self, target, args, kwargs, public_entry=False):
        """Fingerprint the entire reachable abstract state, including aliasing.

        A summary is reusable only when interpreting the call leaves this state
        unchanged. In particular, callback captures, instance fields, module
        rebinding and subprocess contexts are not treated as immutable inputs.
        """
        seen, retained = {}, []
        def freeze(value):
            if value is None or isinstance(value, (str, int, float, bool)):
                return value
            identity = id(value)
            if identity in seen:
                return ("ref", seen[identity])
            seen[identity] = len(seen)
            retained.append(value)
            if isinstance(value, dict):
                return ("dict", tuple((freeze(k), freeze(v)) for k, v in sorted(value.items(), key=lambda p: (type(p[0]).__name__, str(p[0])))))
            if isinstance(value, (tuple, list)):
                return (type(value).__name__, tuple(freeze(v) for v in value))
            if isinstance(value, (_Function, _Class)):
                fields = (value.closure, value.defaults, value.owner) if isinstance(value, _Function) else (
                    value.attributes, value.bases, value.fields, value.problem, value.dataclass_init)
                return (type(value).__name__, value.path, value.name, id(value.node), freeze(fields))
            if isinstance(value, _Unsupported):
                return ("unsupported", value.reason, value.path, id(value.node))
            if hasattr(value, "__dataclass_fields__"):
                return (type(value).__name__, tuple((k, freeze(getattr(value, k))) for k in value.__dataclass_fields__))
            return (type(value).__name__, identity)
        state = freeze((self.modules, target, args, kwargs, public_entry, self.import_roots,
                        tuple(sorted(self.unavailable_modules)), self.cwd,
                        tuple(sorted(self.script_contexts)), tuple(sorted(self.active)), self.branch_stack))
        return hashlib.sha256(repr(state).encode()).digest()

    def invalidate_mutable(self, value):
        retained, seen = [], set()
        def visit(item):
            if id(item) in seen:
                return
            seen.add(id(item))
            if type(item) is list or type(item) is dict:
                retained.append(item)
                for nested in item.values() if type(item) is dict else item:
                    visit(nested)
            elif type(item) is tuple:
                for nested in item:
                    visit(nested)
        visit(value)
        for item in reversed(retained):
            if self.call_context is not None:
                self.call_value_taints[id(item)] = item
            item.clear()
            if type(item) is dict:
                item[None] = None  # Unknown mapping, not a proved empty **kwargs.
            elif self.call_context is not None:
                item.append(None)  # Preserve UNKNOWN through branch copies.

    def call_plain(self, value, active=None, depth=0):
        """Exact internal ordinary facts; UNKNOWN and polluted aliases fail."""
        if depth > 32 or id(value) in self.call_value_taints:
            return False
        if configuration_plain(self, value):
            return True
        if type(value) is _LiteralNone or any(type(value) is native for native in (str, bool, int)):
            return True
        if type(value) is float:
            return math.isfinite(value)
        if (type(value) is not list and type(value) is not dict) or len(value) > 64:
            return False
        active = set() if active is None else active
        if id(value) in active:
            return False
        active.add(id(value))
        try:
            return (all(self.call_plain(v, active, depth + 1) for v in value) if type(value) is list else
                    all(type(k) is str and self.call_plain(v, active, depth + 1) for k, v in value.items()))
        finally:
            active.remove(id(value))

    def iteration_items(self, node, value):
        configured = configuration_items(self, value)
        if configured is not None:
            return configured
        if type(value) is _ClassSequence and value.ordered:
            return value.items
        if type(value) is list and self.call_plain(value):
            return tuple(value)
        if type(value) is _FiniteIteration and value.site == id(node):
            return value.items
        if type(value) is SDKIteration and value.site == id(node):
            return value.items
        if type(value) is SourceGenerator and value.site == id(node):
            return self.source_generator_items(value)
        return None

    def iteration_stamp(self, value):
        if bounded.is_bounded(value) or type(value) is BoundedIteration:
            return repr(value)
        if type(value) is _ClassSequence or type(value) is _ClassSet:
            return (type(value).__name__, tuple((item.path, item.name) for item in value.items))
        if type(value) is SourceGenerator:
            return value.stamp(self)
        if type(value) is _FiniteIteration or type(value) is SDKIteration:
            if any(self.iteration_stamp(source) is None for source in value.sources):
                return None
            return repr(value)
        return repr(value) if self.call_plain(value) else None

    def registration_world(self, cls, path, node):
        if type(cls) is not _Class or cls.problem or self.registration_entry is None:
            raise RegistrationUnknown('original finite class reference is not proved')
        identity = cls.path + ':' + cls.name
        if identity in self.registration_world_failures:
            raise RegistrationUnknown(self.registration_world_failures[identity])
        for world in self.registration_worlds.values():
            # A target world enumerates its root and descendants. Unrelated
            # source classes in the same prefix are not covered enumeration
            # roots merely because their definitions are also recorded.
            if identity == world.root or world.descends(world.root, identity):
                break
        else:
            try:
                world = compile_world(self.inspector, self.registration_entry, self.candidate, cls.path, cls.name)
            except RegistrationUnknown as error:
                self.registration_world_failures[identity] = str(error)
                raise
            self.registration_worlds[identity] = world
        self.protocol(REGISTRATION_RULE, path, node, target_requirements=world.requirements(),
                      operation='original finite class identity and native protocols',
                      runtime='original class creation/subclass operations retained; target read/use interval remains required')
        return world

    def registration_items(self, world, identities):
        result = []
        for identity in identities:
            row = world.row(identity)
            value = self.module(row['path']).get(row['symbol'])
            if type(value) is not _Class or value.problem or value.path + ':' + value.name != identity:
                raise RegistrationUnknown('original class namespace reference is not stable')
            result.append(value)
        return tuple(result)

    @staticmethod
    def registration_collection(items, world):
        unique, seen = [], _REGISTRATION_SET()
        for item in items:
            if type(item) is not _Class:
                raise RegistrationUnknown('finite native class collection contains an unknown item')
            identity = item.path + ':' + item.name
            if identity not in seen:
                seen.add(identity)
                unique.append(item)
        if len(unique) > 256:
            raise RegistrationUnknown('finite native class collection exceeds 256 members')
        return _ClassSet(tuple(unique), world)

    def registration_state(self, target, local, path, node):
        if target.node.args.vararg or target.node.args.kwarg:
            return None
        classes, passive, world = [], [], None
        for parameter in target.node.args.posonlyargs + target.node.args.args + target.node.args.kwonlyargs:
            value = local.get(parameter.arg)
            if type(value) is _Class:
                current = self.registration_world(value, path, node)
                if world is not None and current.requirements()['digest'] != world.requirements()['digest']:
                    return None
                world = current
                classes.append((parameter.arg, value.path + ':' + value.name))
            elif type(value) is _LiteralNone or any(type(value) is native for native in (str, int, bool, float)):
                passive.append((parameter.arg, type(value).__name__, repr(value)))
            else:
                return None
        return (world, tuple(classes), tuple(passive)) if classes else None

    def leave_active(self, key):
        self.active.remove(key)
        if self.registration_frames and self.registration_frames[-1]['key'] == key:
            self.registration_frames.pop()

    def registration_comprehension(self, node, first, env, path, scope, setup):
        """Only finite class references; unordered inner projection has no effects."""
        if (not isinstance(node, ast.ListComp) or not node.generators or setup
                or any(g.is_async or g.ifs or not isinstance(g.target, ast.Name) for g in node.generators)):
            return None
        if type(first) is not _ClassSequence and type(first) is not _ClassSet:
            return None
        results, world, ordered = [], first.world, True
        def consume(index, values, local):
            nonlocal ordered
            if (type(values) is not _ClassSequence and type(values) is not _ClassSet) or values.world is None:
                raise RegistrationUnknown('nested class comprehension iterable is not finite')
            if values.world.requirements()['digest'] != world.requirements()['digest']:
                raise RegistrationUnknown('nested class comprehension changes its complete world')
            unordered = type(values) is _ClassSet or not values.ordered
            if unordered:
                if (index != len(node.generators) - 1 or not isinstance(node.elt, ast.Name)
                        or node.elt.id != node.generators[index].target.id):
                    raise RegistrationUnknown('unordered class iteration has effects or order-dependent projection')
                ordered = False
            for item in values.items:
                local[node.generators[index].target.id] = item
                if index + 1 < len(node.generators):
                    consume(index + 1, self.value(node.generators[index + 1].iter, local, path, scope), local)
                else:
                    value = self.value(node.elt, local, path, scope)
                    if type(value) is not _Class or len(results) >= 256:
                        raise RegistrationUnknown('class comprehension projection or finite bound is not proved')
                    results.append(value)
        consume(0, first, env.copy())
        self.protocol(REGISTRATION_RULE, path, node, target_requirements=world.requirements(),
                      operation='finite class-reference comprehension', result_order='ORIGINAL' if ordered else 'UNKNOWN_SET_ORDER')
        return _ClassSequence(tuple(results), world, ordered)

    def registration_selection(self, node, values, env, path):
        """A pure unique-name selection has the same result for every set order."""
        if ((type(values) is not _ClassSequence and type(values) is not _ClassSet) or values.world is None
                or not isinstance(node.target, ast.Name) or node.orelse or len(node.body) != 1
                or not isinstance(node.body[0], ast.If)):
            return None
        branch = node.body[0]
        if (branch.orelse or len(branch.body) != 1 or not isinstance(branch.body[0], ast.Return)
                or not isinstance(branch.body[0].value, ast.Name) or branch.body[0].value.id != node.target.id
                or not isinstance(branch.test, ast.Compare) or len(branch.test.ops) != 1
                or not isinstance(branch.test.ops[0], ast.Eq) or len(branch.test.comparators) != 1):
            return None
        left, right = branch.test.left, branch.test.comparators[0]
        def member(part):
            return (isinstance(part, ast.Attribute) and part.attr == '__name__'
                    and isinstance(part.value, ast.Name) and part.value.id == node.target.id)
        query = right if member(left) else left if member(right) else None
        if isinstance(query, ast.Name):
            if query.id == node.target.id:
                return None  # The original loop has already rebound this name.
            query = env.get(query.id)
        elif isinstance(query, ast.Constant):
            query = query.value
        else:
            return None
        if type(query) is not str:
            return None
        names = [values.world.name(value.path + ':' + value.name) for value in values.items]
        if len(_REGISTRATION_SET(names)) != len(names):
            self.negative_gap(path, node, 'duplicate native class names make unordered first-match selection ambiguous')
            return False, None
        match = next((value for name, value in zip(names, values.items) if name == query), None)
        if match is not None:
            env[node.target.id] = match
        elif values.items:
            env[node.target.id] = (values.items[-1] if type(values) is _ClassSequence and values.ordered
                                   else _UnorderedClassTarget(values.items))
        elif env.get(node.target.id) is None:
            env[node.target.id] = _UnorderedClassTarget(())
        self.protocol(REGISTRATION_RULE, path, node, target_requirements=values.world.requirements(),
                      operation='pure unique-name class selection for every original set iteration order',
                      selected_name=query, matched=match is not None, original_loop_retained=True)
        if match is not None and self.result_observer is not None:
            self.result_observer.return_value(branch.body[0], path)
        return match is not None, match

    @staticmethod
    def registration_escapes(value, seen=None):
        if any(type(value) is kind for kind in (_Class, _ClassSet, _ClassSequence, _ClassOperation, _UnorderedClassTarget)):
            return True
        if not any(type(value) is kind for kind in (list, tuple, dict, _Alternatives, _Instance, _Function, _BoundMethod)):
            return False
        seen = set() if seen is None else seen
        if id(value) in seen:
            return False
        seen.add(id(value))
        children = ([*value.keys(), *value.values()] if type(value) is dict else
                    value.values if type(value) is _Alternatives else
                    [value.attributes] if type(value) is _Instance else
                    [value.closure, value.defaults, value.owner] if type(value) is _Function else
                    [value.function, value.receiver] if type(value) is _BoundMethod else value)
        return any(_Closure.registration_escapes(child, seen) for child in children)

    @staticmethod
    def generator_fields(value):
        if (type(value) is _Instance and value.type_known and type(value.local_class) is _Class
                and not value.local_class.problem and not value.local_class.bases
                and not {'__getattribute__', '__getattr__', '__setattr__', '__new__', '__slots__'} & value.local_class.attributes.keys()
                and not value.attributes.keys() & value.local_class.attributes.keys()):
            return value.attributes
        return None

    def source_owner_path(self, owner):
        """A finite original plain-field route, never an owner identity grant."""
        root = self.entry_receiver
        if self.generator_fields(root) is None or self.generator_fields(owner) is None:
            return dict(receiver_relation='UNKNOWN_SOURCE_OWNER')
        pending, seen, paths = [(root,[])], set(), []
        while pending and len(seen) <= 64:
            current,path = pending.pop(0)
            if current is owner:
                paths.append(path)
            if id(current) in seen or len(path) >= 16:
                continue
            seen.add(id(current))
            fields = self.generator_fields(current)
            if fields is None:
                continue
            for name,value in sorted(fields.items()):
                if type(name) is str and self.generator_fields(value) is not None:
                    pending.append((value,[*path,name]))
        if pending or len(seen) > 64 or len(paths) != 1:
            return dict(receiver_relation='UNKNOWN_SOURCE_OWNER')
        cls = owner.local_class
        return dict(receiver_relation='ENTRY_OWNER' if not paths[0] else 'ORIGINAL_SOURCE_OWNER_FIELD_PATH',
            receiver_field_path=paths[0],
            receiver_class=dict(path=cls.path,symbol=cls.name,
                source_sha256=self.inspector.files[cls.path]['sha256'],
                definition_ast_sha256=hashlib.sha256(ast.dump(cls.node,include_attributes=False).encode()).hexdigest()),
            target_status='REQUIRES_ORIGINAL_CREATED_OWNER_AND_PLAIN_FIELD_READ_USE_CHECK',
            constructor_invariant_status='UNKNOWN',
            constructor_required=['ORIGINAL_CONSTRUCTOR_AND_ALL_REACHABLE_WRITERS_POSTCONDITION',
                                  'SDK_NO_SOURCE_OWNER_ESCAPE_AND_FRESH_ALLOCATION_EFFECT_PREMISES'],
            scope='source field-route fact only; actual construction and field state are separate obligations',
            execution_permission=False)

    def generator_truth(self, value):
        return False if type(value) is _LiteralNone else plain_truth(value) if self.call_plain(value) else None

    def source_generator_items(self, value):
        try:
            yield from value.iterate(self)
            self.protocol('source-finite-generator/1', value.target.path, value.target.node,
                yields=value.yields, analysis_steps=value.steps,
                target_requirements=self.generator_target_requirements(value),
                runtime='original lazy generator and resume order retained',
                consumption='one immediate complete consumption; no send/throw/close or lifecycle permission')
        except GeneratorUnknown as error:
            value.failure = str(error)
            self.negative_gap(value.target.path, value.target.node, 'per-call source generator remains UNKNOWN: ' + str(error))

    def generator_target_requirements(self, value):
        target = value.target
        nested = '.<locals>.' in target.name
        scope = 'nested' if nested else 'class' if target.owner is not None else 'module'
        def plain(item):
            if type(item) is _LiteralNone:
                return None
            if type(item) is list:
                return [plain(child) for child in item]
            if type(item) is dict:
                return {name: plain(child) for name, child in item.items()}
            return item
        globals_, captures, fields = [], [], []
        module = self.module(target.path)
        for mapping, name, original in value.watchers:
            current = mapping.get(name)
            if type(original) is tuple:
                captures.append(dict(name=name, relation='ORIGINAL_SOURCE_OWNER', target_status='UNKNOWN'))
            elif mapping is module:
                globals_.append(dict(name=name, value=plain(current)))
            elif mapping is target.closure:
                captures.append(dict(name=name, relation='FRESH_PARENT_SAME_CALL', value=plain(current)))
            else:
                owners = [owner for owner in value.environment.values() if type(owner) is _Instance and owner.attributes is mapping]
                ownership = self.source_owner_path(owners[0]) if len(owners) == 1 else dict(receiver_relation='UNKNOWN_SOURCE_OWNER')
                fields.append(dict(name=name, value=plain(current), **ownership))
        arguments = target.node.args.posonlyargs + target.node.args.args
        receiver = value.environment.get(arguments[0].arg) if target.owner is not None and arguments else None
        return dict(schema='source-generator-target-requirements/1', target_symbol=target.name, binding_scope=scope,
            path=target.path, source_sha256=self.inspector.files[target.path]['sha256'],
            definition_ast_sha256=hashlib.sha256(ast.dump(target.node, include_attributes=False).encode()).hexdigest(),
            definition_start=target.node.lineno, definition_end=target.node.end_lineno,
            parent_symbol=target.name.rsplit('.<locals>.', 1)[0] if nested else None,
            receiver_relation=('ENTRY_OWNER' if receiver is self.entry_receiver else 'UNKNOWN_SOURCE_OWNER') if scope == 'class' else None,
            omitted_defaults=[dict(name=name, value=plain(item)) for name, item in value.defaults_used],
            source_globals=globals_, same_call_captures=captures, owner_fields=fields,
            required=['ORIGINAL_FROZEN_HELPER_REFERENCE', 'ACTUAL_CODE_DEFAULTS_KWDEFAULTS_CLOSURE_AND_GLOBAL_BINDING',
                      'ACTUAL_ORDINARY_INPUT_AND_CAPTURE_PREMISES', 'FRESH_NESTED_DEFINITION_OR_ORIGINAL_RECEIVER_DISPATCH'],
            target_status='REQUIRES_NORMAL_WORKER_CHECK', execution_permission=False)

    def ordinary_operation(self, node, env, path, name):
        return (isinstance(node.func, ast.Name) and node.func.id == name and name not in env
                and builtin_state() and self.sdk_source.type_references.builtins_closed()
                and self.sdk_source.unmodified('builtins.' + name))

    def ordinary_value_operation(self, native):
        return (builtin_state() and self.sdk_source.type_references.builtins_closed()
                and self.sdk_source.unmodified('builtins.' + native))

    @staticmethod
    def invalidate_instance_types(value, seen=None):
        seen = set() if seen is None else seen
        if id(value) in seen:
            return
        seen.add(id(value))
        if isinstance(value, _Instance):
            value.type_known = False
        elif isinstance(value, _Class):
            value.problem = "escaped_class_binding_unsupported"
        elif isinstance(value, (list, tuple, dict)):
            for item in value.values() if isinstance(value, dict) else value:
                _Closure.invalidate_instance_types(item, seen)

    @staticmethod
    def invalidate_configuration_streams(value, seen=None):
        seen = set() if seen is None else seen
        if id(value) in seen:
            return
        seen.add(id(value))
        if isinstance(value, _ConfigurationStream):
            value.invalid = True
        elif isinstance(value, _Instance):
            _Closure.invalidate_configuration_streams(value.attributes, seen)
        elif isinstance(value, _Function) and value.closure is not None:
            _Closure.invalidate_configuration_streams(value.closure, seen)
        elif isinstance(value, _BoundMethod):
            _Closure.invalidate_configuration_streams(value.receiver, seen)
        elif isinstance(value, _Alternatives):
            _Closure.invalidate_configuration_streams(value.values, seen)
        elif isinstance(value, (dict, list, tuple)):
            for item in value.values() if isinstance(value, dict) else value:
                _Closure.invalidate_configuration_streams(item, seen)

    def external_call(self, target, args, kwargs, path, node):
        def literal(value, depth=0):
            if depth > 12:
                return dict(status="UNKNOWN")
            if isinstance(value, _LiteralNone):
                return dict(status="CHECKED", value=None)
            if type(value) in (str, int, float, bool):
                return dict(status="CHECKED", value=value)
            if type(value) in (list, tuple):
                items = [literal(item, depth + 1) for item in value]
                if all(item["status"] == "CHECKED" for item in items):
                    return dict(status="CHECKED", value=[item["value"] for item in items])
            if type(value) is dict and all(type(key) is str for key in value):
                items = {key: literal(item, depth + 1) for key, item in value.items()}
                if all(item["status"] == "CHECKED" for item in items.values()):
                    return dict(status="CHECKED", value={key: item["value"] for key, item in items.items()})
            return dict(status="UNKNOWN")
        row = dict(path=path, line=node.lineno, target=target,
            call_ast_sha256=hashlib.sha256(ast.dump(node, include_attributes=False).encode()).hexdigest(),
            call_expression=ast.unparse(node),
            args=[literal(value) for value in args],
            kwargs={key: literal(value) for key, value in kwargs.items() if key is not None},
            extra_arguments="UNKNOWN" if None in kwargs else "CLOSED",
            path_condition_status="UNKNOWN" if any(b["status"] != "CHECKED" for b in self.branch_stack) else "CHECKED",
            branch_conditions=copy.deepcopy(self.branch_stack), evidence=[_cite(path, node)])
        if self.configuration_conditions:
            row.update(path_condition_status="UNKNOWN",
                       configuration_conditions=copy.deepcopy(self.configuration_conditions))
        if row not in self.external_calls:
            self.external_calls.append(row)

    def negative_gap(self, path, node, reason):
        row = dict(reason=reason, evidence=[_cite(path, node)])
        if row not in self.negative_scope_gaps:
            self.negative_scope_gaps.append(row)

    def activation(self, path, node, kind, reason=None, **facts):
        row = dict(kind=kind, path=path, evidence=[] if isinstance(node, ast.Module) else [_cite(path, node)],
                   status='UNKNOWN' if reason else 'SOURCE_CHECKED', **facts)
        if reason:
            row['reason'] = reason
            if row not in self.activation_gaps:
                self.activation_gaps.append(row)
        self.activation_events.append(row)

    def activation_prefix(self):
        return dict(schema='source-activation-prefix/1', status='UNKNOWN' if self.activation_gaps
                    or any(value == 'UNKNOWN' for value in self.module_progress.values()) else 'CHECKED',
                    events=copy.deepcopy(self.activation_events), gaps=copy.deepcopy(self.activation_gaps),
                    modules=dict(self.module_progress),
                    scope='original source parent/module/class/definition activation only; SDK import and effect premises are separate; no prepare or execution permission')

    def annotation_expression(self, node, env, path, scope):
        if node is None:
            return
        future = any(isinstance(item, ast.ImportFrom) and item.module == '__future__'
                     and any(alias.name == 'annotations' for alias in item.names) for item in self.tree(path).body)
        if future:
            self.activation(path, node, 'ANNOTATION_DEFERRED', expression=ast.unparse(node))
            return
        annotation = stdlib_annotation_fact(self,node,env,path)
        if annotation is not None and annotation['status']=='CHECKED':
            self.protocol(STDLIB_PREFIX_RULE,path,node,target_requirements=annotation)
            self.activation(path,node,'ANNOTATION_EAGER',expression=ast.unparse(node),kernel=annotation)
            return
        # Read and retain the original expression, never evaluate annotations
        # with Python or treat an opaque SDK subscription as a type fact.
        self.value(node, env, path, scope, setup=True)
        plain = (isinstance(node, ast.Constant) or isinstance(node, ast.Name)
                 and (node.id in env or node.id in {'str', 'int', 'float', 'bool', 'list', 'dict', 'tuple', 'object', 'None'}))
        self.activation(path, node, 'ANNOTATION_EAGER',
            None if plain else 'original definition-time annotation protocols/effects are not proved', expression=ast.unparse(node))

    def function_annotations(self, node, env, path, scope):
        for argument in [*node.args.posonlyargs, *node.args.args,
                         *([node.args.vararg] if node.args.vararg else []), *node.args.kwonlyargs,
                         *([node.args.kwarg] if node.args.kwarg else [])]:
            self.annotation_expression(argument.annotation, env, path, scope)
        self.annotation_expression(node.returns, env, path, scope)

    def definition_decorators(self, node, env, path, scope, *, method=False):
        for decorator in node.decorator_list:
            self.value(decorator, env, path, scope, setup=True)
            original = (method and isinstance(decorator, ast.Name) and decorator.id in {'staticmethod', 'classmethod'}
                        and decorator.id not in env and builtin_state()
                        and self.sdk_source.type_references.builtins_closed())
            self.activation(path, decorator, 'DECORATOR_APPLY',
                None if original else 'original definition decorator application/effects are not proved')

    def annotation_target(self, node, env, path, scope, setup):
        target = node.target
        if isinstance(target, ast.Name):
            return None
        if setup:
            self.activation(path, target, 'ANNOTATION_TARGET', 'original non-name annotated target activation/store protocols are not proved')
        # Even without a RHS, Python evaluates a non-name target's receiver
        # and index. It does not read the named attribute or indexed item.
        parent = self.value(target.value, env, path, scope, setup)
        return ((parent, self.value(target.slice, env, path, scope, setup))
                if isinstance(target, ast.Subscript) else (parent,))

    @staticmethod
    def local_binding_target(target):
        return (isinstance(target, ast.Name) or isinstance(target, (ast.Tuple, ast.List))
                and all(_Closure.local_binding_target(child) for child in target.elts))

    @staticmethod
    def source_protocol_value(value, seen=None):
        seen = set() if seen is None else seen
        if id(value) in seen:
            return False
        seen.add(id(value))
        if isinstance(value, (_Function, _BoundMethod, _Class, _ClassSet, _ClassSequence, _ClassOperation, ModelDump)) or is_sdk_value(value):
            return True
        if isinstance(value, _Instance):
            return value.local_class is not None
        if isinstance(value, _Alternatives):
            return any(_Closure.source_protocol_value(item, seen) for item in value.values)
        if isinstance(value, (list, tuple, dict)):
            return any(_Closure.source_protocol_value(item, seen) for item in
                       (value.values() if isinstance(value, dict) else value))
        return False

    def potential_block(self, statements, env, path, scope, node):
        self.branch_stack.append(dict(status="UNKNOWN", path=path, line=node.lineno,
            expression=type(node).__name__, selected="potential execution"))
        try:
            return self.block(statements, env, path, scope)
        finally:
            self.branch_stack.pop()

    @staticmethod
    def immutable_result(value):
        return (value is None or isinstance(value, (str, int, float, bool, _LiteralNone, _Path, _External, _ExternalCallable, _Unsupported, _Module))
                or isinstance(value, _Function) and value.closure is None)

    def protocol(self, rule, path, node, **facts):
        row = dict(rule=rule, status="SOURCE_PROTOCOL_CHECKED", evidence=[_cite(path, node)],
                   scope="dependency reachability only; native execution retained; no lifecycle or extraction permission", **facts)
        if rule == REGISTRATION_RULE:
            row['call_site'] = self.invoked_site(node)
        if row not in self.protocols:
            self.protocols.append(row)

    @staticmethod
    def invoked_site(node):
        return dict(start=node.lineno, end=node.end_lineno,
                    col=getattr(node, 'col_offset', None), endcol=getattr(node, 'end_col_offset', None),
                    ast_sha256=hashlib.sha256(ast.dump(node, include_attributes=False).encode()).hexdigest(),
                    kind=type(node).__name__)

    def no_grad_wrapper(self, target):
        decorators = target.node.decorator_list
        if len(decorators) != 1 or not isinstance(decorators[0], ast.Call):
            return False
        decorator = decorators[0]
        if decorator.args or decorator.keywords:
            return False
        module = self.module(target.path)
        function = self.value(decorator.func, module, target.path, target.name, setup=True)
        if not isinstance(function, _External) or function.local or function.name != "torch.no_grad":
            return False
        root = decorator.func
        while isinstance(root, ast.Attribute):
            root = root.value
        if target.owner is not None and isinstance(root, ast.Name) and root.id in target.owner.attributes:
            return False
        if not self.sdk_source.unmodified("torch.no_grad"):
            return False
        self.protocol("external-zero-argument-call-wrapper/1", target.path, decorator,
                      target="torch.no_grad", definition=target.name, reference=TORCH_NO_GRAD)
        return True

    def spacy_factory(self, node, env, path, scope):
        if not isinstance(node, ast.Call) or len(node.args) != 1 or node.keywords:
            return None
        if not isinstance(node.args[0], ast.Constant) or not isinstance(node.args[0].value, str):
            return None
        factory = self.value(node.func, env, path, scope, setup=True)
        if (not isinstance(factory, _External) or factory.local or factory.name != "spacy.load"
                or not self.sdk_source.unmodified("spacy.load")):
            return None
        self.protocol("external-callable-factory/1", path, node, target="spacy.load", reference=SPACY_LOAD,
                      result_protocol="spacy.language.Language.__call__", resource=dict(
                          kind="spacy_pipeline", name=node.args[0].value, status="ENVIRONMENT_NOT_CHECKED"))
        return _ExternalCallable("spacy.load", node.args[0].value)

    def tree(self, path):
        # One read-only source tree per replay also preserves node identity for
        # lexical checks in TypeReferences. No loaded target state is cached.
        return self.sdk_source.tree(path)

    def unknown_target(self, path, node, target, reason):
        code = ("dependency_closure_limit" if reason.startswith("finite_") else
                "dependency_closure_unresolved" if reason in {"frozen_source_unavailable", "local_call_source_unavailable",
                                                               "subprocess_source_unavailable", "local_base_source_unavailable"} else
                "dependency_closure_unsupported")
        row = dict(target=target, reason=reason, issue_code=code,
                   scope="source" if code == "dependency_closure_unresolved" else "rule", evidence=[_cite(path, node)])
        if row not in self.unknown:
            self.unknown.append(row)

    def roots(self, path):
        # Frozen, explicit script-root additions. Values are evaluated only from
        # pathlib/__file__ and preceding immutable module assignments.
        parent = PurePosixPath(path).parent
        roots = [parent.as_posix()]
        package = parent
        while package.parts and (package / "__init__.py").as_posix() in self.inspector.files:
            package = package.parent
        if package != parent:
            roots.append(package.as_posix())
        return list(dict.fromkeys(roots + [".", "src"]))

    def module_path(self, module, path, level=0):
        if level:
            base = PurePosixPath(path).parent
            for _ in range(level - 1):
                base = base.parent
            names = [str(base / module.replace(".", "/"))]
        else:
            names = [posixpath.normpath(posixpath.join(root, module.replace(".", "/")))
                     for root in self.import_roots.get(path, self.roots(path))]
        choices = [name + suffix for name in names for suffix in (".py", "/__init__.py")]
        found = list(dict.fromkeys(p for p in choices if p in self.inspector.files))
        # Earlier explicit sys.path roots have original Python precedence.
        return found[0] if found else None

    def local_module(self, module, path, level=0):
        if level:
            return True
        prefix = module.split(".")[0]
        return any(p.startswith(posixpath.normpath(posixpath.join(root, prefix)) + "/") or
                   p == posixpath.normpath(posixpath.join(root, prefix)) + ".py"
                   for root in self.import_roots.get(path, self.roots(path)) for p in self.inspector.files)

    def conditional_resource_try(self, node, env, path):
        """Keep an original lookup handler conditional on its worker premise.

        The SDK leaf supplies only a normal-exit obligation, never a synthetic
        lookup result. An original LookupError handler is unreachable only
        when the same normal plan independently checks that obligation before
        the original import. Any other exception still propagates unchanged.
        """
        if (self.call_context is None or type(node) is not ast.Try
                or len(node.body) != 1 or len(node.handlers) != 1
                or node.orelse or node.finalbody or type(node.body[0]) is not ast.Expr
                or type(node.body[0].value) is not ast.Call):
            return False
        handler, call = node.handlers[0], node.body[0].value
        if (handler.name is not None or type(handler.type) is not ast.Name
                or handler.type.id != 'LookupError' or 'LookupError' in env
                or not builtin_state() or not self.sdk_source.type_references.builtins_closed()
                or not self.sdk_source.unmodified('builtins.LookupError')
                or any(type(argument) is ast.Starred for argument in call.args)
                or any(keyword.arg is None for keyword in call.keywords)):
            return False
        # Read only source bindings and exact arguments. None of these calls
        # invokes the source finder, handler, loader or SDK.
        target = self.value(call.func, env, path, '<module>', setup=True)
        if type(target) is not _External or target.local:
            return False
        args = [self.value(argument, env, path, '<module>', setup=True) for argument in call.args]
        kwargs = {keyword.arg: self.value(keyword.value, env, path, '<module>', setup=True)
                  for keyword in call.keywords}
        requirement = sdk_finder_requirement(self,path,call,target.name,args,kwargs)
        if requirement is None:
            return False
        condition = finder_normal_exit(requirement)
        if condition is None:
            return False
        self.external_call(target.name,args,kwargs,path,call)
        self.activation(path,call,'EXTERNAL_SETUP_CALL',target=target.name,
                        kernel=requirement,conditional_normal_exit=condition)
        self.activation(path,node,'MODULE_CONTROL',kernel=requirement,
                        conditional_normal_exit=condition,
                        original_handler='retained; excluded only by the independent normal worker lookup premise')
        return True

    def module(self, path):
        if path in self.modules:
            return self.modules[path]
        parent = PurePosixPath(path).parent
        package_prefix = []
        while parent.parts and parent.name.isidentifier():
            initializer = (parent / '__init__.py').as_posix()
            if initializer != path and initializer in self.inspector.files:
                package_prefix.append(initializer)
            parent = parent.parent
        for initializer in reversed(package_prefix):
            self.module(initializer)
        # A parent can import this child while it is still initializing. Its
        # partially initialized namespace is reused, never executed twice.
        if path in self.modules:
            return self.modules[path]
        env = {"__file__": _Path(path, True, path), "__name__": "__imported__"}
        self.modules[path] = env
        self.module_progress[path] = 'INITIALIZING'
        self.import_roots.setdefault(path, self.roots(path))
        try:
            tree = self.tree(path)
        except SyntaxError as error:
            location = ast.Pass(lineno=error.lineno or 1, end_lineno=error.end_lineno or error.lineno or 1)
            self.unknown_target(path, location, path, "source_syntax_rule_unsupported")
            self.unavailable_modules.add(path)
            self.module_progress[path] = 'UNKNOWN'
            return env
        except (ValueError, KeyError, OSError):
            self.unavailable_modules.add(path)
            self.module_progress[path] = 'UNKNOWN'
            self.unknown.append(dict(target=path, reason="frozen_source_unavailable",
                                     issue_code="dependency_closure_unresolved", scope="source", evidence=[]))
            return env
        self.activation(path, tree, 'MODULE_BEGIN')
        # Module setup imports/definitions/constants, never unused bodies.
        for node in tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
                                 ast.Assign, ast.AnnAssign)):
                self.statement(node, env, path, "<module>", setup=True)
            elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                call = node.value
                if ast.unparse(call.func).endswith(".path.insert") and len(call.args) == 2:
                    value = self.value(call.args[1], env, path, "<module>")
                    if isinstance(value, (str, _Path)):
                        root = value.value if isinstance(value, _Path) else value
                        self.import_roots[path].insert(0, root)
                else:
                    self.value(call, env, path, "<module>", setup=True)
            elif isinstance(node, ast.Expr) and not isinstance(node.value, ast.Constant):
                self.activation(path, node, 'MODULE_EXPRESSION', 'original module expression effects are outside the covered activation forms')
                self.value(node.value, env, path, '<module>', setup=True)
            elif isinstance(node, (ast.If, ast.Try, ast.For, ast.While, ast.With)):
                if self.conditional_resource_try(node,env,path):
                    continue
                # Module branch/configuration evaluation is outside this finite
                # rule. Invalidate affected values rather than use stale constants.
                skipped = isinstance(node, ast.If) and self.truth(node.test, env, path, "<module>") is False and not node.orelse
                if not skipped:
                    self.activation(path, node, 'MODULE_CONTROL', 'original module control/exception activation effects are not proved')
                    parents = {id(child): parent for parent in ast.walk(node) for child in ast.iter_child_nodes(parent)}
                    for child in own_nodes(node):
                        if (isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                                or isinstance(child, ast.Name) and self.source_protocol_value(env.get(child.id))):
                            self.negative_gap(path, node, "untraced module control flow can invoke original source code")
                        if isinstance(child, ast.Call):
                            parent, nested_call = parents.get(id(child)), False
                            while parent is not None and parent is not node:
                                if isinstance(parent, ast.Call):
                                    nested_call = True
                                    break
                                parent = parents.get(id(parent))
                            if not nested_call:
                                self.branch_stack.append(dict(status='UNKNOWN', path=path, line=node.lineno,
                                    expression=type(node).__name__, selected='original module activation alternatives'))
                                try:
                                    self.value(child, _branch_environment(env), path, '<module>', setup=True)
                                finally:
                                    self.branch_stack.pop()
                            target = self.value(child.func, _branch_environment(env), path, "<module>", setup=True)
                            builtin = isinstance(child.func, ast.Name) and child.func.id in _BUILTINS and child.func.id not in env
                            if (isinstance(target, (_Function, _BoundMethod, _Class, _Unsupported))
                                    or isinstance(target, _External) and target.local or target is None and not builtin):
                                self.negative_gap(path, node, "untraced module control flow has an unresolved source call")
                for child in ast.walk(node):
                    if isinstance(child, ast.Name) and isinstance(child.ctx, (ast.Store, ast.Del)):
                        env[child.id] = _Unsupported("module_binding_control_flow_unsupported", path, child)
                    if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute):
                        receiver = child.func.value
                        if isinstance(receiver, ast.Name) and isinstance(env.get(receiver.id), (dict, list)):
                            env[receiver.id] = None
            elif not isinstance(node, (ast.Pass, ast.Expr)):
                self.negative_gap(path, node, "module statement is outside negative call coverage: " + type(node).__name__)
                self.activation(path, node, 'MODULE_STATEMENT', 'original activation statement is outside the finite prefix')
        self.module_progress[path] = 'SOURCE_VISITED'
        self.activation(path, tree, 'MODULE_END')
        location = PurePosixPath(path)
        parent_initializer = ((location.parent.parent if location.name == '__init__.py' else location.parent) / '__init__.py').as_posix()
        if parent_initializer != path and parent_initializer in self.modules:
            name = location.parent.name if location.name == '__init__.py' else location.stem
            self.modules[parent_initializer][name] = _Module(path)
        return env

    def class_attribute(self, cls, name):
        if cls.problem:
            return _Unsupported(cls.problem, cls.path, cls.node)
        if name in cls.attributes:
            return cls.attributes[name]
        if cls.bases:
            base = cls.bases[0]
            if isinstance(base, _Class):
                return self.class_attribute(base, name)
            if isinstance(base, _External) and not base.local:
                # Opaque third-party base: do not read or enumerate its methods.
                return _External(base.name + "." + name)
            return _Unsupported("dynamic_inheritance_unsupported", cls.path, cls.node)
        return None

    def class_definition(self, node, env, path, scope):
        cls = _Class(path, node.name, node, {}, [], {})
        # Resolve only explicit bases; never inspect unrelated external classes.
        cls.bases = [_External("builtins.object") if isinstance(base, ast.Name) and base.id == "object" and "object" not in env else
                     self.value(base, env, path, scope, setup=True) for base in node.bases]
        if len(cls.bases) > 1 or node.keywords or any(not isinstance(b, (_Class, _External)) for b in cls.bases):
            cls.problem = "dynamic_inheritance_unsupported"
        if any(isinstance(b, _External) and b.local for b in cls.bases):
            cls.problem = "local_base_source_unavailable"
        if any(isinstance(b, _Class) and b.problem for b in cls.bases):
            cls.problem = "inherited_class_protocol_unsupported"
        dataclass_options = None
        for decorator in node.decorator_list:
            target = decorator.func if isinstance(decorator, ast.Call) else decorator
            resolved = self.value(target, env, path, scope, setup=True)
            if isinstance(resolved, _External) and resolved.name == "dataclasses.dataclass":
                options = {k.arg: k.value.value for k in decorator.keywords if isinstance(k.value, ast.Constant)} if isinstance(decorator, ast.Call) else {}
                if isinstance(decorator, ast.Call) and (decorator.args or len(options) != len(decorator.keywords)):
                    cls.problem = "dataclass_options_unsupported"
                elif any(k not in {"init", "repr", "eq", "order", "unsafe_hash", "frozen", "match_args", "kw_only", "slots", "weakref_slot"} or not isinstance(v, bool) for k, v in options.items()):
                    cls.problem = "dataclass_options_unsupported"
                dataclass_options = options
            else:
                cls.problem = "decorated_class_target_unsupported"
        if dataclass_options is not None and cls.bases and isinstance(cls.bases[0], _Class):
            cls.fields.update(cls.bases[0].fields)
        class_env = env.copy()
        for child in node.body:
            if self.result_observer is not None:
                self.result_observer.definition(child, path, class_env)
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.definition_decorators(child, class_env, path, scope, method=True)
                kind = "unsupported" if child.decorator_list else "instance"
                if len(child.decorator_list) == 1:
                    decorator = child.decorator_list[0]
                    if isinstance(decorator, ast.Name) and decorator.id in {"staticmethod", "classmethod"} and decorator.id not in env:
                        kind = decorator.id
                function = _Function(path, node.name + "." + child.name, child,
                                     env if scope != "<module>" else None, kind)
                function.owner = cls
                function.defaults = self.function_defaults(child, class_env, path, scope)
                self.function_annotations(child, class_env, path, scope)
                cls.attributes[child.name] = class_env[child.name] = function
                if child.name in {"__new__", "__getattr__", "__getattribute__", "__setattr__"}:
                    cls.problem = "custom_class_protocol_unsupported"
            elif isinstance(child, (ast.Assign, ast.AnnAssign)):
                value = None
                field = None
                if isinstance(child, ast.AnnAssign) and dataclass_options is not None:
                    field = _Field(kw_only=dataclass_options.get("kw_only", False))
                    call = child.value
                    callee = self.value(call.func, class_env, path, scope, setup=True) if isinstance(call, ast.Call) else None
                    if isinstance(callee, _External) and callee.name == "dataclasses.field":
                        options = {k.arg: self.value(k.value, class_env, path, scope, setup=True) for k in call.keywords}
                        if call.args or any(k not in {"default", "default_factory", "init", "kw_only", "repr", "compare", "hash", "metadata"} for k in options):
                            cls.problem = "dataclass_field_unsupported"
                        field = _Field(options.get("default"), options.get("default_factory"), options.get("init", True), options.get("kw_only", field.kw_only))
                        value = field.default
                    else:
                        value = self.value(child.value, class_env, path, scope, setup=True)
                        field = _Field(value, kw_only=field.kw_only)
                    if isinstance(child.target, ast.Name):
                        # ClassVar/InitVar need different generated constructor semantics.
                        annotation = ast.unparse(child.annotation)
                        if "ClassVar" in annotation or "InitVar" in annotation:
                            cls.problem = "dataclass_annotation_unsupported"
                        cls.fields[child.target.id] = field
                else:
                    value = self.value(child.value, class_env, path, scope, setup=True)
                evaluated = self.annotation_target(child, class_env, path, scope, True) if isinstance(child, ast.AnnAssign) else None
                if isinstance(child, ast.Assign) and any(not self.local_binding_target(target) for target in child.targets):
                    self.activation(path, child, 'SETUP_TARGET_STORE', 'original non-name class activation assignment/store effects are not proved')
                if not isinstance(child, ast.AnnAssign) or child.value is not None:
                    for target in child.targets if isinstance(child, ast.Assign) else [child.target]:
                        self.bind(target, value, class_env, path, scope, evaluated=evaluated)
                        if isinstance(target, ast.Name):
                            cls.attributes[target.id] = value
                if isinstance(child, ast.AnnAssign):
                    self.annotation_expression(child.annotation, class_env, path, scope)
            elif isinstance(child, (ast.Import, ast.ImportFrom)):
                self.statement(child, class_env, path, '<class>', setup=True)
                cls.problem = 'class_body_rule_unsupported'
            elif not isinstance(child, ast.Pass) and not (isinstance(child, ast.Expr) and isinstance(child.value, ast.Constant)):
                cls.problem = "class_body_rule_unsupported"
                self.activation(path, child, 'CLASS_STATEMENT', 'original class activation statement is outside the finite prefix')
                if isinstance(child, ast.Expr):
                    self.value(child.value, class_env, path, scope, setup=True)
        cls.dataclass_init = dataclass_options is not None and dataclass_options.get("init", True) and "__init__" not in cls.attributes
        bases, pending = [], list(cls.bases)
        while pending and len(bases) <= 16:
            base = pending.pop()
            bases.append(base)
            if type(base) is _Class:
                pending.extend(base.bases)
        unknown_creation = (bool(cls.problem) or bool(node.keywords) or bool(node.decorator_list)
                            or len(bases) > 16 or any(type(base) is _Class and
                                {'__init_subclass__', '__set_name__'} & base.attributes.keys() for base in bases))
        if any(type(base) is _External for base in bases):
            from seampact.source.abc_kernel import check_abc_source
            from seampact.source.frozen_inheritance import ClassSource
            chain = [ClassSource(cls.path, cls.node)] + [ClassSource(base.path, base.node) for base in bases if type(base) is _Class]
            unknown_creation |= (any(type(base) is not _Class and
                not (type(base) is _External and base.name == 'abc.ABC' and not base.local) for base in bases)
                or check_abc_source(self.inspector, chain)['status'] != 'CHECKED')
        self.activation(path, node, 'CLASS_CREATION',
            'original class creation/metaclass/descriptor effects are not proved' if unknown_creation else None)
        model_definition(self, cls, path, node)
        return cls

    def construct(self, cls, args, kwargs, path, scope, node):
        configured = model_construct(self, cls, args, kwargs, path, scope, node)
        if configured is not MODEL_UNHANDLED:
            return configured
        if cls.problem:
            self.unknown_target(path, node, cls.name, cls.problem)
            self.unknown_target(cls.path, cls.node, cls.name, cls.problem)
            return _Unsupported(cls.problem, cls.path, cls.node)
        edge = dict(source_path=path, source_symbol=scope, target_path=cls.path,
                    target_symbol=cls.name, evidence=[_cite(path, node)])
        if edge not in self.calls:
            self.calls.append(edge)
        self.visited.add((cls.path, cls.name))
        instance = _Instance(cls.name + "()", {}, cls)
        if cls.dataclass_init:
            position = 0
            for name, field in cls.fields.items():
                value = field.default
                if field.init and name in kwargs:
                    value = kwargs[name]
                elif field.init and not field.kw_only and position < len(args):
                    value = args[position]
                    position += 1
                elif field.factory is not None:
                    value = self.call_value(field.factory, [], {}, path, scope, node)
                instance.attributes[name] = value
            post = self.class_attribute(cls, "__post_init__")
            if isinstance(post, _Function):
                self.invoke(post, [instance], {}, path, scope, node)
        else:
            init = self.class_attribute(cls, "__init__")
            if isinstance(init, _Function):
                self.invoke(init, [instance] + args, kwargs, path, scope, node)
            elif isinstance(init, _Unsupported):
                self.unknown_target(path, node, cls.name, init.reason)
        return instance

    def call_value(self, function, args, kwargs, path, scope, node, capture_result=True):
        sdk_result = sdk_call(self,function,args,kwargs,path,scope,node)
        if sdk_result is not TRANSFORMER_UNHANDLED:
            return sdk_result
        configured = model_call(self, function, args, kwargs, path, scope, node)
        if configured is not MODEL_UNHANDLED:
            return configured
        if isinstance(function, _Alternatives):
            results = [self.call_value(value, args, kwargs, path, scope, node, capture_result) for value in function.values]
            return results[0] if all(value == results[0] for value in results) else None
        if isinstance(function, (_BoundMethod, _Function)):
            sink = {}
            target = function.function if isinstance(function, _BoundMethod) else function
            actual_args = [function.receiver] + args if isinstance(function, _BoundMethod) else args
            result = self.invoke(target, actual_args, kwargs, path, scope, node, result_sink=sink)
            if self.result_observer is not None and capture_result:
                self.result_observer.call_result(node, sink.get("proof"))
            return result
        if isinstance(function, _Class):
            return self.construct(function, args, kwargs, path, scope, node)
        if isinstance(function, _Instance) and function.local_class is not None:
            method = self.class_attribute(function.local_class, "__call__")
            if isinstance(method, _Function):
                return self.invoke(method, [function] + args, kwargs, path, scope, node)
            self.unknown_target(path, node, function.origin, "instance_call_protocol_unsupported")
        if isinstance(function, _Unsupported):
            self.unknown_target(path, node, ast.unparse(node.func) if isinstance(node, ast.Call) else scope, function.reason)
            self.unknown_target(function.path, function.node, scope, function.reason)
        elif isinstance(function, _External) and function.local:
            self.unknown_target(path, node, function.name, "local_call_source_unavailable")
        elif function is None:
            self.unknown_target(path, node, scope, "dynamic_call_target_unresolved")
        return None

    def function_defaults(self, node, env, path, scope):
        parameters = node.args.posonlyargs + node.args.args
        defaults = dict(zip([p.arg for p in parameters[-len(node.args.defaults):]], node.args.defaults)) if node.args.defaults else {}
        defaults.update((p.arg, value) for p, value in zip(node.args.kwonlyargs, node.args.kw_defaults) if value is not None)
        return {name: self.value(value, env, path, scope, setup=True) for name, value in defaults.items()}

    def bind(self, target, value, env, path=None, scope='', *, evaluated=None):
        if isinstance(target, ast.Name):
            env[target.id] = value
        elif isinstance(target, (ast.Tuple, ast.List)):
            if isinstance(value, _ConfigurationStream):
                value.invalid = True  # Original unpacking consumes an iterator.
            for index, item in enumerate(target.elts):
                self.bind(item, value[index] if isinstance(value, (tuple, list)) and index < len(value) else None, env, path, scope)
        elif (isinstance(target, ast.Subscript) and path is not None
                and self.call_context is not None and not self.call_context.compiling):
            mapping, key = evaluated if evaluated is not None else (
                self.value(target.value, env, path, scope), self.value(target.slice, env, path, scope))
            if type(mapping) is dict and None not in mapping and any(type(key) is native for native in (str, int, bool)):
                mapping[key] = value
                return
            self.negative_gap(path, target, 'per-call subscript assignment is outside finite value propagation')
            if type(mapping) is list or type(mapping) is dict:
                self.invalidate_mutable(mapping)
            if isinstance(target.value, ast.Name):
                env[target.value.id] = None
        elif isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name):
            mapping = env.get(target.value.id)
            if isinstance(mapping, _ConfigurationStream):
                mapping.invalid = True
            key = target.slice.value if isinstance(target.slice, ast.Constant) else None
            if isinstance(mapping, dict) and key is not None:
                mapping[key] = value
            else:
                env[target.value.id] = None
        elif isinstance(target, ast.Attribute):
            def receiver(node):
                if isinstance(node, ast.Name):
                    return env.get(node.id)
                if isinstance(node, ast.Attribute):
                    parent = receiver(node.value)
                    if isinstance(parent, (_Instance, _Class)):
                        member = parent.attributes.get(node.attr)
                        if member is None and type(parent) is _Instance and parent.local_class is not None:
                            member = self.class_attribute(parent.local_class, node.attr)
                            if type(member) is _Function:
                                return _BoundMethod(member, parent)
                        return member
                    if type(parent) is _BoundMethod and node.attr == '__func__':
                        return parent.function
                    if isinstance(parent, _ConfigurationStream):
                        parent.invalid = True
                if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
                    parent = receiver(node.value)
                    try:
                        return parent[node.slice.value] if type(parent) in (dict, list, tuple) else None
                    except (KeyError, IndexError, TypeError):
                        return None
                return None
            parent = evaluated[0] if evaluated is not None else receiver(target.value)
            if type(parent) is _Function or type(parent) is _BoundMethod:
                self.negative_gap(path, target, 'source callable metadata or attributes are modified without an original binding proof')
                return
            if isinstance(parent, _ConfigurationStream):
                parent.invalid = True
                return
            if isinstance(parent, (_Instance, _Class)):
                parent.attributes[target.attr] = value
                if isinstance(parent, _Instance) and target.attr == "__class__":
                    parent.type_known = False
                if isinstance(parent, _Class) and target.attr in {
                        "__bases__", "__class__", "__new__", "__getattribute__", "__getattr__", "__setattr__"}:
                    parent.problem = "custom_class_protocol_unsupported"
                return
            root = target.value
            while isinstance(root, ast.Attribute):
                root = root.value
            if isinstance(root, ast.Name) and isinstance(env.get(root.id), _External):
                env[root.id] = _External("<modified:" + env[root.id].name + ">", True)

    def attribute_value(self, node, parent, path, scope, calling=False):
        sdk_result = sdk_attribute(self,parent,node.attr,path,node,calling=calling)
        if sdk_result is not TRANSFORMER_UNHANDLED:
            return sdk_result
        configured = model_attribute(self, parent, node.attr, path, node, calling=calling)
        if configured is not MODEL_UNHANDLED:
            return configured
        if type(parent) is _ClassSet:
            if calling and node.attr == 'union':
                return _ClassOperation('union', parent)
            self.negative_gap(path, node, 'finite class set attribute or bound operation escapes its checked consumption')
            return _Unsupported('finite_class_set_operation_unknown', path, node)
        if (self.call_context is not None and not self.call_context.compiling and type(parent) is list
                and not (calling and node.attr in {'append', 'extend'})):
            self.negative_gap(path, node, 'per-call list method or bound method escape is UNKNOWN')
            self.invalidate_mutable(parent)
            return None
        if isinstance(parent, _ConfigurationStream):
            # Even retaining an opaque bound read/seek method can later move
            # this same stream through a name/container/closure alias.
            parent.invalid = True
            return None
        if isinstance(parent, _Module):
            module = self.module(parent.path)
            if parent.path in self.unavailable_modules:
                return _External(parent.path + "." + node.attr, True)
            return module.get(node.attr, _Unsupported("source_export_rule_unsupported", path, node))
        if isinstance(parent, _External):
            if parent.name == "sys" and node.attr == "executable":
                return "python"
            return _External(parent.name + "." + node.attr, parent.local)
        if isinstance(parent, _Unsupported):
            return parent
        if isinstance(parent, _Super):
            base = parent.cls.bases[0] if parent.cls.bases else None
            if isinstance(base, _Class):
                member = self.class_attribute(base, node.attr)
                return _BoundMethod(member, parent.receiver) if isinstance(member, _Function) else member
            if isinstance(base, _External):
                return _External(base.name + "." + node.attr, base.local)
            return _Unsupported("super_dispatch_unsupported", path, node)
        if isinstance(parent, _Instance):
            if parent.local_class is not None and not parent.type_known:
                return _Unsupported("escaped_instance_binding_unsupported", path, node)
            if node.attr in parent.attributes:
                return parent.attributes[node.attr]
            if parent.local_class is None:
                return _External(parent.origin + '.' + node.attr)
            member = self.class_attribute(parent.local_class, node.attr)
            if isinstance(member, _Function):
                if member.method_kind == "unsupported" and not self.no_grad_wrapper(member):
                    self.unknown_target(path, node, member.name, "decorated_method_target_unsupported")
                    self.unknown_target(member.path, member.node, member.name, "decorated_method_target_unsupported")
                    return _Unsupported("decorated_method_target_unsupported", member.path, member.node)
                return member if member.method_kind == "staticmethod" else _BoundMethod(
                    member, parent.local_class if member.method_kind == "classmethod" else parent)
            return member if member is not None else _Unsupported("local_attribute_target_unsupported", path, node)
        if isinstance(parent, _Class):
            if node.attr in {'__subclasses__', '__name__'}:
                try:
                    world = self.registration_world(parent, path, node)
                    if node.attr == '__name__' and not calling:
                        return world.name(parent.path + ':' + parent.name)
                    if node.attr == '__subclasses__' and calling:
                        return _ClassOperation('__subclasses__', (parent, world))
                except RegistrationUnknown as error:
                    self.negative_gap(path, node, str(error))
                return _Unsupported('finite_class_reference_protocol_unknown', path, node)
            member = self.class_attribute(parent, node.attr)
            return (_BoundMethod(member, parent) if isinstance(member, _Function) and member.method_kind == "classmethod" else
                    member if member is not None else _Unsupported("local_attribute_target_unsupported", path, node))
        if isinstance(parent, _Path):
            if node.attr == "parent":
                return (_Path(posixpath.dirname(parent.value) or ".", parent.frozen_anchor,
                              posixpath.dirname(parent.original or parent.value) or ".")
                        if parent.value is not None else _Path(None))
            if node.attr == "parents":
                result, current, original = [], parent.value, parent.original or parent.value
                if current is None:
                    return None
                for _ in range(8):
                    current = posixpath.dirname(current) or "."
                    original = posixpath.dirname(original) or "."
                    result.append(_Path(current, parent.frozen_anchor, original))
                return result
        if isinstance(parent, dict):
            return parent.get(node.attr)
        return None

    def value(self, node, env, path, scope, setup=False):
        if node is None:
            return None
        self.steps += 1
        if self.steps > 16000:
            if not any(u["reason"] == "finite_analysis_limit" for u in self.unknown):
                self.unknown_target(path, node, scope, "finite_analysis_limit")
            return None
        if (not setup and self.call_context is not None and not self.call_context.compiling
                and not self.in_bounded_expression
                and (isinstance(node, (ast.BoolOp, ast.ListComp, ast.BinOp, ast.Subscript, ast.Compare, ast.UnaryOp))
                     or isinstance(node, ast.Call) and (
                         isinstance(node.func, ast.Name) and node.func.id == 'len'
                         or isinstance(node.func, ast.Attribute) and node.func.attr in {'split', 'join', 'replace', 'strip'}))
                and any(isinstance(child, ast.Name) and self.bounded_value(env.get(child.id))
                        for child in ast.walk(node))):
            from seampact.source.sdk_bounded_flow import evaluate_expression
            self.in_bounded_expression = True
            try:
                return evaluate_expression(self, node, env, path, scope)
            except bounded.BoundedUnknown as error:
                self.negative_gap(path, node, 'bounded ordinary expression remains UNKNOWN: ' + str(error))
                return None
            finally:
                self.in_bounded_expression = False
        if isinstance(node, ast.Constant):
            return LITERAL_NONE if node.value is None else node.value
        if isinstance(node, ast.Name):
            value = env.get(node.id)
            if type(value) is _UnorderedClassTarget:
                self.negative_gap(path, node, 'unmatched class selection leaves an unknown original final loop target or an unbound local')
                return _Alternatives(value.items)
            return value
        if isinstance(node, ast.Attribute):
            parent = self.value(node.value, env, path, scope, setup)
            if setup:
                if type(parent) is _Module:
                    namespace = self.module(parent.path)
                    if node.attr not in namespace and '__getattr__' in namespace:
                        self.activation(path, node, 'MODULE_GETATTR', 'original module attribute activation protocol is not traced',
                                        module_path=parent.path, attribute=node.attr)
                elif type(parent) is _External or type(parent) is _Instance and parent.local_class is None:
                    kernel = None
                    if type(parent) is _External and parent.name == 'abc' and not parent.local:
                        from seampact.source.abc_kernel import check_abc_kernel
                        kernel = check_abc_kernel()
                    if (self.call_context is not None and type(parent) is _External and not parent.local
                            and self.sdk_source.unmodified(parent.name + '.' + node.attr)):
                        kernel = sdk_attribute_requirement(self,path,node,parent.name + '.' + node.attr) or kernel
                    self.activation(path, node, 'EXTERNAL_ATTRIBUTE',
                        None if kernel and kernel['status'] == 'CHECKED' else 'original external attribute activation effects are not proved',
                        attribute=node.attr, **(dict(kernel=kernel) if kernel else {}))
            return self.attribute_value(node, parent, path, scope)
        if isinstance(node, (ast.List, ast.Tuple)):
            result = []
            for item in node.elts:
                if isinstance(item, ast.Starred):
                    value = self.value(item.value, env, path, scope, setup)
                    if not isinstance(value, (list, tuple)):
                        self.invalidate_configuration_streams(value)
                        return None
                    result.extend(value)
                else:
                    result.append(self.value(item, env, path, scope, setup))
            return tuple(result) if isinstance(node, ast.Tuple) else result
        if isinstance(node, ast.Dict):
            result = {}
            for key, item in zip(node.keys, node.values):
                if key is None:
                    other = self.value(item, env, path, scope, setup)
                    if isinstance(other, dict):
                        result.update(other)
                    else:
                        self.invalidate_configuration_streams(other)
                        return None
                else:
                    key = self.value(key, env, path, scope, setup)
                    if isinstance(key, (str, int)):
                        result[key] = self.value(item, env, path, scope, setup)
            return result
        if isinstance(node, ast.Subscript):
            parent = self.value(node.value, env, path, scope, setup)
            key = self.value(node.slice, env, path, scope, setup)
            sdk_result = sdk_subscript(self,parent,key,path,node)
            if sdk_result is not TRANSFORMER_UNHANDLED:
                return sdk_result
            if (self.call_context is not None and not self.call_context.compiling and isinstance(node.slice, ast.Slice)
                    and (type(parent) is list or isinstance(parent, _PlainShape) and parent.kind == 'list')):
                # The successful original builtin slice has exact list outer
                # type. No elements, length or successful completion is proved.
                bounds = (node.slice.lower, node.slice.upper, node.slice.step)
                if all(bound is None or isinstance(bound, ast.Constant) and type(bound.value) is int
                       or isinstance(bound, ast.UnaryOp) and isinstance(bound.op, ast.USub)
                       and isinstance(bound.operand, ast.Constant) and type(bound.operand.value) is int for bound in bounds):
                    if type(parent) is list and self.call_plain(parent):
                        def bound(part):
                            if part is None:
                                return None
                            return (-part.operand.value if isinstance(part, ast.UnaryOp) else part.value)
                        limits = [bound(part) for part in bounds]
                        if limits[2] != 0:
                            return parent[slice(*limits)]
                        self.negative_gap(path, node, 'per-call slice has no proved normal result')
                        return None
                    if id(parent) not in self.call_value_taints:
                        return _PlainShape('list')
                    self.negative_gap(path, node, 'per-call slice receiver has a contaminated alias')
                    return None
            if isinstance(parent, _Unsupported):
                return parent
            if isinstance(parent, _ConfigurationStream):
                parent.invalid = True
            if isinstance(parent, _Instance) and parent.local_class is not None:
                self.negative_gap(path, node, "implicit source subscript protocol is not traced")
            try:
                return parent[key] if isinstance(parent, (dict, list, tuple)) and key is not None else None
            except (KeyError, IndexError, TypeError):
                return None
        if (isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub))
                and self.call_context is not None and not self.call_context.compiling):
            operand = self.value(node.operand, env, path, scope, setup)
            if self.ordinary_value_operation('int'):
                try:
                    return finite_integer('positive' if isinstance(node.op, ast.UAdd) else 'negative', [operand])
                except CallUnknown:
                    pass
            self.negative_gap(path, node, 'per-call integer operation binding or finite operands/result are UNKNOWN')
            return None
        if isinstance(node, ast.BinOp):
            left, right = self.value(node.left, env, path, scope, setup), self.value(node.right, env, path, scope, setup)
            if isinstance(left, _ConfigurationStream) or isinstance(right, _ConfigurationStream):
                self.invalidate_configuration_streams((left, right))
            if any(isinstance(value, _Instance) and value.local_class is not None for value in (left, right)):
                self.negative_gap(path, node, "implicit source binary protocol is not traced")
            if isinstance(node.op, ast.Div) and isinstance(left, _Path):
                part = right.value if isinstance(right, _Path) else right
                return (_Path(posixpath.normpath(posixpath.join(left.value, part)), left.frozen_anchor,
                              posixpath.join(left.original or left.value, right.original or part if isinstance(right, _Path) else part))
                        if isinstance(left.value, str) and isinstance(part, str) else _Path(None))
            if isinstance(node.op, ast.Add) and isinstance(left, (str, list)) and type(left) is type(right):
                return left + right
            if (self.call_context is not None and not self.call_context.compiling
                    and isinstance(node.op, (ast.Add, ast.Sub))):
                if self.ordinary_value_operation('int'):
                    try:
                        return finite_integer('add' if isinstance(node.op, ast.Add) else 'subtract', [left, right])
                    except CallUnknown:
                        pass
                self.negative_gap(path, node, 'per-call integer operation binding or finite operands/result are UNKNOWN')
                return None
            if (self.call_context is not None and not self.call_context.compiling
                    and isinstance(node.op, ast.Mult)
                    and (type(left) is list or isinstance(left, _PlainShape) and left.kind == 'list')
                    and (type(right) is int or isinstance(right, _PlainShape) and right.kind == 'integer')):
                return _PlainShape('list')
            return None
        if isinstance(node, ast.IfExp):
            truth = self.conditional_truth(node.test, env, path, scope)
            if truth is not None:
                return self.value(node.body if truth else node.orelse, env, path, scope, setup)
            self.branch_stack.append(dict(status="UNKNOWN", path=path, line=node.lineno,
                expression=ast.unparse(node.test), selected="conditional expression alternatives"))
            try:
                values = [self.value(part, _branch_environment(env), path, scope, setup) for part in (node.body, node.orelse)]
            finally:
                self.branch_stack.pop()
            if all(isinstance(value, _Path) for value in values):
                return values[0] if values[0] == values[1] else _Path(None)
            parts = (node.body, node.orelse)
            if all(isinstance(value, _Path) or isinstance(part, ast.Constant) and part.value is None
                   for value, part in zip(values, parts)) and any(isinstance(value, _Path) for value in values):
                return _OptionalPath()
            if all(isinstance(value, (_Function, _BoundMethod, _Class, _Unsupported, _Alternatives)) or value is None for value in values):
                return _Alternatives(tuple(values))
            return values[0] if values[0] == values[1] else None
        if isinstance(node, ast.BoolOp):
            if self.call_context is not None and not self.call_context.compiling:
                for index, part in enumerate(node.values):
                    value = self.value(part, env, path, scope, setup)
                    truth = (False if isinstance(value, _LiteralNone) else
                             plain_truth(value) if self.call_plain(value) else None)
                    if truth is None:
                        if isinstance(value, _PlainShape) and value.kind == 'list':
                            self.branch_stack.append(dict(status='UNKNOWN',path=path,line=node.lineno,
                                expression=type(node.op).__name__,selected='ordinary list truth alternatives'))
                            try:
                                rest = [self.value(remaining, env, path, scope, setup) for remaining in node.values[index+1:]]
                            finally:
                                self.branch_stack.pop()
                            if all(type(item) is list or isinstance(item,_PlainShape) and item.kind=='list' for item in rest):
                                return _PlainShape('list')
                            self.negative_gap(path, node, 'per-call short-circuit operand is not an exact ordinary value')
                            return None
                        self.negative_gap(path, node, 'per-call short-circuit operand is not an exact ordinary value')
                        # Retain every possible remaining effect, without
                        # treating a guessed branch as a checked path.
                        for remaining in node.values[index + 1:]:
                            self.value(remaining, env, path, scope, setup)
                        return None
                    if (isinstance(node.op, ast.And) and not truth
                            or isinstance(node.op, ast.Or) and truth):
                        return value
                return value
            self.branch_stack.append(dict(status="UNKNOWN", path=path, line=node.lineno,
                expression=type(node.op).__name__, selected="short circuit alternatives"))
            try:
                values = [self.value(part, env, path, scope, setup) for part in node.values]
            finally:
                self.branch_stack.pop()
            if isinstance(node.op, ast.Or) and isinstance(values[-1], _Path) and all(
                    isinstance(value, (_Path, _OptionalPath)) for value in values):
                return _Path(None)
            return None
        if isinstance(node, ast.Lambda):
            function = ast.FunctionDef(name="<lambda>", args=node.args,
                body=[ast.copy_location(ast.Return(value=node.body), node)], decorator_list=[])
            ast.copy_location(function, node)
            target = _Function(path, scope + ".<lambda>@" + str(node.lineno), function, env)
            target.defaults = self.function_defaults(function, env, path, scope)
            return target
        if isinstance(node, ast.Call):
            receiver = self.value(node.func.value, env, path, scope, setup) if isinstance(node.func, ast.Attribute) else None
            function = (self.attribute_value(node.func, receiver, path, scope, calling=True) if isinstance(node.func, ast.Attribute)
                        else self.value(node.func, env, path, scope, setup))
            if self.result_observer is not None:
                targets = function.values if isinstance(function, _Alternatives) else [function]
                if not all(isinstance(target, (_Function, _BoundMethod)) for target in targets):
                    # A mixed dispatch or a later SDK/constructor occurrence
                    # must not inherit an earlier source call's return proof.
                    self.result_observer.call_result(node, None)
            args, kwargs, unknown_positionals = [], {}, False
            for argument in node.args:
                if isinstance(argument, ast.Starred):
                    expanded = self.value(argument.value, env, path, scope, setup)
                    if not unknown_positionals and isinstance(expanded, list):
                        args.extend(expanded)
                    else:
                        unknown_positionals = True
                        self.invalidate_configuration_streams(expanded)
                else:
                    value = self.value(argument, env, path, scope, setup)
                    if not unknown_positionals:
                        args.append(value)
            for keyword in node.keywords:
                value = self.value(keyword.value, env, path, scope, setup)
                if keyword.arg is not None:
                    kwargs[keyword.arg] = value
                elif isinstance(value, dict):
                    kwargs.update(value)
                else:
                    kwargs[None] = None
                    self.invalidate_configuration_streams(value)
            if unknown_positionals:
                kwargs[None] = None
            if not setup:
                configured = configuration_collect(self,node,args,kwargs,env,path)
                if configured is not CONFIG_UNHANDLED:
                    return configured
            if type(function) is _ClassOperation and not setup:
                try:
                    if kwargs or any(isinstance(part, ast.Starred) for part in node.args):
                        raise RegistrationUnknown('native class collection call binding is not closed')
                    if function.name == '__subclasses__' and not args:
                        cls, world = function.receiver
                        identities = world.direct_subclasses(cls.path + ':' + cls.name)
                        self.protocol(REGISTRATION_RULE, path, node, target_requirements=world.requirements(),
                                      operation='original ordered native direct subclass enumeration',
                                      receiver_class=cls.path + ':' + cls.name, direct_subclasses=list(identities))
                        return _ClassSequence(self.registration_items(world, identities), world)
                    if function.name == 'union':
                        collection = function.receiver
                        world, items = collection.world, list(collection.items)
                        for value in args:
                            if type(value) is not _ClassSet and type(value) is not _ClassSequence:
                                raise RegistrationUnknown('native class union has an unknown iterable')
                            if value.world is not None:
                                if world is not None and world.requirements()['digest'] != value.world.requirements()['digest']:
                                    raise RegistrationUnknown('native class union crosses an unproved class world')
                                world = value.world
                            items.extend(value.items)
                        if world is None:
                            raise RegistrationUnknown('native class union has no complete world premise')
                        self.protocol(REGISTRATION_RULE, path, node, target_requirements=world.requirements(),
                                      operation='original native class set union; iteration order remains UNKNOWN')
                        return self.registration_collection(items, world)
                    raise RegistrationUnknown('native class collection call form is not supported')
                except RegistrationUnknown as error:
                    self.negative_gap(path, node, str(error))
                    return None
            if function is None and isinstance(node.func, ast.Name) and node.func.id == 'set' and 'set' not in env and not setup:
                try:
                    if (not builtin_state() or builtins.__dict__.get('set') is not _REGISTRATION_SET
                            or not self.sdk_source.type_references.builtins_closed() or not self.sdk_source.unmodified('builtins.set')
                            or kwargs or len(args) > 1 or any(isinstance(part, ast.Starred) for part in node.args)):
                        raise RegistrationUnknown('original native set binding is not closed')
                    if not args:
                        if not self.registration_worlds:
                            raise RegistrationUnknown('empty native class set has no complete world premise')
                        world = next(iter(self.registration_worlds.values()))
                        self.protocol(REGISTRATION_RULE, path, node, target_requirements=world.requirements(),
                                      operation='original empty native class set')
                        return _ClassSet((), world)
                    if type(args[0]) is not _ClassSequence and type(args[0]) is not _ClassSet:
                        raise RegistrationUnknown('native set operand has no finite class-reference proof')
                    if args[0].world is None:
                        raise RegistrationUnknown('native class set operand has no complete world premise')
                    self.protocol(REGISTRATION_RULE, path, node, target_requirements=args[0].world.requirements(),
                                  operation='original native set of exact class references; iteration order remains UNKNOWN')
                    return self.registration_collection(args[0].items, args[0].world)
                except RegistrationUnknown as error:
                    self.negative_gap(path, node, str(error))
                    return None
            if (not setup and isinstance(node.func, ast.Name) and node.func.id == 'isinstance'
                    and len(node.args) == 2 and not node.keywords):
                type_fact = self.sdk_source.type_reference(path, node, node.args[1])
                self.protocol(type_fact['rule'], path, node, type_reference=type_fact)
                if self.call_context is not None:
                    if self.call_context.compiling and not self.call_context.initialization_only:
                        return None  # A compiled obligation, never an actual call proof.
                    if not self.call_context.compiling:
                        tested = self.plain_instance_test(node, args[0] if args else None, path)
                        if tested is not None:
                            return tested
                self.negative_gap(path, node, _CALL_TYPE_GAP)
            if self.call_context is not None and not self.call_context.compiling and not setup:
                operation = node.func.id if isinstance(node.func, ast.Name) else None
                if operation in {'len', 'range', 'zip'} and operation not in env:
                    if (not self.ordinary_operation(node, env, path, operation)
                            or any(isinstance(argument, ast.Starred) for argument in node.args)
                            or any(keyword.arg is None for keyword in node.keywords)):
                        self.negative_gap(path, node, 'per-call original builtin binding is not closed')
                        return None
                    if operation == 'len' and len(args) == 1 and not kwargs:
                        value = args[0]
                        if any(type(value) is native for native in (list, dict, str)) and self.call_plain(value):
                            return len(value)
                        if isinstance(value, _PlainShape) and value.kind == 'list':
                            return _PlainShape('integer')
                    elif operation == 'range' and not kwargs:
                        try:
                            return _FiniteIteration(finite_range(args), id(node))
                        except CallUnknown:
                            pass
                    elif operation == 'zip' and not kwargs and len(args) <= 64:
                        operands = [self.iteration_items(argument, value) for argument, value in zip(node.args, args)]
                        if len(operands) == len(node.args) and all(type(items) is tuple for items in operands):
                            count = min((len(items) for items in operands), default=0)
                            return _FiniteIteration(tuple(tuple(items[i] for items in operands) for i in range(count)), id(node), tuple(args))
                    self.negative_gap(path, node, 'per-call builtin operands or finite normal result are UNKNOWN')
                    return None
                if isinstance(node.func, ast.Attribute) and node.func.attr == 'split' and type(receiver) is str:
                    if (builtin_state() and self.sdk_source.type_references.builtins_closed()
                            and not any(isinstance(argument, ast.Starred) for argument in node.args)
                            and not any(keyword.arg is None for keyword in node.keywords)):
                        try:
                            if any(value is None for value in [*args, *kwargs.values()]):
                                raise CallUnknown('unknown split operand')
                            return finite_split(receiver, [None if isinstance(v, _LiteralNone) else v for v in args],
                                {k: None if isinstance(v, _LiteralNone) else v for k, v in kwargs.items()})
                        except CallUnknown:
                            pass
                    self.negative_gap(path, node, 'per-call split binding or finite normal result is UNKNOWN')
                    return None
                if (isinstance(node.func, ast.Attribute) and node.func.attr in {'join', 'replace', 'strip'}
                        and type(receiver) is str):
                    if (self.ordinary_value_operation('str')
                            and not any(isinstance(argument, ast.Starred) for argument in node.args)
                            and not any(keyword.arg is None for keyword in node.keywords)
                            and all(self.call_plain(value) for value in [*args, *kwargs.values()])):
                        try:
                            return finite_string(node.func.attr, receiver,
                                [None if isinstance(value, _LiteralNone) else value for value in args],
                                {key: None if isinstance(value, _LiteralNone) else value for key, value in kwargs.items()})
                        except CallUnknown:
                            pass
                    self.negative_gap(path, node, 'per-call string operation binding or finite operands/result are UNKNOWN')
                    return None
            if isinstance(function, _External) and not function.local:
                self.external_call(function.name, args, kwargs, path, node)
                if not setup:
                    sdk_result = summarize_transformer_factory(self,function.name,args,kwargs,path,node)
                    if sdk_result is not TRANSFORMER_UNHANDLED:
                        return sdk_result
                    summary = summarize_sdk_call(self, function.name, args, kwargs, path, node)
                    if summary is not None and summary[0] is not None:
                        return summary[0]
                if setup:
                    self.activation(path, node, 'EXTERNAL_SETUP_CALL', 'original definition/module external call effects are not proved', target=function.name)
            if isinstance(function, _ExternalCallable):
                self.protocol("external-factory-result-call/1", path, node, target="spacy.language.Language.__call__",
                              factory=function.factory, resource=function.resource, reference=SPACY_CALL)
                return _Instance("spacy.tokens.Doc()", {})
            if isinstance(function, _External) and function.name == "spacy.load":
                result = self.spacy_factory(node, env, path, scope)
                if result is not None:
                    return result
            name = function.name if isinstance(function, _External) else ast.unparse(node.func)
            if (function is None and name == "open" and "open" not in env and not setup and args
                    and isinstance(args[0], _Path) and args[0].frozen_anchor
                    and isinstance(args[0].value, str) and args[0].value.endswith((".yaml", ".yml"))):
                original = args[0]
                try:
                    if not self.sdk_source.unmodified("builtins.open"):
                        raise ConfigurationUnknown("original built-in open is explicitly modified")
                    if (not 1 <= len(args) <= 2 or set(kwargs) - {"mode", "encoding"}
                            or len(args) == 2 and "mode" in kwargs):
                        raise ConfigurationUnknown("original open arguments are not covered")
                    mode = args[1] if len(args) == 2 else kwargs.get("mode", "r")
                    encoding = kwargs.get("encoding")
                    if (type(mode) is not str or mode not in {"r", "rt"}
                            or "encoding" in kwargs and (type(encoding) is not str or encoding not in {"ascii", "utf-8"})):
                        raise ConfigurationUnknown("configuration needs original plain read-only text open")
                    filename, _ = frozen_bytes(self.inspector, original.original or original.value)
                    self.protocol("source-frozen-configuration-open/1", path, node, configuration_file=filename,
                                  read_effect="original open/read/close retained; success is not assumed")
                    return _ConfigurationStream(filename, encoding=dict(
                        argument=encoding if "encoding" in kwargs else "OMITTED",
                        resolved=encoding if "encoding" in kwargs else "UNKNOWN_ORIGINAL_RUNTIME_DEFAULT"))
                except (ConfigurationUnknown, OSError, ValueError, KeyError) as error:
                    self.negative_gap(path, node, str(error))
                    self.unknown_target(path, node, "open", "frozen_configuration_path_or_open_unproved")
                    return None
            if isinstance(function, _External) and not function.local and name == "yaml.safe_load" and not setup:
                try:
                    if len(args) != 1 or kwargs or not isinstance(args[0], _ConfigurationStream):
                        raise ConfigurationUnknown("safe_load needs the exact original opened text stream")
                    stream = args[0]
                    if stream.invalid or not stream.active or stream.closed or stream.consumed:
                        raise ConfigurationUnknown("configuration stream state is not proved")
                    stream.consumed = True
                    source_check = reader_source_check(self.inspector)
                    if not self.sdk_source.unmodified("yaml") or source_check["status"] != "FINITE_FORMS_CHECKED":
                        raise ConfigurationUnknown("frozen reader binding use is outside the finite source check: " +
                                                   str(source_check.get("reason", "explicit mutation or escape")))
                    identity = reader_identity()
                    if identity["installed_source"] != "MATCHED":
                        raise ConfigurationUnknown("reviewed safe reader code identity is not matched in this checker")
                    filename, data = frozen_bytes(self.inspector, stream.path)
                    ordinary = plain_yaml_value(data)
                    result = _configuration_value(ordinary)
                    condition = dict(configuration_file=filename, sha256=self.inspector.files[filename]["sha256"],
                        source_version=self.inspector.request.get("source_version"),
                        value_sha256=hashlib.sha256(json.dumps(ordinary, sort_keys=True, allow_nan=False).encode()).hexdigest(),
                        original_reader="yaml.safe_load", reader_identity=identity, source_reader_check=source_check,
                        original_text_encoding=stream.encoding,
                        stream_state="FINITE_SOURCE_USES_CHECKED_RUNTIME_UNPROVED",
                        required=["same frozen file bytes at original runtime read", "stream is at its original initial position with original unmodified read/seek/iteration methods and decoder", "original default SafeLoader constructor/resolver tables and no external hooks", "original text decoding produces the same ASCII characters (normal newline handling), with unmodified codec and no unknown decoding hooks", "original open/read/close complete normally"],
                        status="CONDITIONAL", lifecycle="UNKNOWN")
                    if condition not in self.configuration_conditions:
                        self.configuration_conditions.append(condition)
                    register_reader_result(self, result, condition, path, node)
                    self.protocol(CONFIGURATION_RULE, path, node, value_status="CONDITIONAL", condition=condition)
                    self.negative_gap(path, node, "configuration value is conditional on original runtime bytes, reader state and read/close completion")
                    self.unknown_target(path, node, name, "frozen_configuration_runtime_conditions_unproved")
                    return result
                except (ConfigurationUnknown, OSError, ValueError, KeyError) as error:
                    self.negative_gap(path, node, str(error))
                    self.unknown_target(path, node, name, "frozen_configuration_reader_or_stream_unproved")
                    return None
            if function is None and name == "super" and "super" not in env:
                cls = args[0] if len(args) == 2 else env.get("<method_class>")
                receiver = args[1] if len(args) == 2 else env.get("<method_self>")
                if isinstance(cls, _Class) and receiver is not None:
                    return _Super(cls, receiver)
                return _Unsupported("super_dispatch_unsupported", path, node)
            if args and ((isinstance(function, _External) and not function.local and name == "pathlib.Path") or
                         (function is None and name == "str" and "str" not in env)):
                if name == "str":
                    return args[0].value if isinstance(args[0], _Path) else args[0] if isinstance(args[0], str) else None
                if not self.sdk_source.unmodified("pathlib.Path"):
                    return _Path(None)
                if kwargs or not all(isinstance(value, (str, _Path)) for value in args):
                    return _Path(None)
                parts = [value.value if isinstance(value, _Path) else value for value in args]
                raw = [value.original or value.value if isinstance(value, _Path) else value for value in args]
                if not all(isinstance(value, str) for value in parts):
                    return _Path(None)
                return _Path(posixpath.normpath(posixpath.join(*parts)),
                             isinstance(args[0], _Path) and args[0].frozen_anchor, posixpath.join(*raw))
            if name == "sys.executable":
                return "python"
            if isinstance(node.func, ast.Attribute):
                try:
                    configured = configuration_method(self, receiver, node.func.attr, args, kwargs, path, node)
                    if configured is not CONFIG_UNHANDLED:
                        return configured
                except ConfigurationSequenceUnknown as error:
                    self.negative_gap(path, node, 'frozen configuration sequence remains UNKNOWN: ' + str(error))
                    self.invalidate_mutable(receiver)
                    return None
                if isinstance(receiver, _Path) and node.func.attr in {"resolve", "absolute", "expanduser"}:
                    return receiver
                if isinstance(receiver, _Path) and node.func.attr == "joinpath":
                    parts = [value.value if isinstance(value, _Path) else value for value in args]
                    return (_Path(posixpath.normpath(posixpath.join(receiver.value, *parts)), receiver.frozen_anchor,
                                  posixpath.join(receiver.original or receiver.value, *parts))
                            if isinstance(receiver.value, str) and all(isinstance(part, str) for part in parts) else _Path(None))
                if isinstance(receiver, _ConfigurationStream):
                    receiver.invalid = True
                if type(receiver) is dict and None not in receiver and node.func.attr == "get" and 1 <= len(args) <= 2 and not kwargs:
                    if type(args[0]) in (str, int, bool):
                        return receiver.get(args[0], args[1] if len(args) == 2 else LITERAL_NONE)
                if isinstance(receiver, list) and node.func.attr in {"append", "extend"} and args:
                    if (self.call_context is not None and not self.call_context.compiling
                            and (len(args) != 1 or kwargs)):
                        self.negative_gap(path, node, 'per-call list mutation arguments are UNKNOWN')
                        self.invalidate_mutable(receiver)
                        return None
                    if (self.call_context is not None and not self.call_context.compiling
                            and node.func.attr == 'extend'):
                        items = self.iteration_items(node.args[0], args[0])
                        if items is None or len(args) != 1 or kwargs or id(receiver) in self.call_value_taints:
                            self.negative_gap(path, node, 'per-call list extension iterable is UNKNOWN')
                            self.invalidate_mutable(receiver)
                            return None
                        receiver.extend(items)
                        if type(args[0]) is SourceGenerator and args[0].failure is not None:
                            self.invalidate_mutable(receiver)
                        return None
                    receiver.extend(args[0] if node.func.attr == "extend" and isinstance(args[0], list) else [args[0]])
                    return None
                if isinstance(receiver, dict) and node.func.attr not in {"get", "keys", "values", "items", "copy"}:
                    # Do not preserve a frozen command map after an unmodelled
                    # update/delete/escape to a method.
                    self.invalidate_mutable(receiver)
                # Standard executor callback registration is a real call edge.
                if node.func.attr in {"submit", "map"} and args and isinstance(args[0], (_Function, _BoundMethod)) and not setup:
                    self.call_value(args[0], args[1:], {}, path, scope, node, capture_result=False)
            if setup:
                if not isinstance(function, _External):
                    self.activation(path, node, 'SETUP_OPERATION', 'original definition/module operation has no closed activation summary', target=name)
                if isinstance(function, (_Function, _BoundMethod, _Class, _Alternatives)):
                    self.negative_gap(path, node, "source definition-time call is not traced")
                for value in args + list(kwargs.values()):
                    self.invalidate_configuration_streams(value)
                    self.invalidate_instance_types(value)
                    if isinstance(value, (dict, list)):
                        self.invalidate_mutable(value)
                if isinstance(function, _Class):
                    # A setup-created local object must not masquerade as missing
                    # source; analysing arbitrary module initialization is outside this rule.
                    return _Unsupported("module_constructor_unsupported", path, node)
                return _Instance(name + '()', {}) if isinstance(function, _External) and not function.local else None
            if function is None and name in {"locals", "globals", "vars", "eval", "exec"}:
                self.invalidate_configuration_streams(env)
            if isinstance(function, (_Function, _BoundMethod, _Class, _Unsupported, _Alternatives, ModelDump, SDKValue, SDKMethod)) or isinstance(function, _Instance) and function.local_class is not None:
                return self.call_value(function, args, kwargs, path, scope, node)
            retained_arguments = args + list(kwargs.values())
            if any(isinstance(value, (_FiniteIteration, SDKIteration, SourceGenerator, BoundedIteration, ConfigurationIteration)) for value in retained_arguments):
                self.negative_gap(path, node, 'per-call finite iterable escapes immediate consumption')
            if any(self.source_protocol_value(value) for value in retained_arguments):
                self.negative_gap(path, node, "opaque call can invoke a supplied source callable or implicit owner protocol")
            if name in {"subprocess.run", "subprocess.Popen", "subprocess.call", "subprocess.check_call", "subprocess.check_output"}:
                self.process(path, scope, node, args[0] if args else kwargs.get("args"), kwargs)
            elif isinstance(function, _External) and function.local:
                self.unknown_target(path, node, function.name, "dynamic_call_target_unresolved" if
                                    function.name.startswith("<modified:") else "local_call_source_unavailable")
            elif function is None and isinstance(node.func, ast.Name) and (node.func.id not in _BUILTINS or node.func.id in env):
                self.unknown_target(path, node, node.func.id, "dynamic_call_target_unresolved")
            elif function is None and isinstance(node.func, ast.Attribute) and isinstance(
                    receiver, (_Instance, _Class)):
                self.unknown_target(path, node, ast.unparse(node.func), "dynamic_call_target_unresolved")
            elif function is None and not isinstance(node.func, (ast.Name, ast.Attribute)):
                self.unknown_target(path, node, ast.unparse(node.func), "dynamic_call_target_unresolved")
            if name not in {"subprocess.run", "subprocess.Popen", "subprocess.call", "subprocess.check_call", "subprocess.check_output"}:
                for value in retained_arguments:
                    self.invalidate_configuration_streams(value)
                    self.invalidate_instance_types(value)
                    if isinstance(value, (dict, list)):
                        self.invalidate_mutable(value)
            return _Instance(name + '()', {}) if isinstance(function, _External) and not function.local else None
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
            first_evaluated, first = False, None
            if isinstance(node, (ast.ListComp, ast.DictComp)) and node.generators:
                first_evaluated = True
                first = self.value(node.generators[0].iter, env, path, scope, setup)
                configured = configuration_comprehension(self,node,first,env,path,scope,setup=setup)
                if configured is not CONFIG_UNHANDLED:
                    return configured
                if isinstance(node,ast.ListComp) and (type(first) is _ClassSequence or type(first) is _ClassSet):
                    try:
                        result = self.registration_comprehension(node, first, env, path, scope, setup)
                        if result is not None:
                            return result
                        raise RegistrationUnknown('finite class comprehension form is not closed')
                    except RegistrationUnknown as error:
                        self.negative_gap(path, node, str(error))
                        return None
            if self.call_context is not None and not self.call_context.compiling:
                local = env.copy()
                if isinstance(node, ast.ListComp) and len(node.generators) == 1 and not node.generators[0].is_async:
                    generator = node.generators[0]
                    values = first if first_evaluated else self.value(generator.iter, local, path, scope, setup)
                    items = self.iteration_items(generator.iter, values)
                    if items is not None:
                        stamp, results = self.iteration_stamp(values), []
                        for item in items:
                            self.bind(generator.target, item, local, path, scope)
                            if generator.ifs:
                                self.negative_gap(path, node, 'per-call comprehension filters are outside finite value propagation')
                                for condition in generator.ifs:
                                    self.value(condition, local, path, scope, setup)
                            results.append(self.value(node.elt, local, path, scope, setup))
                            if self.iteration_stamp(values) != stamp:
                                self.negative_gap(path, node, 'per-call comprehension iterable changed during consumption')
                                return _PlainShape('list')
                        if type(values) is SourceGenerator and values.failure is not None:
                            return _PlainShape('list')
                        if not generator.ifs and all(self.call_plain(value) for value in results):
                            return results
                        return _PlainShape('list')
                self.negative_gap(path, node, 'per-call comprehension iterable is UNKNOWN')
                # Retain possible effects without guessing a first element or
                # eagerly certifying a source generator's deferred body.
                for generator in node.generators:
                    if not (isinstance(node, ast.ListComp) and len(node.generators) == 1 and not generator.is_async):
                        if not (first_evaluated and generator is node.generators[0]):
                            self.value(generator.iter, local, path, scope, setup)
                    self.bind(generator.target, None, local, path, scope)
                    for condition in generator.ifs:
                        self.value(condition, local, path, scope, setup)
                for child in ([node.key, node.value] if isinstance(node, ast.DictComp) else [node.elt]):
                    self.value(child, local, path, scope, setup)
                return _PlainShape('list') if isinstance(node, ast.ListComp) else None
            local = env.copy()
            for generator in node.generators:
                values = first if first_evaluated and generator is node.generators[0] else self.value(generator.iter, local, path, scope, setup)
                if (self.call_context is not None and not self.call_context.compiling
                        and type(values) is not list and not (isinstance(values,_PlainShape) and values.kind=='list')):
                    self.negative_gap(path, node, 'per-call comprehension iterable is UNKNOWN')
                if isinstance(values, _ConfigurationStream):
                    values.invalid = True
                if isinstance(values, _Instance) and values.local_class is not None:
                    self.negative_gap(path, node, "implicit source iteration protocol is not traced")
                self.bind(generator.target, values[0] if isinstance(values, list) and values else None, local)
            for child in ([node.key, node.value] if isinstance(node, ast.DictComp) else [node.elt]):
                self.value(child, local, path, scope, setup)
            if self.call_context is not None and not self.call_context.compiling and isinstance(node, ast.ListComp):
                return _PlainShape('list')
        elif not isinstance(node, (ast.Lambda, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.expr):
                    value = self.value(child, env, path, scope, setup)
                    if isinstance(value, _ConfigurationStream):
                        value.invalid = True
                    if self.source_protocol_value(value):
                        self.negative_gap(path, node, "expression has an untraced implicit source protocol: " + type(node).__name__)
        return None

    def plain_instance_test(self, node, value, path):
        """Only the original builtin on a proved ordinary first operand."""
        if self.sdk_source.type_references.builtin(path, node)['status'] != 'SOURCE_CHECKED':
            return None
        qualified = self.sdk_source.type_references.resolve(path, node.args[1], node)
        classes = {'builtins.list': list, 'builtins.dict': dict, 'builtins.str': str,
                   'builtins.int': int, 'builtins.float': float, 'builtins.bool': bool}
        expected = classes.get(qualified)
        if expected is None:
            return None  # In particular, no SDK class object is authorized.
        if isinstance(value, _LiteralNone):
            return False
        if bounded.is_bounded(value):
            return {'list': list, 'integer': int, 'string': str, 'boolean': bool}.get(bounded.value_kind(value)) is expected
        if isinstance(value, _PlainShape):
            return {'list': list, 'integer': int}.get(value.kind) is expected
        if type(value) in (list, dict, str, int, float, bool):
            return isinstance(value, expected)
        return None

    def truth(self, node, env, path, scope):
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            result = self.truth(node.operand, env, path, scope)
            return not result if result is not None else None
        if isinstance(node, ast.Compare) and len(node.ops) == 1 and isinstance(node.ops[0], (ast.In, ast.NotIn)):
            value = self.value(node.left, env, path, scope)
            choices = self.value(node.comparators[0], env, path, scope)
            self.invalidate_configuration_streams((value, choices))
            passive = lambda item: type(item) in (str, int, float, bool) or isinstance(item, _LiteralNone)
            if passive(value) and type(choices) is list and all(passive(item) for item in choices):
                found = any((isinstance(value, _LiteralNone) and isinstance(item, _LiteralNone))
                            or (not isinstance(value, _LiteralNone) and not isinstance(item, _LiteralNone) and value == item)
                            for item in choices)
                result = found if isinstance(node.ops[0], ast.In) else not found
                self.protocol("source-literal-membership/1", path, node, expression=ast.unparse(node), result=result,
                    value_digest=hashlib.sha256(repr(value).encode()).hexdigest())
                return result
            if self.source_protocol_value(value) or self.source_protocol_value(choices):
                self.negative_gap(path, node, "implicit source membership protocol is not traced")
            return None
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "isinstance"
                and "isinstance" not in env and len(node.args) == 2 and not node.keywords):
            value = self.value(node.args[0], env, path, scope)
            expected = self.value(node.args[1], env, path, scope)
            if self.call_context is not None and not self.call_context.compiling:
                tested = self.plain_instance_test(node, value, path)
                if tested is not None:
                    return tested
            builtin_fact = self.sdk_source.type_references.builtin(path, node)
            if builtin_fact['status'] != 'SOURCE_CHECKED':
                self.negative_gap(path, node, 'original isinstance builtin binding is not closed')
                return None
            type_fact = self.sdk_source.type_reference(path, node, node.args[1])
            self.protocol(type_fact['rule'], path, node, type_reference=type_fact)
            if isinstance(expected, _External):
                self.negative_gap(path, node, 'external type identity and first-operand protocols remain unproved')
                return None
            def ordinary(cls):
                if (not isinstance(cls, _Class) or cls.problem or cls.node.decorator_list or cls.node.keywords
                        or "__class__" in cls.attributes):
                    return False
                return not cls.bases or len(cls.bases) == 1 and (
                    ordinary(cls.bases[0]) if isinstance(cls.bases[0], _Class) else
                    isinstance(cls.bases[0], _External) and cls.bases[0].name == "builtins.object" and not cls.bases[0].local)
            if (isinstance(value, _Instance) and value.type_known and "__class__" not in value.attributes
                    and ordinary(value.local_class) and ordinary(expected)):
                current, result = value.local_class, False
                while isinstance(current, _Class):
                    if current is expected:
                        result = True
                        break
                    current = current.bases[0] if current.bases else None
                self.protocol("source-exact-local-instance/1", path, node, expression=ast.unparse(node), result=result,
                    actual=dict(path=value.local_class.path, symbol=value.local_class.name),
                    expected=dict(path=expected.path, symbol=expected.name))
                return result
            # Retain call effects when the purported builtin has no exact
            # ordinary source receiver; never reason by a package/type name.
            if ((isinstance(value, _Instance) and value.local_class is not None) or isinstance(expected, _Class)):
                self.negative_gap(path, node, "source instance type protocol is not closed")
            return None
        if isinstance(node, ast.Compare) and len(node.ops) == 1 and isinstance(node.ops[0], (ast.Is, ast.IsNot)):
            a, b = self.value(node.left, env, path, scope), self.value(node.comparators[0], env, path, scope)
            # An absent abstract binding is UNKNOWN, never evidence for None.
            # Identity against literal None is decidable for proven non-None
            # values, without invoking a user-defined __eq__ or __bool__.
            if isinstance(a, _LiteralNone) or isinstance(b, _LiteralNone):
                other = b if isinstance(a, _LiteralNone) else a
                proven = isinstance(other, (_LiteralNone, str, int, float, bool, list, dict,
                    _Function, _BoundMethod, _Class, _Instance, _Module, _ExternalCallable))
                if proven:
                    equal = isinstance(other, _LiteralNone)
                    return equal if isinstance(node.ops[0], ast.Is) else not equal
            return None
        if isinstance(node, ast.Compare) and len(node.ops) == 1 and isinstance(node.ops[0], (ast.Eq, ast.NotEq)):
            a, b = self.value(node.left, env, path, scope), self.value(node.comparators[0], env, path, scope)
            self.invalidate_configuration_streams((a, b))
            if any(isinstance(value, _Instance) and value.local_class is not None for value in (a, b)):
                self.negative_gap(path, node, "implicit source comparison protocol is not traced")
            # Abstract source values are not the original runtime objects.
            # Their dataclass equality cannot prove a branch false (or true).
            if isinstance(a, (str, int, float, bool)) and isinstance(b, (str, int, float, bool)):
                return (a == b) if isinstance(node.ops[0], ast.Eq) else a != b
            return None
        value = self.value(node, env, path, scope)
        configured = configuration_truth(self, value)
        if configured is not None:
            return configured
        if (type(value) is _ClassSequence or type(value) is _ClassSet) and value.world is not None:
            return bool(value.items)
        if type(value) is _Class:
            try:
                self.registration_world(value, path, node)
                return True
            except RegistrationUnknown as error:
                self.negative_gap(path, node, str(error))
                return None
        if isinstance(value, _Instance) and value.local_class is not None:
            self.negative_gap(path, node, "implicit source truth protocol is not traced")
        if isinstance(value, _LiteralNone):
            return False
        if bounded.is_bounded(value):
            return bounded.truth(value)
        if self.call_context is not None and not self.call_context.compiling:
            return plain_truth(value) if self.call_plain(value) else None
        return bool(value) if isinstance(value, (bool, int, str)) else None

    def conditional_truth(self, node, env, path, scope):
        result = self.truth(node, env, path, scope)
        if self.result_observer is not None:
            result = self.result_observer.conditional_truth(node, env, path, result)
        return result

    def statement(self, node, env, path, scope, setup=False):
        if self.result_observer is not None:
            self.result_observer.statement(node, path, env)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            self.definition_decorators(node, env, path, scope)
            if node.decorator_list:
                self.negative_gap(path, node, "source definition-time decorator protocol is not traced")
            function = _Function(path, node.name if scope in {"<module>", "__main__"} else scope + ".<locals>." + node.name,
                                 node, env if scope not in {"<module>", "__main__"} else None)
            function.defaults = self.function_defaults(node, env, path, scope)
            self.function_annotations(node, env, path, scope)
            env[node.name] = function
        elif isinstance(node, ast.ClassDef):
            if node.decorator_list:
                self.negative_gap(path, node, "source definition-time class decorator protocol is not traced")
            env[node.name] = self.class_definition(node, env, path, scope)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            site = dict(path=path, unit="file", start=node.lineno, end=node.end_lineno,
                ast_sha256=hashlib.sha256(ast.dump(node, include_attributes=False).encode()).hexdigest())
            if site not in self.import_sites:
                self.import_sites.append(site)
            for alias in node.names:
                module = node.module or "" if isinstance(node, ast.ImportFrom) else alias.name
                target = self.module_path(module, path, getattr(node, "level", 0))
                bound = alias.asname or (alias.name if isinstance(node, ast.ImportFrom) else alias.name.split(".")[0])
                if target:
                    namespace = self.module(target)
                    if isinstance(node, ast.ImportFrom):
                        value = namespace.get(alias.name)
                        if value is None and '__getattr__' in namespace:
                            self.activation(path, node, 'MODULE_GETATTR', 'original module missing-attribute import protocol is not traced',
                                            imported=alias.name, module_path=target)
                        elif value is None and alias.name != '*':
                            child = self.module_path((module + '.' if module else '') + alias.name, path, getattr(node, 'level', 0))
                            if child:
                                self.module(child)
                                value = _Module(child)
                        if value is None and setup:
                            self.activation(path, node, 'SOURCE_IMPORT_BINDING', 'original source import binding is absent or partially initialized',
                                            imported=alias.name, module_path=target)
                    else:
                        if alias.asname or '.' not in alias.name:
                            value = _Module(target)
                        else:
                            original_parent = self.module_path(alias.name.split('.')[0], path)
                            value = _Module(original_parent) if original_parent else _Unsupported('namespace_import_binding_unsupported', path, node)
                    env[bound] = (value if value is not None else _External(module + "." + alias.name, True)
                                  if target in self.unavailable_modules else _Unsupported("source_export_rule_unsupported", path, node))
                else:
                    name = module + "." + alias.name if isinstance(node, ast.ImportFrom) else module
                    env[bound] = _External(name, self.local_module(module, path, getattr(node, "level", 0)))
                    if setup and module != '__future__':
                        kernel = stdlib_import_fact(module,alias.name)
                        if module == 'abc' and not getattr(node, 'level', 0):
                            from seampact.source.abc_kernel import check_abc_kernel
                            kernel = check_abc_kernel()
                        if (self.call_context is not None and not getattr(node, 'level', 0)
                                and self.sdk_source.unmodified(name)):
                            kernel = sdk_import_requirement(self,path,node,module,
                                alias.name if isinstance(node, ast.ImportFrom) else None) or kernel
                        self.activation(path, node, 'EXTERNAL_IMPORT',
                            None if kernel and kernel['status'] == 'CHECKED' else 'original external import/attribute activation effects are not proved',
                            module=module, imported=alias.name, **(dict(kernel=kernel) if kernel else {}))
                        if kernel is not None and kernel.get('schema')==STDLIB_PREFIX_RULE:
                            self.protocol(STDLIB_PREFIX_RULE,path,node,target_requirements=kernel)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = self.value(node.value, env, path, scope, setup)
            evaluated = self.annotation_target(node, env, path, scope, setup) if isinstance(node, ast.AnnAssign) else None
            if isinstance(value, (_FiniteIteration, SDKIteration, SourceGenerator, BoundedIteration, ConfigurationIteration)):
                self.negative_gap(path, node, 'per-call finite iterable escapes immediate consumption')
            if setup and isinstance(node, ast.Assign) and any(not self.local_binding_target(target) for target in node.targets):
                self.activation(path, node, 'SETUP_TARGET_STORE', 'original non-name activation assignment/store effects are not proved')
            if not isinstance(node, ast.AnnAssign) or node.value is not None:
                for target in node.targets if isinstance(node, ast.Assign) else [node.target]:
                    self.bind(target, value, env, path, scope, evaluated=evaluated)
            if isinstance(node, ast.AnnAssign) and setup:
                self.annotation_expression(node.annotation, env, path, scope)
        elif isinstance(node, ast.AugAssign):
            left, right = self.value(node.target, env, path, scope), self.value(node.value, env, path, scope)
            if (self.call_context is not None and not self.call_context.compiling and type(left) is int
                    and isinstance(node.target, ast.Name) and isinstance(node.op, (ast.Add, ast.Sub))):
                result = None
                if self.ordinary_value_operation('int'):
                    try:
                        result = finite_integer('add' if isinstance(node.op, ast.Add) else 'subtract', [left, right])
                    except CallUnknown:
                        pass
                if result is None:
                    self.negative_gap(path, node, 'per-call integer operation binding or finite operands/result are UNKNOWN')
                self.bind(node.target, result, env, path, scope)
            elif self.call_context is not None and not self.call_context.compiling and type(left) is list:
                if (isinstance(node.target, ast.Name) and isinstance(node.op, ast.Add) and type(right) is list
                        and self.call_plain(left) and self.call_plain(right) and len(left) + len(right) <= 64):
                    left.extend(right)  # Preserve the original list += aliases.
                    self.bind(node.target, left, env, path, scope)
                else:
                    self.negative_gap(path, node, 'per-call augmented list mutation is UNKNOWN')
                    self.invalidate_mutable(left)
            else:
                self.bind(node.target, left + right if isinstance(node.op, ast.Add) and isinstance(left, list) and isinstance(right, list) else None, env, path, scope)
        elif isinstance(node, ast.Return):
            result = self.value(node.value, env, path, scope)
            if isinstance(result, (_FiniteIteration, SDKIteration, SourceGenerator, BoundedIteration, ConfigurationIteration)):
                self.negative_gap(path, node, 'per-call finite iterable escapes immediate consumption')
            if (self.registration_escapes(result) and self.registration_frames
                    and self.registration_frames[-1]['public_entry']):
                self.negative_gap(path, node, 'finite class reference or collection escapes the checked synchronous source call')
            if self.result_observer is not None:
                self.result_observer.return_value(node, path)
            return True, result
        elif isinstance(node, ast.Raise):
            self.value(node.exc, env, path, scope)
            return True, None
        elif isinstance(node, ast.Assert):
            self.value(node.test, env, path, scope)
            self.value(node.msg, env, path, scope)
        elif isinstance(node, (ast.Break, ast.Continue)):
            return True, None
        elif isinstance(node, ast.Delete):
            for target in node.targets:
                value = (env.get(target.id) if isinstance(target, ast.Name) else
                         self.value(target.value, env, path, scope) if isinstance(target, (ast.Attribute, ast.Subscript)) else None)
                self.invalidate_configuration_streams(value)
            self.negative_gap(path, node, "statement is outside complete negative call coverage: Delete")
        elif isinstance(node, ast.If):
            truth = self.conditional_truth(node.test, env, path, scope)
            if truth is not None:
                self.protocol("source-known-branch/1", path, node.test,
                    expression=ast.unparse(node.test), selected="body" if truth else "orelse",
                    ast_sha256=hashlib.sha256(ast.dump(node.test, include_attributes=False).encode()).hexdigest())
                # This is the original sole path, so writes must reach its
                # caller's exact abstract receiver and aliases.
                self.branch_stack.append(dict(status="CHECKED", path=path, line=node.test.lineno,
                    expression=ast.unparse(node.test), selected="body" if truth else "orelse"))
                try:
                    return self.block(node.body if truth else node.orelse, env, path, scope)
                finally:
                    self.branch_stack.pop()
            branches = [node.body, node.orelse]
            results, states = [], []
            for branch in branches:
                local = _branch_environment(env)
                self.branch_stack.append(dict(status="UNKNOWN", path=path, line=node.test.lineno,
                    expression=ast.unparse(node.test), selected="body" if branch is node.body else "orelse"))
                try:
                    results.append(self.block(branch, local, path, scope))
                finally:
                    self.branch_stack.pop()
                states.append(local)
            env.update(_join_branches(env, states, path, node))
            if results and all(r[0] for r in results):
                values = [r[1] for r in results]
                return True, values[0] if all(v == values[0] for v in values) else None
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            values = self.value(node.iter, env, path, scope)
            if type(values) is BoundedIteration:
                from seampact.source.sdk_bounded_flow import consume_normal_for
                try:
                    consumed = consume_normal_for(self,node,values,env,path,scope)
                    if consumed is not None:
                        return consumed
                except bounded.BoundedUnknown as error:
                    self.negative_gap(path,node,'normal bounded For remains UNKNOWN: ' + str(error))
            configured = configuration_loop(self, node, values, env, path, scope)
            if configured is not CONFIG_UNHANDLED:
                return configured
            selection = self.registration_selection(node, values, env, path) if isinstance(node, ast.For) else None
            if selection is not None:
                return selection
            if type(values) is _ClassSet or type(values) is _ClassSequence and not values.ordered:
                self.negative_gap(path, node, 'native class set iteration order or loop effects are not closed')
            actual = self.call_context is not None and not self.call_context.compiling
            items = self.iteration_items(node.iter, values) if actual and isinstance(node, ast.For) else None
            if actual and items is None:
                self.negative_gap(path, node, 'per-call loop iterable is UNKNOWN')
            if isinstance(values, _ConfigurationStream):
                values.invalid = True
            if isinstance(values, _Instance) and values.local_class is not None:
                self.negative_gap(path, node, "implicit source iteration protocol is not traced")
            if actual and items is not None:
                stamp = self.iteration_stamp(values)
                if items and any(isinstance(child, (ast.Break, ast.Continue, ast.Return, ast.Raise, ast.Yield, ast.YieldFrom))
                                 for statement in node.body for child in ast.walk(statement)):
                    self.negative_gap(path, node, 'per-call loop control is outside finite value propagation')
                for value in items:
                    self.bind(node.target, value, env, path, scope)
                    self.potential_block(node.body, env, path, scope, node)
                    if self.iteration_stamp(values) != stamp:
                        self.negative_gap(path, node, 'per-call loop iterable changed during consumption')
                        break
                if type(values) is SourceGenerator and values.failure is not None:
                    self.bind(node.target, None, env, path, scope)
                    self.potential_block(node.body, env, path, scope, node)
            elif not actual and (isinstance(values, list) and len(values) <= 64
                                or type(values) is _ClassSequence and values.ordered):
                if type(values) is _ClassSequence and any(isinstance(child, (ast.Break, ast.Continue, ast.Return, ast.Raise, ast.Yield, ast.YieldFrom))
                        for statement in node.body for child in ast.walk(statement)):
                    self.negative_gap(path, node, 'ordered finite class iteration has unsupported control effects')
                for value in values.items if type(values) is _ClassSequence else values:
                    self.bind(node.target, value, env, path, scope)
                    self.potential_block(node.body, env, path, scope, node)
            else:
                self.bind(node.target, None, env, path, scope)
                self.potential_block(node.body, env, path, scope, node)
            self.potential_block(node.orelse, env, path, scope, node)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            streams = []
            for item in node.items:
                value = self.value(item.context_expr, env, path, scope)
                if isinstance(value, _ConfigurationStream):
                    if isinstance(node, ast.AsyncWith) or value.closed or value.active:
                        value.invalid = True
                    else:
                        value.active = True
                    streams.append(value)
                if isinstance(value, _Instance) and value.local_class is not None:
                    self.negative_gap(path, node, "implicit source context manager protocol is not traced")
                if item.optional_vars:
                    self.bind(item.optional_vars, value, env, path, scope)
            try:
                return self.block(node.body, env, path, scope)
            finally:
                for stream in streams:
                    stream.active, stream.closed = False, True
        elif isinstance(node, ast.Try):
            self.negative_gap(path, node, "handled exception continuation is not a complete negative call proof")
            result = self.potential_block(node.body, env, path, scope, node)
            for handler in node.handlers:
                local = env.copy()
                if handler.type is not None:
                    self.branch_stack.append(dict(status='UNKNOWN', path=path, line=handler.lineno,
                        expression='exception matching', selected='possible handler type evaluation'))
                    try:
                        self.value(handler.type, local, path, scope)
                    finally:
                        self.branch_stack.pop()
                    fact = self.sdk_source.type_reference(path, handler, handler.type)
                    self.protocol(fact['rule'], path, handler, type_reference=fact)
                if handler.name:
                    local[handler.name] = _Instance('<original-exception>()', {})
                self.potential_block(handler.body, local, path, scope, node)
            self.potential_block(node.orelse, env, path, scope, node)
            self.potential_block(node.finalbody, env, path, scope, node)
            return result
        elif isinstance(node, ast.While):
            if self.conditional_truth(node.test, env, path, scope) is not False:
                self.potential_block(node.body, env, path, scope, node)
        elif isinstance(node, ast.Expr):
            self.value(node.value, env, path, scope, setup)
        elif not isinstance(node, (ast.Pass, ast.Global, ast.Nonlocal)):
            self.negative_gap(path, node, "statement is outside complete negative call coverage: " + type(node).__name__)
        return False, None

    def block(self, statements, env, path, scope):
        for node in statements:
            if self.steps > 16000:
                return False, None
            result = self.statement(node, env, path, scope)
            if result[0]:
                return result
        return False, None

    def invoke(self, target, args, kwargs, path, scope, node, public_entry=False, result_sink=None):
        edge = dict(source_path=path, source_symbol=scope, target_path=target.path,
                    target_symbol=target.name, evidence=[_cite(path, node)])
        if edge not in self.calls:
            self.calls.append(edge)
        occurrence = dict(source_path=path, source_symbol=scope, call_site=self.invoked_site(node),
                          target_path=target.path, target_symbol=target.name)
        if occurrence not in self.invoked_calls:
            self.invoked_calls.append(occurrence)
        site = dict(path=target.path, unit="file", start=target.node.lineno, end=target.node.end_lineno,
            symbol=target.name, ast_sha256=hashlib.sha256(ast.dump(target.node, include_attributes=False).encode()).hexdigest())
        if site not in self.callable_sites:
            self.callable_sites.append(site)
        if target.node.decorator_list and target.method_kind not in {"staticmethod", "classmethod"}:
            module = self.module(target.path)
            decorators = [self.value(d, module, target.path, target.name, setup=True) for d in target.node.decorator_list]
            if (any(not isinstance(d, _External) or d.name != "contextlib.contextmanager" for d in decorators)
                    and not self.no_grad_wrapper(target)):
                self.unknown_target(path, node, target.name, "decorated_call_target_unresolved")
                return None
        base_key = (target.path, target.name)
        self.visited.add(base_key)
        classes = tuple(value.path + ':' + value.name for value in [*args, *kwargs.values()] if type(value) is _Class)
        key = (*base_key, 'finite-class-state', classes) if classes else base_key
        if key in self.active:
            self.unknown_target(path, node, target.name, "finite_recursive_call")
            return None
        if len(self.active) >= 16:
            self.unknown_target(path, node, target.name, "finite_call_depth")
            return None
        self.active.add(key)
        local = self.module(target.path).copy()
        if target.closure is not None:
            local.update(target.closure)
        # Python resolves a function's own locals lexically, including names
        # assigned after a call. Do not fall through to a stale global/capture.
        assigned, declarations = set(), {}
        def bindings(statements):
            for child in statements:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    assigned.add(child.name)
                    continue
                if isinstance(child, (ast.Global, ast.Nonlocal)):
                    declarations.update((name, child) for name in child.names)
                elif isinstance(child, ast.Name) and isinstance(child.ctx, (ast.Store, ast.Del)):
                    assigned.add(child.id)
                elif isinstance(child, (ast.Import, ast.ImportFrom)):
                    assigned.update(a.asname or (a.name if isinstance(child, ast.ImportFrom) else a.name.split('.')[0]) for a in child.names)
                bindings(list(ast.iter_child_nodes(child)))
        bindings(target.node.body)
        for name in assigned - declarations.keys():
            local[name] = None
        path_globals = set()
        for name in assigned & declarations.keys():
            declaration = declarations[name]
            lazy = (self.sdk_source.lazy_guard(target.path, target.node, name)
                    if isinstance(declaration, ast.Global) else None)
            factory = self.spacy_factory(lazy[2], local, target.path, target.name) if lazy else None
            if factory is not None:
                # The unique original guard establishes this invariant before
                # any later read. Analyse both retained/initializing paths with
                # the same documented external callable result.
                local[name] = factory
                self.protocol("module-lazy-external-singleton/1", target.path, lazy[1], variable=name,
                              initialization=[_cite(target.path, lazy[0]), _cite(target.path, lazy[2])],
                              factory=factory.factory, resource=factory.resource)
                continue
            if isinstance(declaration, ast.Global) and isinstance(self.module(target.path).get(name), _Path):
                path_globals.add(name)
                continue
            reason = "lexical_binding_mutation_unsupported"
            self.unknown_target(target.path, declaration, name, reason)
            marker = _Unsupported(reason, target.path, declaration)
            local[name] = marker
            # Do not let a later closure call recover a pre-mutation binding.
            frame = target.closure if isinstance(declaration, ast.Nonlocal) else self.module(target.path)
            if frame is not None:
                frame[name] = marker
        if target.owner is not None:
            local["<method_class>"] = target.owner
            local["<method_self>"] = args[0] if args else None
        parameters = target.node.args.posonlyargs + target.node.args.args
        defaults = target.defaults or {}
        for index, parameter in enumerate(parameters):
            local[parameter.arg] = (args[index] if index < len(args) else kwargs[parameter.arg] if parameter.arg in kwargs
                                    else None if public_entry or None in kwargs else defaults.get(parameter.arg))
        for parameter in target.node.args.kwonlyargs:
            local[parameter.arg] = (kwargs[parameter.arg] if parameter.arg in kwargs else
                                    None if public_entry or None in kwargs else defaults.get(parameter.arg))
        if public_entry and self.call_context is not None and not self.call_context.compiling:
            for parameter in parameters + target.node.args.kwonlyargs:
                if parameter.arg not in kwargs and parameter.arg not in {'self', 'cls'}:
                    if parameter.arg in defaults:
                        # Defaults were evaluated from source by the existing
                        # tracer. Mutable/dynamic defaults are not call facts.
                        value = defaults[parameter.arg]
                        if isinstance(value, _LiteralNone) or type(value) in (str, int, float, bool):
                            local[parameter.arg] = value
                        else:
                            self.negative_gap(target.path, target.node, 'omitted original default is not a proved immutable value')
        if target.node.args.vararg:
            local[target.node.args.vararg.arg] = None if public_entry or None in kwargs else args[len(parameters):]
        if target.node.args.kwarg:
            names = {p.arg for p in parameters + target.node.args.kwonlyargs}
            local[target.node.args.kwarg.arg] = {k: v for k, v in kwargs.items() if k not in names}
            if public_entry:
                local[target.node.args.kwarg.arg][None] = None
        parents = [frame for frame in self.registration_frames if frame['base_key'] == base_key]
        if parents:
            try:
                previous = self.registration_state(target, parents[-1]['local'], path, node)
                current = self.registration_state(target, local, path, node)
                if (previous is None or current is None or previous[2] != current[2]
                        or tuple(name for name, _ in previous[1]) != tuple(name for name, _ in current[1])
                        or previous[0].requirements()['digest'] != current[0].requirements()['digest']
                        or not current[0].strict_descent(tuple(identity for _, identity in previous[1]),
                                                       tuple(identity for _, identity in current[1]))):
                    raise RegistrationUnknown('recursive class arguments do not strictly descend in one complete finite world')
            except RegistrationUnknown as error:
                self.unknown_target(path, node, target.name, 'finite_recursive_class_state_unknown')
                self.negative_gap(path, node, str(error))
                self.leave_active(key)
                return None
        self.registration_frames.append(dict(key=key, base_key=base_key, local=local, public_entry=public_entry,target=target))
        if self.call_context is not None and not self.call_context.compiling and not public_entry:
            from seampact.source.sdk_bounded_flow import try_flow, RULE as BOUNDED_FLOW_RULE, TARGET_SCHEMA
            names = [parameter.arg for parameter in parameters]
            keyword_names = {parameter.arg for parameter in target.node.args.kwonlyargs}
            omitted_defaults = [(name, local.get(name)) for name in [*names[len(args):], *sorted(keyword_names)]
                                if name not in kwargs and name in defaults]
            bound_domain = any(self.bounded_value(value) for value in [*args, *kwargs.values()])
            invalid_binding = (len(args) > len(parameters) or None in kwargs
                or any(name not in set(names) | keyword_names for name in kwargs)
                or any(name in kwargs for name in names[:len(args)])
                or any(parameter.arg in kwargs for parameter in target.node.args.posonlyargs)
                or any(local.get(parameter.arg) is None for parameter in parameters + target.node.args.kwonlyargs)
                or any(not (type(item) is _LiteralNone or type(item) in (str, int, float, bool))
                       for _, item in omitted_defaults))
            if not invalid_binding:
                enclosing_bounded_flow = self.in_bounded_flow_attempt
                self.in_bounded_flow_attempt = True
                try:
                    flow = try_flow(self, target, local, node, omitted_defaults)
                    if flow is not None:
                        for requirement in flow.requirements:
                            if requirement.get('schema') == TARGET_SCHEMA:
                                self.protocol(BOUNDED_FLOW_RULE, target.path, target.node,
                                    target_requirements=requirement,
                                    runtime='original operations and resume order retained; no representative values executed')
                        self.leave_active(key)
                        return flow.value
                except bounded.BoundedUnknown as error:
                    if bound_domain and enclosing_bounded_flow:
                        self.negative_gap(target.path, target.node, 'bounded source flow remains UNKNOWN: ' + str(error))
                        self.leave_active(key)
                        return None
                    # Mixed ordinary/native source functions retain the normal
                    # interpreter. The attempted finite helper flow can only
                    # update fresh analysis locals; nested attempts cannot
                    # fall back and mutate an original source owner here.
                finally:
                    self.in_bounded_flow_attempt = enclosing_bounded_flow
            elif bound_domain:
                self.negative_gap(target.path, target.node, 'bounded source flow original binding remains UNKNOWN')
                self.leave_active(key)
                return None
        if (self.call_context is not None and not self.call_context.compiling and is_generator(target.node)):
            self.leave_active(key)
            names = [parameter.arg for parameter in parameters]
            keyword_names = {parameter.arg for parameter in target.node.args.kwonlyargs}
            omitted_defaults = [(name, local.get(name)) for name in [*names[len(args):], *sorted(keyword_names)]
                                if name not in kwargs and name in defaults]
            invalid = (not isinstance(target.node, ast.FunctionDef) or public_entry
                       or target.node.decorator_list or target.node.args.vararg or target.node.args.kwarg
                       or target.node.returns is not None
                       or any(parameter.annotation is not None for parameter in parameters + target.node.args.kwonlyargs)
                       or len(args) > len(parameters) or None in kwargs
                       or any(name not in set(names) | keyword_names for name in kwargs)
                       or any(name in kwargs for name in names[:len(args)])
                       or any(parameter.arg in kwargs for parameter in target.node.args.posonlyargs)
                       or any(local.get(parameter.arg) is None for parameter in parameters + target.node.args.kwonlyargs)
                       or any(not (type(item) is _LiteralNone or type(item) in (str, int, float, bool)) for _, item in omitted_defaults)
                       or any(not self.call_plain(local.get(parameter.arg))
                              and not (index == 0 and target.owner is not None and self.generator_fields(local.get(parameter.arg)) is not None)
                              for index, parameter in enumerate(parameters + target.node.args.kwonlyargs)))
            if invalid:
                self.negative_gap(target.path, target.node, 'per-call source generator original binding is UNKNOWN')
                return None
            loaded = {child.id for child in own_nodes(target.node) if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load)}
            sources = [local[parameter.arg] for index, parameter in enumerate(parameters + target.node.args.kwonlyargs)
                       if not (index == 0 and target.owner is not None and self.generator_fields(local[parameter.arg]) is not None)]
            watches = []
            for name in loaded:
                value = local.get(name)
                owner = self.generator_fields(value) is not None
                if name in set(names) | keyword_names:
                    continue
                elif name not in assigned and value is not None:
                    mapping = target.closure if target.closure is not None and name in target.closure else self.module(target.path)
                    watches.append((mapping, name, ('identity', value) if owner else self.iteration_stamp(value)))
            return SourceGenerator(target, local, id(node), tuple(sources), tuple(watches), tuple(omitted_defaults))
        state = self.call_state(target, args, kwargs, public_entry)
        if self.result_observer is None and state in self.call_summaries:
            self.summary_hits += 1
            self.leave_active(key)
            return self.call_summaries[state]
        frame = self.result_observer.begin(target) if self.result_observer is not None else None
        try:
            terminated, result = self.block(target.node.body, local, target.path, target.name)
            if self.result_observer is not None:
                proof = self.result_observer.finish(frame, terminated)
                if result_sink is not None:
                    result_sink["proof"] = proof
            for name in path_globals:
                value = local.get(name)
                if isinstance(value, _Path):
                    self.module(target.path)[name] = value
                    self.protocol("source-global-path-assignment/1", target.path, declarations[name], variable=name,
                                  value=value.value, precision="known" if value.value is not None else "unknown path")
                else:
                    self.module(target.path)[name] = _Unsupported("global_path_assignment_unsupported", target.path, declarations[name])
                    self.unknown_target(target.path, declarations[name], name, "global_path_assignment_unsupported")
            if self.immutable_result(result) and state == self.call_state(target, args, kwargs, public_entry):
                self.call_summaries[state] = result
            return result
        finally:
            if self.result_observer is not None:
                self.result_observer.discard(frame)
            self.leave_active(key)

    def process(self, path, scope, node, argv, kwargs):
        target = None
        cwd_value = kwargs.get("cwd")
        cwd_node = next((k.value for k in node.keywords if k.arg == 'cwd'), None)
        if isinstance(cwd_value, _Path):
            cwd = cwd_value.value
        elif isinstance(cwd_value, str) and self.cwd is not None:
            cwd = posixpath.normpath(posixpath.join(self.cwd, cwd_value))
        else:
            cwd = self.cwd if "cwd" not in kwargs or isinstance(cwd_node, ast.Constant) and cwd_node.value is None else None
        if cwd is not None and (cwd.startswith('/') or cwd == '..' or cwd.startswith('../') or ':' in cwd or '\\' in cwd):
            cwd = None
        if isinstance(argv, list) and ("shell" not in kwargs or kwargs["shell"] is False):
            words = [v.value if isinstance(v, _Path) else v for v in argv]
            # Python script or module invocation, without shell interpretation.
            if words and isinstance(words[0], str) and (PurePosixPath(words[0]).name.startswith("python") or words[0] == "<python>"):
                if len(words) > 2 and words[1] == "-m" and isinstance(words[2], str):
                    target = self.module_path(words[2], path)
                elif len(words) > 1 and isinstance(words[1], str) and words[1].endswith(".py"):
                    target = posixpath.normpath(words[1]) if isinstance(argv[1], _Path) else posixpath.normpath(posixpath.join(cwd, words[1])) if cwd is not None else None
            elif words and isinstance(words[0], str) and words[0].endswith(".py"):
                target = posixpath.normpath(words[0]) if isinstance(argv[0], _Path) else posixpath.normpath(posixpath.join(cwd, words[0])) if cwd is not None else None
        if target is not None and (target.startswith('/') or target == '..' or target.startswith('../') or ':' in target or '\\' in target):
            target = None
        env_node = next((k.value for k in node.keywords if k.arg == "env"), None)
        edge = dict(source_path=path, source_symbol=scope, target_path=target,
                    cwd=cwd,
                    argv=[v if isinstance(v, (str, int, float, bool)) else None for v in argv] if isinstance(argv, list) else None,
                    environment=dict(mode="inherit" if env_node is None else "explicit", expression=ast.unparse(env_node) if env_node else None),
                    evidence=[_cite(path, node, "subprocess")])
        if edge not in self.processes:
            self.processes.append(edge)
        if target is None:
            self.unknown_target(path, node, ast.unparse(node), "subprocess_target_unresolved")
        elif target not in self.inspector.files:
            self.unknown_target(path, node, target, "subprocess_source_unavailable")
        else:
            self.script(target, cwd)

    def script(self, path, cwd):
        key = (path, "__main__")
        if (path, cwd) in self.script_contexts:
            return
        self.script_contexts.add((path, cwd))
        self.visited.add(key)
        previous = self.cwd
        self.cwd = cwd
        try:
            env = self.module(path).copy()
            env["__name__"] = "__main__"
            if path in self.unavailable_modules:
                return
            self.block(self.tree(path).body, env, path, "__main__")
        finally:
            self.cwd = previous

    def entry(self, entry):
        self.calls, self.unknown, self.processes, self.invoked_calls, self.visited = [], [], [], [], set()
        self.modules, self.import_roots, self.steps, self.active = {}, {}, 0, set()
        self.unavailable_modules = set()
        self.cwd, self.script_contexts = '.', set()
        self.protocols = []
        self.call_summaries = {}
        self.summary_hits = 0
        self.external_calls, self.branch_stack = [], []
        self.negative_scope_gaps = []
        self.callable_sites, self.import_sites = [], []
        self.configuration_conditions = []
        self.sdk_source = ExternalSourceProtocols(self.inspector)
        self.inheritance_rejection = None
        self.activation_events, self.activation_gaps = [], []
        self.module_progress = {}
        self.entry_receiver = None
        self.registration_entry = entry
        self.registration_worlds, self.registration_frames = {}, []
        self.registration_world_failures = {}
        self.result_output = {}
        self.sdk_source.bind_activation(entry,self.candidate,checker=self)
        if self.result_observer is not None:
            self.result_observer.reset(self)
        binding_facts = None
        path, symbol = entry.get("path"), entry.get("symbol", "__main__")
        def not_covered(reason):
            return dict(entry_id=entry["id"], status="NOT_COVERED", scope_covered=False,
                        inheritance_rejection=self.inheritance_rejection,
                        activation_prefix=self.activation_prefix(),
                        activation_scope=self.sdk_source.activation_diagnostic(),
                        reachable_entries=[], dependency_ids=[], missing_dependency_ids=[], call_edges=[], process_edges=[],
                        evidence=[], unresolved_targets=[dict(target=symbol, reason=reason, evidence=[])], issues=[])
        if path not in self.inspector.files or not path.endswith(".py"):
            return not_covered("entry_source_scope_unsupported")
        if entry.get("kind") == "cli":
            self.script(path, process_context(self.inspector, entry)['cwd'])
        elif entry.get("kind") == "method" and isinstance(entry.get("constructor"), dict):
            parts = symbol.split(".")
            if len(parts) != 2 or entry["constructor"].get("symbol") != parts[0]:
                return not_covered("native_receiver_binding_unsupported")
            resolver = FrozenInheritance(self.inspector)
            def entry_wrapper(method):
                cls = self.module(path).get(parts[0])
                target = self.class_attribute(cls, parts[1]) if isinstance(cls, _Class) else None
                return (isinstance(target, _Function) and target.path == method.path
                        and target.name == method.symbol and self.no_grad_wrapper(target))
            try:
                chain, definition = resolver.resolve(path, symbol, wrapper_check=entry_wrapper)
            except InheritanceIssue as error:
                self.inheritance_rejection = copy.deepcopy(getattr(error, 'facts', None))
                location = next(iter(self.tree(path).body), ast.Pass(lineno=1, end_lineno=1))
                self.unknown_target(path, location, symbol, "native_receiver_source_binding_unsupported")
            else:
                binding_facts = resolver.facts(chain, definition)
                cls = self.module(path).get(parts[0])
                if not isinstance(cls, _Class):
                    self.unknown_target(path, chain[0].node, parts[0], "native_receiver_class_binding_unsupported")
                else:
                    # Inspect the original constructor and keep this exact
                    # abstract receiver for inherited self/super dispatch.
                    receiver = self.construct(cls, [], _configuration_value(entry["constructor"].get("arguments", {})),
                                              path, "<entry-construction>", chain[0].node)
                    if isinstance(receiver, _Instance):
                        self.entry_receiver = receiver
                        for initializer in entry.get("initializers", []):
                            name = initializer["symbol"].split(".")[-1]
                            initial = self.class_attribute(cls, name)
                            if isinstance(initial, _Function):
                                self.invoke(initial, [receiver], _configuration_value(initializer.get("arguments", {})),
                                            initial.path, "<entry-initialization>", initial.node)
                            else:
                                self.unknown_target(path, chain[0].node, name, "native_initializer_binding_unsupported")
                        target = self.class_attribute(cls, parts[1])
                        if (isinstance(target, _Function) and target.path == definition.path
                                and target.name == definition.symbol and (target.method_kind == "instance"
                                    or target.method_kind == "unsupported" and self.no_grad_wrapper(target))):
                            if self.result_observer is not None:
                                self.result_observer.prepare_entry(receiver)
                            arguments = dict(entry.get('fixed_arguments', {}))
                            if self.call_context is not None and not self.call_context.compiling:
                                arguments.update(self.call_context.values)
                            if not (self.call_context is not None and self.call_context.initialization_only):
                                self.invoke(target, [receiver], _configuration_value(arguments),
                                            target.path, "<entry>", target.node, public_entry=True, result_sink=self.result_output)
                        else:
                            self.unknown_target(path, chain[0].node, symbol, "native_method_effective_binding_unsupported")
        else:
            target = self.module(path).get(symbol)
            if isinstance(target, _Function):
                if self.result_observer is not None:
                    self.result_observer.prepare_entry(None)
                arguments = dict(entry.get('fixed_arguments', {}))
                if self.call_context is not None and not self.call_context.compiling:
                    arguments.update(self.call_context.values)
                if not (self.call_context is not None and self.call_context.initialization_only):
                    self.invoke(target, [], _configuration_value(arguments), path, "<entry>", target.node,
                                public_entry=True, result_sink=self.result_output)
            else:
                return not_covered("callable_scope_unsupported")
        reachable = []
        for other in self.candidate.get("entries", []):
            if not isinstance(other, dict):
                continue
            key = (other.get("path"), "__main__" if other.get("kind") == "cli" else other.get("symbol"))
            if other.get("id") != entry.get("id") and key in self.visited:
                reachable.append(other["id"])
        dependencies, missing = [], []
        for dep in self.candidate.get("dependencies", []):
            if set(dep.get("entry_ids", [])) & set(reachable):
                dependencies.append(dep["id"])
                if entry["id"] not in dep.get("entry_ids", []):
                    missing.append(dep["id"])
        evidence = [item for edge in self.calls + self.processes for item in edge["evidence"]]
        issues = []
        for identity in missing:
            index = next(i for i, d in enumerate(self.candidate["dependencies"]) if d["id"] == identity)
            issues.append(dict(issue_code="host_dependency_missing", entry_id=entry["id"], dependency_id=identity,
                field_paths=[f"dependencies[{index}].entry_ids"], evidence=evidence,
                reason="Original host reaches an entry with this discovered dependency; host membership and mapping are missing.",
                repair_action="Include this host in the affected dependency and prove its original parameter/environment transfer."))
        for world in self.registration_worlds.values():
            if not world.accepts_prefix(self.activation_prefix()):
                location = next(iter(self.tree(path).body), ast.Pass(lineno=1, end_lineno=1))
                self.negative_gap(path, location, 'class read/use call changes its complete original creation/import prefix')
        descriptions = {
            "dependency_closure_unresolved": ("Called local code or original subprocess target lacks frozen source.",
                "Provide the specifically missing frozen source or retain an unresolved host scope."),
            "dependency_closure_unsupported": ("Available source uses a call or binding outside the finite dependency rules.",
                "Resolve the cited unsupported rule or retain unknown scope; rereading available source cannot establish closure."),
            "dependency_closure_limit": ("Dependency investigation reached its finite analysis bound.",
                "Retain unknown scope and inspect the cited bound; do not request repeated source reads or infer closure.")}
        for code, (reason, repair) in descriptions.items():
            targets = [u for u in self.unknown if u["issue_code"] == code]
            if targets:
                issues.append(dict(issue_code=code, scope="source" if code == "dependency_closure_unresolved" else "rule",
                    repair_scope="evidence" if code == "dependency_closure_unresolved" else "rule",
                    model_repairable=code == "dependency_closure_unresolved",
                    entry_id=entry["id"], field_paths=["entries"], evidence=[e for u in targets for e in u["evidence"]],
                    reason=reason, repair_action=repair))
        return dict(entry_id=entry["id"], status="MISSING" if issues else "CHECKED", scope_covered=True,
                    analysis_steps=self.steps, reused_call_summaries=self.summary_hits,
                    inheritance_binding=binding_facts,
                    inheritance_rejection=self.inheritance_rejection,
                    activation_prefix=self.activation_prefix(),
                    activation_scope=self.sdk_source.activation_diagnostic(),
                    external_protocols=self.protocols,
                    configuration_conditions=self.configuration_conditions,
                    type_reference_facts=self.sdk_source.type_reference_facts,
                    reachable_entries=reachable, dependency_ids=dependencies, missing_dependency_ids=missing,
                    call_edges=self.calls, process_edges=self.processes, evidence=evidence,
                    unresolved_targets=self.unknown, issues=issues)


def source_import_scope(inspector, entry, candidate=None):
    """Exclude only imports in exact unvisited ordinary callable bodies.

    Module/class initialization and imports inside visited callables remain
    requirements even when a particular branch import was not observed. An
    incomplete trace, unknown implicit protocol or B-CALL prefix never provides
    a negative import proof. This changes neither source nor dependency specs.
    """
    checker = _Closure(inspector, candidate or dict(entries=[entry], dependencies=[]))
    trace = checker.entry(entry)
    closed = (trace["status"] == "CHECKED" and not trace.get("unresolved_targets")
              and not checker.negative_scope_gaps and not entry.get("binding_program")
              and checker.activation_prefix()['status'] == 'CHECKED')
    visited = {(site["path"], site["start"], site["end"], site["ast_sha256"]) for site in checker.callable_sites}
    observed = {(site["path"], site["start"], site["end"], site["ast_sha256"]) for site in checker.import_sites}
    sites, unclassified = [], []
    for path in inspector.files:
        if not path.endswith(".py"):
            continue
        try:
            tree = checker.tree(path)
        except (ValueError, KeyError, OSError, SyntaxError):
            unclassified.append(path)
            continue  # No import in an unclassified file is excluded.
        parents = {id(child): parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            owner, current, child = None, parents.get(id(node)), node
            source_scope = "MODULE_INITIALIZATION"
            while current is not None:
                if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if child in current.body:
                        source_scope = "CALLABLE_BODY"
                        owner = dict(path=path, start=current.lineno, end=current.end_lineno,
                            ast_sha256=hashlib.sha256(ast.dump(current, include_attributes=False).encode()).hexdigest())
                    else:
                        source_scope = "DEFINITION_TIME_EXPRESSION"
                    break
                if isinstance(current, ast.ClassDef):
                    source_scope = "CLASS_INITIALIZATION"
                    break
                child, current = current, parents.get(id(current))
            owner_key = (owner["path"], owner["start"], owner["end"], owner["ast_sha256"]) if owner else None
            ast_sha = hashlib.sha256(ast.dump(node, include_attributes=False).encode()).hexdigest()
            status = "NOT_REACHED" if closed and owner is not None and owner_key not in visited else "RETAIN"
            if not closed:
                status = "UNKNOWN"
            originals = ([dict(module=alias.name, names=[], level=0, kind="import") for alias in node.names]
                         if isinstance(node, ast.Import) else [dict(module=node.module,
                            names=[alias.name for alias in node.names], level=node.level, kind="from")])
            sites.extend(dict(path=path, unit="file", line=node.lineno, end=node.end_lineno,
                source_sha256=inspector.files[path]["sha256"], ast_sha256=ast_sha,
                status=status, source_scope=source_scope, owner=owner,
                execution_observed=(path, node.lineno, node.end_lineno, ast_sha) in observed, **original)
                for original in originals)
    return dict(schema="source-import-scope/1", rule="unvisited-source-callable-imports/1",
        entry_id=entry.get("id"), source_version=inspector.request.get("source_version"),
        status="CHECKED" if closed else "UNKNOWN", trace_status=trace["status"],
        negative_scope_status="UNKNOWN" if checker.negative_scope_gaps else "CHECKED",
        negative_scope_gaps=checker.negative_scope_gaps, unresolved_targets=trace["unresolved_targets"],
        activation_prefix=checker.activation_prefix(),
        visited_callables=checker.callable_sites, observed_imports=checker.import_sites,
        sites=sites, unclassified_sources=unclassified,
        scope="one exact frozen entry/configuration; only unvisited callable-body imports may be excluded; no install, source execution or lifecycle permission")


def resolve_resource_calls(inspector, entry, candidate=None):
    """Ephemeral literal call values from the same finite source tracer.

    No dependency checker is called, no source/SDK is imported, and no loader
    effects or permission are inferred. Callers must select the exact external
    loader protocol and retain UNKNOWN path/argument obligations. These values
    are not copied into the ordinary public dependency report.
    """
    checker = _Closure(inspector, candidate or dict(entries=[entry], dependencies=[]))
    result = checker.entry(entry)
    return dict(schema="source-resource-call-values/1", entry_id=entry["id"],
        status=result["status"], calls=checker.external_calls,
        inheritance_rejection=result.get('inheritance_rejection'),
        activation_prefix=result.get('activation_prefix'),
        activation_scope=result.get('activation_scope'),
        external_protocols=result.get('external_protocols', []),
        invoked_callables=copy.deepcopy(checker.callable_sites),
        invoked_calls=copy.deepcopy(checker.invoked_calls),
        negative_scope_status="UNKNOWN" if checker.negative_scope_gaps else "CHECKED",
        negative_scope_gaps=checker.negative_scope_gaps,
        branch_evidence=[row for row in checker.protocols if row["rule"] in {
            "source-known-branch/1", "source-literal-membership/1", "source-exact-local-instance/1"}],
        unresolved_targets=result["unresolved_targets"],
        configuration_evidence=[row for row in checker.protocols if row["rule"] == CONFIGURATION_RULE],
        type_reference_facts=checker.sdk_source.type_reference_facts,
        scope="finite source potential-call values; literal None differs from UNKNOWN; no SDK effect or runtime resource identity proof")


def dependency_closure(inspector, candidate):
    """Return source reachability facts and blocking candidate consistency issues.

    Cache is on the inspector and includes the entire candidate and frozen source
    digests. Callers receive copies so diagnostics cannot poison a later check.
    CHECKED means this finite audit found no issue, not complete discovery.
    """
    identity = reader_identity() if any(path.endswith((".yaml", ".yml")) for path in inspector.files) else None
    key = hashlib.sha256(json.dumps(dict(candidate=candidate, files=inspector.files, configuration_reader=identity), sort_keys=True,
                                   default=str, ensure_ascii=False).encode()).hexdigest()
    cached = getattr(inspector, "_dependency_closure_cache", None)
    if cached and cached[0] == key:
        return copy.deepcopy(cached[1])
    checker = _Closure(inspector, candidate)
    entries = {}
    for entry in candidate.get("entries", []):
        if not isinstance(entry, dict):
            continue  # UnitCatalog reports malformed entry rows as MISSING.
        result = checker.entry(entry)
        if result is not None:
            entries[entry["id"]] = result
    result = dict(schema="dependency-closure/1", entries=entries,
                  issues=[i for row in entries.values() for i in row["issues"]])
    # These conditional reads must recheck bytes and path state at each audit;
    # the ordinary source-record key alone cannot observe later CAS corruption
    # or a newly introduced source-root symlink.
    conditional_read = any(row.get("configuration_conditions") or any(
        issue.get("reason", "").startswith("frozen_configuration_")
        for issue in row.get("unresolved_targets", [])) for row in entries.values())
    if not conditional_read:
        inspector._dependency_closure_cache = (key, copy.deepcopy(result))
    return result


def entry_dependency_issues(inspector, candidate, entry):
    return dependency_closure(inspector, candidate)["entries"].get(entry["id"], {}).get("issues", [])
