"""Source-checked native entries and their shared-runtime callable bindings.

No model claim grants parallel, remote, or independent execution. Native code is
loaded only when a ready entry is invoked, and its original owner is reused.
"""

import ast
import asyncio
import builtins
import contextlib
import contextvars
import copy
import importlib
import importlib.abc
import importlib.util
import inspect
import json
import math
import os
import re
import shutil
import subprocess
import symtable
import sys
import threading
import time
import types
import uuid
from pathlib import Path

from seampact.handoff import ToolListHandoff, install_method_proxy
from seampact.source.callsite import lower_callsites
from seampact.configuration.dependencies import DependencyBindings, has_reference as has_resource_reference, reference as resource_reference
from seampact.source.inspection import Inspector, digest, native_ast, python_runtime_issue, units
from seampact.source.lowering import lower_loop
from seampact.runtime import ProtocolError
from seampact.configuration.resource_origins import initialization_gaps
from seampact.source.execution_context import ContextIssue, explicit_import_roots, process_context
from seampact.completion import after_completion, before_completion, check_completion
from seampact.configuration.configuration_files import configuration_arguments, configuration_plans, redact_process_result
from seampact.store import now
from seampact.source.verification import check_candidate, source_unit
from seampact.source.frozen_inheritance import FrozenInheritance, InheritanceIssue
from seampact.source.keyword_domain import literal_equal, narrow_keywords
from seampact.source.finite_registration import RULE as REGISTRATION_RULE, RegistrationUnknown
from seampact.source.registration_targets import (
    required_worlds as registration_worlds, module_targets as registration_modules,
    capture_targets as capture_registration_targets, verify_targets as verify_registration_targets,
)


_native_context_lock = threading.RLock()
_EMPTY = inspect.Parameter.empty
_NATIVE_TYPE, _NATIVE_FUNCTION, _NATIVE_METHOD = type, types.FunctionType, types.MethodType
_NATIVE_DICT, _NATIVE_STR = dict, str
_NATIVE_STATIC, _NATIVE_CLASS, _NATIVE_PROPERTY = staticmethod, classmethod, property
_NATIVE_OBJECT, _NATIVE_GETSET = object, types.GetSetDescriptorType
_NATIVE_MODULE = types.ModuleType
_NATIVE_MODULE_DICT = types.ModuleType.__dict__['__dict__']
_NATIVE_CLASS_DICT = type.__dict__['__dict__']
_NATIVE_MRO = type.__dict__['__mro__']
_NATIVE_QUALNAME = type.__dict__['__qualname__']
_UNSET_NATIVE_ATTRIBUTE = object()


class MissingBinding(ValueError):
    pass


def _native_function_chain(callable_):
    """Follow only exact Python callables, before reading wrapper metadata."""
    current, functions, seen = callable_, [], set()
    while True:
        if _NATIVE_TYPE(current) is _NATIVE_METHOD:
            current = current.__func__
        if _NATIVE_TYPE(current) is not _NATIVE_FUNCTION or id(current) in seen:
            raise MissingBinding('native callable has an unknown or cyclic wrapper protocol')
        seen.add(id(current))
        attributes = current.__dict__
        if _NATIVE_TYPE(attributes) is not _NATIVE_DICT or any(_NATIVE_TYPE(key) is not _NATIVE_STR for key in attributes):
            raise MissingBinding('native callable metadata has an unknown mapping/key protocol')
        functions.append(current)
        if '__wrapped__' not in attributes:
            return functions
        current = attributes['__wrapped__']


def _native_callable_snapshot(callable_):
    rows = []
    for function in _native_function_chain(callable_):
        closure = function.__closure__
        try:
            contents = tuple(cell.cell_contents for cell in closure) if closure else ()
        except ValueError as error:
            raise MissingBinding('native callable has an unreadable closure') from error
        kwdefaults = function.__kwdefaults__
        if kwdefaults is not None and (_NATIVE_TYPE(kwdefaults) is not _NATIVE_DICT
                or any(_NATIVE_TYPE(key) is not _NATIVE_STR for key in kwdefaults)):
            raise MissingBinding('native keyword defaults have an unknown mapping/key protocol')
        rows.append(dict(function=function, code=function.__code__, globals=function.__globals__,
                         defaults=function.__defaults__, kwdefaults=kwdefaults,
                         kwdefault_items=tuple(kwdefaults.items()) if kwdefaults is not None else None,
                         closure=closure, contents=contents,
                         wrapped=function.__dict__.get('__wrapped__', _UNSET_NATIVE_ATTRIBUTE),
                         signature=function.__dict__.get('__signature__', _UNSET_NATIVE_ATTRIBUTE)))
    return rows


def _verify_native_callable_snapshot(callable_, expected):
    actual = _native_callable_snapshot(callable_)
    if len(actual) != len(expected):
        raise MissingBinding('native callable wrapper chain changed after binding')
    for row, prior in zip(actual, expected):
        current_kw, previous_kw = row['kwdefault_items'], prior['kwdefault_items']
        keyword_changed = ((current_kw is None) != (previous_kw is None)
            or current_kw is not None and (len(current_kw) != len(previous_kw)
                or any(key != previous_key or value is not previous_value
                       for (key, value), (previous_key, previous_value) in zip(current_kw, previous_kw))))
        if (any(row[key] is not prior[key] for key in ('function', 'code', 'globals', 'defaults', 'kwdefaults',
                                                        'closure', 'wrapped', 'signature'))
                or keyword_changed
                or len(row['contents']) != len(prior['contents'])
                or any(value is not previous for value, previous in zip(row['contents'], prior['contents']))):
            raise MissingBinding('native callable code, defaults, globals or retained binding changed')


def _plain_callable_defaults(callable_):
    """Snapshot only exact ordinary defaults; never compare unknown objects."""
    from seampact.source.call_value_conditions import ordinary
    rows = []
    for function in _native_function_chain(callable_):
        positional, keyword = function.__defaults__, function.__kwdefaults__
        if positional is not None and _NATIVE_TYPE(positional) is not tuple:
            raise MissingBinding('native defaults have an unknown tuple protocol')
        if keyword is not None and (_NATIVE_TYPE(keyword) is not _NATIVE_DICT or
                any(_NATIVE_TYPE(key) is not _NATIVE_STR for key in keyword)):
            raise MissingBinding('native keyword defaults have unknown key protocols')
        rows.append(dict(positional=[ordinary(item) for item in positional or ()],
                         keyword=ordinary(keyword or {})))
    return json.dumps(rows, sort_keys=True, ensure_ascii=False, allow_nan=False)


def _plain_immutable_class_value(value):
    kind = _NATIVE_TYPE(value)
    if value is None or kind is str or kind is int or kind is bool:
        return True
    if kind is float:
        return math.isfinite(value)
    if kind is tuple:
        return len(value) <= 64 and all(_plain_immutable_class_value(item) for item in value)
    return False


def _checked_module_table():
    modules = sys.modules
    if _NATIVE_TYPE(modules) is not _NATIVE_DICT or any(_NATIVE_TYPE(key) is not _NATIVE_STR for key in modules):
        raise MissingBinding('original module table has unknown key protocols')
    return modules


def _import_only_function_metadata(function):
    attributes = function.__dict__
    if (_NATIVE_TYPE(attributes) is not _NATIVE_DICT or
            any(_NATIVE_TYPE(key) is not _NATIVE_STR for key in attributes)):
        raise MissingBinding('imported function metadata has unknown mapping/key protocols')
    return attributes


def _capture_import_only_loader_kernel():
    """Capture the original controlled loader, before any source is loaded.

    This is the finite loading mechanism used by import-only preparation, not a
    claim about every object or extension in the worker process.
    """
    from seampact.source import inspection, verification
    bootstrap = importlib._bootstrap
    classes = [_FrozenLoader, _FrozenFinder, NativeBindings, Inspector,
               contextlib._GeneratorContextManager, contextlib._GeneratorContextManagerBase,
               importlib.machinery.ModuleSpec, Path]
    functions = [source_unit, Inspector.data, units, inspection.decode_text,
                 inspection.digest, inspection.withheld_source, _checked_module_table,
                 NativeBindings._context, NativeBindings._namespace,
                 NativeBindings._inject_callsites, NativeBindings._callsite_factory,
                 _FrozenLoader.__init__, _FrozenLoader.create_module, _FrozenLoader.exec_module,
                 _FrozenFinder.__init__, _FrozenFinder.find_spec,
                 importlib.import_module, importlib.util.spec_from_loader,
                 contextlib._GeneratorContextManagerBase.__init__,
                 contextlib._GeneratorContextManager.__enter__, contextlib._GeneratorContextManager.__exit__,
                 Path.read_bytes, Path.open, Path.cwd]
    # Frozen importlib's helpers and lock/spec classes form the same original
    # loading mechanism. Pin their loaded Python code as well as exported names.
    for value in bootstrap.__dict__.values():
        if _NATIVE_TYPE(value) is _NATIVE_FUNCTION:
            functions.append(value)
        elif _NATIVE_TYPE(value) is _NATIVE_TYPE:
            classes.append(value)
    class_seen, expanded = set(), []
    for class_ in classes:
        for original in (*_NATIVE_MRO.__get__(class_, _NATIVE_TYPE),
                         *_NATIVE_MRO.__get__(_NATIVE_TYPE(class_), _NATIVE_TYPE)):
            if id(original) not in class_seen:
                class_seen.add(id(original))
                expanded.append(original)
    classes = expanded
    for class_ in classes:
        for value in _NATIVE_CLASS_DICT.__get__(class_, _NATIVE_TYPE).values():
            if _NATIVE_TYPE(value) is _NATIVE_STATIC or _NATIVE_TYPE(value) is _NATIVE_CLASS:
                value = value.__func__
            if _NATIVE_TYPE(value) is _NATIVE_FUNCTION:
                functions.append(value)
    rows, seen = [], set()
    for callable_ in functions:
        for function in _native_function_chain(callable_):
            if id(function) in seen:
                continue
            seen.add(id(function))
            namespace = function.__globals__
            if (_NATIVE_TYPE(namespace) is not _NATIVE_DICT or
                    any(_NATIVE_TYPE(key) is not _NATIVE_STR for key in namespace)):
                raise MissingBinding('controlled source loader has unknown global key protocols')
            names = set(function.__code__.co_names) | {'__builtins__'}
            rows.append(dict(function=function, snapshot=_native_callable_snapshot(function),
                globals=namespace, references=tuple((name, namespace.get(name, _UNSET_NATIVE_ATTRIBUTE))
                                                     for name in names),
                builtins=function.__builtins__, metadata=tuple(_import_only_function_metadata(function).items())))
    module_specs = [
        (sys, 'sys', ('modules', 'exc_info')),
        (builtins, 'builtins', ('__import__', 'compile', 'exec', '__build_class__')),
        (types, 'types', ('FunctionType', 'MethodType', 'ModuleType')),
        (importlib, 'importlib', ('import_module', '_bootstrap', 'util', 'machinery')),
        (importlib.abc, 'importlib.abc', ('Loader', 'MetaPathFinder')),
        (importlib.util, 'importlib.util', ('spec_from_loader',)),
        (importlib.machinery, 'importlib.machinery', ('ModuleSpec',)),
        (bootstrap, '_frozen_importlib', tuple(name for name in bootstrap.__dict__
            if name in {'_gcd_import', '_find_and_load', '_find_and_load_unlocked', '_find_spec',
                        '_load_unlocked', 'module_from_spec', '_init_module_attrs',
                        '_call_with_frames_removed', '_handle_fromlist', '_resolve_name',
                        '_ModuleLockManager', '_get_module_lock', 'ModuleSpec'})),
        (contextlib, 'contextlib', ('_GeneratorContextManager', '_GeneratorContextManagerBase')),
        (verification, 'seampact.source.verification', ('source_unit', 'units')),
        (inspection, 'seampact.source.inspection', ('Inspector', 'units', 'decode_text', 'digest', 'withheld_source')),
        (os, 'os', ('chdir', 'getcwd', 'fspath')),
        (inspection.io, 'io', ('open', 'text_encoding')),
        (inspection.hashlib, 'hashlib', ('sha256',)),
    ]
    for name in ('_imp', '_thread', '_weakref'):
        module = bootstrap.__dict__[name]
        module_specs.append((module, name, tuple(module.__dict__)))
    modules = []
    for module, name, names in module_specs:
        namespace = _NATIVE_MODULE_DICT.__get__(module, _NATIVE_MODULE)
        modules.append((module, name, namespace, tuple((key, namespace[key]) for key in names)))
    return dict(functions=rows, classes=tuple((class_, _NATIVE_TYPE(class_),
        tuple(_NATIVE_MRO.__get__(class_, _NATIVE_TYPE)),
        tuple(_NATIVE_CLASS_DICT.__get__(class_, _NATIVE_TYPE).items())) for class_ in classes),
        modules=modules, builtins=tuple(builtins.__dict__.items()),
        meta_path=tuple(sys.meta_path), binding_class=NativeBindings, inspector_class=Inspector,
        lock_type=_NATIVE_TYPE(_native_context_lock),
        protected=('_context', '_namespace', '_inject_callsites', '_callsite_factory',
                   '_capture_import_only_target', '_capture_imported_helpers', '_entry_anchor',
                   '_assert_callable', '_assert_inherited_owner', '_source_method_descriptor'))


def _check_import_only_loader(binding):
    """Check actual loaded objects before entering the original import prefix."""
    kernel = _IMPORT_ONLY_LOADER_KERNEL
    for class_, meta, mro, expected in kernel['classes']:
        if _NATIVE_TYPE(class_) is not meta:
            raise MissingBinding('controlled source loader metaclass changed')
        actual_mro = _NATIVE_MRO.__get__(class_, _NATIVE_TYPE)
        values = _NATIVE_CLASS_DICT.__get__(class_, _NATIVE_TYPE)
        if (len(actual_mro) != len(mro) or any(a is not b for a, b in zip(actual_mro, mro)) or
                any(_NATIVE_TYPE(key) is not _NATIVE_STR for key in values) or len(values) != len(expected) or
                any(key not in values or values[key] is not value for key, value in expected)):
            raise MissingBinding('controlled source loader class protocol changed')
    if _NATIVE_TYPE(binding) is not kernel['binding_class']:
        raise MissingBinding('controlled source loader binding has an unknown protocol')
    attributes = _NATIVE_OBJECT.__getattribute__(binding, '__dict__')
    if (_NATIVE_TYPE(attributes) is not _NATIVE_DICT or
            any(_NATIVE_TYPE(key) is not _NATIVE_STR for key in attributes) or
            any(name in attributes for name in kernel['protected'])):
        raise MissingBinding('controlled source loader has an instance method shadow')
    inspector = attributes.get('inspector')
    if _NATIVE_TYPE(attributes.get('lock')) is not kernel['lock_type']:
        raise MissingBinding('controlled source loader lock has an unknown protocol')
    if _NATIVE_TYPE(inspector) is not kernel['inspector_class']:
        raise MissingBinding('controlled source reader has an unknown protocol')
    reader_attributes = _NATIVE_OBJECT.__getattribute__(inspector, '__dict__')
    if (_NATIVE_TYPE(reader_attributes) is not _NATIVE_DICT or
            any(_NATIVE_TYPE(key) is not _NATIVE_STR for key in reader_attributes) or 'data' in reader_attributes):
        raise MissingBinding('controlled source reader has an instance method shadow')
    # Inspect exact module dictionaries before any string-key lookup. A str
    # subclass stored as a key can run __eq__ even in an otherwise exact dict.
    for module, name, namespace, references in kernel['modules']:
        if (_NATIVE_TYPE(module) is not _NATIVE_MODULE or
                _NATIVE_MODULE_DICT.__get__(module, _NATIVE_MODULE) is not namespace or
                any(_NATIVE_TYPE(key) is not _NATIVE_STR for key in namespace) or
                any(namespace.get(key, _UNSET_NATIVE_ATTRIBUTE) is not value for key, value in references)):
            raise MissingBinding('controlled source loader module binding changed: ' + name)
    builtin_values = _NATIVE_MODULE_DICT.__get__(builtins, _NATIVE_MODULE)
    if (len(builtin_values) != len(kernel['builtins']) or
            any(key not in builtin_values or builtin_values[key] is not value for key, value in kernel['builtins'])):
        raise MissingBinding('controlled source loader builtin protocol changed')
    if (_NATIVE_TYPE(sys.meta_path) is not list or len(sys.meta_path) != len(kernel['meta_path']) or
            any(actual is not expected for actual, expected in zip(sys.meta_path, kernel['meta_path'])) or
            _NATIVE_TYPE(sys.path) is not list or any(_NATIVE_TYPE(path) is not _NATIVE_STR for path in sys.path)):
        raise MissingBinding('controlled source loader import search protocol changed')
    for row in kernel['functions']:
        function, namespace = row['function'], row['globals']
        if (any(_NATIVE_TYPE(key) is not _NATIVE_STR for key in namespace) or
                any(namespace.get(name, _UNSET_NATIVE_ATTRIBUTE) is not value for name, value in row['references']) or
                function.__builtins__ is not row['builtins']):
            raise MissingBinding('controlled source loader function globals changed')
        metadata = _import_only_function_metadata(function)
        if (len(metadata) != len(row['metadata']) or
                any(key not in metadata or metadata[key] is not value for key, value in row['metadata'])):
            raise MissingBinding('controlled source loader function metadata changed')
        _verify_native_callable_snapshot(function, row['snapshot'])
    modules = _checked_module_table()
    if any(modules.get(name) is not module for module, name, _, _ in kernel['modules']):
        raise MissingBinding('controlled source loader module identity changed')


def _anchor(source, symbol):
    parts = symbol.split(".")
    tree = native_ast(source)
    found = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
             and node.name == parts[0]]
    if len(found) != 1:
        raise MissingBinding("native source anchor is absent or ambiguous: " + symbol)
    node = found[0]
    if len(parts) == 2 and isinstance(node, ast.ClassDef):
        found = [member for member in node.body if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and member.name == parts[1]]
        if len(found) != 1:
            raise MissingBinding("native method anchor is absent or ambiguous: " + symbol)
        return found[0], node
    if len(parts) != 1:
        raise MissingBinding("nested native entries need an explicit owner binding")
    return node, None


def _type_schema(annotation):
    if annotation is None:
        return {}
    if isinstance(annotation, ast.Constant):
        if annotation.value is None:
            return {"type": "null"}
        if isinstance(annotation.value, str):
            try:
                return _type_schema(ast.parse(annotation.value, mode="eval").body)
            except SyntaxError:
                return None
    name = ast.unparse(annotation).split(".")[-1]
    primitive = {"str": "string", "int": "integer", "float": "number", "bool": "boolean",
                 "list": "array", "dict": "object", "List": "array", "Dict": "object", "None": "null", "NoneType": "null"}
    if name in primitive:
        return {"type": primitive[name]}
    if name in {"Any", "object"}:
        return {}
    if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        branches = [_type_schema(annotation.left), _type_schema(annotation.right)]
        return {"anyOf": branches} if all(branch is not None for branch in branches) else None
    if isinstance(annotation, ast.Subscript):
        outer = ast.unparse(annotation.value).split(".")[-1]
        arguments = list(annotation.slice.elts) if isinstance(annotation.slice, ast.Tuple) else [annotation.slice]
        if outer in {"Optional", "Union"}:
            branches = [_type_schema(argument) for argument in arguments]
            if outer == "Optional":
                branches.append({"type": "null"})
            return {"anyOf": branches} if all(branch is not None for branch in branches) else None
        if outer == "Annotated":
            return _type_schema(arguments[0])
        if outer in {"List", "list", "Sequence"}:
            item = _type_schema(arguments[0])
            return {"type": "array", "items": item} if item is not None else None
        if outer in {"Dict", "dict", "Mapping"} and len(arguments) == 2:
            key, value = _type_schema(arguments[0]), _type_schema(arguments[1])
            return {"type": "object", "additionalProperties": value} if key == {"type": "string"} and value is not None else None
        if outer == "Literal":
            try:
                return {"enum": [ast.literal_eval(argument) for argument in arguments]}
            except (ValueError, TypeError):
                return None
    return None


def _object_matches(value, schema, matches):
    """Enforce the same record fields for JSON and original-native leaves."""
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    additional = schema.get("additionalProperties", True)
    if (type(properties) is not dict or any(type(name) is not str for name in properties)
            or type(required) is not list or any(type(name) is not str for name in required)
            or any(name not in value for name in required)):
        return False
    for name, item in value.items():
        if name in properties:
            if not matches(item, properties[name]):
                return False
        elif additional is False:
            return False
        elif type(additional) is dict and not matches(item, additional):
            return False
    return True


def _matches(value, schema):
    if not schema:
        return True
    if "x-native-type" in schema:
        return False
    if "anyOf" in schema:
        return any(_matches(value, branch) for branch in schema["anyOf"])
    if "enum" in schema:
        return any(literal_equal(value, item) for item in schema["enum"])
    kind = schema.get("type")
    valid = {"string": isinstance(value, str), "integer": type(value) is int,
             "number": type(value) in {int, float}, "boolean": type(value) is bool,
             "null": value is None, "array": isinstance(value, list), "object": isinstance(value, dict)}
    if kind is not None and not valid.get(kind, False):
        return False
    if kind == "array":
        return all(_matches(item, schema.get("items", {})) for item in value)
    if kind == "object":
        return _object_matches(value, schema, _matches)
    return True


def _result_number(value):
    """Comparison-only value for exact finite numeric result scalars.

    The original object is never replaced. Input/fixed-value matching retains
    its existing exact Python value domain, and group-port/3 keeps the original
    NumPy scalar dtype in the actual encoded result.
    """
    kind = type(value)
    if kind is int:
        return value
    if kind is float:
        return value if math.isfinite(value) else None
    numpy = sys.modules.get("numpy")
    if numpy is None or not any(kind is scalar for scalar in numpy.sctypeDict.values()):
        return None
    array = numpy.asarray(value)  # Exact official scalar only; no source hooks.
    dtype = array.dtype
    if (dtype.hasobject or dtype.fields is not None or dtype.subdtype is not None
            or dtype.kind not in {"i", "u", "f"} or dtype.kind == "f" and dtype.char not in {"e", "f", "d"}):
        return None
    plain = numpy.ndarray.tolist(array)
    return plain if type(plain) is int or type(plain) is float and math.isfinite(plain) else None


def _result_matches(value, schema):
    """Check result values without widening request or native-object ports."""
    if not schema:
        return True
    if "x-native-type" in schema:
        return False  # Original native/CLI rules retain precedence.
    if "anyOf" in schema:
        return any(_result_matches(value, branch) for branch in schema["anyOf"])
    if "enum" in schema:
        return any(literal_equal(value, item) for item in schema["enum"])
    kind = schema.get("type")
    if kind in {"number", "integer"}:
        number = _result_number(value)
        return type(number) is int or kind == "number" and type(number) is float
    if kind == "array":
        container = schema.get("x-python-container", "list")
        if type(container) is not str or container not in {"list", "tuple"} or type(value) is not (tuple if container == "tuple" else list):
            return False
        prefix = schema.get("prefixItems")
        if prefix is not None:
            # Source-proved literals retain their original container and arity.
            # This result-only rule never turns JSON lists into tuples.
            return (type(prefix) is list
                and type(schema.get("minItems")) is int and type(schema.get("maxItems")) is int
                and schema.get("minItems") == schema.get("maxItems") == len(prefix) == len(value)
                and all(type(item) is dict for item in prefix)
                and all(_result_matches(item, item_schema) for item, item_schema in zip(value, prefix)))
        return container == "list" and all(_result_matches(item, schema.get("items", {})) for item in value)
    if kind == "object":
        return type(value) is dict and _object_matches(value, schema, _result_matches)
    if kind == "string":
        numpy = sys.modules.get("numpy")
        # np.str_ already satisfied the old string matcher and is encoded by
        # group-port/3. Preserve that supported path without consulting an
        # arbitrary object's __class__ or accepting source string subclasses.
        return type(value) is str or numpy is not None and type(value) is numpy.str_
    return (kind is None
            or kind == "boolean" and type(value) is bool or kind == "null" and value is None)


def _result_root_compatible(declared, proved):
    """Three-valued root comparison; unknown leaves never erase annotations."""
    if not declared:
        return True
    if "anyOf" in declared:
        answers = [_result_root_compatible(branch, proved) for branch in declared["anyOf"]]
        return True if True in answers else None if None in answers else False
    if "enum" in declared or "anyOf" in proved:
        return None
    source_kind, declared_kind = proved.get("type"), declared.get("type")
    if source_kind is None:
        return None
    if "x-native-type" in declared:
        return False if source_kind in {"array", "string", "integer", "number", "boolean", "null"} else None
    if declared_kind is None:
        return None
    if declared_kind == "number" and source_kind == "integer":
        return True
    if declared_kind != source_kind:
        return False
    if source_kind == "array":
        if declared.get("x-python-container", "list") != proved.get("x-python-container", "list"):
            return False
        expected, actual = declared.get("prefixItems"), proved.get("prefixItems")
        if type(expected) is list and type(actual) is list and len(expected) != len(actual):
            return False
    return True


def _select_result_structure(declared, proof):
    """Use only a checked source root, preserving compatible declared guards.

    A successful-return structure is not a claim about exceptions, owner
    lifecycle or unknown children. Actual group-port values still need encoding
    and result checks; this selection never changes an original result object.
    """
    proof = copy.deepcopy(proof)
    effective = copy.deepcopy(declared)
    proved = proof.get("result_schema")
    proof.update(declared_schema=copy.deepcopy(declared), selection="DECLARED_ANNOTATION")
    if proof.get("status") == "CHECKED" and type(proved) is dict and proved:
        compatible = _result_root_compatible(declared, proved)
        if not declared or compatible is False:
            effective = copy.deepcopy(proved)
            proof["selection"] = "SOURCE_PROVED_STRUCTURE"
        proof["root_annotation_check"] = ("CONFLICT" if compatible is False else
                                           "COMPATIBLE_ROOT" if compatible is True else "UNKNOWN")
    proof["effective_schema"] = copy.deepcopy(effective)
    return effective, proof


def _fixed_annotation_matches(value, annotation):
    """A known scalar union arm suffices for an explicit fixed literal.

    Unknown object arms remain unavailable; this does not convert a Path or
    other native object to JSON or admit it through a business tool port.
    """
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        try:
            annotation = ast.parse(annotation.value, mode="eval").body
        except SyntaxError:
            return False
    if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        return (_fixed_annotation_matches(value, annotation.left)
                or _fixed_annotation_matches(value, annotation.right))
    if isinstance(annotation, ast.Subscript) and ast.unparse(annotation.value).split(".")[-1] in {"Union", "Optional"}:
        arguments = list(annotation.slice.elts) if isinstance(annotation.slice, ast.Tuple) else [annotation.slice]
        return any(_fixed_annotation_matches(value, argument) for argument in arguments)
    schema = _type_schema(annotation)
    return schema is not None and "x-native-type" not in schema and _matches(value, schema)


def _same_literal(left, right):
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(_same_literal(left[name], right[name]) for name in left)
    if isinstance(left, (list, tuple)):
        return len(left) == len(right) and all(_same_literal(a, b) for a, b in zip(left, right))
    return left == right


def _configured_resource_literals(value, configured):
    """Only checked resource leaves may differ from explicit user literals."""
    if resource_reference(value) is not None:
        return True
    if isinstance(value, dict) and isinstance(configured, dict):
        return value.keys() == configured.keys() and all(
            _configured_resource_literals(child, configured[key]) for key, child in value.items())
    if isinstance(value, list) and isinstance(configured, list):
        return len(value) == len(configured) and all(
            _configured_resource_literals(child, original) for child, original in zip(value, configured))
    return _same_literal(value, configured)


def _literal_defaults(node, receiver=False):
    parameters = list(node.args.posonlyargs) + list(node.args.args)
    defaults = [None] * (len(parameters) - len(node.args.defaults)) + list(node.args.defaults)
    if receiver:
        parameters, defaults = parameters[1:], defaults[1:]
    parameters += list(node.args.kwonlyargs)
    defaults += list(node.args.kw_defaults)
    result = {}
    for parameter, expression in zip(parameters, defaults):
        if expression is not None:
            try:
                result[parameter.arg] = dict(value=ast.literal_eval(expression), line=expression.lineno,
                    ast_sha256=digest(ast.dump(expression, include_attributes=False).encode()))
            except (ValueError, TypeError):
                pass
    return result


def _interface(node, fixed=None, receiver=False, require_all=False, native_schemas=None, bound_refs=()):
    fixed = fixed or {}
    if not isinstance(fixed, dict):
        raise MissingBinding("native arguments must be a JSON object")
    parameters = list(node.args.posonlyargs) + list(node.args.args)
    defaults = [None] * (len(parameters) - len(node.args.defaults)) + list(node.args.defaults)
    if receiver:
        if not parameters:
            raise MissingBinding("native instance receiver is not statically identified")
        parameters, defaults = parameters[1:], defaults[1:]
    parameters += list(node.args.kwonlyargs)
    defaults += list(node.args.kw_defaults)
    native_schemas, bound_refs = native_schemas or {}, set(bound_refs)
    known = {parameter.arg for parameter in parameters}
    if node.args.vararg:
        known.add(node.args.vararg.arg)
    if not node.args.kwarg and set(fixed) - known:
        raise MissingBinding("native arguments contain unknown parameters: " + ", ".join(sorted(set(fixed) - known)))
    properties, required, omitted = {}, [], []
    for parameter, default in zip(parameters, defaults):
        schema = _type_schema(parameter.annotation)
        if parameter.arg in bound_refs:
            continue
        if schema is None:
            schema = native_schemas.get(parameter.arg) or None
        if parameter.arg in fixed:
            if ((schema is None and not _fixed_annotation_matches(fixed[parameter.arg], parameter.annotation))
                    or (schema is not None and ("x-native-type" in schema or not _matches(fixed[parameter.arg], schema)))):
                raise MissingBinding("original object or parameter type is missing: " + parameter.arg)
            continue
        if require_all:
            if default is None:
                raise MissingBinding("required native initialization parameter is missing: " + parameter.arg)
            continue
        if schema is None:
            if default is None:
                raise MissingBinding("required original Model/Context/object parameter cannot be supplied as JSON: " + parameter.arg)
            omitted.append(parameter.arg)
            continue
        properties[parameter.arg] = schema
        if default is None:
            required.append(parameter.arg)
    if node.args.vararg and not require_all:
        item = _type_schema(node.args.vararg.annotation)
        if item is None:
            raise MissingBinding("original variadic object arguments are not JSON-bindable")
        properties[node.args.vararg.arg] = {"type": "array", "items": item}
    return dict(type="object", properties=properties, required=required,
                additionalProperties=bool(node.args.kwarg)), omitted


def _actual_arguments(callable_, values):
    signature = inspect.signature(callable_)
    remaining, positional, keyword = dict(values), [], {}
    parameters = list(signature.parameters.values())
    variable = next((parameter for parameter in parameters if parameter.kind == inspect.Parameter.VAR_POSITIONAL), None)
    extra = remaining.pop(variable.name, []) if variable else []
    if not isinstance(extra, list):
        raise MissingBinding("native variadic arguments require a list")
    positional_parameters = [parameter for parameter in parameters if parameter.kind in {
        inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD}]
    last = len(positional_parameters) - 1 if extra else max((index for index, parameter in enumerate(positional_parameters)
        if parameter.kind == inspect.Parameter.POSITIONAL_ONLY and parameter.name in remaining), default=-1)
    for index, parameter in enumerate(positional_parameters):
        if index <= last:
            value = remaining.pop(parameter.name, parameter.default)
            if value is _EMPTY:
                raise MissingBinding("required native positional parameter is missing: " + parameter.name)
            positional.append(value)
        elif parameter.kind == inspect.Parameter.POSITIONAL_OR_KEYWORD and parameter.name in remaining:
            keyword[parameter.name] = remaining.pop(parameter.name)
    positional.extend(extra)
    for parameter in parameters:
        if parameter.kind == inspect.Parameter.KEYWORD_ONLY and parameter.name in remaining:
            keyword[parameter.name] = remaining.pop(parameter.name)
    keyword.update(remaining)
    try:
        signature.bind(*positional, **keyword)
    except TypeError as error:
        raise MissingBinding("native parameter binding failed: " + str(error)) from error
    return positional, keyword


def _definition_cells(inspector, path, unit, patches):
    result = []
    stop = int(unit.split(":")[1])
    for cell, source in units(path, inspector.data(path)):
        if int(cell.split(":")[1]) > stop:
            break
        source = patches.get((path, cell), source)
        tree = native_ast(source)
        body = []
        for node in tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                body.append(node)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
                try:
                    ast.literal_eval(node.value)
                except (ValueError, TypeError):
                    continue
                if isinstance(node, ast.AnnAssign) and not isinstance(node.target, ast.Name):
                    continue
                if isinstance(node, ast.Assign) and not all(isinstance(target, (ast.Name, ast.Tuple, ast.List)) for target in node.targets):
                    continue
                body.append(node)
        result.append((cell, ast.Module(body=body, type_ignores=[])))
    return result


def _notebook_definitions(inspector, path, unit, patches, injected=()):
    cells = _definition_cells(inspector, path, unit, patches)
    known, referenced = set(dir(builtins)) | set(injected), set()
    for _, tree in cells:
        table = symtable.symtable(ast.unparse(tree), path, "exec")
        known.update(symbol.get_name() for symbol in table.get_symbols()
                     if symbol.is_assigned() or symbol.is_imported() or symbol.is_namespace())
        pending = [table]
        while pending:
            scope = pending.pop()
            referenced.update(symbol.get_name() for symbol in scope.get_symbols()
                              if symbol.is_referenced() and symbol.is_global())
            pending.extend(scope.get_children())
    missing = referenced - known - {"__name__", "__file__", "__package__", "__builtins__"}
    if missing:
        raise MissingBinding("original notebook global initialization is missing: " + ", ".join(sorted(missing)))
    return cells


class _FrozenLoader(importlib.abc.Loader):
    def __init__(self, binding, path, package):
        self.binding, self.path, self.package = binding, path, package

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        filename = str(self.binding.inspector.root / self.path)
        module.__file__ = filename
        if self.package:
            module.__path__ = [str(Path(filename).parent)]
        source = self.binding.patches.get((self.path, "file"), source_unit(self.binding.inspector, self.path))
        self.binding._inject_callsites(module.__dict__, self.path, "file")
        exec(compile(source, filename, "exec"), module.__dict__)


class _FrozenFinder(importlib.abc.MetaPathFinder):
    def __init__(self, binding):
        self.binding = binding

    def find_spec(self, fullname, path=None, target=None):
        record = self.binding.module_paths.get(fullname)
        if record:
            relative, package = record
            return importlib.util.spec_from_loader(fullname, _FrozenLoader(self.binding, relative, package), is_package=package)
        if any(name.startswith(fullname + ".") for name in self.binding.module_paths):
            child, record = next((name, record) for name, record in self.binding.module_paths.items()
                                 if name.startswith(fullname + "."))
            directory = (self.binding.inspector.root / record[0]).parent
            for _ in range(len(child.split(".")) - len(fullname.split(".")) - 1):
                directory = directory.parent
            spec = importlib.machinery.ModuleSpec(fullname, None, is_package=True)
            spec.submodule_search_locations = [str(directory)]
            return spec
        return None


class NativeBindings:
    """One native source context; every execution gate uses the supplied runtime."""
    def __init__(self, inspector, candidate, runtime, resource_binding=None):
        request = json.loads(json.dumps(inspector.request))
        self.inspector = Inspector(inspector.root, inspector.snapshots, request)
        from seampact.source.call_value_conditions import context
        if context(inspector) is not None:
            self.inspector._call_value_context = context(inspector)
        self.candidate = json.loads(json.dumps(candidate))
        self.runtime = runtime
        self.resource_binding = resource_binding
        self.boundaries = {boundary["id"]: boundary for boundary in candidate.get("boundaries", [])}
        self.report = check_candidate(self.inspector, candidate)
        self.checked = {row["id"]: row for row in self.report["boundaries"]}
        self.patches, self.recipes, self.module_paths = {}, {}, {}
        self.modules, self.notebooks, self.owners, self.entries = {}, {}, {}, {}
        self.import_only_entries = {}
        self.approved_entries, self.function_owners, self.owner_keys = {}, {}, {}
        self.programs, self.program_helpers = {}, {}
        self.effective_bindings = {}
        self.inheritance = FrozenInheritance(self.inspector)
        self.inherited_entries = {}
        self.inherited_calls = {}
        self.fixed_value_bindings = {}
        self.execution_contexts = {}
        self.import_contexts = {}
        self.import_scope_proofs = {}
        self.result_structures = {}
        self.completion_contracts = {}
        self.dependency_closure = None
        self.callsite_gates, self.entry_owner_ids = {}, {}
        self.active_owner = contextvars.ContextVar("seampact_native_binding_owner", default=None)
        self.loop, self.thread = None, None
        self.lock = threading.RLock()
        self.detached = False
        self.active_calls = 0
        # Set only while describing an entry for a separately declared host.
        self.environment_gaps = None
        for path in self.inspector.files:
            if not path.endswith(".py"):
                continue
            parts = list(Path(path).with_suffix("").parts)
            package = parts[-1] == "__init__"
            if package:
                parts.pop()
            if parts and all(part.isidentifier() for part in parts):
                self.module_paths[".".join(parts)] = (path, package)
                if parts[0] == "src" and len(parts) > 1:
                    self.module_paths[".".join(parts[1:])] = (path, package)

    def prepare_transformations(self):
        selected = {name for entry in self.approved_entries.values() for name in entry.get("boundary_ids", [])}
        callsites = {}
        for identity in selected:
            boundary = self.boundaries[identity]
            if boundary.get("rule") == "inplace-callsite/1":
                key = (boundary["path"], boundary.get("unit", "file"))
                callsites.setdefault(key, []).append(boundary)
        for key, boundaries in callsites.items():
            original = source_unit(self.inspector, *key)
            lowered = lower_callsites(original, [dict(symbol=boundary["symbol"], callsite=boundary["callsite"])
                                                 for boundary in boundaries])
            self.patches[key] = lowered["source"]
            for boundary, recipe in zip(boundaries, lowered["recipes"]):
                self.recipes[boundary["id"]] = recipe
        for identity in self.boundaries:
            if identity not in selected:
                continue
            boundary = self.boundaries[identity]
            if boundary["mode"] == "R" and boundary.get("rule") != "inplace-callsite/1":
                key = (boundary["path"], boundary.get("unit", "file"))
                original = self.patches.get(key, source_unit(self.inspector, *key))
                lowered = lower_loop(original, boundary["symbol"], boundary.get("loop_index", 0))
                self.patches[key], self.recipes[boundary["id"]] = lowered["source"], lowered["recipe"]

    def _dependencies(self, path, unit, entry=None):
        exclusions = set()
        if entry is not None:
            from seampact.source.dependency_closure import source_import_scope
            proof = source_import_scope(self.inspector, entry, self.candidate)
            self.import_scope_proofs[entry["id"]] = proof
            if proof["status"] == "CHECKED":
                exclusions = {(site["path"], site["unit"], site["line"], site["module"],
                    tuple(site["names"]), site["level"], site["kind"])
                    for site in proof["sites"] if site["status"] == "NOT_REACHED"}
        missing, installed = [], []
        queue, visited = [(path, unit)], set()
        while queue:
            current, current_unit = queue.pop()
            if (current, current_unit) in visited:
                continue
            visited.add((current, current_unit))
            facts = self.inspector.inspect_imports(current, None if current.endswith(".ipynb") else current_unit)
            if current.endswith(".ipynb"):
                stop = int(current_unit.split(":")[1])
                facts["imports"] = [item for item in facts["imports"] if int(item["unit"].split(":")[1]) <= stop]
            for issue in facts.get("unknown", []):
                if issue.get("kind") not in {"PARSE_UNSUPPORTED", "SOURCE_STRUCTURE_PARSE_UNSUPPORTED", "NATIVE_SYNTAX_UNSUPPORTED"}:
                    continue
                issue_unit = issue.get("unit", "")
                if current.endswith(".ipynb") and issue_unit.startswith("cell:") and int(issue_unit.split(":")[1]) > stop:
                    continue
                missing.append("native dependency source syntax is unsupported in this interpreter: " + current +
                               (" at line " + str(issue["line"]) if issue.get("line") is not None else ""))
            module = next((name for name, value in self.module_paths.items() if value[0] == current), "")
            package = module if current.endswith("/__init__.py") else module.rpartition(".")[0]
            for imported in facts["imports"]:
                if (current, imported.get("unit", "file"), imported.get("line"), imported.get("module"),
                        tuple(imported.get("names", [])), imported.get("level", 0), imported.get("kind")) in exclusions:
                    continue
                name = imported.get("module") or ""
                roots = explicit_import_roots(self.inspector, current, imported.get("line")) if current.endswith(".py") else []
                for root in roots:
                    self.import_contexts.setdefault(path, {})[(current, root["start"])] = dict(source=current, **root)
                if not imported.get("level"):
                    for root in reversed(roots):
                        if imported.get("line", 0) <= root["end"]:
                            continue
                        relative = (Path(root["path"]) / Path(*name.split("."))).as_posix()
                        options = [(relative + ".py", False), (relative + "/__init__.py", True)]
                        found = [record for record in options if record[0] in self.inspector.files]
                        if found:
                            if name in self.module_paths and self.module_paths[name] != found[0]:
                                raise MissingBinding("explicit native import path is ambiguous: " + name)
                            self.module_paths[name] = found[0]
                            break
                if imported.get("level"):
                    try:
                        name = importlib.util.resolve_name("." * imported["level"] + name, package)
                    except (ValueError, ImportError):
                        missing.append("relative import owner is unknown: " + name)
                        continue
                local = self.module_paths.get(name)
                if local:
                    queue.append((local[0], "file"))
                for member in imported.get("names", []):
                    nested = self.module_paths.get(name + "." + member)
                    if nested:
                        queue.append((nested[0], "file"))
                top = name.split(".")[0]
                if not top or top in sys.builtin_module_names or top in self.module_paths or any(key.startswith(top + ".") for key in self.module_paths):
                    continue
                try:
                    available = top in sys.modules or importlib.util.find_spec(top) is not None
                except (ImportError, ValueError, ModuleNotFoundError):
                    available = False
                if not available:
                    installed.append("native dependency is not installed in this execution environment: " + top)
        if missing:
            raise MissingBinding("; ".join(dict.fromkeys(missing)))
        if installed:
            self._environment("; ".join(dict.fromkeys(installed)))

    def _environment(self, message):
        if self.environment_gaps is None:
            raise MissingBinding(message)
        self.environment_gaps.append(message)

    def _resource_check(self, entry, selected):
        declared = list(entry.get("resources", []))
        for boundary in selected:
            declared.extend(boundary.get("resources", []))
            if boundary.get("unknowns"):
                raise MissingBinding("native boundary has unresolved binding/resource facts: " + boundary["id"])
        for resource in declared:
            path = resource if isinstance(resource, str) else resource.get("path") if isinstance(resource, dict) else None
            if not isinstance(path, str) or not path:
                raise MissingBinding("native resource path is not identified")
            location = Path(path).expanduser()
            if not location.is_absolute():
                location = self.inspector.root / location
            if not location.exists():
                self._environment("native resource is missing in this execution environment: " + path)

    def _fixed_values(self, entry, node, values, location, receiver=False, call_literals=None, allow_resources=False):
        """Check value origin at this parameter anchor, not string occurrence."""
        if not isinstance(values, dict):
            raise MissingBinding(location + " must be a parameter mapping")
        defaults = _literal_defaults(node, receiver) if node is not None else {}
        configuration = self.inspector.request.get("configuration") or {}
        user_entries = configuration.get("entries", {}) if isinstance(configuration, dict) else {}
        if not isinstance(user_entries, dict):
            raise MissingBinding("user configuration.entries must be an entry-ID mapping")
        user_entry = user_entries.get(entry.get("id"), {})
        if not isinstance(user_entry, dict):
            raise MissingBinding("user entry configuration must be a binding mapping")
        if location.startswith("initializers."):
            initializers = user_entry.get("initializers", {})
            if not isinstance(initializers, dict):
                raise MissingBinding("user configuration initializers must be a symbol mapping")
            configured = initializers.get(location[len("initializers."):], {})
        else:
            field = {"entry.fixed_arguments": "fixed_arguments", "constructor.arguments": "constructor_arguments",
                     "binding_program.arguments": "binding_arguments"}.get(location)
            configured = user_entry.get(field, {}) if field else {}
        rows = self.fixed_value_bindings.setdefault(entry.get("id"), [])
        for parameter, value in values.items():
            source, evidence = None, None
            reference = resource_reference(value)
            if reference is not None:
                self._resource_values(entry, location, {parameter: value})
                source, evidence = "USER_RESOURCE_REFERENCE", copy.deepcopy(reference)
            elif isinstance(configured, dict) and parameter in configured and _same_literal(value, configured[parameter]):
                source, evidence = "USER_CONFIGURATION", "configuration.entries." + str(entry.get("id")) + "." + location + "." + parameter
            elif (isinstance(configured, dict) and parameter in configured and has_resource_reference(value)
                  and _configured_resource_literals(value, configured[parameter])):
                self._resource_values(entry, location, {parameter: value})
                source, evidence = "USER_CONFIGURATION_AND_RESOURCE_REFERENCES", "configuration.entries." + str(entry.get("id")) + "." + location + "." + parameter
            elif parameter in (call_literals or {}) and _same_literal(value, call_literals[parameter]["value"]):
                source, evidence = "ORIGINAL_REQUEST_LITERAL", call_literals[parameter]["line"]
            elif parameter in defaults and _same_literal(value, defaults[parameter]["value"]):
                source, evidence = "SIGNATURE_LITERAL_DEFAULT", defaults[parameter]["line"]
            elif location == "entry.fixed_arguments" and parameter in entry.get("configuration", {}):
                plan = configuration_plans(self.resource_binding, entry).get(parameter)
                if plan and _same_literal(value, plan["original"]):
                    source, evidence = "FROZEN_CONFIGURATION_FILE", plan["filename"]
            elif allow_resources and isinstance(value, str):
                for resource in self.inspector.request.get("user_resources", []):
                    path = resource if isinstance(resource, str) else resource.get("path") if isinstance(resource, dict) else None
                    if isinstance(path, str) and _same_literal(value, path):
                        source, evidence = "USER_INITIALIZATION_RESOURCE", "user_resources"
                        break
            if source is None:
                raise MissingBinding(location + "." + parameter + " lacks an original literal default, anchored request literal, or user configuration; "
                                     "remove the fixed value and expose the task parameter, or provide the real initialization configuration")
            row = dict(location=location, parameter=parameter, status="VALUE_ORIGIN_CHECKED", source=source, evidence=evidence,
                       scope="individual fixed value origin; not complete configuration equivalence")
            if row not in rows:
                rows.append(row)

    def _resource_values(self, entry, location, values, runtime=False):
        if not isinstance(values, dict):
            raise MissingBinding(location + " must be a parameter mapping")
        if self.resource_binding is not None:
            return self.resource_binding.values(values, entry["id"], location, runtime=runtime)
        if has_resource_reference(values):
            raise MissingBinding("resource dependency registry is unavailable")
        return dict(values)

    def redact(self, error):
        return self.resource_binding.redact(error) if self.resource_binding is not None else str(error)

    def _original_defaults(self, entry, location, values):
        original = {row["parameter"] for row in self.fixed_value_bindings.get(entry.get("id"), [])
                    if row["location"] == location and row["source"] == "SIGNATURE_LITERAL_DEFAULT"}
        # Omitting the copied JSON value reuses the actual original default
        # object, including mutable defaults and their alias relationships.
        values = {name: value for name, value in values.items() if name not in original}
        return self._resource_values(entry, location, values, runtime=True)

    def _port_schema(self, annotation, path, cell="file"):
        """Resolve object ports from frozen annotations, without importing code."""
        primitive = _type_schema(annotation)
        if primitive is not None:
            return primitive
        if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
            try:
                annotation = ast.parse(annotation.value, mode="eval").body
            except SyntaxError:
                return {}
        if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
            return {"anyOf": [self._port_schema(annotation.left, path, cell), self._port_schema(annotation.right, path, cell)]}
        if isinstance(annotation, ast.Subscript):
            name = ast.unparse(annotation.value).split(".")[-1]
            items = list(annotation.slice.elts) if isinstance(annotation.slice, ast.Tuple) else [annotation.slice]
            if name in {"list", "List", "Sequence"}:
                return {"type": "array", "items": self._port_schema(items[0], path, cell)}
            if name in {"dict", "Dict", "Mapping"} and len(items) == 2 and _type_schema(items[0]) == {"type": "string"}:
                return {"type": "object", "additionalProperties": self._port_schema(items[1], path, cell)}
            if name == "Annotated":
                return self._port_schema(items[0], path, cell)
            if name in {"Optional", "Union"}:
                branches = [self._port_schema(item, path, cell) for item in items]
                if name == "Optional":
                    branches.append({"type": "null"})
                return {"anyOf": branches}
            return {}
        if not isinstance(annotation, (ast.Name, ast.Attribute)):
            return {}
        name = ast.unparse(annotation)
        tree = native_ast(source_unit(self.inspector, path, cell))
        local = next((node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == name), None)
        if local is not None:
            return {"x-native-type": {"source_version": self.inspector.request["source_version"], "path": path, "symbol": name}}
        module = next((name for name, record in self.module_paths.items() if record[0] == path), "")
        package = module if path.endswith("/__init__.py") else module.rpartition(".")[0]
        for node in tree.body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    prefix = alias.asname or alias.name.split(".")[0]
                    if name.startswith(prefix + "."):
                        qualified = (alias.name if alias.asname else prefix) + name[len(prefix):]
                        return self._qualified_port(qualified)
            elif isinstance(node, ast.ImportFrom):
                imported = node.module or ""
                if node.level:
                    try:
                        imported = importlib.util.resolve_name("." * node.level + imported, package)
                    except (ValueError, ImportError):
                        continue
                for alias in node.names:
                    prefix = alias.asname or alias.name
                    if name == prefix or name.startswith(prefix + "."):
                        return self._qualified_port(imported + "." + alias.name + name[len(prefix):])
        return {}

    def _qualified_port(self, qualified):
        for module in sorted(self.module_paths, key=len, reverse=True):
            if qualified.startswith(module + "."):
                path, _ = self.module_paths[module]
                symbol = qualified[len(module) + 1:]
                tree = native_ast(source_unit(self.inspector, path))
                if any(isinstance(node, ast.ClassDef) and node.name == symbol for node in tree.body):
                    return {"x-native-type": {"source_version": self.inspector.request["source_version"], "path": path, "symbol": symbol}}
                return {}
        module, _, symbol = qualified.rpartition(".")
        return {"x-native-type": {"module": module, "symbol": symbol}} if module and symbol else {}

    def entry_ports(self, entry):
        if entry["kind"] == "cli":
            return {}, {"x-native-type": {"module": "subprocess", "symbol": "CompletedProcess"}}
        path, cell = entry["path"], entry.get("unit", "file")
        node, _ = self._entry_anchor(entry)
        inherited = self.inherited_entries.get(entry["id"])
        if inherited:
            path, cell = inherited["method"].path, "file"
        arguments = list(node.args.posonlyargs) + list(node.args.args) + list(node.args.kwonlyargs)
        declared = self._port_schema(node.returns, path, cell)
        if entry["id"] not in self.result_structures:
            from seampact.source.result_structure import derive_result_structure
            proof = derive_result_structure(self.inspector, entry, self.candidate)
            _, proof = _select_result_structure(declared, proof)
            self.result_structures[entry["id"]] = proof
        return ({arg.arg: self._port_schema(arg.annotation, path, cell) for arg in arguments},
                copy.deepcopy(self.result_structures[entry["id"]]["effective_schema"]))

    def _entry_anchor(self, entry):
        path, cell, symbol = entry["path"], entry.get("unit", "file"), entry["symbol"]
        source = source_unit(self.inspector, path, cell)
        parts = symbol.split(".")
        if path.endswith(".py") and cell == "file" and len(parts) == 2:
            classes = [node for node in native_ast(source).body if isinstance(node, ast.ClassDef) and node.name == parts[0]]
            if len(classes) == 1 and any(not isinstance(base, ast.Name) or base.id != "object" for base in classes[0].bases):
                try:
                    chain, method = self.inheritance.resolve(path, symbol)
                except InheritanceIssue as error:
                    raise MissingBinding(str(error)) from error
                self.inherited_entries[entry["id"]] = dict(chain=chain, method=method,
                    facts=self.inheritance.facts(chain, method))
                return method.node, chain[0].node
        return _anchor(source, symbol)

    def native_matches(self, value, schema):
        if "anyOf" in schema:
            return any(self.native_matches(value, branch) for branch in schema["anyOf"])
        reference = schema.get("x-native-type")
        if reference:
            for class_ in type(value).__mro__:
                name = type.__getattribute__(class_, "__qualname__")
                module = type.__getattribute__(class_, "__module__")
                if name != reference.get("symbol"):
                    continue
                if "path" not in reference:
                    if module == reference.get("module"):
                        return True
                    continue
                if reference.get("source_version") != self.inspector.request["source_version"]:
                    continue
                if reference["path"].endswith(".ipynb"):
                    definitions = []
                    for cell, source in units(reference["path"], self.inspector.data(reference["path"])):
                        try:
                            tree = native_ast(source)
                        except SyntaxError:
                            continue
                        if any(isinstance(node, ast.ClassDef) and node.name == name for node in tree.body):
                            definitions.append(cell)
                    if len(definitions) == 1:
                        namespace = self.notebooks.get((reference["path"], definitions[0]))
                        if namespace is not None and namespace.get("__name__") == module and namespace.get(name) is class_:
                            return True
                    # Distinct notebook-prefix namespaces are distinct class
                    # creation occurrences, even when the source name matches.
                    continue
                module_record = self.module_paths.get(module)
                if not module_record or module_record[0] != reference["path"]:
                    continue
                loaded = self.modules.get(module)
                if loaded is not None and loaded.__dict__.get(name) is class_:
                    return True
            return False
        if schema.get("type") == "array" and isinstance(value, list):
            return all(self.native_matches(item, schema.get("items", {})) for item in value)
        if schema.get("type") == "object" and type(value) is dict:
            return _object_matches(value, schema, self.native_matches)
        return _matches(value, schema)

    @staticmethod
    def _source_call_arguments(node, call, receiver):
        parameters = list(node.args.posonlyargs) + list(node.args.args)
        if receiver:
            parameters = parameters[1:]
        if any(isinstance(value, ast.Starred) for value in call.args) or any(keyword.arg is None for keyword in call.keywords):
            raise MissingBinding("construction request argument unpacking requires its own binding rule")
        if len(call.args) > len(parameters):
            raise MissingBinding("construction request variadic arguments are not supported")
        values = {parameter.arg: value for parameter, value in zip(parameters, call.args)}
        for keyword in call.keywords:
            if keyword.arg in values:
                raise MissingBinding("original construction request duplicates a parameter")
            values[keyword.arg] = keyword.value
        return values

    def _global_targets(self, path, cell):
        tree = native_ast(source_unit(self.inspector, path, cell))
        module = next((name for name, record in self.module_paths.items() if record[0] == path), "")
        package = module if path.endswith("/__init__.py") else module.rpartition(".")[0]
        targets = {name: {"kind": "builtin", "symbol": name} for name in dir(builtins)}
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                targets[node.name] = {"kind": "class" if isinstance(node, ast.ClassDef) else "function",
                                      "path": path, "unit": cell, "symbol": node.name}
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                self._import_targets(node, targets, package)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                assigned = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in assigned:
                    if isinstance(target, ast.Name):
                        targets.pop(target.id, None)
        return targets, package

    def _import_targets(self, node, targets, package):
        if isinstance(node, ast.Import):
            for alias in node.names:
                targets[alias.asname or alias.name.split(".")[0]] = {"kind": "module", "module": alias.name if alias.asname else alias.name.split(".")[0]}
            return
        module = node.module or ""
        if node.level:
            try:
                module = importlib.util.resolve_name("." * node.level + module, package)
            except (ValueError, ImportError):
                return
        for alias in node.names:
            target = self._qualified_target(module + "." + alias.name)
            if target:
                targets[alias.asname or alias.name] = target

    def _qualified_target(self, qualified):
        if qualified in self.module_paths:
            return {"kind": "module", "module": qualified}
        for module in sorted(self.module_paths, key=len, reverse=True):
            if qualified.startswith(module + "."):
                path, _ = self.module_paths[module]
                symbol = qualified[len(module) + 1:]
                try:
                    node, _ = _anchor(source_unit(self.inspector, path), symbol)
                except (ValueError, SyntaxError):
                    return None
                return {"kind": "class" if isinstance(node, ast.ClassDef) else "function",
                        "path": path, "unit": "file", "symbol": symbol}
        top = qualified.split(".")[0]
        if top in getattr(sys, "stdlib_module_names", set()):
            return {"kind": "stdlib", "symbol": qualified}
        return None

    def _resolve_target(self, expression, targets):
        if isinstance(expression, ast.Name):
            return targets.get(expression.id)
        if isinstance(expression, ast.Attribute):
            parent = self._resolve_target(expression.value, targets)
            if parent and parent["kind"] == "module":
                return self._qualified_target(parent["module"] + "." + expression.attr)
            if parent and parent["kind"] in {"class", "object"}:
                try:
                    node, _ = _anchor(source_unit(self.inspector, parent["path"], parent.get("unit", "file")), parent["symbol"] + "." + expression.attr)
                except (ValueError, SyntaxError):
                    return None
                return {"kind": "function", "path": parent["path"], "unit": parent.get("unit", "file"), "symbol": parent["symbol"] + "." + expression.attr}
        return None

    def _factory_object(self, target, call, targets):
        if not target:
            return None
        if target["kind"] == "class":
            return dict(target, kind="object")
        if target.get("symbol") == "dataclasses.replace" and call.args:
            original = self._resolve_target(call.args[0], targets)
            return original if original and original["kind"] == "object" else None
        if target["kind"] != "function":
            return None
        node, _ = _anchor(source_unit(self.inspector, target["path"], target.get("unit", "file")), target["symbol"])
        # A return annotation describes an expected type, not the object that
        # the original factory actually creates. This finite inference follows
        # original constructor/alias operations only; branch factories remain
        # unknown until their control-sensitive return relation is supported.
        if any(isinstance(statement, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try,
                                      ast.With, ast.AsyncWith, ast.Match)) for statement in node.body):
            return None
        local, _ = self._global_targets(target["path"], target.get("unit", "file"))
        candidates = []
        for statement in node.body:
            if isinstance(statement, (ast.Assign, ast.AnnAssign)):
                expression = statement.value
                constructor = self._resolve_target(expression.func, local) if isinstance(expression, ast.Call) else None
                object_ = dict(constructor, kind="object") if constructor and constructor["kind"] == "class" else None
                if not isinstance(expression, ast.Call):
                    alias = self._resolve_target(expression, local)
                    object_ = alias if alias and alias["kind"] == "object" else None
                assigned = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
                for name in assigned:
                    if isinstance(name, ast.Name):
                        if object_:
                            local[name.id] = object_
                        else:
                            local.pop(name.id, None)
            elif isinstance(statement, ast.Return):
                if isinstance(statement.value, ast.Call):
                    constructor = self._resolve_target(statement.value.func, local)
                    candidates.append(dict(constructor, kind="object") if constructor and constructor["kind"] == "class" else None)
                else:
                    object_ = self._resolve_target(statement.value, local)
                    candidates.append(object_ if object_ and object_["kind"] == "object" else None)
        return candidates[0] if candidates and all(value == candidates[0] for value in candidates) else None

    def _construction_targets(self, path, cell, prefix):
        targets, package = self._global_targets(path, cell)
        facts = []
        for statement in prefix:
            if isinstance(statement, (ast.Import, ast.ImportFrom)):
                self._import_targets(statement, targets, package)
            for call in (value for value in ast.walk(statement) if isinstance(value, ast.Call)):
                target = self._resolve_target(call.func, targets)
                if not target or target["kind"] not in {"class", "function", "builtin", "stdlib"}:
                    raise MissingBinding("construction call target is dynamic or lacks a frozen/stdlib source binding at line " + str(call.lineno))
                if target.get("path"):
                    target_node, _ = _anchor(source_unit(self.inspector, target["path"], target.get("unit", "file")), target["symbol"])
                    if isinstance(target_node, ast.ClassDef) and any(isinstance(value, (ast.FunctionDef, ast.AsyncFunctionDef))
                            and value.name == "__del__" for value in target_node.body):
                        raise MissingBinding("construction target has implicit destruction outside the finite lifetime profile")
                    for value in ast.walk(target_node):
                        if isinstance(value, ast.Call) and ((isinstance(value.func, ast.Name) and value.func.id in {"eval", "exec"})
                                or isinstance(value.func, ast.Attribute) and value.func.attr in {"_getframe", "currentframe", "stack", "getrefcount"}):
                            raise MissingBinding("construction target observes dynamic execution or the added caller frame")
                facts.append(dict(line=call.lineno, column=call.col_offset, target=target))
            if isinstance(statement, (ast.Assign, ast.AnnAssign)):
                expression = statement.value
                target = self._factory_object(self._resolve_target(expression.func, targets), expression, targets) if isinstance(expression, ast.Call) else self._resolve_target(expression, targets)
                assigned = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
                for name in assigned:
                    if isinstance(name, ast.Name):
                        if target:
                            targets[name.id] = target
                        else:
                            targets.pop(name.id, None)
        return facts, targets

    def _construction(self, entry, node, owner):
        issue = python_runtime_issue()
        if issue:
            raise MissingBinding("B-CALL " + issue)
        program = entry.get("binding_program")
        if not isinstance(program, dict):
            raise MissingBinding("binding_program must describe an anchored original construction prefix")
        key = entry["id"]
        if key in self.programs:
            return self.programs[key]
        path, cell = program.get("path", entry["path"]), program.get("unit", entry.get("unit", "file"))
        if path not in self.inspector.files:
            raise MissingBinding("construction scope is outside frozen source")
        source = source_unit(self.inspector, path, cell)
        scope, enclosing = _anchor(source, program.get("scope", ""))
        if not isinstance(scope, ast.FunctionDef) or enclosing is not None or scope.decorator_list:
            raise MissingBinding("B-CALL currently requires an undecorated top-level synchronous construction function")
        if program.get("scope_lifetime", "request") != "request":
            raise MissingBinding("B-CALL currently preserves request creation occurrences; retained owner scope needs a source lifecycle binding")
        matches = [(index, statement) for index, statement in enumerate(scope.body) if statement.lineno == program.get("call_line")]
        if len(matches) != 1:
            raise MissingBinding("construction request must identify one original top-level statement line")
        index, statement = matches[0]
        call = getattr(statement, "value", None)
        if not isinstance(statement, (ast.Return, ast.Assign, ast.AnnAssign, ast.Expr)) or not isinstance(call, ast.Call):
            raise MissingBinding("construction request is not a directly evaluated original call")
        if owner is not None:
            receiver = program.get("receiver")
            if not (isinstance(call.func, ast.Attribute) and isinstance(call.func.value, ast.Name)
                    and call.func.value.id == receiver and call.func.attr == node.name):
                raise MissingBinding("construction receiver does not match the original request call")
        prefix = scope.body[:index]
        allowed = (ast.Import, ast.ImportFrom, ast.Assign, ast.AnnAssign, ast.Expr, ast.Pass)
        excluded = (ast.Await, ast.Yield, ast.YieldFrom, ast.Lambda, ast.ListComp, ast.SetComp,
                    ast.DictComp, ast.GeneratorExp, ast.NamedExpr, ast.Global, ast.Nonlocal)
        reflection = {"locals", "globals", "vars", "eval", "exec", "_getframe", "currentframe", "stack", "getrefcount"}
        for original in prefix:
            if not isinstance(original, allowed) or any(isinstance(value, excluded) for value in ast.walk(original)):
                raise MissingBinding("construction prefix is outside the straight-line B-CALL subset")
            if any(isinstance(value, ast.Call) and (isinstance(value.func, ast.Name) and value.func.id in reflection
                    or isinstance(value.func, ast.Attribute) and value.func.attr in reflection) for value in ast.walk(original)):
                raise MissingBinding("construction prefix observes a frame or object reference count")
        target_facts, prefix_targets = self._construction_targets(path, cell, prefix)
        if owner is None:
            request_target = self._resolve_target(call.func, prefix_targets)
            if not request_target or (request_target.get("path"), request_target.get("unit", "file"), request_target.get("symbol")) != (
                    entry["path"], entry.get("unit", "file"), entry["symbol"]):
                raise MissingBinding("construction request target is not the frozen original function")
        # A suffix can contain only the direct return of the call's already
        # produced value. Cleanup and other effects cannot silently be omitted.
        suffix = scope.body[index + 1:]
        assigned = (statement.targets[0].id if isinstance(statement, ast.Assign) and len(statement.targets) == 1
                    and isinstance(statement.targets[0], ast.Name) else statement.target.id
                    if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name) else None)
        if suffix and not (len(suffix) == 1 and isinstance(suffix[0], ast.Return)
                           and isinstance(suffix[0].value, ast.Name) and suffix[0].value.id == assigned):
            raise MissingBinding("construction scope has a post-request effect or cleanup without an export lifecycle rule")
        if owner is not None and any(isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)) and member.name == "__del__"
                                     for member in owner.body):
            raise MissingBinding("construction receiver has observable implicit destruction without a lifetime rule")
        profile = self.inspector.request.get("entry_profile") or {}
        if not (profile.get("confirmed") is True and profile.get("frame_observation_excluded") is True
                and profile.get("explicit_lifetime") is True):
            raise MissingBinding("B-CALL requires the user entry_profile: frame_observation_excluded and explicit_lifetime")
        arguments, tasks, references = program.get("arguments", {}), program.get("task_parameters", {}), program.get("argument_refs", {})
        if not all(isinstance(value, dict) for value in (arguments, tasks, references)):
            raise MissingBinding("construction arguments, task_parameters and argument_refs must be mappings")
        if any(not isinstance(name, str) or not isinstance(value, str) or not value.isidentifier()
               for mapping in (tasks, references) for name, value in mapping.items()):
            raise MissingBinding("construction references must name original local bindings")
        original_values = self._source_call_arguments(node, call, owner is not None)
        call_literals = {}
        for parameter, expression in original_values.items():
            try:
                call_literals[parameter] = dict(value=ast.literal_eval(expression), line=expression.lineno,
                    ast_sha256=digest(ast.dump(expression, include_attributes=False).encode()))
            except (ValueError, TypeError):
                pass
        for parameter, reference in references.items():
            expression = original_values.get(parameter)
            if not isinstance(expression, ast.Name) or expression.id != reference:
                raise MissingBinding("native object reference is not the original request argument: " + parameter)
        fixed = self._resource_values(entry, "entry.fixed_arguments", entry.get("fixed_arguments", {}))
        for parameter, expression in original_values.items():
            if parameter in references:
                continue
            if isinstance(expression, ast.Name) and expression.id in tasks and tasks[expression.id] == parameter:
                continue
            try:
                value = ast.literal_eval(expression)
            except (ValueError, TypeError):
                raise MissingBinding("original request argument needs its source-bound object reference: " + parameter) from None
            if parameter not in fixed or fixed[parameter] != value or type(fixed[parameter]) is not type(value):
                raise MissingBinding("original request literal must retain its fixed binding: " + parameter)
        scope_parameters = list(scope.args.posonlyargs) + list(scope.args.args) + list(scope.args.kwonlyargs)
        names = {parameter.arg for parameter in scope_parameters}
        if set(tasks) - names or set(arguments) - names or set(tasks) & set(arguments):
            raise MissingBinding("construction inputs do not match the original function interface")
        self._fixed_values(entry, scope, arguments, "binding_program.arguments", allow_resources=True)
        _interface(scope, self._resource_values(entry, "binding_program.arguments", arguments),
                   require_all=True, bound_refs=tasks)
        source_task_schema, _ = _interface(scope)
        task_schema = {}
        task_required = []
        for original, exported in tasks.items():
            schema = source_task_schema["properties"].get(original)
            if schema is None or "x-native-type" in schema:
                raise MissingBinding("construction task input needs a source-derived JSON parameter: " + original)
            task_schema[exported] = schema
            if original in source_task_schema["required"]:
                task_required.append(exported)
        produced = set(names)
        for value in prefix:
            if isinstance(value, ast.AnnAssign) and value.value is None:
                continue
            produced.update(child.id for child in ast.walk(value) if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store))
            if isinstance(value, (ast.Import, ast.ImportFrom)):
                produced.update(alias.asname or alias.name.split(".")[0] for alias in value.names)
        required_refs = set(references.values()) | ({program["receiver"]} if owner is not None else set())
        if required_refs - produced:
            raise MissingBinding("construction object is not produced in the original prefix: " + ", ".join(sorted(required_refs - produced)))
        facts = dict(rule="B-CALL-straight-prefix/1", status="SOURCE_BOUND", path=path, unit=cell,
            scope=scope.name, scope_lifetime="request", call_line=statement.lineno,
            source_sha256=self.inspector.files[path]["sha256"], initialization="NOT_RUN",
            call_targets=target_facts,
            nodes=[dict(line=value.lineno, end_line=value.end_lineno, kind=type(value).__name__,
                        ast_sha256=digest(ast.dump(value, include_attributes=False).encode())) for value in prefix],
            profile="USER_ENTRY_PROFILE", semantic_guarantee="CONDITIONAL; not a complete export proof")
        record = dict(spec=program, source=source, scope=scope, prefix=prefix, facts=facts,
                      task_schema=task_schema, task_required=task_required, references=references, path=path, cell=cell,
                      call_literals=call_literals)
        self.programs[key] = record
        self._dependencies(path, cell)
        return record

    def _direct_reachable(self, source, entry_symbol, boundary_symbol):
        tree = native_ast(source)
        parts = entry_symbol.split(".")
        if len(parts) == 2:
            owner = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == parts[0])
            methods = {node.name: node for node in owner.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
            pending, visited = [parts[1]], set()
            target = boundary_symbol.split(".")
            if len(target) != 2 or target[0] != parts[0]:
                return False
            while pending:
                name = pending.pop()
                if name in visited:
                    continue
                visited.add(name)
                if name == target[1]:
                    return True
                method = methods.get(name)
                if method:
                    positional = list(method.args.posonlyargs) + list(method.args.args)
                    receiver = positional[0].arg if positional else None
                    pending.extend(node.func.attr for node in ast.walk(method)
                        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and isinstance(node.func.value, ast.Name) and node.func.value.id == receiver)
            return False
        functions = {node.name: node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        pending, visited = [entry_symbol], set()
        while pending:
            name = pending.pop()
            if name in visited:
                continue
            visited.add(name)
            if name == boundary_symbol:
                return True
            function = functions.get(name)
            if function:
                pending.extend(node.func.id for node in ast.walk(function)
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name))
        return False

    def _owner_initialization(self, owner, entry, initialization_names, chain=None):
        from seampact.source.owner_constants import owner_constants, reachable_owner_nodes
        constant_chain = chain
        if constant_chain is None:
            try:
                constant_chain = self.inheritance.chain(entry["path"], owner.name)
            except (InheritanceIssue, KeyError):
                pass  # Unknown class rewrites do not become constant-field facts.
        constants = owner_constants(owner, entry, constant_chain) if constant_chain else {}
        methods = {node.name: node for node in owner.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        if chain:
            methods = {node.name: node for item in reversed(chain) for node in item.node.body
                       if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        def attributes(names):
            reads, writes, visited = set(), set(), set()
            pending = [methods[name] for name in names if name in methods]
            while pending:
                method = pending.pop()
                if id(method) in visited:
                    continue
                visited.add(id(method))
                positional = list(method.args.posonlyargs) + list(method.args.args)
                receiver = positional[0].arg if positional else None
                for node in reachable_owner_nodes(method, constants):
                    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == receiver:
                        if node.attr in methods:
                            pending.append(methods[node.attr])
                        elif isinstance(node.ctx, ast.Store):
                            writes.add(node.attr)
                        elif isinstance(node.ctx, ast.Load):
                            reads.add(node.attr)
                    if (chain and isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                            and isinstance(node.func.value, ast.Call) and isinstance(node.func.value.func, ast.Name)
                            and node.func.value.func.id == "super"):
                        if node.func.value.args or node.func.value.keywords:
                            raise MissingBinding("inherited initialization requires the original zero-argument super dispatch")
                        index = next(index for index, item in enumerate(chain) if method in item.node.body)
                        inherited = self.inheritance.method(chain[index + 1:], node.func.attr)
                        if inherited:
                            pending.append(inherited.node)
            return reads, writes
        known = {"__class__", "__dict__", "__weakref__"}
        for node in ([node for item in chain for node in item.node.body] if chain else owner.body):
            if isinstance(node, ast.Assign):
                known.update(target.id for target in node.targets if isinstance(target, ast.Name))
            elif isinstance(node, ast.AnnAssign) and node.value is not None and isinstance(node.target, ast.Name):
                known.add(node.target.id)
        _, initialized = attributes(["__init__", *initialization_names])
        reads, internal_writes = attributes([entry["symbol"].split(".")[-1]])
        missing = reads - known - initialized - internal_writes
        if missing:
            raise MissingBinding("original native owner initialization is missing for: " + ", ".join(sorted(missing)))

    def _effective_binding(self, path, cell, node, owner):
        if owner is None:
            return "function"
        decorators = [ast.unparse(value).split(".")[-1] for value in node.decorator_list]
        if not any(value in {"staticmethod", "classmethod", "property"} for value in decorators):
            return "instance"
        if (len(node.decorator_list) != 1 or not isinstance(node.decorator_list[0], ast.Name)
                or node.decorator_list[0].id not in {"staticmethod", "classmethod"}):
            raise MissingBinding("native property or combined descriptor decorators need a distinct effective binding")
        name = node.decorator_list[0].id
        targets, _ = self._global_targets(path, cell)
        if targets.get(name) != {"kind": "builtin", "symbol": name}:
            raise MissingBinding("native class descriptor is not the unshadowed builtin: " + name)
        if (owner.decorator_list or owner.keywords
                or any(not isinstance(base, ast.Name) or base.id != "object" for base in owner.bases)):
            raise MissingBinding("native class descriptor requires an undecorated direct class without inherited/metaclass dispatch")
        for member in owner.body:
            if member.lineno >= node.lineno:
                break
            rebound = (isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and member.name == name)
            if isinstance(member, (ast.Import, ast.ImportFrom)):
                rebound |= any((alias.asname or alias.name.split(".")[0]) == name for alias in member.names)
            if rebound or any(isinstance(value, ast.Name) and isinstance(value.ctx, (ast.Store, ast.Del)) and value.id == name
                              for value in ast.walk(member)):
                raise MissingBinding("native descriptor name is rebound in the original class scope: " + name)
        return name

    def describe(self, entry):
        path, cell, symbol, kind = entry["path"], entry.get("unit", "file"), entry["symbol"], entry["kind"]
        if path not in self.inspector.files:
            raise MissingBinding("native entry file is outside the frozen source scope")
        if kind == "cli":
            if "keyword_domain" in entry:
                raise MissingBinding("keyword_domain applies to an original Python **kwargs callable, not CLI argv")
            self._cli_shape(entry)
            self.execution_contexts[entry["id"]] = process_context(self.inspector, entry)
            try:
                self.completion_contracts[entry["id"]] = check_completion(self.inspector, entry)
            except ValueError as error:
                raise ContextIssue("completion_contract_unproven", "completion", str(error)) from error
        from seampact.analysis_support.candidate_checks import candidate_issues
        issues = candidate_issues(self.inspector, entry, self.candidate.get("dependencies", []))
        from seampact.source.dependency_closure import dependency_closure
        if self.dependency_closure is None:
            self.dependency_closure = dependency_closure(self.inspector, self.candidate)
        closure = self.dependency_closure["entries"].get(entry["id"], {})
        issues += closure.get("issues", [])
        if issues:
            error = MissingBinding(issues[0]["reason"])
            error.code = issues[0]["issue_code"]
            error.field_paths = issues[0]["field_paths"]
            error.candidate_issues = issues
            raise error
        initialization = initialization_gaps(self.inspector, entry)
        if initialization:
            first = initialization[0]
            operation = first["operations"][0]
            error = MissingBinding("initialization_incomplete: " + first["reason"] + " (" +
                operation["path"] + ":" + str(operation["start"]) + ")")
            error.initialization_gaps = initialization
            raise error
        if self.resource_binding is not None:
            self.resource_binding.check_entry(entry["id"])
        environment = entry.get("environment", {})
        if environment:
            if kind != "cli" or not isinstance(environment, dict):
                raise MissingBinding("resource environment mappings require the original CLI process start")
            for name, value in environment.items():
                if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) or resource_reference(value) is None:
                    raise MissingBinding("CLI environment must map original variable names to checked dependency references")
            resolved = self._resource_values(entry, "environment", environment)
            if any(not isinstance(value, str) for value in resolved.values()):
                raise MissingBinding("original CLI environment values must be strings")
        selected = []
        for identity in entry.get("boundary_ids", []):
            boundary = self.boundaries.get(identity)
            if not boundary or not self.checked[identity]["buildable"]:
                raise MissingBinding("native boundary is unsupported or absent: " + str(identity))
            selected.append(boundary)
        self._resource_check(entry, selected)
        if kind == "cli":
            if selected:
                raise MissingBinding("CLI subprocess cannot bind internal gates to this in-process native owner")
            if entry.get("configuration"):
                if self.resource_binding is None:
                    raise MissingBinding("CLI configuration resources need their checked original bindings")
                configuration_plans(self.resource_binding, entry)
            self._fixed_values(entry, None, entry.get("fixed_arguments", {}), "entry.fixed_arguments")
            return self._cli_interface(entry)
        if entry.get("configuration"):
            raise MissingBinding("configuration file injection requires the original CLI process")
        if kind not in {"function", "method"}:
            raise MissingBinding("unsupported native entry kind")
        if not path.endswith((".py", ".ipynb")):
            raise MissingBinding("this native callable host requires Python source or notebook definitions")
        if path.endswith(".py") and not any(record[0] == path for record in self.module_paths.values()):
            raise MissingBinding("native Python module path is not importable")
        source = source_unit(self.inspector, path, cell)
        if path.endswith(".ipynb"):
            _notebook_definitions(self.inspector, path, cell, self.patches)
        node, owner = self._entry_anchor(entry)
        inherited = self.inherited_entries.get(entry["id"])
        chain = inherited["chain"] if inherited else None
        if inherited and entry.get("binding_program"):
            raise MissingBinding("inherited entries currently require their original constructor binding")
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            raise MissingBinding("native entry must identify an original callable")
        if any(isinstance(value, (ast.Yield, ast.YieldFrom)) for value in ast.walk(node)):
            raise MissingBinding("streaming/generator lifecycle needs an original host binding")
        if (kind == "method") != (owner is not None):
            raise MissingBinding("entry kind does not match its native source receiver")
        effective = self._effective_binding(path, cell, node, owner)
        class_bound = effective in {"staticmethod", "classmethod"}
        receiver = owner is not None and effective != "staticmethod"
        self.effective_bindings[entry["id"]] = effective
        if class_bound:
            if entry.get("constructor") or entry.get("initializers") or entry.get("binding_program"):
                raise MissingBinding("native class descriptor binds the original class; instance construction or a request prefix would change its binding")
            if any(boundary["symbol"] != symbol or boundary["mode"] != "W"
                   or boundary.get("native_wiring") or boundary.get("rule") for boundary in selected):
                raise MissingBinding("native class descriptor currently supports only its whole original W call, without internal descriptor rewriting")
            if len(selected) > 1:
                raise MissingBinding("one original class descriptor occurrence cannot acquire duplicate whole-call gates")
        if entry.get("binding_program") and owner is not None and any(isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
                and member.name in {"__getattribute__", "__getattr__", "__setattr__"} for member in owner.body):
            raise MissingBinding("source construction receiver has dynamic attribute dispatch without its host rule")
        ports, result_schema = self.entry_ports(entry)
        program = self._construction(entry, node, owner) if entry.get("binding_program") is not None else None
        self._fixed_values(entry, node, entry.get("fixed_arguments", {}), "entry.fixed_arguments", receiver=receiver,
                           call_literals=program["call_literals"] if program else None)
        schema, omitted = _interface(node, self._resource_values(entry, "entry.fixed_arguments", entry.get("fixed_arguments", {})), receiver=receiver,
                                     native_schemas=ports, bound_refs=program["references"] if program else ())
        if program:
            for name, port in program["task_schema"].items():
                if name in schema["properties"] and schema["properties"][name] != port:
                    raise MissingBinding("construction and native entry disagree on task input type: " + name)
                schema["properties"][name] = port
                if name in program["task_required"] and name not in schema["required"]:
                    schema["required"].append(name)
            if any(isinstance(boundary.get("native_wiring"), dict) for boundary in selected):
                raise MissingBinding("construction prefix SDK registration needs a before-registration construction hook")
        if "keyword_domain" in entry:
            schema = narrow_keywords(schema, node, entry["keyword_domain"], entry.get("fixed_arguments", {}))
        if owner is not None and not program and not class_bound:
            constructor = entry.get("constructor")
            if not isinstance(constructor, dict) or constructor.get("symbol") != owner.name:
                raise MissingBinding("the original owner constructor must be identified")
            if any(ast.unparse(decorator).split(".")[-1] in {"staticmethod", "classmethod"} for decorator in node.decorator_list):
                raise MissingBinding("static/class receiver needs a distinct native binding")
            initializer_source = self.inheritance.method(chain, "__init__") if chain else None
            initializer = (initializer_source.node if initializer_source else None) if chain else next(
                (member for member in owner.body if isinstance(member, ast.FunctionDef) and member.name == "__init__"), None)
            if initializer is not None and chain and initializer.decorator_list:
                raise MissingBinding("inherited constructor decorator has no ordinary instance binding")
            if isinstance(initializer, ast.AsyncFunctionDef):
                raise MissingBinding("native constructor cannot be an async instance initializer")
            if any(isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)) and member.name == "__new__" for member in owner.body):
                raise MissingBinding("custom native allocation needs its original owner binding")
            if any(isinstance(member, ast.AsyncFunctionDef) and member.name == "__init__" for member in owner.body):
                raise MissingBinding("native constructor cannot be an async instance initializer")
            if initializer is None:
                if not chain and any(not isinstance(base, ast.Name) or base.id != "object" for base in owner.bases):
                    raise MissingBinding("inherited native constructor parameters are not identified")
                if constructor.get("arguments"):
                    raise MissingBinding("native object constructor accepts no supplied arguments")
            else:
                self._fixed_values(entry, initializer, constructor.get("arguments", {}), "constructor.arguments",
                                   receiver=True, allow_resources=True)
                _interface(initializer, self._resource_values(entry, "constructor.arguments", constructor.get("arguments", {})),
                           receiver=True, require_all=True)
            for initialization in entry.get("initializers", []):
                if initialization["symbol"].count(".") > 1 or ("." in initialization["symbol"] and initialization["symbol"].split(".")[0] != owner.name):
                    raise MissingBinding("original initializer belongs to a different native owner")
                name = initialization["symbol"].split(".")[-1]
                if chain:
                    initial_source = self.inheritance.method(chain, name)
                    if initial_source is None or initial_source.node.decorator_list:
                        raise MissingBinding("inherited initializer is absent or has an unknown descriptor")
                    initial, initial_owner = initial_source.node, initial_source.owner
                else:
                    initial, initial_owner = _anchor(source, owner.name + "." + name)
                if initial_owner is None:
                    raise MissingBinding("native initialization owner is not identified")
                if any(isinstance(value, (ast.Yield, ast.YieldFrom)) for value in ast.walk(initial)):
                    raise MissingBinding("generator initialization needs an original lifecycle binding")
                self._fixed_values(entry, initial, initialization.get("arguments", {}), "initializers." + initialization["symbol"],
                                   receiver=True, allow_resources=True)
                _interface(initial, self._resource_values(entry, "initializers." + initialization["symbol"], initialization.get("arguments", {})),
                           receiver=True, require_all=True)
            self._owner_initialization(owner, entry,
                [initialization["symbol"].split(".")[-1] for initialization in entry.get("initializers", [])], chain=chain)
        elif program:
            if entry.get("constructor") or entry.get("initializers"):
                raise MissingBinding("source construction program cannot duplicate its original constructor or initialization")
        elif entry.get("constructor") or entry.get("initializers"):
            raise MissingBinding("function entry cannot invent a native object owner")
        for boundary in selected:
            wiring = boundary.get("native_wiring")
            if inherited:
                definition = inherited["method"]
                # Prose describes the original wiring; only a structured
                # adapter changes it and is unavailable for inherited W calls.
                if (boundary["mode"] != "W" or boundary.get("rule")
                        or (wiring is not None and not isinstance(wiring, str))
                        or boundary["path"] != definition.path or boundary["symbol"] != definition.symbol
                        or boundary.get("unit", "file") != "file" or len(selected) != 1):
                    raise MissingBinding("inherited native binding supports only its whole original W definition")
                continue
            if isinstance(wiring, dict):
                if owner is None or wiring["controller_path"] != path or wiring["controller_symbol"] != owner.name:
                    raise MissingBinding("native tool-list controller does not match this entry owner")
                names = ([value.func.attr for original in program["prefix"] for value in ast.walk(original)
                          if isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute)
                          and isinstance(value.func.value, ast.Name) and value.func.value.id == program["spec"].get("receiver")]
                         if program else [item["symbol"].split(".")[-1] for item in entry.get("initializers", [])])
                if wiring["setup"] not in names:
                    raise MissingBinding("original setup must be supplied before native SDK invocation")
            elif (boundary["path"] != path or boundary.get("unit", "file") != cell
                  or not self._direct_reachable(source, symbol, boundary["symbol"])):
                raise MissingBinding("native controller-to-boundary binding is not statically identified: " + boundary["id"])
            if boundary.get("rule") == "inplace-callsite/1":
                issue = python_runtime_issue()
                if issue:
                    self._environment("R-INPLACE " + issue)
                profile = self.inspector.request.get("entry_profile") or {}
                if not (profile.get("confirmed") is True and profile.get("frame_observation_excluded") is True
                        and profile.get("explicit_lifetime") is True):
                    raise MissingBinding("R-INPLACE requires the confirmed finite native entry profile")
        self.check_nested(entry, selected)
        self._dependencies(path, cell, entry=entry)
        return schema, omitted

    def check_nested(self, entry, selected):
        path, cell, symbol = entry["path"], entry.get("unit", "file"), entry["symbol"]
        if entry["kind"] == "cli":
            return
        source = source_unit(self.inspector, path, cell)
        for region in selected:
            if region["mode"] != "R" or region.get("rule") == "inplace-callsite/1":
                continue
            controller, controller_owner = _anchor(source, region["symbol"])
            loops = [value for value in controller.body if isinstance(value, ast.For)]
            loop = loops[region.get("loop_index", 0)]
            direct = []
            for value in ast.walk(loop):
                if not isinstance(value, ast.Call):
                    continue
                if (controller_owner is not None and isinstance(value.func, ast.Attribute)
                        and isinstance(value.func.value, ast.Name) and value.func.value.id == "self"):
                    direct.append(controller_owner.name + "." + value.func.attr)
                elif controller_owner is None and isinstance(value.func, ast.Name):
                    direct.append(value.func.id)
            for child in selected:
                if child["id"] == region["id"] or child["path"] != path or child.get("unit", "file") != cell:
                    continue
                at = child.get("callsite") or {}
                inside = (child.get("rule") == "inplace-callsite/1" and child["symbol"] == region["symbol"]
                          and loop.lineno <= at.get("line", -1) <= loop.end_lineno)
                reached = any(self._direct_reachable(source, symbol, child["symbol"]) for symbol in direct)
                if inside or reached:
                    raise MissingBinding("controlled extraction would hold the resource needed by its nested gate: "
                                         + region["id"] + " -> " + child["id"])
        for whole in selected:
            callsite = whole.get("rule") == "inplace-callsite/1"
            if (whole["mode"] not in {"W", "H"} and not callsite) or isinstance(whole.get("native_wiring"), dict):
                continue
            whole_symbol = (self.checked[whole["id"]].get("lowering") or {}).get("target_symbol", whole["symbol"]) if callsite else whole["symbol"]
            for child in selected:
                if child["id"] == whole["id"]:
                    continue
                wiring = child.get("native_wiring")
                nested = (isinstance(wiring, dict) and self._direct_reachable(source, whole_symbol, symbol)) or (
                    child["path"] == path and child.get("unit", "file") == cell
                    and self._direct_reachable(source, whole_symbol, child["symbol"]))
                if nested:
                    raise MissingBinding("nested native gates require the unsupported parent/child resource rule: "
                                         + whole["id"] + " -> " + child["id"])

    def _cli_shape(self, entry):
        """Check candidate command structure before resolving any resources."""
        def fail(code, field, reason):
            raise ContextIssue(code, field, reason)
        command = entry.get("command")
        if not isinstance(command, list) or not command or any(not isinstance(argument, str) for argument in command):
            fail("cli_command_invalid", "command", "CLI requires its original script command as an argv list")
        source_path = (self.inspector.root / entry["path"]).resolve()
        if entry["symbol"] not in {"__main__", "script", "<script>", source_path.name}:
            fail("cli_symbol_invalid", "symbol", "CLI symbol must identify the original script as a whole")
        indices = [index for index, argument in enumerate(command[:2])
                   if (self.inspector.root / argument).resolve() == source_path]
        if len(indices) != 1 or indices[0] not in {0, 1}:
            fail("cli_command_invalid", "command", "CLI command must invoke this exact original script")
        index = indices[0]
        if index == 0 and entry["path"].endswith(".py") and not self.inspector.data(entry["path"]).startswith(b"#!"):
            fail("cli_interpreter_missing", "command.0", "original Python script without a shebang needs an explicit Python interpreter")
        if index == 1 and Path(command[0]).name.lower() not in {"python", "python3", "python.exe", "bash", "sh", "pwsh", "powershell", "powershell.exe"}:
            fail("cli_interpreter_invalid", "command.0", "original CLI interpreter is not identified")
        placeholders = [match.group(1) for argument in command[index + 1:]
                        for match in re.finditer(r"^\{([A-Za-z_][A-Za-z0-9_]*)\}$", argument)]
        if any(("{" in argument or "}" in argument) and not re.fullmatch(r"\{[A-Za-z_][A-Za-z0-9_]*\}", argument)
               for argument in command[index + 1:]):
            fail("cli_placeholder_invalid", "command", "CLI task placeholders must occupy complete argv elements")
        declared = entry.get("fixed_arguments", {})
        if not isinstance(declared, dict) or set(declared) - set(placeholders):
            fail("cli_placeholder_invalid", "fixed_arguments", "CLI fixed arguments must name actual command placeholders")
        return index, placeholders

    def _cli_interface(self, entry):
        index, placeholders = self._cli_shape(entry)
        command = entry["command"]
        source_path = (self.inspector.root / entry["path"]).resolve()
        if index == 1 and Path(command[0]).name.lower() not in {"python", "python3", "python.exe"} and not shutil.which(command[0]):
            raise MissingBinding("native CLI interpreter is unavailable")
        if index == 0 and not os.access(source_path, os.X_OK):
            raise MissingBinding("original CLI interpreter must be supplied")
        declared = entry.get("fixed_arguments", {})
        if not isinstance(declared, dict):
            raise MissingBinding("CLI fixed arguments must be a parameter mapping")
        if any((resource_reference(value) or {}).get("field") == "credential" for value in declared.values()):
            raise MissingBinding("private credentials cannot be supplied in original CLI command arguments")
        fixed = self._resource_values(entry, "entry.fixed_arguments", declared)
        if not isinstance(fixed, dict) or set(fixed) - set(placeholders):
            raise MissingBinding("CLI fixed arguments must name actual command placeholders")
        # Command arguments are native argv strings; no shell evaluation occurs.
        properties = {name: {"type": "string"} for name in dict.fromkeys(placeholders) if name not in fixed}
        if any(not isinstance(value, str) for value in fixed.values()):
            raise MissingBinding("native CLI argv values must be strings")
        if entry["path"].endswith(".py"):
            tree = native_ast(source_unit(self.inspector, entry["path"], entry.get("unit", "file")))
            declarations = []
            for call in ast.walk(tree):
                if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) and call.func.attr == "add_argument":
                    names = [argument.value for argument in call.args if isinstance(argument, ast.Constant) and isinstance(argument.value, str)]
                    if len(names) != len(call.args) or not names:
                        raise MissingBinding("native CLI parser contains unresolved argument declarations")
                    flags = {keyword.arg: keyword.value for keyword in call.keywords}
                    required = isinstance(flags.get("required"), ast.Constant) and flags["required"].value is True
                    action = flags.get("action")
                    no_value = isinstance(action, ast.Constant) and action.value in {"store_true", "store_false", "count", "help", "version"}
                    declarations.append((names, required, no_value))
            argv = command[index + 1:]
            if declarations:
                options = {name for names, _, _ in declarations for name in names if name.startswith("-")}
                for argument in argv:
                    flag = argument.split("=", 1)[0]
                    if flag.startswith("-") and flag not in options | {"-h", "--help"} and not re.fullmatch(r"-\d+(?:\.\d+)?", flag):
                        raise MissingBinding("native CLI option is absent from source: " + flag)
                for names, required, _ in declarations:
                    if required and not any(any(argument == name or argument.startswith(name + "=") for argument in argv) for name in names):
                        raise MissingBinding("required native CLI option is missing: " + names[0])
            elif argv:
                raise MissingBinding("native CLI argument interface is not statically identified")
        return dict(type="object", properties=properties, required=list(properties), additionalProperties=False), []

    @contextlib.contextmanager
    def _context(self):
        with _native_context_lock:
            prefixes = {name.split(".")[0] for name in self.module_paths}
            owned = lambda name: name.split(".")[0] in prefixes
            previous = {name: module for name, module in list(sys.modules.items()) if owned(name)}
            for name in previous:
                sys.modules.pop(name, None)
            sys.modules.update(self.modules)
            finder = _FrozenFinder(self)
            original_path, original_directory = list(sys.path), Path.cwd()
            sys.meta_path.insert(0, finder)
            sys.path[:0] = [str(self.inspector.root), str(self.inspector.root / "src")]
            os.chdir(self.inspector.root)
            try:
                yield
            finally:
                self.modules = {name: module for name, module in list(sys.modules.items()) if owned(name)}
                for name in self.modules:
                    sys.modules.pop(name, None)
                sys.modules.update(previous)
                sys.meta_path.remove(finder)
                sys.path[:] = original_path
                os.chdir(original_directory)

    def _namespace(self, entry):
        path, cell = entry["path"], entry.get("unit", "file")
        if path.endswith(".ipynb"):
            key = (path, cell)
            if key not in self.notebooks:
                namespace = dict(__name__="_seampact_notebook_" + digest((path + cell).encode())[:16],
                                 __file__=str(self.inspector.root / path), __package__=None, __builtins__=builtins.__dict__)
                injected = self._inject_callsites(namespace, path, None)
                for _, tree in _notebook_definitions(self.inspector, path, cell, self.patches, injected):
                    exec(compile(ast.fix_missing_locations(tree), namespace["__file__"], "exec"), namespace)
                self.notebooks[key] = namespace
            return self.notebooks[key]
        module = next((name for name, record in self.module_paths.items() if record[0] == path and not name.startswith("src.")), None)
        if module is None:
            raise MissingBinding("native Python module path is not importable")
        return importlib.import_module(module).__dict__

    def _callsite_factory(self, identity):
        def factory(callee):
            active = self.active_owner.get()
            gate = self.callsite_gates.get((identity, active))
            # A different exported entry may execute this same original code
            # without selecting this cut. Its effective callable stays native.
            if gate is None:
                return callee
            if not (inspect.isfunction(callee) or inspect.ismethod(callee)):
                raise MissingBinding("original callsite effective callable is outside its source host binding")
            boundary, recipe = self.boundaries[identity], self.recipes[identity]
            self._assert_callable(callee, recipe["target_symbol"], boundary["path"])
            return self.runtime.wrap(callee, gate, active)
        return factory

    def _inject_callsites(self, namespace, path, cell):
        helpers = []
        for identity, recipe in self.recipes.items():
            boundary = self.boundaries[identity]
            if recipe.get("rule") != "inplace-callsite/1" or boundary["path"] != path:
                continue
            if cell is not None and boundary.get("unit", "file") != cell:
                continue
            namespace[recipe["helper"]] = self._callsite_factory(identity)
            helpers.append(recipe["helper"])
        return helpers

    def _register(self, gate, owner, unit):
        if self.detached:
            raise MissingBinding("Harness source is detached")
        self.runtime.call("register", gate, owner,
            unit=dict(unit, source_id=self.inspector.request.get("source_id")))

    def _boundary_unit(self, boundary, entry_unit):
        return dict(id=self.inspector.request["source_version"][:16] + ":" + boundary["id"],
                    name=boundary["id"], symbol=boundary["symbol"], path=boundary["path"],
                    source_id=self.inspector.request.get("source_id"), source_version=entry_unit["source_version"],
                    installation_id=entry_unit["installation_id"], root_unit_id=entry_unit["id"],
                    boundary_id=boundary["id"], source_unit=boundary.get("unit", "file"), mode=boundary["mode"])

    def _assert_callable(self, callable_, symbol, path):
        functions = _native_function_chain(callable_)
        original, outer = functions[-1], functions[0]
        code = original.__code__
        if (_NATIVE_TYPE(outer.__qualname__) is not _NATIVE_STR or outer.__qualname__ != symbol
            or _NATIVE_TYPE(code.co_filename) is not _NATIVE_STR
            or Path(code.co_filename).resolve() != (self.inspector.root / path).resolve()):
            raise MissingBinding("native callable does not match its frozen source anchor: " + symbol)

    def _assert_inherited_owner(self, class_, binding):
        chain = binding["chain"]
        endpoint = chain[-1].external_endpoint
        expected_meta, tail = _NATIVE_TYPE, [_NATIVE_OBJECT]
        if endpoint:
            if endpoint != 'abc.ABC':
                raise MissingBinding('native inherited external endpoint has no finite target protocol')
            from seampact.source.abc_kernel import abc_objects, check_abc_source, check_abc_target
            checked = check_abc_source(self.inspector, chain)
            if checked['status'] != 'CHECKED':
                raise MissingBinding('native original ABC source/kernel conditions changed')
            abc_class, expected_meta = abc_objects()
            tail = [abc_class, _NATIVE_OBJECT]
        if _NATIVE_TYPE(class_) is not expected_meta:
            raise MissingBinding("native inherited owner is not an ordinary source class")
        actual = _NATIVE_MRO.__get__(class_, _NATIVE_TYPE)
        if len(actual) != len(chain) + len(tail) or any(value is not expected for value, expected in zip(actual[-len(tail):], tail)):
            raise MissingBinding("native inheritance differs from its frozen single-class chain")
        local_classes = list(actual[:len(chain)])
        if endpoint and check_abc_target(class_, local_classes, chain)['status'] != 'CHECKED':
            raise MissingBinding('native original ABC target class/meta/state conditions changed')
        for original, source in zip(actual, chain):
            if _NATIVE_TYPE(original) is not expected_meta:
                raise MissingBinding('native inherited class has an unknown metaclass')
            self.inspector.data(source.path)  # Fresh original snapshot identity.
            namespace = self._namespace(dict(path=source.path))
            class_namespace = _NATIVE_CLASS_DICT.__get__(original, _NATIVE_TYPE)
            if (_NATIVE_TYPE(namespace) is not _NATIVE_DICT or any(_NATIVE_TYPE(key) is not _NATIVE_STR for key in namespace)
                    or any(_NATIVE_TYPE(key) is not _NATIVE_STR for key in class_namespace)):
                raise MissingBinding('native inherited source namespace has unknown key protocols')
            qualname = _NATIVE_QUALNAME.__get__(original, _NATIVE_TYPE)
            module = class_namespace.get('__module__')
            if (namespace.get(source.node.name) is not original or _NATIVE_TYPE(qualname) is not _NATIVE_STR
                    or qualname != source.node.name or _NATIVE_TYPE(module) is not _NATIVE_STR
                    or module != namespace.get("__name__")):
                raise MissingBinding("native inherited class differs from its original frozen definition")
            expected_names = {member.name for member in source.node.body
                              if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))}
            actual_names = {name for name, value in class_namespace.items()
                            if _NATIVE_TYPE(value) is _NATIVE_FUNCTION or _NATIVE_TYPE(value) is _NATIVE_STATIC
                            or _NATIVE_TYPE(value) is _NATIVE_CLASS or _NATIVE_TYPE(value) is _NATIVE_PROPERTY}
            if actual_names != expected_names:
                raise MissingBinding("native inherited class method set differs from frozen source")
            for member in source.node.body:
                if not isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                descriptor = class_namespace.get(member.name)
                expected_descriptor = _NATIVE_FUNCTION
                if (len(member.decorator_list) == 1 and isinstance(member.decorator_list[0], ast.Name)
                        and member.decorator_list[0].id in {'staticmethod', 'classmethod'}):
                    expected_descriptor = _NATIVE_STATIC if member.decorator_list[0].id == 'staticmethod' else _NATIVE_CLASS
                if _NATIVE_TYPE(descriptor) is not expected_descriptor:
                    raise MissingBinding("native inherited method descriptor differs from its original source binding")
                function = descriptor.__func__ if expected_descriptor is not _NATIVE_FUNCTION else descriptor
                functions = _native_function_chain(function)
                if endpoint and len(functions) != 1:
                    raise MissingBinding('finite original ABC method acquired an unproved wrapper')
                if endpoint and function.__dict__:
                    raise MissingBinding('finite original ABC method acquired unproved callable metadata')
                self._assert_callable(function, source.node.name + "." + member.name, source.path)
                original_function = functions[-1]
                if original_function.__globals__ is not namespace:
                    raise MissingBinding('native inherited method global namespace differs from its original module')
                first_line = min([member.lineno, *(value.lineno for value in member.decorator_list)])
                if original_function.__code__.co_firstlineno != first_line:
                    raise MissingBinding("native inherited method was replaced after its source definition")

    @staticmethod
    def _source_method_descriptor(class_, name):
        for original in _NATIVE_MRO.__get__(class_, _NATIVE_TYPE):
            namespace = _NATIVE_CLASS_DICT.__get__(original, _NATIVE_TYPE)
            if any(_NATIVE_TYPE(key) is not _NATIVE_STR for key in namespace):
                raise MissingBinding('native class dictionary has unknown key protocols')
            if name in namespace:
                return namespace[name]
        raise MissingBinding('original inherited instance method is absent')

    @staticmethod
    def _instance_dictionary(owner, class_):
        for original in _NATIVE_MRO.__get__(class_, _NATIVE_TYPE):
            namespace = _NATIVE_CLASS_DICT.__get__(original, _NATIVE_TYPE)
            if any(_NATIVE_TYPE(key) is not _NATIVE_STR for key in namespace):
                raise MissingBinding('native class dictionary has unknown key protocols')
            if '__dict__' in namespace:
                descriptor = namespace['__dict__']
                if _NATIVE_TYPE(descriptor) is not _NATIVE_GETSET:
                    raise MissingBinding('native owner dictionary has an unknown descriptor')
                value = descriptor.__get__(owner, class_)
                if _NATIVE_TYPE(value) is not _NATIVE_DICT or any(_NATIVE_TYPE(key) is not _NATIVE_STR for key in value):
                    raise MissingBinding('native owner dictionary has unknown key protocols')
                return value
        return {}

    def _verify_inherited_call(self, entry, unit, callable_):
        if entry['id'] not in self.inherited_entries:
            return
        retained = self.inherited_calls.get(unit['id'])
        if retained is None or retained['callable'] is not callable_:
            raise MissingBinding('original inherited callable binding is absent or replaced')
        binding, owner, class_, record = retained['binding'], retained['owner'], retained['class'], retained['record']
        self._assert_inherited_owner(class_, binding)
        if _NATIVE_TYPE(owner) is not class_:
            raise MissingBinding('native inherited owner identity or class changed')
        fields = self._instance_dictionary(owner, class_)
        methods = {member.name for item in binding['chain'] for member in item.node.body
                   if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))}
        proxies = record.get('method_proxies', {})
        for name in methods & set(fields):
            if name not in proxies or fields[name] is not proxies[name]['method']:
                raise MissingBinding('native inherited instance shadows an original method')
        for name, method in record.get('retained_methods', {}).items():
            original = method['bound']
            if (_NATIVE_TYPE(original) is not _NATIVE_METHOD or original.__self__ is not owner
                    or original.__func__ is not self._source_method_descriptor(class_, name)):
                raise MissingBinding('retained inherited bound method differs from the current original descriptor')
            _verify_native_callable_snapshot(original, method['snapshot'])
        for proxy in proxies.values():
            _verify_native_callable_snapshot(proxy['method'], proxy['method_snapshot'])
            _verify_native_callable_snapshot(proxy['proxy'], proxy['proxy_snapshot'])
        _verify_native_callable_snapshot(callable_, retained['snapshot'])

    def _assert_entry_callable(self, callable_, entry):
        inherited = self.inherited_entries.get(entry["id"])
        if inherited:
            definition = inherited["method"]
            self._assert_callable(callable_, definition.symbol, definition.path)
        else:
            self._assert_callable(callable_, entry["symbol"], entry["path"])

    def _capture_import_only_target(self, entry, namespace):
        """Capture only already-defined native functions and class bindings."""
        if (_NATIVE_TYPE(namespace) is not _NATIVE_DICT or
                any(_NATIVE_TYPE(key) is not _NATIVE_STR for key in namespace)):
            raise MissingBinding('original module namespace has unknown key protocols')
        parts = entry['symbol'].split('.')
        classes, functions = [], []
        if entry['kind'] == 'function' and len(parts) == 1:
            function = namespace.get(parts[0])
            node, _ = self._entry_anchor(entry)
            if _NATIVE_TYPE(function) is not _NATIVE_FUNCTION or function.__globals__ is not namespace:
                raise MissingBinding('imported original function identity is UNKNOWN')
            metadata = _import_only_function_metadata(function)
            if node.decorator_list or len(metadata) or function.__code__.co_firstlineno != node.lineno:
                raise MissingBinding('original function definition or wrapper is outside the import-only subset')
            self._assert_callable(function, entry['symbol'], entry['path'])
        elif entry['kind'] == 'method' and len(parts) == 2:
            class_ = namespace.get(parts[0])
            inherited = self.inherited_entries.get(entry['id'])
            if inherited is None:
                inherited = dict(chain=self.inheritance.chain(entry['path'], parts[0]))
            # Every original class module must already have been imported by
            # the selected prefix. This check never imports an extra module.
            modules = _checked_module_table()
            for source in inherited['chain']:
                module = next((name for name, record in self.module_paths.items()
                               if record[0] == source.path and not name.startswith('src.')), None)
                if not module or _NATIVE_TYPE(modules.get(module)) is not types.ModuleType:
                    raise MissingBinding('original class source module is not already imported')
            self._assert_inherited_owner(class_, inherited)
            actual = _NATIVE_MRO.__get__(class_, _NATIVE_TYPE)
            function = self._source_method_descriptor(class_, parts[1])
            if _NATIVE_TYPE(function) is not _NATIVE_FUNCTION:
                raise MissingBinding('imported original method is not an exact instance function')
            definition = self.inheritance.method(inherited['chain'], parts[1])
            if definition is None:
                raise MissingBinding('imported original method definition is absent')
            self._assert_callable(function, definition.symbol, definition.path)
            for original, source in zip(actual, inherited['chain']):
                values = _NATIVE_CLASS_DICT.__get__(original, _NATIVE_TYPE)
                if any(_NATIVE_TYPE(key) is not _NATIVE_STR for key in values):
                    raise MissingBinding('original class has unknown key protocols')
                declared = {member.name for member in source.node.body
                            if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))}
                for name, value in values.items():
                    if name in declared:
                        target = value.__func__ if _NATIVE_TYPE(value) is _NATIVE_STATIC or _NATIVE_TYPE(value) is _NATIVE_CLASS else value
                        if _NATIVE_TYPE(target) is not _NATIVE_FUNCTION:
                            raise MissingBinding('original class method descriptor is UNKNOWN')
                        functions.append((target, _native_callable_snapshot(target), _plain_callable_defaults(target)))
                    elif name not in {'__dict__', '__weakref__', '__abstractmethods__', '_abc_impl'} and not _plain_immutable_class_value(value):
                        raise MissingBinding('original class contains mutable or unknown class state')
                classes.append((original, tuple(values.items())))
        else:
            raise MissingBinding('import-only original entry is outside the finite source binding')
        if any('__signature__' in item.__dict__ for item in _native_function_chain(function)):
            raise MissingBinding('imported original callable has an unproved signature override')
        return dict(namespace=namespace, function=function, snapshot=_native_callable_snapshot(function),
                    defaults_state=_plain_callable_defaults(function), classes=classes,
                    class_mro=tuple(_NATIVE_MRO.__get__(class_, _NATIVE_TYPE)) if classes else None,
                    class_=class_ if classes else None, inherited=inherited if classes else None,
                    methods=functions)

    def _verify_import_only_target(self, entry, receipt):
        namespace, function, class_ = receipt['namespace'], receipt['function'], receipt['class_']
        if (_NATIVE_TYPE(namespace) is not _NATIVE_DICT or
                any(_NATIVE_TYPE(key) is not _NATIVE_STR for key in namespace)):
            raise MissingBinding('imported original namespace has unknown key protocols')
        if class_ is None:
            current = namespace.get(entry['symbol'])
            proxy = receipt.get('approved_proxy')
            if current is proxy and proxy is not None:
                _verify_native_callable_snapshot(proxy, receipt['approved_proxy_snapshot'])
            elif current is not function:
                raise MissingBinding('imported original function binding changed')
            if function.__globals__ is not namespace:
                raise MissingBinding('imported original function binding changed')
        else:
            parts = entry['symbol'].split('.')
            if namespace.get(parts[0]) is not class_:
                raise MissingBinding('imported original class binding changed')
            mro = _NATIVE_MRO.__get__(class_, _NATIVE_TYPE)
            if len(mro) != len(receipt['class_mro']) or any(a is not b for a, b in zip(mro, receipt['class_mro'])):
                raise MissingBinding('imported original class method order changed')
            if self._source_method_descriptor(class_, parts[1]) is not function:
                raise MissingBinding('imported original method descriptor changed')
            for original, expected in receipt['classes']:
                values = _NATIVE_CLASS_DICT.__get__(original, _NATIVE_TYPE)
                if any(_NATIVE_TYPE(key) is not _NATIVE_STR for key in values) or len(values) != len(expected):
                    raise MissingBinding('imported original class namespace changed')
                if any(name not in values or values[name] is not value for name, value in expected):
                    raise MissingBinding('imported original class binding changed')
            for method, snapshot, defaults in receipt['methods']:
                _verify_native_callable_snapshot(method, snapshot)
                if _plain_callable_defaults(method) != defaults:
                    raise MissingBinding('imported original method defaults changed')
            if receipt['inherited']['chain'][-1].external_endpoint:
                from seampact.source.abc_kernel import check_abc_target
                if check_abc_target(class_, list(mro[:len(receipt['classes'])]), receipt['inherited']['chain'])['status'] != 'CHECKED':
                    raise MissingBinding('imported original ABC state changed')
        _verify_native_callable_snapshot(function, receipt['snapshot'])
        if _plain_callable_defaults(function) != receipt['defaults_state']:
            raise MissingBinding('imported original defaults changed')
        return function

    def _capture_imported_helpers(self, plan):
        """Capture already-imported original module functions, without calling them."""
        helpers = {}
        modules = _checked_module_table()
        for path in plan.get('modules_order', []):
            module = next((name for name, record in self.module_paths.items()
                           if record[0] == path and not name.startswith('src.')), None)
            actual = modules.get(module) if module else None
            if _NATIVE_TYPE(actual) is not types.ModuleType:
                raise MissingBinding('original imported helper module is unavailable')
            namespace = types.ModuleType.__dict__['__dict__'].__get__(actual, types.ModuleType)
            if any(_NATIVE_TYPE(key) is not _NATIVE_STR for key in namespace):
                raise MissingBinding('original helper namespace has unknown key protocols')
            for node in native_ast(source_unit(self.inspector, path, 'file')).body:
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or node.decorator_list:
                    continue
                function = namespace.get(node.name)
                if _NATIVE_TYPE(function) is not _NATIVE_FUNCTION:
                    continue
                metadata = _import_only_function_metadata(function)
                if (function.__globals__ is not namespace or function.__code__.co_firstlineno != node.lineno or
                        len(metadata)):
                    continue
                try:
                    self._assert_callable(function, node.name, path)
                    snapshot = _native_callable_snapshot(function)
                    defaults = _plain_callable_defaults(function)
                except (ValueError, TypeError):
                    continue
                helpers[(path, node.name)] = dict(namespace=namespace, function=function, snapshot=snapshot,
                    defaults=defaults, source_sha256=self.inspector.files[path]['sha256'],
                    definition_ast_sha256=digest(ast.dump(node, include_attributes=False).encode()), node=node)
        return helpers

    def _check_generator_targets(self, entry, unit, requirements):
        """Consume finite source generator obligations against actual imported functions."""
        from seampact.source.call_value_conditions import ordinary, actual_defaults
        receipt = self.import_only_entries.get(unit['id'])
        if receipt is None:
            return False
        self._verify_import_only_target(entry, receipt)
        helpers = receipt.get('helpers', {})
        def same_plain(actual, expected):
            return json.dumps(ordinary(actual), sort_keys=True, ensure_ascii=False, allow_nan=False) == json.dumps(
                ordinary(expected), sort_keys=True, ensure_ascii=False, allow_nan=False)
        for requirement in requirements:
            if (type(requirement) is not dict or requirement.get('schema') not in {
                    'source-generator-target-requirements/1', 'source-bounded-value-target-requirements/1'}
                    or requirement.get('target_status') != 'REQUIRES_NORMAL_WORKER_CHECK'
                    or requirement.get('execution_permission') is not False or requirement.get('owner_fields')):
                return False
            scope = requirement.get('binding_scope')
            target = requirement.get('target_symbol') if scope == 'module' else requirement.get('parent_symbol') if scope == 'nested' else None
            if not target or any(item.get('relation') != 'FRESH_PARENT_SAME_CALL' or item.get('target_status') == 'UNKNOWN'
                                 for item in requirement.get('same_call_captures', [])):
                return False
            item = helpers.get((requirement.get('path'), target))
            if item is None or item['source_sha256'] != requirement.get('source_sha256'):
                return False
            if scope == 'module' and item['definition_ast_sha256'] != requirement.get('definition_ast_sha256'):
                return False
            namespace, function = item['namespace'], item['function']
            if any(_NATIVE_TYPE(key) is not _NATIVE_STR for key in namespace):
                return False
            current = namespace.get(target)
            proxy = receipt.get('approved_proxy') if function is receipt['function'] else None
            if current is proxy and proxy is not None:
                _verify_native_callable_snapshot(proxy, receipt['approved_proxy_snapshot'])
            elif current is not function:
                return False
            if function.__globals__ is not namespace:
                return False
            _verify_native_callable_snapshot(function, item['snapshot'])
            if _plain_callable_defaults(function) != item['defaults']:
                return False
            for fact in requirement.get('source_globals', []):
                name = fact.get('name')
                if type(name) is not str or name not in namespace or not same_plain(namespace[name], fact.get('value')):
                    return False
            if scope == 'module':
                node = item['node']
                names = [parameter.arg for parameter in (*node.args.posonlyargs, *node.args.args)]
                defaults = actual_defaults(function, names)
                for fact in requirement.get('omitted_defaults', []):
                    name = fact.get('name')
                    if type(name) is not str or name not in defaults or not same_plain(defaults[name], fact.get('value')):
                        return False
        return True

    def _verified_runtime_trace(self, entry, unit, ctx, instance_identity):
        """Read actual targets, then replay the same complete ordinary call.

        This method runs under the normal source lock. Source requirements
        describe obligations; only independently checked in-process targets
        can discharge their own fixed gap. Other effects remain blockers.
        """
        from dataclasses import replace
        from seampact.source.call_value_conditions import contextual, runtime_requirements, digest as proof_digest
        from seampact.source.dependency_closure import resolve_resource_calls
        from seampact.source.sdk_value_summaries import RULE as SDK_RULE, GAP as SDK_GAP, verify_sdk_targets
        from seampact.source.configuration_model_flow import GAP as MODEL_GAP
        from seampact.source.pydantic_models import RULE as MODEL_RULE
        from seampact.source.pydantic_targets import verify_model_targets
        from seampact.source.activation_scope import RULE as ACTIVATION_RULE, GAP as ACTIVATION_GAP, verify_activation_targets
        from seampact.source.sdk_import_summaries import RULE as IMPORT_RULE, FINDER, GAP as IMPORT_GAP
        from seampact.source.sdk_transformer_protocols import (
            RULE as TRANSFORMER_RULE, GAP as TRANSFORMER_GAP,
            profiles_from_fixed_resources, verify_transformer_targets,
        )
        if not _native_context_lock._is_owned():
            raise MissingBinding('actual runtime targets require the original source read/use lock')
        base = getattr(self, 'call_source_inspector', self.inspector)
        ctx = replace(ctx, sdk_profiles=profiles_from_fixed_resources(self.resource_binding, entry['id']))
        shadow = contextual(base, ctx)
        trace = resolve_resource_calls(shadow, entry, self.candidate)
        if (trace.get('status') != 'CHECKED' or trace.get('unresolved_targets') or
                trace.get('activation_prefix', {}).get('status') != 'CHECKED' or
                any(gap.get('reason') not in {SDK_GAP, MODEL_GAP, TRANSFORMER_GAP, ACTIVATION_GAP, IMPORT_GAP}
                    for gap in trace.get('negative_scope_gaps', []))):
            raise MissingBinding('same-call finite source/effect/resource coverage remains UNKNOWN')
        requirements = runtime_requirements(trace)
        targets = [row for row in requirements if row['schema'] != REGISTRATION_RULE]
        if not targets:
            if trace.get('negative_scope_status') != 'CHECKED':
                raise MissingBinding('same-call effect coverage remains UNKNOWN')
            return shadow, trace
        imported = self.import_only_entries.get(unit['id'])
        if (imported is None or imported.get('instance_identity') != instance_identity):
            error = MissingBinding('actual original SDK/helper import occurrence or worker identity is absent')
            if all(row.get('input_status', 'CHECKED') == 'CHECKED' for row in targets):
                error.reason_code = ('GENERATOR_BINDING_UNKNOWN' if all(
                    row['schema'] == 'source-generator-target-requirements/1' for row in targets) else 'SDK_BINDING_UNKNOWN')
            raise error
        from seampact.source.import_only import compile_import_only_plan
        frozen_resources = (self.resource_binding.snapshot(entry_ids={entry['id']})
                            if self.resource_binding is not None else dict(requirements=[], bindings={}))
        if self.resource_binding is not None:
            self.resource_binding.check_entry(entry['id'])
        plan = compile_import_only_plan(base, entry, self.candidate, frozen_resources)
        if plan.get('status') != 'CHECKED' or plan.get('plan_digest') != imported.get('plan_digest'):
            raise MissingBinding('original import prefix/source/resource plan changed before actual target check')
        self._verify_import_only_target(entry, imported)
        verified = []
        imports = [row for row in targets if row['schema'] in {IMPORT_RULE, FINDER}]
        if imports:
            evidence = imported.get('sdk_import_evidence') or {}
            expected = {proof_digest(row) for row in imports}
            if (evidence.get('status') != 'CHECKED' or evidence.get('allowed') is not True or
                    evidence.get('plan_digest') != imported['plan_digest'] or
                    not expected <= set(evidence.get('requirement_digests', []))):
                raise MissingBinding('original SDK import/finder prerequisites lack this normal before-exec occurrence')
            verified.extend(proof_digest(row) for row in imports)
        activation = [row for row in targets if row['schema'] == ACTIVATION_RULE]
        if activation:
            checked = verify_activation_targets(shadow, entry, self.candidate, trace, binding=self, receipt=imported)
            if (checked.get('status') != 'CHECKED' or checked.get('allowed') is not True or
                    set(checked.get('requirement_digests', [])) != {proof_digest(row) for row in activation}):
                raise MissingBinding('actual original activation scope/object/no-writer premises remain UNKNOWN')
            verified.extend(checked['requirement_digests'])
        sdk = [row for row in targets if row['schema'] == SDK_RULE]
        if sdk:
            checked = verify_sdk_targets(sdk, context=imported.get('sdk_context'))
            if checked.get('status') != 'CHECKED' or checked.get('allowed') is not True:
                raise MissingBinding('actual original SDK identity/state/assets/effects remain UNKNOWN: ' + str(checked.get('reason')))
            verified.extend(proof_digest(row) for row in sdk)
        transformers = [row for row in targets if row['schema'] == TRANSFORMER_RULE]
        if transformers:
            checked = verify_transformer_targets(transformers, context=imported.get('sdk_context'), profiles=ctx.sdk_profiles)
            if checked.get('status') != 'CHECKED' or checked.get('allowed') is not True:
                raise MissingBinding('actual original transformer/native creation, state, callback or effect premises remain UNKNOWN')
            verified.extend(proof_digest(row) for row in transformers)
        bounded = [row for row in targets if row['schema'] in {
            'source-bounded-value-target-requirements/1', 'source-generator-target-requirements/1'}]
        if bounded:
            if not self._check_generator_targets(entry, unit, bounded):
                raise MissingBinding('actual bounded source helper/owner/default premises remain UNKNOWN')
            verified.extend(proof_digest(row) for row in bounded)
        models = [row for row in targets if row['schema'] == MODEL_RULE]
        if models:
            # A previously verified independent target removes only its own
            # gap before the model verifier sees this same call again.
            shadow = contextual(base, replace(ctx, runtime_verified=tuple(verified)))
            trace = resolve_resource_calls(shadow, entry, self.candidate)
            if runtime_requirements(trace) != requirements:
                raise MissingBinding('same-call runtime requirements changed during independent target checks')
            modules = {path: self.modules.get(name) for name, (path, _) in self.module_paths.items()
                       if name in self.modules}
            checked = verify_model_targets(shadow, entry, self.candidate, trace,
                imported.get('model_observation'), modules)
            if (checked.get('status') != 'CHECKED' or
                    set(checked.get('requirement_digests', [])) != {proof_digest(row) for row in models}):
                raise MissingBinding('actual original configuration model creation/read-use premises remain UNKNOWN')
            verified.extend(checked['requirement_digests'])
        if len(verified) != len(targets):
            raise MissingBinding('an original runtime target protocol has no actual worker consumer')
        shadow = contextual(base, replace(ctx, runtime_verified=tuple(verified)))
        replay = resolve_resource_calls(shadow, entry, self.candidate)
        if (runtime_requirements(replay) != requirements or replay.get('status') != 'CHECKED' or
                replay.get('unresolved_targets') or replay.get('negative_scope_status') != 'CHECKED' or
                replay.get('activation_prefix', {}).get('status') != 'CHECKED'):
            raise MissingBinding('complete same-call replay is not closed after actual target checks')
        return shadow, replay

    def _check_import_prerequisites(self, entry, unit, plan, instance_identity):
        """Recheck original prefix kernels before executing any source import."""
        from seampact.source.stdlib_definition_prefix import verify_requirements
        from seampact.source.sdk_import_summaries import RULE, FINDER, verify_before_sdk_import
        from seampact.source.call_value_conditions import digest as proof_digest
        if not _native_context_lock._is_owned():
            raise MissingBinding('original import prerequisites require the source read/use lock')
        if not verify_requirements(plan.get('stdlib_requirements', [])):
            raise MissingBinding('original stdlib definition/import kernel changed')
        requirements = [row for row in plan.get('runtime_protocol_requirements', [])
                        if row.get('schema') in {RULE, FINDER}]
        if not requirements:
            return None
        evidence = verify_before_sdk_import(requirements, plan.get('sdk_asset_plan'), self,
            entry=entry, unit=unit, plan=plan, instance_identity=instance_identity)
        if evidence.get('status') != 'CHECKED' or evidence.get('allowed') is not True:
            raise MissingBinding('original SDK import prerequisites remain UNKNOWN: ' + '; '.join(evidence.get('remaining', [])))
        return dict(evidence, plan_digest=plan['plan_digest'],
                    requirement_digests=[proof_digest(row) for row in requirements])

    def _verify_locked_runtime_call(self, entry, unit, arguments):
        # Actual ordinary values can expose a helper/target that is absent
        # from the compiler's shape-only path. Never use an empty frozen
        # requirements list to skip the actual call's read/use replay.
        if not unit.get('call_condition'):
            return
        from seampact.source.call_value_conditions import call_context
        ctx, _ = call_context(arguments)
        imported = self.import_only_entries.get(unit['id']) or {}
        self._verified_runtime_trace(entry, unit, ctx, imported.get('instance_identity'))

    def _verify_locked_call_defaults(self, entry, unit, arguments, callable_=None):
        """Recheck omitted literal defaults immediately before original use."""
        if not unit.get('call_condition'):
            return
        if not _native_context_lock._is_owned():
            raise MissingBinding('original default read/use requires the source context lock')
        from seampact.source.call_value_conditions import actual_defaults, literal_default
        node, _ = self._entry_anchor(entry)
        names = [parameter.arg for parameter in (*node.args.posonlyargs, *node.args.args)]
        defaults = dict(zip(names[-len(node.args.defaults):], node.args.defaults)) if node.args.defaults else {}
        defaults.update({parameter.arg: value for parameter, value in zip(node.args.kwonlyargs, node.args.kw_defaults)
                         if value is not None})
        omitted = set(defaults) - set(arguments) - set(entry.get('fixed_arguments', {}))
        if not omitted:
            return
        if callable_ is None:
            callable_ = self.entries.get(unit['id'])
            if callable_ is None:
                receipt = self.import_only_entries.get(unit['id'])
                if receipt is not None:
                    callable_ = self._verify_import_only_target(entry, receipt)
        if callable_ is None:
            raise MissingBinding('omitted defaults have no original current callable')
        values = actual_defaults(callable_, names)
        for name in omitted:
            expected = literal_default(defaults[name])
            if name not in values or type(values[name]) is not type(expected) or values[name] != expected:
                raise MissingBinding('original omitted default changed before call: ' + name)

    def _registration_trace(self, entry, arguments=None):
        from seampact.source.call_value_conditions import call_context, contextual, Context
        from seampact.source.dependency_closure import resolve_resource_calls
        base = getattr(self, 'call_source_inspector', self.inspector)
        ctx = Context(compiling=True) if arguments is None else call_context(arguments)[0]
        shadow = contextual(base, ctx)
        return shadow, resolve_resource_calls(shadow, entry, self.candidate)

    def _verify_registration_targets(self, entry, unit, shadow, trace, *, in_context=False):
        worlds = registration_worlds(shadow, entry, self.candidate, trace, complete_call=True)
        if not worlds:
            return
        imported = self.import_only_entries.get(unit['id'])
        if imported is None or 'registration_targets' not in imported:
            raise RegistrationUnknown('actual complete original registration targets are not independently prepared')
        original = self._verify_import_only_target(entry, imported)
        modules = registration_modules(self, worlds, in_context=in_context)
        verify_registration_targets(shadow, entry, self.candidate, trace, imported['registration_targets'], modules,
                                    entry_function=original)

    def _verify_locked_registration_call(self, entry, unit, arguments):
        closure = (self.dependency_closure or {}).get('entries', {}).get(entry['id'], {})
        requirements = (unit.get('call_condition') or {}).get('runtime_protocol_requirements', [])
        if not (any(row.get('schema') == REGISTRATION_RULE for row in requirements) or
                any(row.get('rule') == REGISTRATION_RULE for row in closure.get('external_protocols', []))):
            return
        if not _native_context_lock._is_owned():
            raise MissingBinding('registration read/use requires the original native source context lock')
        shadow, trace = self._registration_trace(entry, arguments)
        try:
            self._verify_registration_targets(entry, unit, shadow, trace, in_context=True)
        except RegistrationUnknown as error:
            raise MissingBinding(str(error)) from error

    def _owner_key(self, entry):
        # A native constructor may mutate a supplied config object. Its owner
        # identity remains the original binding, rather than the mutated config.
        identity = id(entry)
        if identity not in self.owner_keys:
            # Equal constructor values do not identify the same original
            # creation occurrence. A source-bound entry has its own owner.
            self.owner_keys[identity] = json.dumps([entry["path"], entry.get("unit", "file"),
                entry.get("id"), entry.get("binding_program"), entry.get("constructor"),
                entry.get("initializers", [])], sort_keys=True)
        return self.owner_keys[identity]

    def initialization_state(self, entry, unit):
        """Read this binding's retained native cache, never a Unit's old report."""
        if entry.get("binding_program") is not None:
            # The original B-CALL prefix creates its objects for every request,
            # including function entries whose callable itself is cached.
            return "REQUEST_SCOPED"
        if unit["id"] in self.entries:
            return "BOUND"
        if entry["kind"] == "method":
            record = self.owners.get(self._owner_key(entry))
            if record is not None and record["initialized"]:
                return "BOUND"
        return "NOT_RUN"

    def _construct(self, entry, arguments):
        program = self.programs[entry["id"]]
        specification = program["spec"]
        namespace = self._namespace(dict(path=program["path"], unit=program["cell"]))
        original = namespace.get(program["scope"].name)
        self._assert_callable(original, program["scope"].name, program["path"])
        values = self._original_defaults(entry, "binding_program.arguments", specification.get("arguments", {}))
        values.update({name: arguments[exported] for name, exported in specification.get("task_parameters", {}).items()
                       if exported in arguments})
        positional, keyword = _actual_arguments(original, values)
        bound = inspect.signature(original).bind(*positional, **keyword)
        bound.apply_defaults()
        values = dict(bound.arguments)
        for parameter in inspect.signature(original).parameters.values():
            if parameter.kind == inspect.Parameter.VAR_POSITIONAL:
                values[parameter.name] = list(values[parameter.name])
            elif parameter.kind == inspect.Parameter.VAR_KEYWORD:
                extra = values.pop(parameter.name)
                values.update(extra)
        helper = self.program_helpers.get(entry["id"])
        if helper is None:
            scope = copy.deepcopy(program["scope"])
            scope.name = "_seampact_construct_" + digest(entry["id"].encode())[:16]
            scope.decorator_list, scope.returns, scope.type_comment = [], None, None
            scope.args.defaults = []
            scope.args.kw_defaults = [None] * len(scope.args.kwonlyargs)
            for parameter in [*scope.args.posonlyargs, *scope.args.args, *scope.args.kwonlyargs,
                              *([scope.args.vararg] if scope.args.vararg else []),
                              *([scope.args.kwarg] if scope.args.kwarg else [])]:
                parameter.annotation, parameter.type_comment = None, None
            references = set(program["references"].values())
            if specification.get("receiver"):
                references.add(specification["receiver"])
            expression = ast.Dict(keys=[ast.Constant(value=name) for name in sorted(references)],
                                  values=[ast.Name(id=name, ctx=ast.Load()) for name in sorted(references)])
            scope.body = copy.deepcopy(program["prefix"]) + [ast.Return(value=expression)]
            generated = ast.fix_missing_locations(ast.Module(body=[scope], type_ignores=[]))
            exec(compile(generated, str(self.inspector.root / program["path"]), "exec"), namespace)
            helper = namespace.pop(scope.name)
            self.program_helpers[entry["id"]] = helper
        positional, keyword = _actual_arguments(helper, values)
        return helper(*positional, **keyword)

    def _load_class_entry(self, entry, unit, selected):
        namespace = self._namespace(entry)
        class_name, _, name = entry["symbol"].partition(".")
        if _NATIVE_TYPE(namespace) is not _NATIVE_DICT or any(_NATIVE_TYPE(key) is not _NATIVE_STR for key in namespace):
            raise MissingBinding('native descriptor source namespace has unknown keys')
        owner = namespace.get(class_name)
        if _NATIVE_TYPE(owner) is not _NATIVE_TYPE:
            raise MissingBinding("native descriptor owner is not the original direct source class")
        qualname = _NATIVE_QUALNAME.__get__(owner, _NATIVE_TYPE)
        owner_namespace = _NATIVE_CLASS_DICT.__get__(owner, _NATIVE_TYPE)
        if any(_NATIVE_TYPE(key) is not _NATIVE_STR for key in owner_namespace):
            raise MissingBinding('native descriptor class namespace has unknown keys')
        module = owner_namespace.get('__module__')
        if (_NATIVE_TYPE(qualname) is not _NATIVE_STR or qualname != class_name
                or _NATIVE_TYPE(module) is not _NATIVE_STR or module != namespace.get('__name__')):
            raise MissingBinding("native descriptor owner is not the original direct source class")
        effective = self.effective_bindings[entry["id"]]
        descriptor = inspect.getattr_static(owner, name)
        expected = classmethod if effective == "classmethod" else staticmethod
        if type(descriptor) is not expected:
            raise MissingBinding("original native class descriptor changed after its source binding was checked")
        self._assert_callable(descriptor.__func__, entry["symbol"], entry["path"])
        original = getattr(owner, name)
        self._assert_callable(original, entry["symbol"], entry["path"])
        # The class is created once by its original module, and cls remains that
        # exact class. Do not construct an instance or overwrite its descriptor.
        record = self.function_owners.setdefault(("native-class", entry["path"], entry.get("unit", "file"), class_name),
            dict(id="native-class-" + uuid.uuid4().hex, boundaries=set(), hooks=[]))
        boundary_unit = self._boundary_unit(selected[0], unit) if selected else unit
        gate = uuid.uuid4().hex + ":" + boundary_unit["id"]
        self._register(gate, record["id"], boundary_unit)
        callable_ = self.runtime.wrap(original, gate, record["id"])
        self.entries[unit["id"]] = callable_
        self.entry_owner_ids[unit["id"]] = record["id"]
        return callable_

    async def _load(self, entry, unit, construction=None):
        identity = unit["id"]
        if identity in self.entries and not (construction is not None and entry["kind"] == "method"):
            cached = self.entries[identity]
            self._verify_inherited_call(entry, unit, cached)
            return cached
        related = [item for item in self.approved_entries.values() if self._owner_key(item) == self._owner_key(entry)]
        selected_ids = list(dict.fromkeys(name for item in related for name in item.get("boundary_ids", [])))
        selected = [self.boundaries[name] for name in selected_ids]
        if entry["kind"] == "cli":
            def original(**arguments):
                command = list(entry["command"])
                source_path = str(self.inspector.root / entry["path"])
                actual = Path(source_path).read_bytes()
                if digest(actual) != self.inspector.files[entry["path"]]["sha256"]:
                    raise MissingBinding("original CLI script changed after its source was frozen")
                index = 0 if (self.inspector.root / command[0]).resolve() == Path(source_path).resolve() else 1
                command[index] = source_path
                if index == 1:
                    executable = Path(command[0]).name.lower()
                    command[0] = sys.executable if executable in {"python", "python3", "python.exe"} else shutil.which(command[0])
                    if not command[0]:
                        raise MissingBinding("native CLI interpreter is unavailable")
                with configuration_arguments(self.resource_binding, entry, arguments) as configured:
                    for position in range(index + 1, len(command)):
                        match = re.fullmatch(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", command[position])
                        if match:
                            command[position] = configured[match.group(1)]
                    environment = os.environ.copy()
                    environment.update(self._resource_values(entry, "environment", entry.get("environment", {}), runtime=True))
                    context = process_context(self.inspector, entry)
                    cwd = self.inspector.root / context["cwd"]
                    observation = before_completion(self.completion_contracts.get(entry["id"]), arguments, cwd)
                    result = subprocess.run(command, cwd=cwd, capture_output=True, check=False, env=environment)
                    after_completion(observation, result)
                    return redact_process_result(self.resource_binding, result) if entry.get("configuration") else result
            owner_id = "native-cli-" + uuid.uuid4().hex
            gate = uuid.uuid4().hex + ":" + identity
            self._register(gate, owner_id, unit)
            callable_ = self.runtime.wrap(original, gate, owner_id)
            self.entries[identity] = callable_
            return callable_
        if self.effective_bindings.get(entry["id"]) in {"staticmethod", "classmethod"}:
            return self._load_class_entry(entry, unit, selected)
        original_entry = None
        if construction is not None and entry["kind"] == "method":
            original_entry = getattr(construction[entry["binding_program"]["receiver"]], entry["symbol"].split(".")[-1])
            self._assert_callable(original_entry, entry["symbol"], entry["path"])
            namespace = inspect.unwrap(original_entry).__globals__
        else:
            namespace = self._namespace(entry)
        owner, owner_id = None, None
        if entry["kind"] == "method":
            key = self._owner_key(entry)
            constructor = entry.get("constructor")
            record = (dict(owner=construction[entry["binding_program"]["receiver"]],
                           id="native-owner-" + uuid.uuid4().hex, boundaries=set(), hooks=[], initialized=True)
                      if construction is not None else self.owners.get(key))
            if record is None:
                class_ = namespace[constructor["symbol"]]
                inherited = self.inherited_entries.get(entry["id"])
                if inherited:
                    self._assert_inherited_owner(class_, inherited)
                elif _NATIVE_TYPE(class_) is not _NATIVE_TYPE:
                    raise MissingBinding('source constructor has an unknown metaclass or object protocol')
                qualname = _NATIVE_QUALNAME.__get__(class_, _NATIVE_TYPE)
                if _NATIVE_TYPE(qualname) is not _NATIVE_STR or qualname != constructor['symbol']:
                    raise MissingBinding("source constructor did not resolve to the original native class")
                positional, keyword = _actual_arguments(class_, self._original_defaults(entry, "constructor.arguments", constructor.get("arguments", {})))
                owner = class_(*positional, **keyword)
                if inspect.isawaitable(owner):
                    owner = await owner
                if inherited and type(owner) is not class_:
                    raise MissingBinding("native inherited construction did not preserve its original owner")
                if inherited:
                    self._assert_inherited_owner(class_, inherited)
                    methods = {member.name for item in inherited["chain"] for member in item.node.body
                               if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))}
                    if set(self._instance_dictionary(owner, class_)) & methods:
                        raise MissingBinding("native inherited receiver shadows an original method")
                record = dict(owner=owner, id="native-owner-" + uuid.uuid4().hex, boundaries=set(), hooks=[], initialized=False)
                self.owners[key] = record
            owner, owner_id = record["owner"], record["id"]
            inherited = self.inherited_entries.get(entry['id'])
            if inherited:
                class_ = _NATIVE_TYPE(owner)
                self._assert_inherited_owner(class_, inherited)
                name = inherited['method'].node.name
                descriptor = self._source_method_descriptor(class_, name)
                if _NATIVE_TYPE(descriptor) is not _NATIVE_FUNCTION:
                    raise MissingBinding('inherited instance entry has no exact original function descriptor')
                raw = _NATIVE_METHOD(descriptor, owner)
                record.setdefault('retained_methods', {}).setdefault(name, dict(bound=raw, snapshot=_native_callable_snapshot(raw)))
                record.setdefault('method_proxies', {})
        else:
            key = self._owner_key(entry)
            record = self.function_owners.setdefault(key,
                dict(id="native-module-" + uuid.uuid4().hex, boundaries=set(), hooks=[]))
            owner_id = record["id"]
        groups = {}
        for boundary in selected:
            if boundary["id"] in record["boundaries"]:
                continue
            if boundary.get("rule") == "inplace-callsite/1":
                gate = uuid.uuid4().hex + ":" + boundary["id"]
                self._register(gate, owner_id, self._boundary_unit(boundary, unit))
                self.callsite_gates[(boundary["id"], owner_id)] = gate
                helper = self.recipes[boundary["id"]]["helper"]
                namespace[helper] = self._callsite_factory(boundary["id"])
                record["boundaries"].add(boundary["id"])
                continue
            wiring = boundary.get("native_wiring")
            if isinstance(wiring, dict) and boundary["mode"] != "R":
                key = (wiring["initializer"], wiring["tools_attribute"], wiring.get("schema_policy", "native"))
                groups.setdefault(key, {})[boundary["id"]] = boundary["symbol"]
                record["boundaries"].add(boundary["id"])
                continue
            symbol = self.recipes[boundary["id"]]["helper"] if boundary["mode"] == "R" else boundary["symbol"].split(".")[-1]
            original = getattr(owner, symbol) if owner is not None else namespace[symbol]
            expected = (entry["symbol"].split(".")[0] + "." + symbol) if owner is not None else boundary["symbol"]
            if entry["id"] in self.inherited_entries:
                expected = boundary["symbol"]
            self._assert_callable(original, expected, boundary["path"])
            gate = uuid.uuid4().hex + ":" + boundary["id"]
            self._register(gate, owner_id, self._boundary_unit(boundary, unit))
            proxy = self.runtime.wrap(original, gate, owner_id)
            if owner is not None:
                attached = install_method_proxy(owner, symbol, proxy)
                if entry['id'] in self.inherited_entries:
                    record['retained_methods'].setdefault(symbol, dict(bound=original, snapshot=_native_callable_snapshot(original)))
                    record['method_proxies'][symbol] = dict(method=attached, method_snapshot=_native_callable_snapshot(attached),
                                                           proxy=proxy, proxy_snapshot=_native_callable_snapshot(proxy))
                if ((boundary["symbol"] == entry["symbol"] or entry["id"] in self.inherited_entries)
                        and boundary["mode"] != "R"):
                    original_entry = proxy
            else:
                namespace[symbol] = proxy
            record["boundaries"].add(boundary["id"])
        for (initializer, attribute, policy), selectors in groups.items():
            hook = ToolListHandoff(self.runtime, owner, selectors, initializer, attribute,
                                   owner_id=owner_id, schema_policy=policy,
                                   source_id=self.inspector.request.get("source_id"))
            original_bind = hook.bind_current_tools
            async def bind_verified(hook=hook, original_bind=original_bind, selectors=selectors, attribute=attribute):
                if self.detached:
                    raise MissingBinding("Harness source is detached")
                tools = getattr(owner, attribute)
                for identity, symbol in selectors.items():
                    originals = [tool for tool in tools if getattr(tool, "__qualname__", None) == symbol]
                    if len(originals) != 1:
                        raise MissingBinding("selected native tool is absent or ambiguous: " + identity)
                    self._assert_callable(originals[0], symbol, self.boundaries[identity]["path"])
                await original_bind()
            hook.bind_current_tools = bind_verified
            hook.install()
            record["hooks"].append(hook)
        if owner is not None and not record["initialized"]:
            for initialization in entry.get("initializers", []):
                original = getattr(owner, initialization["symbol"].split(".")[-1])
                positional, keyword = _actual_arguments(original, self._original_defaults(entry, "initializers." + initialization["symbol"],
                                                                                        initialization.get("arguments", {})))
                result = original(*positional, **keyword)
                if inspect.isawaitable(result):
                    await result
            for hook in record["hooks"]:
                if not hook.bindings:
                    raise MissingBinding("original setup did not bind the selected native tools")
                for binding in hook.bindings:
                    boundary = self.boundaries[binding["id"]]
                    self.runtime.call("label", binding["gate"], self._boundary_unit(boundary, unit))
            record["initialized"] = True
        callable_ = original_entry or (getattr(owner, entry["symbol"].split(".")[-1]) if owner is not None else namespace[entry["symbol"]])
        if not callable(callable_):
            raise MissingBinding("native source symbol did not resolve to a callable")
        self._assert_entry_callable(callable_, entry)
        if not selected:
            gate = uuid.uuid4().hex + ":" + identity
            self._register(gate, owner_id, unit)
            callable_ = self.runtime.wrap(callable_, gate, owner_id)
        if construction is None or entry["kind"] != "method":
            self.entries[identity] = callable_
        imported = self.import_only_entries.get(identity)
        if imported is not None and entry['kind'] == 'function':
            current = imported['namespace'].get(entry['symbol'])
            if current is not imported['function']:
                chain = _native_function_chain(current)
                if current is not callable_ or chain[-1] is not imported['function']:
                    raise MissingBinding('imported original function acquired an unknown proxy')
                imported['approved_proxy'] = current
                imported['approved_proxy_snapshot'] = _native_callable_snapshot(current)
        if entry['id'] in self.inherited_entries:
            self.inherited_calls[identity] = dict(binding=self.inherited_entries[entry['id']], owner=owner,
                **{'class': _NATIVE_TYPE(owner)}, record=record, callable=callable_, snapshot=_native_callable_snapshot(callable_))
            self._verify_inherited_call(entry, unit, callable_)
        self.entry_owner_ids[identity] = owner_id
        return callable_

    async def _invoke(self, entry, unit, arguments):
        if self.detached:
            raise MissingBinding("Harness source is detached")
        with self._context():
            self._verify_locked_registration_call(entry, unit, arguments)
            self._verify_locked_runtime_call(entry, unit, arguments)
            self._verify_locked_call_defaults(entry, unit, arguments)
            # invoke() serializes this source binding. Capture readiness here,
            # after any earlier queued call has initialized the same owner.
            unit["last_initialization_before"] = self.initialization_state(entry, unit)
            binding_started = time.perf_counter()
            construction = self._construct(entry, arguments) if entry.get("binding_program") is not None else None
            try:
                callable_ = await self._load(entry, unit, construction)
            except MissingBinding:
                raise
            except Exception as error:
                raise MissingBinding("original native binding/initialization failed: " + str(error)) from error
            unit["last_binding_duration_ms"] = (time.perf_counter() - binding_started) * 1000
            values = self._original_defaults(entry, "entry.fixed_arguments", entry.get("fixed_arguments", {}))
            if construction is not None:
                program_record = self.programs[entry["id"]]
                for row in self.fixed_value_bindings.get(entry["id"], []):
                    if row["location"] == "entry.fixed_arguments" and row["source"] == "ORIGINAL_REQUEST_LITERAL":
                        values[row["parameter"]] = copy.deepcopy(program_record["call_literals"][row["parameter"]]["value"])
            values.update(arguments)
            if construction is not None:
                program = entry["binding_program"]
                for exported in program.get("task_parameters", {}).values():
                    if exported not in inspect.signature(callable_).parameters:
                        values.pop(exported, None)
                values.update({parameter: construction[reference] for parameter, reference in program.get("argument_refs", {}).items()})
            positional, keyword = _actual_arguments(callable_, values)
            if self.detached:
                raise MissingBinding("Harness source is detached")
            active = self.active_owner.set(self.entry_owner_ids.get(unit["id"]))
            try:
                self._verify_inherited_call(entry, unit, callable_)
                self._verify_locked_registration_call(entry, unit, arguments)
                self._verify_locked_runtime_call(entry, unit, arguments)
                self._verify_locked_call_defaults(entry, unit, arguments, callable_)
                result = callable_(*positional, **keyword)
                return await result if inspect.iscoroutinefunction(callable_) else result
            finally:
                self.active_owner.reset(active)

    def prepare(self, entry, unit):
        """Run the original owner construction/module import early; never the entry call."""
        if entry.get("binding_program") is not None or entry["kind"] == "cli":
            raise MissingBinding("request-scoped construction or per-call process cannot be prepared ahead of a request")
        async def load():
            with self._context():
                before = self.initialization_state(entry, unit)
                started = time.perf_counter()
                await self._load(entry, unit)
                return dict(initialization_before=before, initialization_after=self.initialization_state(entry, unit),
                            duration_ms=(time.perf_counter() - started) * 1000)
        return self._run(load())

    def invoke(self, entry, unit, arguments):
        return self._run(self._invoke(entry, unit, arguments))

    def _run(self, coroutine):
        with self.lock:
            if self.loop is None:
                self.loop = asyncio.new_event_loop()
                def run():
                    asyncio.set_event_loop(self.loop)
                    self.loop.run_forever()
                self.thread = threading.Thread(target=run, daemon=True, name="seampact-native-owner")
                self.thread.start()
            self.active_calls += 1
            try:
                return asyncio.run_coroutine_threadsafe(coroutine, self.loop).result()
            except Exception as error:
                message = self.redact(error)
                if message != str(error):
                    raise MissingBinding(message) from None
                raise
            finally:
                self.active_calls -= 1


class UnitCatalog:
    def __init__(self, home, store, runtime):
        self.home, self.store, self.runtime = Path(home), store, runtime
        self.bindings, self.entry_specs = {}, {}
        # Structurally checked entries whose package/file/credential facts belong to a group host.
        self.deferred = {}
        # Structure-only entries retain no callable/dispatch permission. They
        # are deliberately separate from ready and ordinary deferred hosts.
        self.structural = {}
        self.lock = threading.RLock()
        self.remote_router = None
        self.resource_registry = None
        self.host_check = None

    def install(self, inspector, candidate, analysis_id, installation_id=None):
        with self.lock:
            installation_id = installation_id or analysis_id
            old_bindings, old_specs, old_deferred = self.bindings.copy(), self.entry_specs.copy(), self.deferred.copy()
            old_structural = self.structural.copy()
            try:
                with self.store.transaction():
                    if getattr(inspector, "target_runtime", None):
                        from seampact.target_runtime import install_catalog
                        result = install_catalog(self, inspector, candidate, analysis_id, installation_id)
                    else:
                        result = self._install(inspector, candidate, analysis_id, installation_id)
                    # Preserve matching operator-selected group environments.
                    from seampact.target_runtime import environment_selection
                    basis = environment_selection(inspector.request.get("runtime_environment") or {})
                    for identity in {unit.get("runtime_group") for unit in result} - {None}:
                        group = self.store.get("runtime_group", identity)
                        artifact = self.store.get("deployment", identity) or {}
                        if (artifact.get("source_version") == group["source_version"]
                                and artifact.get("contract_digest") == group["contract"]["digest"]
                                and artifact.get("source_runtime_environment") == basis):
                            group["runtime_environment"] = environment_selection(artifact["environment"])
                            self.store.save("runtime_group", identity, group)
                    from seampact.typed_contract import build_contract, entry_evidence, installation_evidence
                    evidence = installation_evidence(inspector, candidate)
                    target_checks = {}
                    if getattr(inspector, "target_runtime", None) and any(unit["id"] in self.deferred for unit in result):
                        checked = check_candidate(inspector, dict(candidate, boundaries=candidate.get("boundaries", [])))
                        if checked.get("source_version") != inspector.request["source_version"]:
                            raise ValueError("target boundary checks do not match the frozen source")
                        target_checks = {row["id"]: row for row in checked["boundaries"]}
                    for raw_entry, unit in zip(candidate["entries"], result):
                        group = self.store.get("runtime_group", unit.get("runtime_group")) if unit.get("runtime_group") else None
                        entry = raw_entry if isinstance(raw_entry, dict) else {}
                        binding = (self.bindings.get(unit["id"]) or (self.deferred.get(unit["id"]) or
                                   self.structural.get(unit["id"]) or (None,))[0])
                        checked = getattr(binding, "checked", target_checks)
                        unit["typed_evidence"] = dict(evidence, **entry_evidence(inspector, candidate, unit, entry, checked))
                        unit["typed_contract"] = build_contract(unit, group=group)
                        self.store.save("unit", unit["id"], unit)
                        if binding is not None:
                            binding.typed_installation_evidence = evidence
                            if not hasattr(binding, "typed_entry_evidence"):
                                binding.typed_entry_evidence = {}
                            binding.typed_entry_evidence[unit["id"]] = copy.deepcopy(unit["typed_evidence"])
                return result
            except BaseException:
                self.bindings, self.entry_specs, self.deferred = old_bindings, old_specs, old_deferred
                self.structural = old_structural
                raise

    def detach(self, source_id):
        with self.lock:
            count = 0
            for unit in self.store.list("unit"):
                if unit.get("source_id") != source_id:
                    continue
                binding = (self.bindings.get(unit["id"]) or (self.deferred.get(unit["id"]) or
                           self.structural.get(unit["id"]) or (None,))[0])
                if binding:
                    binding.detached = True
                if unit.get("status") in {"SUPERSEDED", "DETACHED"}:
                    continue
                unit.update(status="DETACHED", detached_at=now())
                self.store.save("unit", unit["id"], unit)
                count += 1
            self.runtime.call("detach_source", source_id)
            return count

    def source_running(self, source_id):
        with self.lock:
            return (any(binding.active_calls for binding in self.bindings.values()
                       if binding.inspector.request.get("source_id") == source_id) or
                    any((call.get("unit") or {}).get("source_id") == source_id and
                        call["physical"] in {"AUTHORIZED", "STARTED", "UNKNOWN"}
                        for call in self.runtime.call("active_view")["calls"].values()))

    def _candidate_binding(self, inspector, candidate):
        if not isinstance(candidate, dict) or "entries" not in candidate or not isinstance(candidate["entries"], list):
            raise ValueError("candidate.entries must be an array")
        normalized = dict(candidate, boundaries=candidate.get("boundaries", []))
        try:
            resources = DependencyBindings(self.resource_registry, inspector, normalized) if self.resource_registry is not None else None
            if resources is not None:
                resources.host_check = self.host_check
            binding, binding_error = NativeBindings(inspector, normalized, self.runtime, resource_binding=resources), None
        except (ValueError, KeyError, TypeError, SyntaxError) as error:
            binding, binding_error = None, str(error)
        identifiers = [entry.get("id") for entry in candidate.get("entries", [])
                       if isinstance(entry, dict) and isinstance(entry.get("id"), str)]
        duplicates = {identity for identity in identifiers if identifiers.count(identity) > 1}
        return binding, binding_error, duplicates

    @staticmethod
    def _inspect_entry(binding, binding_error, entry, source_id, duplicates, seen):
        from seampact.capabilities import capability_card
        result = dict(status="MISSING", reason=None, parameters=None, input_schema=None,
                      result_schema={}, native_input_schemas={}, construction=None, fixed_value_bindings=[],
                      operations={"export_entry": {"status": "UNKNOWN", "formal_proof": False}})
        result["capability"] = capability_card()
        structure_prerequisites = False
        try:
            result["capability"] = capability_card(entry.get("capability"))
            if not isinstance(source_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", source_id) or source_id in seen or source_id in duplicates:
                raise MissingBinding("entry IDs must be unique ASCII identifiers")
            seen.add(source_id)
            if binding_error:
                raise MissingBinding(binding_error)
            for required in ("path", "symbol", "kind"):
                if not isinstance(entry.get(required), str) or not entry[required]:
                    raise MissingBinding("native entry field is missing: " + required)
            structure_prerequisites = True
            gaps = []
            binding.environment_gaps = gaps
            if binding.resource_binding is not None:
                binding.resource_binding.environment_gaps = gaps
            try:
                parameters, omitted = binding.describe(entry)
            finally:
                binding.environment_gaps = None
                if binding.resource_binding is not None:
                    binding.resource_binding.environment_gaps = None
            _, result_schema = binding.entry_ports(entry)
            construction = binding.programs.get(entry.get("id"))
            from seampact.source.call_value_conditions import context, compile_condition
            closure = (binding.dependency_closure or {}).get('entries', {}).get(entry.get('id'), {})
            if context(binding.inspector) is None and any(row.get('rule') == REGISTRATION_RULE
                    for row in closure.get('external_protocols', [])):
                raise MissingBinding('actual complete original registration targets and read/use conditions are required')
            if context(binding.inspector) is None and compile_condition(binding.inspector, entry, binding.candidate) is not None:
                raise MissingBinding('actual call values are required for the finite negative scope')
            result.update(status="READY", parameters=parameters, input_schema=parameters,
                          omitted_object_parameters=omitted, binding="SOURCE_CHECKED_NATIVE_ENTRY",
                          result_schema=result_schema,
                          native_input_schemas={name: port for name, port in parameters["properties"].items()
                                                if "x-native-type" in port},
                          construction=construction["facts"] if construction else None,
                          fixed_value_bindings=binding.fixed_value_bindings.get(entry.get("id"), []),
                          effective_binding=binding.effective_bindings.get(entry.get("id"), "cli"),
                          inheritance_binding=(binding.inherited_entries.get(entry.get("id")) or {}).get("facts"),
                          execution_context=binding.execution_contexts.get(entry.get("id")),
                          import_context=list(binding.import_contexts.get(entry.get("path"), {}).values()),
                          dependency_import_scope=binding.import_scope_proofs.get(entry.get("id")),
                          result_structure=binding.result_structures.get(entry.get("id")),
                          completion_contract=binding.completion_contracts.get(entry.get("id")),
                          dependency_closure=(binding.dependency_closure or {}).get("entries", {}).get(entry.get("id")),
                          operations={"export_entry": {"status": "SOURCE_BOUND", "formal_proof": False,
                                       "scope": "user-confirmed prototype native binding; semantic export obligations remain conditional"}})
            if gaps:
                gaps = list(dict.fromkeys(gaps))
                result.update(status="MISSING", reason="; ".join(gaps), host_condition=dict(
                    status="ENVIRONMENT_DEFERRED", gaps=gaps,
                    scope="source, interface and construction rules passed; these execution-environment facts are re-checked by the runtime-group host"))
        except (ValueError, KeyError, TypeError, SyntaxError, OSError) as error:
            # Compile an enforced conditional binding, never change the
            # original entry/resource audit to a universal CHECKED result.
            if binding is not None:
                from seampact.source.call_value_conditions import compile_condition, contextual, context, Context
                if context(binding.inspector) is None:
                    recipe = compile_condition(binding.inspector, entry, binding.candidate)
                    if recipe is not None:
                        shadow = contextual(binding.inspector, Context(compiling=True))
                        resources = (DependencyBindings(binding.resource_binding.registry, shadow, binding.candidate)
                                     if binding.resource_binding is not None else None)
                        if resources is not None:
                            resources.host_check = binding.resource_binding.host_check
                        conditional = NativeBindings(shadow, binding.candidate, binding.runtime, resource_binding=resources)
                        conditional.call_source_inspector = binding.inspector
                        alternative = UnitCatalog._inspect_entry(conditional, None, entry, source_id, set(), set())
                        if alternative['status'] == 'READY' or alternative.get('host_condition'):
                            conditional.prepare_transformations()
                            if not hasattr(binding, 'call_bindings'):
                                binding.call_bindings = {}
                            binding.call_bindings[entry['id']] = conditional
                            alternative.update(status='CALL_CONDITION_DEFERRED', call_condition=recipe,
                                binding='SOURCE_CONDITIONAL_NATIVE_ENTRY',
                                reason='actual group-port/3 call values and original runtime bindings must be checked')
                            alternative['operations']['export_entry']['status'] = 'CONDITIONAL_BOUND'
                            alternative['dependency_closure'] = (binding.dependency_closure or {}).get('entries', {}).get(entry['id'])
                            for name in ('dependency_import_scope', 'result_structure'):
                                if isinstance(alternative.get(name), dict):
                                    alternative[name] = dict(alternative[name], status='CONDITIONAL', required_call_condition=recipe['digest'])
                            return alternative
            reason = binding.redact(error) if binding is not None else str(error)
            result["reason"] = reason
            for attribute in ("code", "field_paths", "structured_issues", "candidate_issues"):
                if hasattr(error, attribute):
                    result[attribute] = getattr(error, attribute)
            if binding is not None:
                result["dependency_closure"] = (binding.dependency_closure or {}).get("entries", {}).get(entry.get("id"))
                result["dependency_import_scope"] = binding.import_scope_proofs.get(entry.get("id"))
                result["result_structure"] = binding.result_structures.get(entry.get("id"))
            if getattr(error, "initialization_gaps", None):
                result["code"] = "initialization_incomplete"
                result["initialization_gaps"] = error.initialization_gaps
            if entry.get("binding_program"):
                result["construction"] = {"rule": "B-CALL-straight-prefix/1", "status": "UNKNOWN", "reason": reason}
            if binding is not None:
                result["fixed_value_bindings"] = binding.fixed_value_bindings.get(entry.get("id"), [])
                if entry.get("id") in binding.effective_bindings:
                    result["effective_binding"] = binding.effective_bindings[entry["id"]]
                if entry.get("id") in binding.inherited_entries:
                    result["inheritance_binding"] = binding.inherited_entries[entry["id"]]["facts"]
        if result['status'] == 'MISSING' and binding is not None and structure_prerequisites:
            from seampact.source.value_conditioned_admission import compile_structure_plan
            structure = compile_structure_plan(binding.inspector, entry, binding.candidate)
            if structure.get('status') == 'STRUCTURE_CHECKED':
                result.update(status='STRUCTURE_ONLY', binding='SOURCE_VALUE_CONDITIONED_NATIVE_ENTRY',
                    structure_admission=structure, parameters=copy.deepcopy(structure['interface']),
                    input_schema=copy.deepcopy(structure['interface']),
                    result_schema=copy.deepcopy(structure['declared_result_schema']),
                    native_input_schemas={}, effective_binding='function',
                    reason='original structure checked; same-call source and actual worker proofs still required')
                result['operations']['export_entry'] = dict(status='REQUIRES_SAME_CALL_PROOF', formal_proof=False)
        return result

    def inspect_candidate(self, inspector, candidate):
        """Return the same static entry checks as installation, without state writes."""
        if getattr(inspector, "target_runtime", None):
            return inspector.target_runtime.call("inspect_candidate", candidate=candidate)
        binding, _, rows = self._candidate_checks(inspector, candidate)
        from seampact.hosting.deployment import runtime_groups
        entries = [dict(id=row["entry_id"], path=row["entry"].get("path"),
            unit=row["entry"].get("unit", "file"), symbol=row["entry"].get("symbol"),
            kind=row["entry"].get("kind"), **row["check"]) for row in rows]
        ready = sum(entry["status"] == "READY" for entry in entries)
        return dict(schema="native-entry-report/1", source_version=inspector.request["source_version"],
            entries=entries, status="READY" if ready == len(entries) and ready else "PARTIAL" if ready else "MISSING",
            has_bindable_entries=bool(ready), reason=None if ready else "no bindable native entry was discovered",
            initialization="NOT_RUN", parallel=False, remote=False,
            runtime_groups=runtime_groups(inspector, candidate, rows, binding.resource_binding if binding else None))

    def _candidate_checks(self, inspector, candidate):
        binding, binding_error, duplicates = self._candidate_binding(inspector, candidate)
        rows, seen, groups = [], set(), {}
        for index, value in enumerate(candidate["entries"]):
            entry = dict(value) if isinstance(value, dict) else {}
            identity = entry.get("id") or "entry-" + str(index)
            checked = self._inspect_entry(binding, binding_error, entry, identity, duplicates, seen)
            row = dict(index=index, entry_id=identity, entry=entry, check=checked)
            rows.append(row)
            if checked["status"] == "READY" or checked.get("host_condition"):
                groups.setdefault(binding._owner_key(entry), []).append(row)
        checked = lambda row: row["check"]["status"] == "READY" or row["check"].get("host_condition")
        for group in groups.values():
            versions = {}
            for row in group:
                entry = row["entry"]
                versions.setdefault((entry["path"], entry.get("unit", "file"), entry["symbol"]), []).append(row)
            for alternatives in versions.values():
                cuts = {tuple(sorted(row["entry"].get("boundary_ids", []))) for row in alternatives}
                if len(cuts) > 1:
                    for row in alternatives:
                        row["check"].pop("host_condition", None)
                        row["check"].update(status="MISSING",
                            reason="different granularities of one native entry require separate alternative bindings")
            group = [row for row in group if checked(row)]
            selected_ids = list(dict.fromkeys(identity for row in group for identity in row["entry"].get("boundary_ids", [])))
            selected = [binding.boundaries[identity] for identity in selected_ids]
            try:
                for row in group:
                    binding.check_nested(row["entry"], selected)
            except (ValueError, KeyError, TypeError, SyntaxError) as error:
                for row in group:
                    row["check"].pop("host_condition", None)
                    row["check"].update(status="MISSING", reason=str(error))
        return binding, duplicates, rows

    def _install(self, inspector, candidate, analysis_id, installation_id):
        binding, duplicates, rows = self._candidate_checks(inspector, candidate)
        source_id = inspector.request.get("source_id")
        if source_id:
            for previous in self.store.list("unit"):
                if previous.get("source_id") == source_id and previous.get("installation_id", previous.get("analysis_id")) != installation_id:
                    previous.update(status="SUPERSEDED", superseded_by=analysis_id)
                    self.store.save("unit", previous["id"], previous, "unit_superseded")
        result = []
        for row in rows:
            index, entry, source_id, checked = row["index"], row["entry"], row["entry_id"], row["check"]
            identity = str(installation_id) + ":" + str(source_id)
            if isinstance(source_id, str) and source_id in duplicates:
                identity += ":" + str(index)
            unit = dict(schema="native-unit/1", id=identity, entry_id=source_id,
                name=entry.get("name") or source_id, tool_name="unit_" + digest(identity.encode())[:32],
                path=entry.get("path"), source_unit=entry.get("unit", "file"), symbol=entry.get("symbol"),
                kind=entry.get("kind"), description=entry.get("description") or entry.get("symbol") or "Native entry",
                source_id=inspector.request.get("source_id"), source_version=inspector.request["source_version"],
                semantic_key=inspector.request["source_version"][:16] + ":" + str(source_id),
                analysis_id=analysis_id, installation_id=installation_id, installed_at=now(), status="MISSING", reason=None,
                parameters=None, input_schema=None, initialization="NOT_RUN", result_type=None,
                parallel=False, remote=False, boundary_ids=entry.get("boundary_ids", []))
            unit.update(checked)
            if checked["status"] == "READY":
                self.bindings[identity], self.entry_specs[identity] = binding, entry
                binding.approved_entries[identity] = entry
            elif checked.get('call_condition'):
                conditional = binding.call_bindings[source_id]
                conditional.approved_entries[identity] = entry
                self.deferred[identity] = (conditional, entry)
            elif checked.get("host_condition"):
                self.deferred[identity] = (binding, entry)
            elif checked.get('binding') == 'SOURCE_VALUE_CONDITIONED_NATIVE_ENTRY':
                self.structural[identity] = (binding, entry)
            self.store.save("unit", identity, unit, "unit_installed")
            result.append(unit)
        if binding is not None:
            try:
                binding.prepare_transformations()
            except (ValueError, KeyError, TypeError, SyntaxError) as error:
                for unit in result:
                    if unit["status"] == "READY":
                        unit.update(status="MISSING", reason="native R binding could not be prepared: " + str(error))
                        self.bindings.pop(unit["id"], None)
                        self.entry_specs.pop(unit["id"], None)
                        self.store.save("unit", unit["id"], unit, "unit_binding_missing")
        from seampact.hosting.deployment import runtime_groups
        group_report = runtime_groups(inspector, candidate, rows, binding.resource_binding if binding else None)
        for group in group_report["groups"]:
            group.update(installation_id=installation_id, analysis_id=analysis_id,
                units=[unit["id"] for unit in result if unit["entry_id"] in group["entries"]])
            self.store.save("runtime_group", group["id"], group)
            for unit in result:
                if unit["id"] not in group["units"]:
                    continue
                unit["runtime_group"] = group["id"]
                unit["operations"]["remote"] = group["remote_conditions"][unit["entry_id"]]
                unit["operations"]["group_host"] = group["host"]["members"][unit["entry_id"]]
                unit["remote"] = unit["operations"]["remote"]["status"] == "REMOTE_CHECKED"
                self.store.save("unit", unit["id"], unit)
        return result

    def public(self):
        with self.lock:
            result = []
            for unit in self.store.list("unit"):
                if unit.get("status") in {"SUPERSEDED", "DETACHED"}:
                    continue
                if unit["status"] == "READY" and unit["id"] not in self.bindings:
                    unit.update(status="MISSING", reason="native binding is not loaded in this process")
                deferred = unit["status"] != "READY" and unit["id"] in self.deferred
                dispatch = self.remote_router.dispatch(unit, deferred) if self.remote_router else None
                unit["execution"] = "REMOTE" if dispatch and dispatch["status"] == "READY_INSTANCE" else "LOCAL"
                unit["dispatch"] = dispatch or dict(status="READY_LOCAL" if unit["status"] == "READY" else "UNAVAILABLE",
                                                    reason=unit.get("reason"))
                if deferred and unit["execution"] == "REMOTE" and not unit.get('call_condition'):
                    unit.update(status="READY", reason=None, local_status="ENVIRONMENT_DEFERRED")
                unit["scheduling"] = self.store.get("unit_policy", unit["id"]) or dict(
                    visibility="business", activation="business", declared=False)
                from seampact.capabilities import capability_card
                unit["capability"] = (self.store.get("unit_capability", unit["id"]) or {}).get(
                    "card", unit.get("capability") or capability_card())
                from seampact.typed_contract import contract_checks
                group = self.store.get("runtime_group", unit.get("runtime_group")) if unit.get("runtime_group") else None
                binding = (self.bindings.get(unit["id"]) or (self.deferred.get(unit["id"]) or
                           self.structural.get(unit["id"]) or (None,))[0])
                evidence = self._binding_evidence(binding, unit) if binding is not None else None
                unit.update(contract_checks(unit, group=group, evidence=evidence))
                result.append(unit)
            return result

    def tools(self):
        return [dict(type="function", function=dict(name=unit["tool_name"], description=unit["description"],
                    parameters=unit["parameters"])) for unit in self.business_units()]

    def business_units(self):
        return [unit for unit in self.public() if unit["status"] == "READY"
                and unit["scheduling"]["visibility"] == "business"
                and unit["scheduling"]["activation"] == "business"]

    def scheduling_policy(self, body):
        unit_id = body["unit_id"]
        if not self.store.get("unit", unit_id):
            raise ValueError("没有这个 Unit")
        pair = (body.get("visibility"), body.get("activation"))
        if pair not in {("business", "business"), ("transparent", "platform")}:
            raise ValueError("选择业务可见/业务申请，或透明/平台触发")
        value = dict(visibility=pair[0], activation=pair[1], declared=True)
        self.store.save("unit_policy", unit_id, value)
        return dict(unit_id=unit_id, scheduling=value)

    def name_to_id(self, name):
        found = [unit["id"] for unit in self.business_units() if unit["tool_name"] == name]
        if len(found) != 1:
            raise ValueError("ready native tool name is not found")
        return found[0]

    def set_capability(self, body):
        from seampact.capabilities import capability_card, content_digest
        unit = self.store.get("unit", body["unit_id"])
        if not unit:
            raise ValueError("没有这个 Unit")
        card = capability_card(body["card"])
        value = dict(card=card, digest=content_digest(card), source_version=unit["source_version"],
                     path=unit["path"], symbol=unit["symbol"], updated_at=now())
        self.store.save("unit_capability", unit["id"], value)
        return value

    def validate_arguments(self, unit_id, arguments, native_arguments=(), deferred_arguments=()):
        """Validate a proposed call before the caller's RL action selection."""
        binding, entry, unit = self._checked_unit(unit_id)
        if unit.get('call_condition'):
            self.require_call_conditions(unit_id, arguments, native_arguments, deferred_arguments)
        return self._validate_bound_arguments(binding, entry, unit, arguments, native_arguments, deferred_arguments)

    def check_call_conditions(self, unit_id, arguments, native_arguments=(), deferred_arguments=(), *,
                              frozen=None, instance_identity=None):
        """Check actual values; no source import, construction or original call."""
        from seampact.source.call_value_conditions import (
            RULE, CallUnknown, ordinary, call_context, contextual, builtin_state, verify_recipe, digest,
        )
        result = dict(schema='per-call-condition-evidence/1', status='UNKNOWN', allowed=False,
                      reason=None, recipe_digest=None, payload_digest=None, supplied=[], omitted=[])
        unit = self.store.get('unit', unit_id)
        if not unit:
            return dict(result, reason='native Unit is missing')
        if unit.get('binding') == 'SOURCE_VALUE_CONDITIONED_NATIVE_ENTRY':
            if native_arguments or deferred_arguments or frozen is not None:
                return dict(result, reason_code='CONDITION_UNKNOWN',
                    reason='structural admission requires the exact current ordinary call')
            try:
                return self.check_structural_call_source(unit_id, arguments)
            except (ValueError, KeyError, TypeError, SyntaxError, OSError) as error:
                return dict(result, reason_code='CONDITION_UNKNOWN', reason=str(error))
        if not unit.get('call_condition'):
            return dict(result, status='NOT_REQUIRED', allowed=True)
        try:
            if instance_identity is None and getattr(self, '_worker_call_identity', None):
                instance_identity = self._worker_call_identity
            binding, entry, current = frozen or self._checked_unit(unit_id)
            if current.get('typed_contract') != unit.get('typed_contract'):
                raise CallUnknown('call belongs to a different frozen Unit contract')
            self._verify_operation(binding, unit)
            if native_arguments or deferred_arguments:
                raise CallUnknown('native/deferred references have no actual ordinary call proof')
            values = ordinary(arguments)
            if type(values) is not dict:
                raise CallUnknown('actual call arguments are not yet available')
            # Validate exact types before schema checks or the general codec.
            self._validate_bound_arguments(binding, entry, unit, values)
            from seampact.hosting.deployment import encode_port
            payload = encode_port(values)
            ctx, payload_digest = call_context(payload)
            base = getattr(binding, 'call_source_inspector', binding.inspector)
            recipe = unit['call_condition']
            if not verify_recipe(base, entry, binding.candidate, recipe):
                raise CallUnknown('per-call recipe/source/entry/resource definition changed')
            result.update(recipe_digest=recipe['digest'], payload_digest=payload_digest,
                supplied=sorted(values), unit_id=unit_id, source_version=unit['source_version'],
                contract_digest=unit['typed_contract']['digest'])
            if not getattr(self, '_worker_call_identity', None):
                if self.remote_router is None:
                    raise CallUnknown('conditional calls require the actual group-port/3 worker')
                proposal = dict(result, recipe_digest=recipe['digest'], payload_digest=payload_digest,
                    supplied=sorted(values), unit_id=unit_id,
                    contract_digest=unit['typed_contract']['digest'], source_version=unit['source_version'])
                # Obtain the full target check before the action is legal;
                # local absence is not cleared or treated as target evidence.
                return self.remote_router.check_call_conditions(unit, values, proposal, instance_identity)
            if not builtin_state():
                raise CallUnknown('original builtin operation or type binding changed')
            with binding.lock:
                with binding._context():
                    shadow, trace = binding._verified_runtime_trace(entry, unit, ctx, instance_identity)
            resources = (DependencyBindings(binding.resource_binding.registry, shadow, binding.candidate)
                         if binding.resource_binding is not None else None)
            if resources is not None:
                resources.host_check = binding.resource_binding.host_check
            checked = NativeBindings(shadow, binding.candidate, self.runtime, resource_binding=resources)
            parameters, _ = checked.describe(entry)
            if parameters != unit['parameters']:
                raise CallUnknown('actual call would change the original Unit interface')
            if any(row.get('rule') == REGISTRATION_RULE for row in trace.get('external_protocols', [])):
                registration_worlds(shadow, entry, binding.candidate, trace, complete_call=True)
                receipt = binding.import_only_entries.get(unit_id)
                if receipt is None or 'registration_targets' not in receipt:
                    return dict(result, reason_code='REGISTRATION_BINDING_UNKNOWN',
                                reason='actual complete original registration targets are not independently prepared')
                plan = self.require_import_only_plan(unit_id, frozen=(binding, entry, current))
                if (receipt['instance_identity'] != instance_identity or receipt['plan_digest'] != plan['plan_digest']):
                    raise CallUnknown('original registration import occurrence or actual worker identity changed')
                binding._verify_registration_targets(entry, unit, shadow, trace)
            node, _ = checked._entry_anchor(entry)
            fixed = entry.get('fixed_arguments', {})
            parameters_ = list(node.args.posonlyargs) + list(node.args.args)
            defaults = dict(zip([p.arg for p in parameters_[-len(node.args.defaults):]], node.args.defaults)) if node.args.defaults else {}
            defaults.update({p.arg:d for p,d in zip(node.args.kwonlyargs,node.args.kw_defaults) if d is not None})
            omitted = sorted(set(defaults) - set(values) - set(fixed))
            # An existing owner can have changed __defaults__. Inspect only an
            # already-bound original function; never load it to obtain proof.
            if omitted:
                original = getattr(binding, 'entries', {}).get(unit_id)
                if original is None:
                    receipt = binding.import_only_entries.get(unit_id)
                    if receipt is not None:
                        plan = self.require_import_only_plan(unit_id, frozen=(binding, entry, current))
                        if (plan.get('plan_digest') != receipt['plan_digest'] or
                                receipt['instance_identity'] != instance_identity):
                            raise CallUnknown('import-only original callable identity changed')
                        original = binding._verify_import_only_target(entry, receipt)
                if original is None:
                    return dict(result, reason_code='DEFAULT_BINDING_UNKNOWN',
                                reason='omitted defaults require an independently prepared original callable')
                from seampact.source.call_value_conditions import literal_default, actual_defaults
                actual_values = actual_defaults(original, [p.arg for p in parameters_])
                for name in omitted:
                    expected = literal_default(defaults[name])
                    if name not in actual_values:
                        raise CallUnknown('original immutable default binding is absent')
                    actual = actual_values[name]
                    if type(actual) is not type(expected) or actual != expected:
                        raise CallUnknown('original immutable default binding changed')
            result.update(status='CHECKED', allowed=True, recipe_digest=recipe['digest'],
                payload_digest=payload_digest, supplied=sorted(values), omitted=omitted,
                unit_id=unit_id, contract_digest=unit['typed_contract']['digest'],
                instance_identity=copy.deepcopy(instance_identity), source_version=unit['source_version'])
            if instance_identity != self._worker_call_identity:
                raise CallUnknown('actual worker instance identity differs')
            return result
        except (ValueError, KeyError, TypeError, SyntaxError, OSError) as error:
            code = getattr(error, 'reason_code', 'CONDITION_UNKNOWN')
            answer = dict(result, status='UNKNOWN', allowed=False, reason_code=code, reason=str(error))
            if code == 'SDK_BINDING_UNKNOWN' and getattr(self, '_worker_call_identity', None):
                try:
                    plan = self.require_import_only_plan(unit_id, frozen=(binding, entry, current))
                    with binding.lock:
                        with binding._context():
                            binding._check_import_prerequisites(entry, unit, plan, self._worker_call_identity)
                    answer['import_plan'] = plan
                except (ValueError, KeyError, TypeError, SyntaxError, OSError) as prerequisite_error:
                    answer['reason_code'] = 'CONDITION_UNKNOWN'
                    answer['reason'] = str(prerequisite_error)
            return answer

    def require_call_condition_structure(self, unit_id, *, frozen=None):
        """Freeze only a replayed conditional host structure, never a call."""
        from seampact.source.call_value_conditions import verify_recipe
        with self.lock:
            binding, entry, unit = frozen or self._checked_unit(unit_id)
            current = self.store.get('unit', unit_id)
            if (not current or current.get('typed_contract') != unit.get('typed_contract')
                    or current.get('status') != 'CALL_CONDITION_DEFERRED'):
                raise ValueError('conditional Unit installation is no longer current')
            self._verify_operation(binding, current, full_source_check=True, operation='prepare')
            group = self.store.get('runtime_group', current.get('runtime_group'))
            member = ((group or {}).get('host') or {}).get('members', {}).get(current['entry_id'], {})
            base = getattr(binding, 'call_source_inspector', binding.inspector)
            if (member.get('status') != 'HOST_CHECKED' or member.get('call_condition') != current.get('call_condition')
                    or not verify_recipe(base, entry, binding.candidate, current.get('call_condition'))):
                raise ValueError('conditional host/source/recipe has not been mechanically closed')
            return dict(status='STRUCTURE_CHECKED', call_permission='REQUIRES_ACTUAL_CALL',
                        recipe_digest=current['call_condition']['digest'],
                        contract_digest=current['typed_contract']['digest'], runtime_group=group['id'])

    def require_import_only_plan(self, unit_id, *, frozen=None):
        """Replay the frozen original import prefix without granting a call."""
        from seampact.source.import_only import compile_import_only_plan
        binding, entry, unit = frozen or self._checked_unit(unit_id)
        self.require_call_condition_structure(unit_id, frozen=(binding, entry, unit))
        parts = entry.get('symbol', '').split('.')
        kind = entry.get('kind')
        if (entry.get('unit', 'file') != 'file' or not entry.get('path', '').endswith('.py')
                or entry.get('binding_program') or not all(parts) or
                not (kind == 'function' and len(parts) == 1 or kind == 'method' and len(parts) == 2
                     and (entry.get('constructor') or {}).get('symbol') == parts[0])):
            raise ValueError('import-only original default inspection requires a source function or ordinary method')
        self._verify_operation(binding, unit, full_source_check=True, operation='prepare')
        resources = (binding.resource_binding.snapshot(entry_ids={entry['id']}) if binding.resource_binding is not None
                     else dict(requirements=[], bindings={}))
        if binding.resource_binding is not None:
            binding.resource_binding.check_entry(entry['id'])
        base = getattr(binding, 'call_source_inspector', binding.inspector)
        plan = compile_import_only_plan(base, entry, binding.candidate, resources)
        if plan.get('status') != 'CHECKED' or not plan.get('plan_digest'):
            raise ValueError('original import prefix, external effects or frozen resources are UNKNOWN')
        return plan

    def validate_worker_import_plan(self, unit_id, proposal, *, frozen=None):
        """Check a trusted worker proposal against the controller's frozen facts.

        Actual SDK/asset premises belong to that worker. This only checks its
        plan identity; the existing full claim and worker's independent plan
        replay remain necessary before any original prefix executes.
        """
        from seampact.source.import_only import _metadata
        from seampact.source.call_value_conditions import digest as proof_digest
        binding, entry, unit = frozen or self._checked_unit(unit_id)
        self.require_call_condition_structure(unit_id, frozen=(binding, entry, unit))
        self._verify_operation(binding, unit, full_source_check=True, operation='prepare')
        plan = _metadata(proposal)
        resources = (binding.resource_binding.snapshot(entry_ids={entry['id']}) if binding.resource_binding is not None
                     else dict(requirements=[], bindings={}))
        if binding.resource_binding is not None:
            binding.resource_binding.check_entry(entry['id'])
        body = {key: value for key, value in plan.items() if key != 'plan_digest'}
        expected = dict(source_version=binding.inspector.request.get('source_version'), entry_id=entry['id'],
            path=entry['path'], entry_digest=proof_digest(entry), candidate_digest=proof_digest(binding.candidate),
            resource_digest=proof_digest(resources))
        if (plan.get('schema') != 'source-import-only-plan/1' or plan.get('status') != 'CHECKED' or
                plan.get('activation_prefix', {}).get('status') != 'CHECKED' or
                plan.get('plan_digest') != proof_digest(body) or
                any(plan.get(key) != value for key, value in expected.items())):
            raise ValueError('actual worker import plan differs from the frozen source/entry/resource identity')
        return plan

    def prepare_import_only(self, unit_id, plan_digest):
        """Import the original checked module; never construct or invoke an owner."""
        # _checked_unit and the source replay import their checking modules.
        # Validate the actual import mechanism before those imports as well.
        prepared_binding = self.bindings.get(unit_id)
        if prepared_binding is None:
            deferred = self.deferred.get(unit_id)
            prepared_binding = deferred[0] if deferred else None
        if prepared_binding is None:
            raise MissingBinding('import-only native source binding is unavailable')
        _check_import_only_loader(prepared_binding)
        binding, entry, unit = self._checked_unit(unit_id)
        if binding is not prepared_binding:
            raise MissingBinding('import-only native source binding changed')
        plan = self.require_import_only_plan(unit_id, frozen=(binding, entry, unit))
        if type(plan_digest) is not str or plan_digest != plan['plan_digest']:
            raise ValueError('original import prefix plan changed')
        identity = getattr(self, '_worker_call_identity', None)
        if type(identity) is not dict:
            raise ValueError('import-only preparation requires the actual worker identity')
        prefix_modules = {name for name, record in binding.module_paths.items()
                          if record[0] in plan.get('modules_order', []) and not name.startswith('src.')}
        if prefix_modules & set(binding.modules):
            raise ValueError('original import prefix was already loaded without this checked receipt')
        _checked_module_table()
        with binding.lock:
            _check_import_only_loader(binding)
            with binding._context():
                from seampact.source.sdk_value_summaries import RULE as SDK_RULE
                from seampact.source.sdk_runtime_targets import (
                    begin_original_import, finish_original_import, abort_original_import,
                )
                requirements = (unit.get('call_condition') or {}).get('runtime_protocol_requirements', [])
                import_evidence = binding._check_import_prerequisites(entry, unit, plan, identity)
                sdk_token = (begin_original_import(binding, entry, unit, plan, identity,
                    asset_plan=plan.get('sdk_asset_plan'))
                    if any(row.get('schema') == SDK_RULE for row in requirements) else None)
                try:
                    namespace = binding._namespace(entry)
                    receipt = binding._capture_import_only_target(entry, namespace)
                    receipt['helpers'] = binding._capture_imported_helpers(plan)
                    if any(row.get('schema') == REGISTRATION_RULE for row in requirements):
                        shadow, trace = binding._registration_trace(entry)
                        worlds = registration_worlds(shadow, entry, binding.candidate, trace, complete_call=False)
                        if not worlds:
                            raise RegistrationUnknown('original registration creation requirements were not reproduced')
                        modules = registration_modules(binding, worlds, in_context=True)
                        receipt['registration_targets'] = capture_registration_targets(shadow, entry, binding.candidate,
                                                                                     trace, modules)
                    current = self.require_import_only_plan(unit_id, frozen=(binding, entry, unit))
                    if current.get('plan_digest') != plan_digest:
                        raise ValueError('original import prefix changed during preparation')
                    receipt.update(plan_digest=plan_digest, instance_identity=copy.deepcopy(identity))
                    if import_evidence is not None:
                        receipt['sdk_import_evidence'] = import_evidence
                    binding.import_only_entries[unit_id] = receipt
                    if sdk_token is not None:
                        receipt['sdk_context'] = finish_original_import(sdk_token)
                except BaseException:
                    binding.import_only_entries.pop(unit_id, None)
                    raise
                finally:
                    if sdk_token is not None:
                        abort_original_import(sdk_token)
        return dict(status='IMPORTED', plan_digest=plan_digest, owner_construction=False, original_call=False)

    def require_call_conditions(self, unit_id, arguments, native_arguments=(), deferred_arguments=(), **kwargs):
        result = self.check_call_conditions(unit_id, arguments, native_arguments, deferred_arguments, **kwargs)
        if not result['allowed']:
            raise ValueError('actual call condition is UNKNOWN: ' + str(result['reason']))
        return result


    def require_initialization_conditions(self, unit_id):
        """Check only the independently permitted original preparation prefix."""
        from seampact.source.call_value_conditions import contextual, Context, verify_recipe
        binding, entry, unit = self._checked_unit(unit_id)
        recipe = unit.get('call_condition')
        if not recipe:
            return
        base = getattr(binding, 'call_source_inspector', binding.inspector)
        if not verify_recipe(base, entry, binding.candidate, recipe) or recipe['initialization']['status'] != 'CHECKED':
            raise ValueError('original initialization is not independently checked')
        prefix = contextual(base, Context(compiling=True, initialization_only=True))
        if binding.resource_binding is not None:
            resources = DependencyBindings(binding.resource_binding.registry, prefix, binding.candidate)
            resources.host_check = binding.resource_binding.host_check
            resources.check_entry(entry['id'])

    def _checked_unit(self, unit_id):
        unit = self.store.get("unit", unit_id)
        if unit and unit["status"] == "READY" and unit_id in self.bindings:
            binding, entry = self.bindings[unit_id], self.entry_specs[unit_id]
        elif unit and unit.get("status") not in {"SUPERSEDED", "DETACHED"} and unit_id in self.deferred:
            binding, entry = self.deferred[unit_id]
        else:
            raise ValueError("native Unit is not ready")
        self._verify_operation(binding, unit)
        return binding, entry, unit

    @staticmethod
    def _binding_evidence(binding, unit, full_source_check=False):
        from seampact.typed_contract import binding_evidence, installation_evidence
        frozen = getattr(binding, "typed_installation_evidence", {})
        entry = getattr(binding, "typed_entry_evidence", {}).get(unit["id"], {})
        if unit.get('binding') == 'SOURCE_VALUE_CONDITIONED_NATIVE_ENTRY':
            fresh = {**frozen, **entry}
            if full_source_check:
                fresh.update(installation_evidence(binding.inspector, binding.candidate))
            basis = binding_evidence(binding.inspector, binding.candidate, fresh)
        else:
            basis = (installation_evidence(binding.inspector, binding.candidate) if full_source_check else
                     binding_evidence(binding.inspector, binding.candidate, frozen))
        return {**entry, **basis}

    def structure_materialization(self, unit_id):
        """Return checked source material facts; never a semantic export."""
        from seampact.typed_contract import require_operation
        with self.lock:
            unit = self.store.get('unit', unit_id)
            frozen = self.structural.get(unit_id)
            if (not unit or unit.get('status') != 'STRUCTURE_ONLY' or frozen is None or
                    unit.get('binding') != 'SOURCE_VALUE_CONDITIONED_NATIVE_ENTRY'):
                raise MissingBinding('entry has no current independently checked structural materialization')
            binding, entry = frozen
            if binding.detached:
                raise MissingBinding('structural source is detached')
            evidence = self._binding_evidence(binding, unit, full_source_check=True)
            group = self.store.get('runtime_group', unit.get('runtime_group')) if unit.get('runtime_group') else None
            require_operation(unit, 'materialize_guarded_entry', group=group, evidence=evidence)
            return dict(schema='guarded-entry-materialization/1', unit_id=unit_id,
                structure=copy.deepcopy(unit['structure_admission']),
                source_version=unit['source_version'], request=copy.deepcopy(binding.inspector.request),
                candidate=copy.deepcopy(binding.candidate), entry=copy.deepcopy(entry),
                semantic_export=False, invocation=False, process_start=False)

    def check_structural_call_source(self, unit_id, arguments):
        """Normal catalog C_x consumer; source facts do not imply NO_WORKER."""
        from seampact.source.value_conditioned_calls import check_call_source
        material = self.structure_materialization(unit_id)
        binding, entry = self.structural[unit_id]
        proof = check_call_source(binding.inspector, entry, binding.candidate,
                                  material['structure'], arguments)
        return dict(proof, source_proof=copy.deepcopy(proof), unit_id=unit_id, allowed=False, can_start=False,
                    reason_code='NEEDS_ACTUAL_WORKER_AND_RESOURCE_PROOF' if
                        proof['status'] == 'CONTROLLER_SOURCE_CHECKED' else 'CONDITION_UNKNOWN')

    def _verify_operation(self, binding, unit, full_source_check=False, operation="invoke"):
        from seampact.typed_contract import require_operation
        if not unit or not unit.get("id"):
            raise MissingBinding("installed native Unit is missing")
        if binding.detached:
            raise MissingBinding("Harness source is detached")
        evidence = self._binding_evidence(binding, unit, full_source_check=full_source_check)
        group = self.store.get("runtime_group", unit.get("runtime_group")) if unit.get("runtime_group") else None
        return require_operation(unit, operation, group=group, evidence=evidence)

    @staticmethod
    def _validate_bound_arguments(binding, entry, unit, arguments, native_arguments=(), deferred_arguments=()):
        if not isinstance(arguments, dict):
            raise ValueError("native tool arguments must be a JSON object")
        native_arguments, deferred_arguments = set(native_arguments), set(deferred_arguments)
        if (native_arguments | deferred_arguments) - set(arguments):
            raise ValueError("native/deferred argument names must identify actual supplied parameters")
        schema = unit["parameters"]
        program = entry.get("binding_program") or {}
        references = set(program.get("argument_refs", {})) - set(program.get("task_parameters", {}).values())
        if set(arguments) & (set(entry.get("fixed_arguments", {})) | set(unit.get("omitted_object_parameters", [])) | references):
            raise ValueError("fixed or original-object native arguments cannot be overridden")
        missing = set(schema["required"]) - set(arguments)
        unknown = set(arguments) - set(schema["properties"])
        if missing or (unknown and not schema["additionalProperties"]):
            raise ValueError("native tool arguments do not match the source-derived interface")
        for name, value in arguments.items():
            if name in deferred_arguments:
                continue
            if name in native_arguments:
                if name not in schema["properties"] or not binding.native_matches(value, schema["properties"][name]):
                    raise ValueError("native result object does not match its source-derived parameter type: " + name)
                continue
            try:
                json.dumps(value, allow_nan=False)
            except (ValueError, TypeError) as error:
                raise ValueError("native tool arguments must contain JSON values or established native references") from error
            if name in schema["properties"] and not _matches(value, schema["properties"][name]):
                raise ValueError("native tool parameter type mismatch: " + name)
        return True

    def freeze_resources(self, unit_ids, remote=()):
        """Keep the actual source binding/owner and private credentials in memory.

        Units placed on a runtime-group host resolve credentials in that host.
        """
        frozen, bindings, sources_checked = {}, {}, set()
        with self.lock:
            for unit_id in dict.fromkeys(unit_ids):
                try:
                    binding, entry, unit = self._checked_unit(unit_id)
                except ValueError:
                    raise MissingBinding("cannot freeze an unavailable native Unit") from None
                if unit_id not in remote and (unit["status"] != "READY" or unit_id not in self.bindings):
                    raise MissingBinding("cannot freeze an unavailable native Unit")
                if id(binding) not in sources_checked:
                    self._verify_operation(binding, unit, full_source_check=True)
                    sources_checked.add(id(binding))
                frozen[unit_id] = (binding, entry, copy.deepcopy(unit))
                if unit_id not in remote:
                    bindings.setdefault(id(binding), (binding, set()))[1].add(entry["id"])
            for binding, entry_ids in bindings.values():
                if binding.resource_binding is not None:
                    binding.resource_binding.freeze_credentials(entry_ids=entry_ids)
        return frozen

    def snapshot_resources(self, unit_ids, frozen=None):
        """Research-safe configuration evidence; never return private resolvers."""
        result = []
        with self.lock:
            for unit_id in dict.fromkeys(unit_ids):
                if frozen is not None:
                    binding, entry, _ = frozen.get(unit_id, (None, None, None))
                else:
                    binding, entry = self.bindings.get(unit_id), self.entry_specs.get(unit_id)
                    if binding is None and unit_id in self.deferred:
                        binding, entry = self.deferred[unit_id]
                if binding is None or entry is None:
                    continue
                resources = binding.resource_binding
                snapshot = resources.snapshot(entry_ids={entry["id"]}) if resources is not None else dict(requirements=[], bindings={})
                requirements = [row for row in snapshot["requirements"] if entry["id"] in row.get("entry_ids", [])]
                selected_ids = {row["id"] for row in requirements}
                result.append(dict(unit_id=unit_id, entry_id=entry["id"],
                    source_id=binding.inspector.request.get("source_id"),
                    source_version=binding.inspector.request["source_version"], requirements=requirements,
                    bindings={identity: value for identity, value in snapshot["bindings"].items() if identity in selected_ids}))
        return dict(schema="native-resource-snapshot/1", units=result)

    def invoke_frozen(self, frozen, unit_id, arguments, native_arguments=()):
        if unit_id not in frozen:
            raise MissingBinding("native Unit is absent from this run's frozen resources")
        binding, entry, unit = frozen[unit_id]
        current = self.store.get("unit", unit_id)
        if current is None or current.get("typed_contract") != unit.get("typed_contract"):
            raise MissingBinding("frozen native Unit contract changed; recheck and freeze the task again")
        self._verify_operation(binding, current)
        self.require_call_conditions(unit_id, arguments, native_arguments,
                                     frozen=(binding, entry, unit))
        self._validate_bound_arguments(binding, entry, unit, arguments, native_arguments)
        return self._invoke_bound(binding, entry, unit, arguments)

    def invoke(self, unit_id, arguments, native_arguments=()):
        self.validate_arguments(unit_id, arguments, native_arguments=native_arguments)
        unit = self.store.get("unit", unit_id)
        if self.remote_router:
            routed, result = self.remote_router.invoke(unit, arguments, native_arguments)
            if routed:
                with self.lock:
                    unit = self.store.get("unit", unit_id)
                    unit.update(result_type=type(result).__module__ + "." + type(result).__qualname__)
                    self.store.save("unit", unit_id, unit)
                return result
        if unit.get('call_condition') and getattr(self, '_worker_call_identity', None):
            binding, entry = self.deferred[unit_id]
            self.require_call_conditions(unit_id, arguments, native_arguments,
                                         instance_identity=self._worker_call_identity)
            return self._invoke_bound(binding, entry, unit, arguments)
        if unit["status"] != "READY" or unit_id not in self.bindings:
            raise MissingBinding("this Unit's execution-environment facts are met only by its runtime-group host; "
                                 "start and register an instance first: " + str(unit.get("reason")))
        return self._invoke_bound(self.bindings[unit_id], self.entry_specs[unit_id], unit, arguments)

    def _invoke_bound(self, binding, entry, unit, arguments):
        unit_id = unit["id"]
        def record_outcome(success):
            resources = binding.resource_binding
            if resources is not None and self.resource_registry is not None:
                return self.resource_registry.record_execution(binding.inspector.request.get("source_id"),
                    entry["id"], success, resources.snapshot(entry_ids={entry["id"]}))
        try:
            result = binding.invoke(entry, unit, arguments)
        except (MissingBinding, ImportError, ModuleNotFoundError, FileNotFoundError, NameError, AttributeError, ProtocolError) as error:
            record_outcome(False)
            with self.lock:
                for identity, related in self.entry_specs.items():
                    if self.bindings[identity] is binding and binding._owner_key(related) == binding._owner_key(entry):
                        item = self.store.get("unit", identity)
                        if item.get("status") not in {"SUPERSEDED", "DETACHED"}:
                            item["status"] = "MISSING"
                        item["reason"] = binding.redact(error)
                        self.store.save("unit", identity, item, "unit_binding_missing")
            raise
        except Exception:
            record_outcome(False)
            raise
        completion = getattr(result, "seampact_completion", None)
        success = (not isinstance(result, subprocess.CompletedProcess) or result.returncode == 0)
        if completion and completion.get("contract_declared"):
            success = success and completion.get("task_status") == "COMPLETE"
        outcome = record_outcome(success)
        with self.lock:
            binding_duration = unit.get("last_binding_duration_ms")
            initialization_before = unit.get("last_initialization_before")
            unit = self.store.get("unit", unit_id)
            unit.update(initialization=binding.initialization_state(entry, unit),
                        result_type=type(result).__module__ + "." + type(result).__qualname__)
            unit["last_binding_duration_ms"] = binding_duration
            unit["last_initialization_before"] = initialization_before
            if outcome is not None:
                unit["last_execution"] = outcome
            if completion is not None:
                unit["last_completion"] = completion
            self.store.save("unit", unit_id, unit, "unit_returned")
        return result


# Capture after the original classes have been defined, before bindings can
# import any candidate source. File digests do not establish these identities.
_IMPORT_ONLY_LOADER_KERNEL = _capture_import_only_loader_kernel()
