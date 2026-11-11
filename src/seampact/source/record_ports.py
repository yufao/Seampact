"""Value snapshots of frozen, passive Python records; never object transport.

Only a plain original class whose constructor stores parameters/literals in
ordinary fields is covered. No getters, serializers, pickle, reconstruction,
owner handles, or methods cross the port. An unsupported member fails the call.
"""

import ast
import hashlib
import types
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RecordSnapshot:
    module: str
    symbol: str
    source_sha256: str
    fields: dict


class RecordPortGap(ValueError):
    pass


OBJECT_HOOKS = ("__getattribute__", "__getattr__", "__setattr__", "__delattr__", "__del__", "__new__", "__slots__")


def record_fields(node):
    if node.bases or node.keywords or node.decorator_list:
        raise RecordPortGap("record bases, decorators or metaclass are outside the value-port rule")
    methods = {n.name: n for n in node.body if isinstance(n, ast.FunctionDef)}
    if any(name in methods for name in (*OBJECT_HOOKS, "__dict__")):
        raise RecordPortGap("record has an object/state hook")
    if any(not isinstance(n, (ast.FunctionDef, ast.Pass)) and not (
            isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str))
            for n in node.body):
        raise RecordPortGap("record class has non-method initialization")
    init = methods.get("__init__")
    if init is None or init.decorator_list or init.args.vararg or init.args.kwarg:
        raise RecordPortGap("record requires a plain finite constructor")
    parameters = init.args.posonlyargs + init.args.args + init.args.kwonlyargs
    if not parameters:
        raise RecordPortGap("record constructor lacks its receiver")
    receiver, names = parameters[0].arg, {p.arg for p in parameters[1:]}
    fields = []
    for statement in init.body:
        if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant) and isinstance(statement.value.value, str):
            continue
        if not isinstance(statement, ast.Assign) or len(statement.targets) != 1:
            raise RecordPortGap("record constructor has effects beyond field assignment")
        target = statement.targets[0]
        if (not isinstance(target, ast.Attribute) or not isinstance(target.value, ast.Name)
                or target.value.id != receiver or target.attr in fields or target.attr.startswith("__")):
            raise RecordPortGap("record field is not a unique ordinary receiver attribute")
        value = statement.value
        if not (isinstance(value, ast.Name) and value.id in names):
            try:
                ast.literal_eval(value)
            except (ValueError, TypeError):
                raise RecordPortGap("record field is neither a constructor parameter nor a literal") from None
        fields.append(target.attr)
    if not fields:
        raise RecordPortGap("record has no declared data fields")
    return fields, init


def _code_identity(code):
    def constant(value):
        kind = type(value)
        if value is None or value is Ellipsis:
            return ("None" if value is None else "Ellipsis",)
        if kind in (bool, int, float, complex, str, bytes):
            return (kind.__name__, value)
        if kind is tuple:
            return ("tuple", tuple(constant(item) for item in value))
        if kind is frozenset:
            return ("frozenset", frozenset(constant(item) for item in value))
        if kind is types.CodeType:
            return ("code", _code_identity(value))
        raise RecordPortGap("record constructor has a nonstandard runtime code constant")
    return (code.co_code, code.co_names, code.co_varnames, code.co_argcount, code.co_posonlyargcount,
            code.co_kwonlyargcount, code.co_flags, code.co_freevars, code.co_cellvars,
            tuple(constant(c) for c in code.co_consts))


def _record_definition(cls):
    """Use inert function metadata, including after native modules leave sys.modules."""
    if type(cls) is not type or cls.__bases__ != (object,):
        raise RecordPortGap("only a plain frozen source record is covered")
    namespace = vars(cls)
    function = namespace.get("__init__")
    module = namespace.get("__module__")
    symbol = type.__getattribute__(cls, "__qualname__")
    name = type.__getattribute__(cls, "__name__")
    if (type(function) is not types.FunctionType or type(module) is not str or not module
            or type(symbol) is not str or not symbol or symbol != name
            or function.__closure__ is not None):
        raise RecordPortGap("record is not one top-level original constructor definition")
    # NativeBindings retains original module globals privately and removes them
    # from sys.modules after each call. Import/inspect/getattr must not be used
    # to recover the record, nor may a forged module object run attribute hooks.
    original = function.__globals__
    if any(type(key) is not str for key in original):
        raise RecordPortGap("record module has nonstandard namespace keys")
    filename = original.get("__file__")
    if (type(filename) is not str or type(original.get("__name__")) is not str
            or original["__name__"] != module or original.get(name) is not cls
            or Path(function.__code__.co_filename).resolve() != Path(filename).resolve()):
        raise RecordPortGap("record constructor is detached from its original module definition")
    dictionary = namespace.get("__dict__")
    if (type(dictionary) is not types.GetSetDescriptorType or dictionary.__name__ != "__dict__"
            or dictionary.__objclass__ is not cls or any(name in namespace for name in OBJECT_HOOKS)):
        raise RecordPortGap("record has an object/state hook or non-native instance dictionary")
    return namespace, function, module, symbol, Path(filename).resolve()


class FrozenRecordPorts:
    def __init__(self, inspector):
        self.inspector = inspector
        self.checked = {}

    def snapshot(self, value):
        cls = type(value)
        namespace, function, module, symbol, filename = _record_definition(cls)
        try:
            path = filename.relative_to(self.inspector.root).as_posix()
        except ValueError:
            raise RecordPortGap("record class is outside the frozen source") from None
        if path not in self.inspector.files:
            raise RecordPortGap("record source is not frozen")
        sha = self.inspector.files[path]["sha256"]
        try:
            actual_sha = hashlib.sha256(filename.read_bytes()).hexdigest()
        except OSError:
            raise RecordPortGap("record source is unavailable after freezing") from None
        if actual_sha != sha:
            raise RecordPortGap("record source changed after freezing")
        if cls not in self.checked:
            data = self.inspector.data(path)
            tree = ast.parse(data, filename=path)
            nodes = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == symbol]
            if len(nodes) != 1:
                raise RecordPortGap("record class is not one top-level frozen definition")
            fields, init = record_fields(nodes[0])
            if any(name in namespace for name in fields):
                raise RecordPortGap("record class has a field descriptor or runtime object hook")
            compiled = compile(data, str(self.inspector.root / path), "exec", dont_inherit=True)
            class_code = next(c for c in compiled.co_consts if isinstance(c, types.CodeType) and c.co_name == cls.__name__)
            original = next(c for c in class_code.co_consts if isinstance(c, types.CodeType) and c.co_name == "__init__")
            if not isinstance(function, types.FunctionType) or _code_identity(function.__code__) != _code_identity(original):
                raise RecordPortGap("record constructor differs from frozen source")
            self.checked[cls] = (fields, sha, function, _code_identity(original), module, symbol, filename)
        fields, old_sha, original_function, identity, original_module, original_symbol, original_filename = self.checked[cls]
        if (function is not original_function or _code_identity(function.__code__) != identity
                or any(name in namespace for name in fields) or sha != old_sha
                or (module, symbol, filename) != (original_module, original_symbol, original_filename)):
            raise RecordPortGap("record class changed after checking")
        values = object.__getattribute__(value, "__dict__")
        if type(values) is not dict or any(type(key) is not str for key in values) or set(values) != set(fields):
            raise RecordPortGap("record instance fields differ from its frozen constructor")
        return RecordSnapshot(module, symbol, sha, dict(values))
