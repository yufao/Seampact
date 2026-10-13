"""Source-checked native entries and their shared-runtime callable bindings.

No model claim grants parallel, remote, or independent execution. Native code is
loaded only when a ready entry is invoked, and its original owner is reused.
"""

import ast
import asyncio
import builtins
import contextlib
import importlib
import importlib.abc
import importlib.util
import inspect
import json
import os
import re
import shutil
import subprocess
import symtable
import sys
import threading
import uuid
from pathlib import Path

from .handoff import ToolListHandoff, install_method_proxy
from .inspection import Inspector, digest, units
from .lowering import lower_loop
from .runtime import ProtocolError
from .store import now
from .verification import check_candidate, source_unit


_native_context_lock = threading.RLock()
_EMPTY = inspect.Parameter.empty


class MissingBinding(ValueError):
    pass


def _anchor(source, symbol):
    parts = symbol.split(".")
    tree = ast.parse(source)
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


def _matches(value, schema):
    if not schema:
        return True
    if "anyOf" in schema:
        return any(_matches(value, branch) for branch in schema["anyOf"])
    if "enum" in schema:
        return any(type(value) is type(item) and value == item for item in schema["enum"])
    kind = schema.get("type")
    valid = {"string": isinstance(value, str), "integer": type(value) is int,
             "number": type(value) in {int, float}, "boolean": type(value) is bool,
             "null": value is None, "array": isinstance(value, list), "object": isinstance(value, dict)}
    if kind is not None and not valid.get(kind, False):
        return False
    if kind == "array":
        return all(_matches(item, schema.get("items", {})) for item in value)
    if kind == "object" and isinstance(schema.get("additionalProperties"), dict):
        return all(_matches(item, schema["additionalProperties"]) for item in value.values())
    return True


def _interface(node, fixed=None, receiver=False, require_all=False):
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
    known = {parameter.arg for parameter in parameters}
    if node.args.vararg:
        known.add(node.args.vararg.arg)
    if not node.args.kwarg and set(fixed) - known:
        raise MissingBinding("native arguments contain unknown parameters: " + ", ".join(sorted(set(fixed) - known)))
    properties, required, omitted = {}, [], []
    for parameter, default in zip(parameters, defaults):
        schema = _type_schema(parameter.annotation)
        if parameter.arg in fixed:
            if schema is None or not _matches(fixed[parameter.arg], schema):
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
        tree = ast.parse(source)
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


def _notebook_definitions(inspector, path, unit, patches):
    cells = _definition_cells(inspector, path, unit, patches)
    known, referenced = set(dir(builtins)), set()
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
    def __init__(self, inspector, candidate, runtime):
        request = json.loads(json.dumps(inspector.request))
        self.inspector = Inspector(inspector.root, inspector.snapshots, request)
        self.candidate = json.loads(json.dumps(candidate))
        self.runtime = runtime
        self.boundaries = {boundary["id"]: boundary for boundary in candidate.get("boundaries", [])}
        self.report = check_candidate(self.inspector, candidate)
        self.checked = {row["id"]: row for row in self.report["boundaries"]}
        self.patches, self.recipes, self.module_paths = {}, {}, {}
        self.modules, self.notebooks, self.owners, self.entries = {}, {}, {}, {}
        self.approved_entries, self.function_owners, self.owner_keys = {}, {}, {}
        self.loop, self.thread = None, None
        self.lock = threading.RLock()
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
        for identity in self.boundaries:
            if identity not in selected:
                continue
            boundary = self.boundaries[identity]
            if boundary["mode"] == "R":
                key = (boundary["path"], boundary.get("unit", "file"))
                original = self.patches.get(key, source_unit(self.inspector, *key))
                lowered = lower_loop(original, boundary["symbol"], boundary.get("loop_index", 0))
                self.patches[key], self.recipes[boundary["id"]] = lowered["source"], lowered["recipe"]

    def _dependencies(self, path, unit):
        missing = []
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
            module = next((name for name, value in self.module_paths.items() if value[0] == current), "")
            package = module if current.endswith("/__init__.py") else module.rpartition(".")[0]
            for imported in facts["imports"]:
                name = imported.get("module") or ""
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
                    missing.append("native dependency is not installed: " + top)
        if missing:
            raise MissingBinding("; ".join(dict.fromkeys(missing)))

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
                raise MissingBinding("native resource is missing: " + path)

    def _direct_reachable(self, source, entry_symbol, boundary_symbol):
        tree = ast.parse(source)
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

    def _owner_initialization(self, owner, entry, initialization_names):
        methods = {node.name: node for node in owner.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        def attributes(names):
            reads, writes, visited = set(), set(), set()
            pending = list(names)
            while pending:
                name = pending.pop()
                if name in visited or name not in methods:
                    continue
                visited.add(name)
                method = methods[name]
                positional = list(method.args.posonlyargs) + list(method.args.args)
                receiver = positional[0].arg if positional else None
                for node in ast.walk(method):
                    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == receiver:
                        if node.attr in methods:
                            pending.append(node.attr)
                        elif isinstance(node.ctx, ast.Store):
                            writes.add(node.attr)
                        elif isinstance(node.ctx, ast.Load):
                            reads.add(node.attr)
            return reads, writes
        known = {"__class__", "__dict__", "__weakref__"}
        for node in owner.body:
            if isinstance(node, ast.Assign):
                known.update(target.id for target in node.targets if isinstance(target, ast.Name))
            elif isinstance(node, ast.AnnAssign) and node.value is not None and isinstance(node.target, ast.Name):
                known.add(node.target.id)
        _, initialized = attributes(["__init__", *initialization_names])
        reads, internal_writes = attributes([entry["symbol"].split(".")[-1]])
        missing = reads - known - initialized - internal_writes
        if missing:
            raise MissingBinding("original native owner initialization is missing for: " + ", ".join(sorted(missing)))

    def describe(self, entry):
        path, cell, symbol, kind = entry["path"], entry.get("unit", "file"), entry["symbol"], entry["kind"]
        if path not in self.inspector.files:
            raise MissingBinding("native entry file is outside the frozen source scope")
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
            return self._cli_interface(entry)
        if kind not in {"function", "method"}:
            raise MissingBinding("unsupported native entry kind")
        if not path.endswith((".py", ".ipynb")):
            raise MissingBinding("this native callable host requires Python source or notebook definitions")
        if path.endswith(".py") and not any(record[0] == path for record in self.module_paths.values()):
            raise MissingBinding("native Python module path is not importable")
        source = source_unit(self.inspector, path, cell)
        if path.endswith(".ipynb"):
            _notebook_definitions(self.inspector, path, cell, self.patches)
        node, owner = _anchor(source, symbol)
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            raise MissingBinding("native entry must identify an original callable")
        if any(isinstance(value, (ast.Yield, ast.YieldFrom)) for value in ast.walk(node)):
            raise MissingBinding("streaming/generator lifecycle needs an original host binding")
        if (kind == "method") != (owner is not None):
            raise MissingBinding("entry kind does not match its native source receiver")
        schema, omitted = _interface(node, entry.get("fixed_arguments", {}), receiver=owner is not None)
        if owner is not None:
            constructor = entry.get("constructor")
            if not isinstance(constructor, dict) or constructor.get("symbol") != owner.name:
                raise MissingBinding("the original owner constructor must be identified")
            if any(ast.unparse(decorator).split(".")[-1] in {"staticmethod", "classmethod"} for decorator in node.decorator_list):
                raise MissingBinding("static/class receiver needs a distinct native binding")
            initializer = next((member for member in owner.body if isinstance(member, ast.FunctionDef) and member.name == "__init__"), None)
            if any(isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)) and member.name == "__new__" for member in owner.body):
                raise MissingBinding("custom native allocation needs its original owner binding")
            if any(isinstance(member, ast.AsyncFunctionDef) and member.name == "__init__" for member in owner.body):
                raise MissingBinding("native constructor cannot be an async instance initializer")
            if initializer is None:
                if any(not isinstance(base, ast.Name) or base.id != "object" for base in owner.bases):
                    raise MissingBinding("inherited native constructor parameters are not identified")
                if constructor.get("arguments"):
                    raise MissingBinding("native object constructor accepts no supplied arguments")
            else:
                _interface(initializer, constructor.get("arguments", {}), receiver=True, require_all=True)
            for initialization in entry.get("initializers", []):
                if initialization["symbol"].count(".") > 1 or ("." in initialization["symbol"] and initialization["symbol"].split(".")[0] != owner.name):
                    raise MissingBinding("original initializer belongs to a different native owner")
                name = initialization["symbol"].split(".")[-1]
                initial, initial_owner = _anchor(source, owner.name + "." + name)
                if initial_owner is None:
                    raise MissingBinding("native initialization owner is not identified")
                if any(isinstance(value, (ast.Yield, ast.YieldFrom)) for value in ast.walk(initial)):
                    raise MissingBinding("generator initialization needs an original lifecycle binding")
                _interface(initial, initialization.get("arguments", {}), receiver=True, require_all=True)
            self._owner_initialization(owner, entry,
                [initialization["symbol"].split(".")[-1] for initialization in entry.get("initializers", [])])
        elif entry.get("constructor") or entry.get("initializers"):
            raise MissingBinding("function entry cannot invent a native object owner")
        for boundary in selected:
            wiring = boundary.get("native_wiring")
            if isinstance(wiring, dict):
                if owner is None or wiring["controller_path"] != path or wiring["controller_symbol"] != owner.name:
                    raise MissingBinding("native tool-list controller does not match this entry owner")
                names = [item["symbol"].split(".")[-1] for item in entry.get("initializers", [])]
                if wiring["setup"] not in names:
                    raise MissingBinding("original setup must be supplied before native SDK invocation")
            elif (boundary["path"] != path or boundary.get("unit", "file") != cell
                  or not self._direct_reachable(source, symbol, boundary["symbol"])):
                raise MissingBinding("native controller-to-boundary binding is not statically identified: " + boundary["id"])
        self.check_nested(entry, selected)
        self._dependencies(path, cell)
        return schema, omitted

    def check_nested(self, entry, selected):
        path, cell, symbol = entry["path"], entry.get("unit", "file"), entry["symbol"]
        if entry["kind"] == "cli":
            return
        source = source_unit(self.inspector, path, cell)
        for whole in selected:
            if whole["mode"] not in {"W", "H"} or isinstance(whole.get("native_wiring"), dict):
                continue
            for child in selected:
                if child["id"] == whole["id"]:
                    continue
                wiring = child.get("native_wiring")
                nested = (isinstance(wiring, dict) and self._direct_reachable(source, whole["symbol"], symbol)) or (
                    child["path"] == path and child.get("unit", "file") == cell
                    and self._direct_reachable(source, whole["symbol"], child["symbol"]))
                if nested:
                    raise MissingBinding("nested native gates require the unsupported parent/child resource rule: "
                                         + whole["id"] + " -> " + child["id"])

    def _cli_interface(self, entry):
        command = entry.get("command")
        if not isinstance(command, list) or not command or any(not isinstance(argument, str) for argument in command):
            raise MissingBinding("CLI requires its original script command as an argv list")
        source_path = (self.inspector.root / entry["path"]).resolve()
        if entry["symbol"] not in {"__main__", "script", "<script>", source_path.name}:
            raise MissingBinding("CLI symbol must identify the original script as a whole")
        indices = [index for index, argument in enumerate(command[:2])
                   if (self.inspector.root / argument).resolve() == source_path]
        if len(indices) != 1 or indices[0] not in {0, 1}:
            raise MissingBinding("CLI command must invoke this exact original script")
        index = indices[0]
        if index == 1 and Path(command[0]).name.lower() not in {"python", "python3", "python.exe", "bash", "sh", "pwsh", "powershell", "powershell.exe"}:
            raise MissingBinding("original CLI interpreter is not identified")
        if index == 1 and Path(command[0]).name.lower() not in {"python", "python3", "python.exe"} and not shutil.which(command[0]):
            raise MissingBinding("native CLI interpreter is unavailable")
        if index == 0 and not os.access(source_path, os.X_OK):
            raise MissingBinding("original CLI interpreter must be supplied")
        placeholders = [match.group(1) for argument in command[index + 1:]
                        for match in re.finditer(r"^\{([A-Za-z_][A-Za-z0-9_]*)\}$", argument)]
        fixed = entry.get("fixed_arguments", {})
        if not isinstance(fixed, dict) or set(fixed) - set(placeholders):
            raise MissingBinding("CLI fixed arguments must name actual command placeholders")
        # Command arguments are native argv strings; no shell evaluation occurs.
        properties = {name: {"type": "string"} for name in dict.fromkeys(placeholders) if name not in fixed}
        if any(not isinstance(value, str) for value in fixed.values()):
            raise MissingBinding("native CLI argv values must be strings")
        if entry["path"].endswith(".py"):
            tree = ast.parse(source_unit(self.inspector, entry["path"], entry.get("unit", "file")))
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
                for _, tree in _notebook_definitions(self.inspector, path, cell, self.patches):
                    exec(compile(ast.fix_missing_locations(tree), namespace["__file__"], "exec"), namespace)
                self.notebooks[key] = namespace
            return self.notebooks[key]
        module = next((name for name, record in self.module_paths.items() if record[0] == path and not name.startswith("src.")), None)
        if module is None:
            raise MissingBinding("native Python module path is not importable")
        return importlib.import_module(module).__dict__

    def _register(self, gate, owner, unit):
        self.runtime.call("register", gate, owner, unit=unit)

    def _boundary_unit(self, boundary):
        return dict(id=self.inspector.request["source_version"][:16] + ":" + boundary["id"],
                    name=boundary["id"], symbol=boundary["symbol"], path=boundary["path"])

    def _assert_callable(self, callable_, symbol, path):
        original = inspect.unwrap(callable_)
        code = getattr(original, "__code__", None)
        if (getattr(callable_, "__qualname__", None) != symbol or code is None
            or Path(code.co_filename).resolve() != (self.inspector.root / path).resolve()):
            raise MissingBinding("native callable does not match its frozen source anchor: " + symbol)

    def _owner_key(self, entry):
        # A native constructor may mutate a supplied config object. Its owner
        # identity remains the original binding, rather than the mutated config.
        identity = id(entry)
        if identity not in self.owner_keys:
            self.owner_keys[identity] = json.dumps([entry["path"], entry.get("unit", "file"),
                entry.get("constructor"), entry.get("initializers", [])], sort_keys=True)
        return self.owner_keys[identity]

    async def _load(self, entry, unit):
        identity = unit["id"]
        if identity in self.entries:
            return self.entries[identity]
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
                for position in range(index + 1, len(command)):
                    match = re.fullmatch(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", command[position])
                    if match:
                        command[position] = arguments[match.group(1)]
                return subprocess.run(command, cwd=self.inspector.root, capture_output=True, check=False)
            owner_id = "native-cli-" + uuid.uuid4().hex
            gate = uuid.uuid4().hex + ":" + identity
            self._register(gate, owner_id, unit)
            callable_ = self.runtime.wrap(original, gate, owner_id)
            self.entries[identity] = callable_
            return callable_
        namespace = self._namespace(entry)
        owner, owner_id = None, None
        if entry["kind"] == "method":
            constructor = entry["constructor"]
            key = self._owner_key(entry)
            record = self.owners.get(key)
            if record is None:
                class_ = namespace[constructor["symbol"]]
                if not inspect.isclass(class_) or class_.__qualname__ != constructor["symbol"]:
                    raise MissingBinding("source constructor did not resolve to the original native class")
                positional, keyword = _actual_arguments(class_, constructor.get("arguments", {}))
                owner = class_(*positional, **keyword)
                if inspect.isawaitable(owner):
                    owner = await owner
                record = dict(owner=owner, id="native-owner-" + uuid.uuid4().hex, boundaries=set(), hooks=[], initialized=False)
                self.owners[key] = record
            owner, owner_id = record["owner"], record["id"]
        else:
            key = self._owner_key(entry)
            record = self.function_owners.setdefault(key,
                dict(id="native-module-" + uuid.uuid4().hex, boundaries=set(), hooks=[]))
            owner_id = record["id"]
        groups = {}
        for boundary in selected:
            if boundary["id"] in record["boundaries"]:
                continue
            wiring = boundary.get("native_wiring")
            if isinstance(wiring, dict) and boundary["mode"] != "R":
                key = (wiring["initializer"], wiring["tools_attribute"], wiring.get("schema_policy", "native"))
                groups.setdefault(key, {})[boundary["id"]] = boundary["symbol"]
                record["boundaries"].add(boundary["id"])
                continue
            symbol = self.recipes[boundary["id"]]["helper"] if boundary["mode"] == "R" else boundary["symbol"].split(".")[-1]
            original = getattr(owner, symbol) if owner is not None else namespace[symbol]
            expected = (entry["constructor"]["symbol"] + "." + symbol) if owner is not None else boundary["symbol"]
            self._assert_callable(original, expected, boundary["path"])
            gate = uuid.uuid4().hex + ":" + boundary["id"]
            self._register(gate, owner_id, self._boundary_unit(boundary))
            proxy = self.runtime.wrap(original, gate, owner_id)
            if owner is not None:
                install_method_proxy(owner, symbol, proxy)
            else:
                namespace[symbol] = proxy
            record["boundaries"].add(boundary["id"])
        for (initializer, attribute, policy), selectors in groups.items():
            hook = ToolListHandoff(self.runtime, owner, selectors, initializer, attribute,
                                   owner_id=owner_id, schema_policy=policy)
            original_bind = hook.bind_current_tools
            async def bind_verified(hook=hook, original_bind=original_bind, selectors=selectors, attribute=attribute):
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
                positional, keyword = _actual_arguments(original, initialization.get("arguments", {}))
                result = original(*positional, **keyword)
                if inspect.isawaitable(result):
                    await result
            for hook in record["hooks"]:
                if not hook.bindings:
                    raise MissingBinding("original setup did not bind the selected native tools")
                for binding in hook.bindings:
                    boundary = self.boundaries[binding["id"]]
                    self.runtime.call("label", binding["gate"], self._boundary_unit(boundary))
            record["initialized"] = True
        callable_ = getattr(owner, entry["symbol"].split(".")[-1]) if owner is not None else namespace[entry["symbol"]]
        if not callable(callable_):
            raise MissingBinding("native source symbol did not resolve to a callable")
        self._assert_callable(callable_, entry["symbol"], entry["path"])
        if not selected:
            gate = uuid.uuid4().hex + ":" + identity
            self._register(gate, owner_id, unit)
            callable_ = self.runtime.wrap(callable_, gate, owner_id)
        self.entries[identity] = callable_
        return callable_

    async def _invoke(self, entry, unit, arguments):
        with self._context():
            try:
                callable_ = await self._load(entry, unit)
            except MissingBinding:
                raise
            except Exception as error:
                raise MissingBinding("original native binding/initialization failed: " + str(error)) from error
            values = dict(entry.get("fixed_arguments", {}))
            values.update(arguments)
            positional, keyword = _actual_arguments(callable_, values)
            result = callable_(*positional, **keyword)
            return await result if inspect.iscoroutinefunction(callable_) else result

    def invoke(self, entry, unit, arguments):
        with self.lock:
            if self.loop is None:
                self.loop = asyncio.new_event_loop()
                def run():
                    asyncio.set_event_loop(self.loop)
                    self.loop.run_forever()
                self.thread = threading.Thread(target=run, daemon=True, name="seampact-native-owner")
                self.thread.start()
            return asyncio.run_coroutine_threadsafe(self._invoke(entry, unit, arguments), self.loop).result()


class UnitCatalog:
    def __init__(self, home, store, runtime):
        self.home, self.store, self.runtime = Path(home), store, runtime
        self.bindings, self.entry_specs = {}, {}
        self.lock = threading.RLock()

    def install(self, inspector, candidate, analysis_id):
        with self.lock:
            return self._install(inspector, candidate, analysis_id)

    def _candidate_binding(self, inspector, candidate):
        if not isinstance(candidate, dict) or "entries" not in candidate or not isinstance(candidate["entries"], list):
            raise ValueError("candidate.entries must be an array")
        normalized = dict(candidate, boundaries=candidate.get("boundaries", []))
        try:
            binding, binding_error = NativeBindings(inspector, normalized, self.runtime), None
        except (ValueError, KeyError, TypeError, SyntaxError) as error:
            binding, binding_error = None, str(error)
        identifiers = [entry.get("id") for entry in candidate.get("entries", [])
                       if isinstance(entry, dict) and isinstance(entry.get("id"), str)]
        duplicates = {identity for identity in identifiers if identifiers.count(identity) > 1}
        return binding, binding_error, duplicates

    @staticmethod
    def _inspect_entry(binding, binding_error, entry, source_id, duplicates, seen):
        result = dict(status="MISSING", reason=None, parameters=None, input_schema=None)
        try:
            if not isinstance(source_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", source_id) or source_id in seen or source_id in duplicates:
                raise MissingBinding("entry IDs must be unique ASCII identifiers")
            seen.add(source_id)
            if binding_error:
                raise MissingBinding(binding_error)
            for required in ("path", "symbol", "kind"):
                if not isinstance(entry.get(required), str) or not entry[required]:
                    raise MissingBinding("native entry field is missing: " + required)
            parameters, omitted = binding.describe(entry)
            result.update(status="READY", parameters=parameters, input_schema=parameters,
                          omitted_object_parameters=omitted, binding="SOURCE_CHECKED_NATIVE_ENTRY")
        except (ValueError, KeyError, TypeError, SyntaxError, OSError) as error:
            result["reason"] = str(error)
        return result

    def inspect_candidate(self, inspector, candidate):
        """Return the same static entry checks as installation, without state writes."""
        _, _, rows = self._candidate_checks(inspector, candidate)
        entries = [dict(id=row["entry_id"], path=row["entry"].get("path"),
            unit=row["entry"].get("unit", "file"), symbol=row["entry"].get("symbol"),
            kind=row["entry"].get("kind"), **row["check"]) for row in rows]
        ready = sum(entry["status"] == "READY" for entry in entries)
        return dict(schema="native-entry-report/1", source_version=inspector.request["source_version"],
            entries=entries, status="READY" if ready == len(entries) and ready else "PARTIAL" if ready else "MISSING",
            has_bindable_entries=bool(ready), reason=None if ready else "no bindable native entry was discovered",
            initialization="NOT_RUN", parallel=False, remote=False)

    def _candidate_checks(self, inspector, candidate):
        binding, binding_error, duplicates = self._candidate_binding(inspector, candidate)
        rows, seen, groups = [], set(), {}
        for index, value in enumerate(candidate["entries"]):
            entry = dict(value) if isinstance(value, dict) else {}
            identity = entry.get("id") or "entry-" + str(index)
            checked = self._inspect_entry(binding, binding_error, entry, identity, duplicates, seen)
            row = dict(index=index, entry_id=identity, entry=entry, check=checked)
            rows.append(row)
            if checked["status"] == "READY":
                groups.setdefault(binding._owner_key(entry), []).append(row)
        for group in groups.values():
            versions = {}
            for row in group:
                entry = row["entry"]
                versions.setdefault((entry["path"], entry.get("unit", "file"), entry["symbol"]), []).append(row)
            for alternatives in versions.values():
                cuts = {tuple(sorted(row["entry"].get("boundary_ids", []))) for row in alternatives}
                if len(cuts) > 1:
                    for row in alternatives:
                        row["check"].update(status="MISSING",
                            reason="different granularities of one native entry require separate alternative bindings")
            group = [row for row in group if row["check"]["status"] == "READY"]
            selected_ids = list(dict.fromkeys(identity for row in group for identity in row["entry"].get("boundary_ids", [])))
            selected = [binding.boundaries[identity] for identity in selected_ids]
            try:
                for row in group:
                    binding.check_nested(row["entry"], selected)
            except (ValueError, KeyError, TypeError, SyntaxError) as error:
                for row in group:
                    row["check"].update(status="MISSING", reason=str(error))
        return binding, duplicates, rows

    def _install(self, inspector, candidate, analysis_id):
        binding, duplicates, rows = self._candidate_checks(inspector, candidate)
        result = []
        for row in rows:
            index, entry, source_id, checked = row["index"], row["entry"], row["entry_id"], row["check"]
            identity = str(analysis_id) + ":" + str(source_id)
            if isinstance(source_id, str) and source_id in duplicates:
                identity += ":" + str(index)
            unit = dict(schema="native-unit/1", id=identity, entry_id=source_id,
                name=entry.get("name") or source_id, tool_name="unit_" + digest(identity.encode())[:32],
                path=entry.get("path"), source_unit=entry.get("unit", "file"), symbol=entry.get("symbol"),
                kind=entry.get("kind"), description=entry.get("description") or entry.get("symbol") or "Native entry",
                source_id=inspector.request.get("source_id"), source_version=inspector.request["source_version"],
                semantic_key=inspector.request["source_version"][:16] + ":" + str(source_id),
                analysis_id=analysis_id, installed_at=now(), status="MISSING", reason=None,
                parameters=None, input_schema=None, initialization="NOT_RUN", result_type=None,
                parallel=False, remote=False, boundary_ids=entry.get("boundary_ids", []))
            unit.update(checked)
            if checked["status"] == "READY":
                self.bindings[identity], self.entry_specs[identity] = binding, entry
                binding.approved_entries[identity] = entry
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
        return result

    def public(self):
        with self.lock:
            result = []
            for unit in self.store.list("unit"):
                if unit["status"] == "READY" and unit["id"] not in self.bindings:
                    unit.update(status="MISSING", reason="native binding is not loaded in this process")
                result.append(unit)
            return result

    def tools(self):
        return [dict(type="function", function=dict(name=unit["tool_name"], description=unit["description"],
                    parameters=unit["parameters"])) for unit in self.public() if unit["status"] == "READY"]

    def name_to_id(self, name):
        found = [unit["id"] for unit in self.public() if unit["tool_name"] == name and unit["status"] == "READY"]
        if len(found) != 1:
            raise ValueError("ready native tool name is not found")
        return found[0]

    def validate_arguments(self, unit_id, arguments):
        """Validate a proposed call before the caller's RL action selection."""
        unit = self.store.get("unit", unit_id)
        if not unit or unit["status"] != "READY" or unit_id not in self.bindings:
            raise ValueError("native Unit is not ready")
        if not isinstance(arguments, dict):
            raise ValueError("native tool arguments must be a JSON object")
        try:
            json.dumps(arguments, allow_nan=False)
        except (ValueError, TypeError) as error:
            raise ValueError("native tool arguments must contain JSON values") from error
        schema = unit["parameters"]
        entry = self.entry_specs[unit_id]
        if set(arguments) & (set(entry.get("fixed_arguments", {})) | set(unit.get("omitted_object_parameters", []))):
            raise ValueError("fixed or original-object native arguments cannot be overridden")
        missing = set(schema["required"]) - set(arguments)
        unknown = set(arguments) - set(schema["properties"])
        if missing or (unknown and not schema["additionalProperties"]):
            raise ValueError("native tool arguments do not match the source-derived interface")
        for name, value in arguments.items():
            if name in schema["properties"] and not _matches(value, schema["properties"][name]):
                raise ValueError("native tool parameter type mismatch: " + name)
        return True

    def invoke(self, unit_id, arguments):
        self.validate_arguments(unit_id, arguments)
        unit = self.store.get("unit", unit_id)
        entry = self.entry_specs[unit_id]
        try:
            result = self.bindings[unit_id].invoke(entry, unit, arguments)
        except (MissingBinding, ImportError, ModuleNotFoundError, FileNotFoundError, NameError, AttributeError, ProtocolError) as error:
            binding = self.bindings[unit_id]
            for identity, related in self.entry_specs.items():
                if self.bindings[identity] is binding and binding._owner_key(related) == binding._owner_key(entry):
                    item = self.store.get("unit", identity)
                    item.update(status="MISSING", reason=str(error))
                    self.store.save("unit", identity, item, "unit_binding_missing")
            raise
        unit.update(initialization="BOUND", result_type=type(result).__module__ + "." + type(result).__qualname__)
        self.store.save("unit", unit_id, unit, "unit_returned")
        return result
