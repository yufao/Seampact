"""Scoped observations of original model/schema/native-target associations.

Opaque native validators and serializers cannot be weakly referenced. They
are borrowed only inside one original synchronous read/use region. Exiting
that region destroys all observations; an id, schema digest, empty plugin
cache, or surviving class weak reference cannot revive their qualification.

Observation is not permission. The normal worker must independently close
the original SDK prefix, callbacks, mutation and complete read/use effects.
"""
import ast
import hashlib
import sys
import threading
import types
import weakref
from dataclasses import dataclass

from seampact.source.pydantic_models import (
    RULE, REGION_RULE, PydanticUnknown, compile_model_plan,
)
from seampact.source.call_value_conditions import digest as proof_digest


_TYPE, _DICT, _STR = type, dict, str
_FUNCTION, _FRAME, _REF = types.FunctionType, types.FrameType, weakref.ReferenceType
_CLASS_DICT = type.__dict__['__dict__']
_DICT_VALUES = type({}.values())
_GETPROFILE, _SETPROFILE, _GETIDENT = sys.getprofile, sys.setprofile, threading.get_ident


def _code_token(code, depth=0):
    """Compare code without invoking equality/hash on an opaque constant."""
    if _TYPE(code) is not types.CodeType or depth > 32 or _TYPE(code.co_consts) is not tuple:
        return None

    visits = [0]

    def constant(value, nesting=0):
        visits[0] += 1
        if visits[0] > 4096 or nesting > 32:
            return None
        kind = _TYPE(value)
        if value is None or value is Ellipsis or kind in (str, bytes, bool, int):
            return ('literal', kind.__name__, value)
        if kind is float:
            return ('float', float.hex(value))
        if kind is tuple:
            values = tuple(constant(item, nesting + 1) for item in value)
            return None if any(item is None for item in values) else ('tuple', values)
        if kind is types.CodeType:
            nested = _code_token(value, depth + 1)
            return None if nested is None else ('code', nested)
        return None

    strings = (code.co_name, code.co_filename, getattr(code, 'co_qualname', code.co_name))
    names = (code.co_names, code.co_varnames, code.co_freevars, code.co_cellvars)
    byte_fields = (code.co_code, getattr(code, 'co_linetable', code.co_lnotab), getattr(code, 'co_exceptiontable', b''))
    if (any(_TYPE(value) is not _STR for value in strings) or
            any(_TYPE(values) is not tuple or any(_TYPE(value) is not _STR for value in values) for values in names) or
            any(_TYPE(value) is not bytes for value in byte_fields)):
        return None
    constants = tuple(constant(value) for value in code.co_consts)
    if any(value is None for value in constants):
        return None
    return (strings, names, byte_fields, constants, code.co_argcount, code.co_posonlyargcount,
            code.co_kwonlyargcount, code.co_nlocals, code.co_stacksize, code.co_flags, code.co_firstlineno,
            )


@dataclass(frozen=True)
class PythonCreationAnchor:
    """Compiled frozen source, with no SDK import or function execution."""
    filename: str
    symbol: str
    source_sha256: str
    code: object

    @classmethod
    def from_source(cls, source, filename, symbol):
        if any(type(value) is not str for value in (source, filename, symbol)) or not symbol.isidentifier():
            raise PydanticUnknown('original SDK creation source anchor is malformed')
        tree = ast.parse(source, filename=filename)
        declarations = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == symbol]
        if len(declarations) != 1 or declarations[0].decorator_list:
            raise PydanticUnknown('original SDK creation anchor is not one unwrapped top-level definition')
        compiled = compile(source, filename, 'exec', dont_inherit=True, optimize=sys.flags.optimize)
        codes = [code for code in compiled.co_consts if type(code) is types.CodeType and code.co_name == symbol
                 and code.co_firstlineno == declarations[0].lineno]
        if len(codes) != 1 or codes[0].co_freevars:
            raise PydanticUnknown('original SDK creation code anchor is absent or has a closure')
        if _code_token(codes[0]) is None:
            raise PydanticUnknown('original SDK code constants exceed the finite passive anchor domain')
        return cls(filename, symbol, hashlib.sha256(source.encode()).hexdigest(), codes[0])


def _dictionary(value):
    if _TYPE(value) is not _DICT or any(_TYPE(key) is not _STR for key in value):
        raise PydanticUnknown('configuration target mapping has an unknown key/object protocol')
    return value


def _class_values(value, metaclass):
    if _TYPE(value) is not metaclass:
        raise PydanticUnknown('configuration model metaclass identity is UNKNOWN')
    namespace = _CLASS_DICT.__get__(value, _TYPE)
    if any(_TYPE(key) is not _STR for key in namespace):
        raise PydanticUnknown('configuration model class namespace has an unknown key protocol')
    return namespace


class CreationObservation:
    """Passive before-exec observer; never executes a model or SDK operation.

    ``functions`` and ``kernel`` are actual targets obtained by the normal
    worker. Merely supplying them does not establish source or SDK semantics.
    The source checker must prove the exact original paths before execution;
    ``verify_model_targets`` consumes that proof separately before use.
    """
    def __init__(self, functions, kernel):
        functions, kernel = _dictionary(functions), _dictionary(kernel)
        if set(functions) != {'complete_model_class', 'create_schema_validator', 'get_plugins'}:
            raise PydanticUnknown('complete original model/plugin creation call targets are absent')
        if set(kernel) != {'metaclass', 'validator', 'serializer'}:
            raise PydanticUnknown('complete original model/native kernel targets are absent')
        if any(_TYPE(value) is not _FUNCTION or value.__closure__ is not None for value in functions.values()):
            raise PydanticUnknown('model creation targets are not original unwrapped Python functions')
        if any(_TYPE(value) is not _TYPE for value in kernel.values()):
            raise PydanticUnknown('model/native kernel has an unknown metaclass protocol')
        # SDK functions/types are weakly referencable. Borrowed native objects,
        # schema and model references are populated only while active below.
        self._functions = {name: _REF(value) for name, value in functions.items()}
        self._codes = {name: value.__code__ for name, value in functions.items()}
        self._kernel = {name: _REF(value) for name, value in kernel.items()}
        self._active = False
        self._clear()

    def _clear(self):
        self._pending, self._validators, self._models = [], [], []
        self._plugin_cache = None
        self._failure = None

    def __enter__(self):
        if self._active or _GETPROFILE() is not None:
            raise PydanticUnknown('configuration observation cannot replace an existing observer or enter twice')
        if sys.getprofile is not _GETPROFILE or sys.setprofile is not _SETPROFILE or threading.get_ident is not _GETIDENT:
            raise PydanticUnknown('original passive observation kernel changed')
        self._clear()
        self._thread = _GETIDENT()
        self._callback = self._profile
        self._active = True
        _SETPROFILE(self._callback)
        return self

    def __exit__(self, kind, value, traceback):
        try:
            if _GETPROFILE() is self._callback:
                _SETPROFILE(None)
        finally:
            self._active = False
            self._clear()  # Includes every native/schema/model strong reference.
            self._callback = None
        return False

    def _current(self):
        if not self._active or _GETIDENT() != self._thread:
            raise PydanticUnknown('original model creation observation ended; cross-region native identity is UNKNOWN')
        if _GETPROFILE() is not self._callback:
            raise PydanticUnknown('original before-exec model observation was replaced')
        if self._failure is not None:
            raise PydanticUnknown(self._failure)
        for name, reference in self._functions.items():
            function = reference()
            if function is None or function.__code__ is not self._codes[name] or function.__closure__ is not None:
                raise PydanticUnknown('original model/plugin creation function binding changed')
        return {name: reference() for name, reference in self._kernel.items()}

    def _profile(self, frame, event, result):
        # A profile callback records facts; it never changes a source return,
        # suppresses an exception, invokes a callback, or creates a model.
        if self._failure is not None or event not in {'call', 'return'} or _TYPE(frame) is not _FRAME:
            return
        name = next((name for name, code in self._codes.items() if frame.f_code is code), None)
        if name is None:
            return
        try:
            function = self._functions[name]()
            if function is None or frame.f_globals is not function.__globals__:
                raise PydanticUnknown('observed SDK code has another original global binding')
            namespace, values = _dictionary(frame.f_globals), _dictionary(frame.f_locals)
            if event == 'call':
                initial = None
                if name == 'get_plugins':
                    initial = (namespace.get('_plugins'), namespace.get('_loading_plugins'))
                self._pending.append((frame, name, initial))
                return
            pending = next((row for row in reversed(self._pending) if row[0] is frame), None)
            if pending is None:
                raise PydanticUnknown('SDK creation return has no original observed call occurrence')
            self._pending.remove(pending)
            if name == 'get_plugins':
                cache = namespace.get('_plugins')
                if (values.get('disabled_plugins') is not None or namespace.get('_loading_plugins') is not False or
                        _TYPE(cache) is not _DICT or len(cache) or _TYPE(result) is not _DICT_VALUES or len(result) or
                        'entry_point' in values or pending[2][1] is not False or
                        not (pending[2][0] is None or pending[2][0] is self._plugin_cache)):
                    raise PydanticUnknown('original no-plugin discovery/creation provenance remains UNKNOWN')
                self._plugin_cache = cache
            elif name == 'create_schema_validator':
                kernel = self._current()
                loader = self._functions['get_plugins']().__globals__
                if self._plugin_cache is None or loader.get('_plugins') is not self._plugin_cache:
                    raise PydanticUnknown('model validator has no original same-region no-plugin provenance')
                if _TYPE(result) is not kernel['validator']:
                    raise PydanticUnknown('original native model validator target identity is UNKNOWN')
                self._validators.append((values.get('schema'), values.get('schema_type'), values.get('config'), result))
            else:
                kernel = self._current()
                if result is not True or 'schema' not in values or 'core_config' not in values:
                    raise PydanticUnknown('original model schema completion did not complete normally')
                class_, schema, config = values.get('cls'), values['schema'], values['core_config']
                attributes = _class_values(class_, kernel['metaclass'])
                validator, serializer = attributes.get('__pydantic_validator__'), attributes.get('__pydantic_serializer__')
                if (attributes.get('__pydantic_core_schema__') is not schema or
                        _TYPE(serializer) is not kernel['serializer'] or
                        namespace.get('SchemaSerializer') is not kernel['serializer'] or
                        namespace.get('create_schema_validator') is not self._functions['create_schema_validator']() or
                        not any(a is schema and b is class_ and c is config and d is validator for a, b, c, d in self._validators)):
                    raise PydanticUnknown('original schema/validator/serializer creation association remains UNKNOWN')
                if any(row[0] is class_ for row in self._models):
                    raise PydanticUnknown('repeated/rebuilt native model association requires separate original reuse proof')
                self._models.append((class_, schema, config, validator, serializer))
        except (ValueError, KeyError, TypeError) as error:
            # Keep the original SDK's exception/return intact. The next normal
            # gate reports UNKNOWN before the source configuration call.
            message = str(error)
            self._clear()  # Drop borrowed native targets before original error continuation.
            self._failure = message

    def verify(self, class_):
        """Fresh association inside the same region; no standalone permission."""
        kernel = self._current()
        row = next((row for row in self._models if row[0] is class_), None)
        if row is None:
            raise PydanticUnknown('model has no original native creation association in this read/use region')
        attributes = _class_values(class_, kernel['metaclass'])
        if any(attributes.get(name) is not value for name, value in (
                ('__pydantic_core_schema__', row[1]), ('__pydantic_validator__', row[3]),
                ('__pydantic_serializer__', row[4]))):
            raise PydanticUnknown('original native model schema/validator/serializer binding changed')
        return dict(status='SAME_REGION_ASSOCIATION_OBSERVED', execution_permission=False,
                    cross_region_qualification=False, lifecycle_permissions=[])


class DeferredCreationObservation(CreationObservation):
    """Bind first original SDK frames without advancing a lazy import.

    Source/code anchors identify an occurrence only. The normal SDK-prefix,
    kernel and read/use checker must independently establish semantics before
    executing it; this class cannot turn source hashes into permission.
    """
    def __init__(self, anchors):
        anchors = _dictionary(anchors)
        if (set(anchors) != {'complete_model_class', 'create_schema_validator', 'get_plugins'} or
                any(_TYPE(anchor) is not PythonCreationAnchor or anchor.symbol != name for name, anchor in anchors.items())):
            raise PydanticUnknown('complete original lazy SDK creation anchors are absent')
        self._anchors = dict(anchors)
        self._anchor_tokens = {name: _code_token(anchor.code) for name, anchor in anchors.items()}
        self._functions, self._codes, self._kernel = {}, {}, {}
        self._active = False
        self._clear()

    def _current(self):
        if (set(self._functions) != set(self._anchors) or
                set(self._kernel) != {'metaclass', 'validator', 'serializer'}):
            raise PydanticUnknown('original SDK/kernel definitions have not appeared in their original execution order')
        return super()._current()

    def _profile(self, frame, event, result):
        if self._failure is not None:
            return
        if event == 'call' and _TYPE(frame) is _FRAME:
            code = frame.f_code
            name = code.co_name if _TYPE(code.co_name) is _STR else None
            if (name not in self._anchors or _TYPE(code.co_filename) is not _STR or
                    code.co_filename != self._anchors[name].filename or
                    code is not self._codes.get(name) and _code_token(code) != self._anchor_tokens[name]):
                name = None
            if name is not None:
                try:
                    namespace = _dictionary(frame.f_globals)
                    function = namespace.get(name)
                    module_name = namespace.get('__name__')
                    module = sys.modules.get(module_name) if _TYPE(module_name) is _STR else None
                    if (_TYPE(module) is not types.ModuleType or
                            types.ModuleType.__dict__['__dict__'].__get__(module, types.ModuleType) is not namespace or
                            _TYPE(function) is not _FUNCTION or function.__globals__ is not namespace or
                            function.__code__ is not frame.f_code or function.__closure__ is not None or
                            _TYPE(function.__qualname__) is not _STR or function.__qualname__ != name or
                            name in self._functions and self._functions[name]() is not function):
                        raise PydanticUnknown('lazy original SDK function/module/global occurrence differs from frozen source')
                    self._functions[name], self._codes[name] = _REF(function), frame.f_code
                    bindings = {'complete_model_class': {'metaclass': 'ModelMetaclass', 'serializer': 'SchemaSerializer'},
                                'create_schema_validator': {'validator': 'SchemaValidator'}, 'get_plugins': {}}[name]
                    for role, attribute in bindings.items():
                        value = namespace.get(attribute)
                        if _TYPE(value) is not _TYPE or role in self._kernel and self._kernel[role]() is not value:
                            raise PydanticUnknown('original lazy SDK/native kernel target binding changed')
                        self._kernel[role] = _REF(value)
                except (ValueError, TypeError) as error:
                    message = str(error)
                    self._clear()
                    self._failure = message
                    return
        super()._profile(frame, event, result)


def required_models(inspector, trace):
    """Rebuild every model's source requirements, never accept supplied names."""
    result = {}
    for protocol in trace.get('external_protocols', []):
        if protocol.get('rule') != RULE:
            continue
        supplied = _dictionary(protocol.get('target_requirements'))
        root = supplied.get('root')
        if _TYPE(root) is not _STR or ':' not in root:
            raise PydanticUnknown('original configuration model source anchor is malformed')
        path, symbol = root.rsplit(':', 1)
        plan = compile_model_plan(inspector, path, symbol)
        if plan.requirements() != supplied:
            raise PydanticUnknown('original configuration model source requirements changed')
        result[supplied['digest']] = plan
    return result


def verify_model_targets(inspector, entry, candidate, trace, observation, modules):
    """Normal worker gate for one actual configuration-model read/use region.

    The ordinary actual-value closure is recomputed; an external flag or a
    source-only plan cannot close SDK effects. Current unsupported SDK paths
    consequently remain UNKNOWN even when association observations exist.
    """
    plans = required_models(inspector, trace)
    if not plans:
        return {}
    from seampact.source.dependency_closure import resolve_resource_calls
    from seampact.source.configuration_model_flow import GAP
    actual = resolve_resource_calls(inspector, entry, candidate)
    if actual != trace or (actual.get('status') != 'CHECKED' or actual.get('unresolved_targets') or
            any(gap.get('reason') != GAP for gap in actual.get('negative_scope_gaps', [])) or
            actual.get('activation_prefix', {}).get('status') != 'CHECKED'):
        raise PydanticUnknown('complete original configuration/SDK read-use source effects remain UNKNOWN')
    regions = [row for row in actual.get('external_protocols', []) if row.get('rule') == REGION_RULE]
    if not regions:
        raise PydanticUnknown('original configuration SDK creation/mutation/read-use provenance remains UNKNOWN')
    # A region metadata flag is deliberately insufficient: the actual full
    # call trace and a still-active before-exec observation are both required.
    if _TYPE(observation) not in (CreationObservation, DeferredCreationObservation):
        raise PydanticUnknown('normal worker has no same-region original model creation observation')
    modules = _dictionary(modules)
    checked = {}
    for plan in plans.values():
        classes = {}
        for model in plan.requirements()['models']:
            module = modules.get(model['source']['path'])
            if _TYPE(module) is not types.ModuleType:
                raise PydanticUnknown('original model source module is not currently bound')
            namespace = _dictionary(types.ModuleType.__dict__['__dict__'].__get__(module, types.ModuleType))
            class_ = namespace.get(model['symbol'])
            checked[model['identity']] = observation.verify(class_)
            classes[model['identity']] = class_
        for name, factory in plan.requirements().get('factories', {}).items():
            module = modules.get(factory['source']['path'])
            if _TYPE(module) is not types.ModuleType:
                raise PydanticUnknown('original model factory source module is not currently bound')
            namespace = _dictionary(types.ModuleType.__dict__['__dict__'].__get__(module, types.ModuleType))
            actual = _dictionary(namespace.get(name))
            members = factory['members']
            if list(actual) != [row['key'] for row in members] or any(
                    actual[row['key']] is not classes[row['model']] for row in members):
                raise PydanticUnknown('complete original model factory membership/order or class target changed')
    return dict(status='CHECKED', schema=RULE,
                requirement_digests=[proof_digest(plan.requirements()) for plan in plans.values()],
                models=checked, execution_permission=False,
                scope='actual target prerequisites only; normal worker must replay the complete same call before permission')
