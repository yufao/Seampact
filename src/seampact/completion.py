"""Finite, single-file CLI completion evidence; never a model-quality judgment.

The contract names actual argv placeholders, not a guessed output convention.
Only JSON objects and numeric NPY members in NPZ archives are checked. Existing
files are observed, never deleted, repaired, or attributed to a fresh execution.
"""

import ast
import hashlib
import json
import math
import os
import re
import struct
import zipfile
from pathlib import Path

from .verification import source_unit


SCHEMA = "native-completion/1"
_UNKNOWN = object()


class _ProfilePath(str):
    def __new__(cls, value, parameter, whole=True):
        result = super().__new__(cls, value)
        result.parameter = parameter
        result.whole = whole
        return result


def _covered(evidence, node):
    return any(row["start"] <= node.lineno and node.end_lineno <= row["end"] for row in evidence)


def _literal(node, default=None):
    return node.value if isinstance(node, ast.Constant) else default


class _OutputProfile:
    """Small syntax interpreter for the exact-file output profile, without imports.

    Unknown branches join conservatively. It follows parser namespaces, simple
    aliases, dictionary keys, and filename tests. It does not interpret algorithms,
    helper functions, arbitrary calls, directory naming, or arbitrary dataflow.
    """

    def __init__(self, tree, destinations, suffix, evidence):
        self.destinations, self.suffix, self.evidence = destinations, suffix, evidence
        self.writes = []
        self.numpy = {alias.asname or alias.name for node in tree.body if isinstance(node, ast.Import)
                      for alias in node.names if alias.name == "numpy"}
        self.json = {alias.asname or alias.name for node in tree.body if isinstance(node, ast.Import)
                     for alias in node.names if alias.name == "json"}
        self.argparse = {alias.asname or alias.name for node in tree.body if isinstance(node, ast.Import)
                         for alias in node.names if alias.name == "argparse"}

    def value(self, node, env):
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            return env.get(node.id, _UNKNOWN)
        if isinstance(node, ast.Attribute):
            value = self.value(node.value, env)
            return value.get(node.attr, _UNKNOWN) if isinstance(value, dict) else _UNKNOWN
        if isinstance(node, ast.Dict):
            keys = [self.value(key, env) for key in node.keys if key is not None]
            if len(keys) == len(node.keys) and all(isinstance(key, str) for key in keys):
                return {key: _UNKNOWN for key in keys}
        if isinstance(node, ast.Subscript):
            value, key = self.value(node.value, env), self.value(node.slice, env)
            if isinstance(value, (tuple, list)) and type(key) is int and -len(value) <= key < len(value):
                return value[key]
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = self.value(node.left, env), self.value(node.right, env)
            if isinstance(left, str) and isinstance(right, str):
                return left + right
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            value = self.value(node.operand, env)
            return not value if type(value) is bool else _UNKNOWN
        if isinstance(node, ast.Compare) and len(node.ops) == 1:
            left, right = self.value(node.left, env), self.value(node.comparators[0], env)
            if isinstance(left, _ProfilePath) or isinstance(right, _ProfilePath):
                return _UNKNOWN
            if left is not _UNKNOWN and right is not _UNKNOWN:
                if isinstance(node.ops[0], ast.Eq):
                    return left == right
                if isinstance(node.ops[0], ast.NotEq):
                    return left != right
                if isinstance(node.ops[0], ast.In) and isinstance(left, str) and isinstance(right, str):
                    return left in right
        if isinstance(node, ast.Call):
            name = ast.unparse(node.func)
            args = [self.value(arg, env) for arg in node.args]
            if name in {alias + ".ArgumentParser" for alias in self.argparse}:
                return ("argparse-parser",)
            if isinstance(node.func, ast.Attribute) and node.func.attr == "parse_args" and self.value(node.func.value, env) == ("argparse-parser",):
                return dict(self.destinations)
            if name == "dict" and not args and all(kw.arg is not None for kw in node.keywords):
                return {kw.arg: _UNKNOWN for kw in node.keywords}
            if name == "open" and args and isinstance(args[0], str):
                return ("file", args[0], args[1] if len(args) > 1 else "r")
            if name in {"os.path.basename", "os.path.splitext", "os.path.isfile", "os.path.isdir"} and len(args) == 1 and isinstance(args[0], str):
                if isinstance(args[0], _ProfilePath):
                    value = args[0]
                    if name == "os.path.basename":
                        return _ProfilePath(value, value.parameter, whole=False)
                    if name == "os.path.splitext":
                        return (_UNKNOWN, self.suffix if value.parameter == "output" else _UNKNOWN)
                    if name == "os.path.isfile":
                        return True if value.parameter == "input" else _UNKNOWN
                    return False
                return {"os.path.basename": lambda: os.path.basename(args[0]),
                        "os.path.splitext": lambda: os.path.splitext(args[0]),
                        "os.path.isfile": lambda: _UNKNOWN,
                        "os.path.isdir": lambda: _UNKNOWN}[name]()
            if isinstance(node.func, ast.Attribute) and node.func.attr == "endswith" and len(args) == 1:
                value = self.value(node.func.value, env)
                if isinstance(value, _ProfilePath):
                    return True if value.parameter == "output" and args[0] == self.suffix else _UNKNOWN
                if isinstance(value, str) and isinstance(args[0], str):
                    return value.endswith(args[0])
        return _UNKNOWN

    @staticmethod
    def join(left, right):
        result = {}
        for key in left.keys() | right.keys():
            a, b = left.get(key, _UNKNOWN), right.get(key, _UNKNOWN)
            if isinstance(a, dict) and isinstance(b, dict):
                result[key] = {name: _UNKNOWN for name in a.keys() & b.keys()}
            elif isinstance(a, _ProfilePath) or isinstance(b, _ProfilePath):
                result[key] = a if isinstance(a, _ProfilePath) and isinstance(b, _ProfilePath) and a.parameter == b.parameter and a.whole == b.whole else _UNKNOWN
            else:
                result[key] = a if a == b else _UNKNOWN
        return result

    def calls(self, node, env):
        for call in ast.walk(node):
            if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute):
                continue
            owner = call.func.value.id if isinstance(call.func.value, ast.Name) else None
            if isinstance(self.value(call.func.value, env), dict):
                raise ValueError("completion source profile does not support dictionary/namespace method mutation")
            keys, target, format_ = set(), _UNKNOWN, None
            if owner in self.numpy and call.func.attr in {"savez", "savez_compressed"}:
                format_ = "npz"
                target = self.value(call.args[0], env) if call.args else _UNKNOWN
                for keyword in call.keywords:
                    if keyword.arg is not None:
                        keys.add(keyword.arg)
                    else:
                        value = self.value(keyword.value, env)
                        if isinstance(value, dict):
                            keys.update(value)
            if owner in self.json and call.func.attr == "dump" and len(call.args) >= 2:
                format_ = "json"
                value, handle = self.value(call.args[0], env), self.value(call.args[1], env)
                keys = set(value) if isinstance(value, dict) else set()
                if isinstance(handle, tuple) and len(handle) == 3 and handle[0] == "file" and isinstance(handle[2], str) and "w" in handle[2]:
                    target = handle[1]
            if format_:
                self.writes.append((call, target, format_, keys))

    def scan(self, statements, env):
        env = dict(env)
        for node in statements:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if isinstance(node, ast.If):
                condition = self.value(node.test, env)
                if type(condition) is bool:
                    env = self.scan(node.body if condition else node.orelse, env)
                else:
                    env = self.join(self.scan(node.body, env), self.scan(node.orelse, env))
            elif isinstance(node, (ast.For, ast.While)):
                # A writer can be under an original per-input loop; source shape
                # does not establish that this loop actually processed an item.
                env = self.scan(node.body, env)
            elif isinstance(node, ast.With):
                for item in node.items:
                    if isinstance(item.optional_vars, ast.Name):
                        env[item.optional_vars.id] = self.value(item.context_expr, env)
                env = self.scan(node.body, env)
            elif isinstance(node, ast.Try):
                env = self.scan(node.body, env)
                env = self.scan(node.orelse + node.finalbody, env)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                self.calls(node.value, env)
                value = self.value(node.value, env)
                for target in node.targets if isinstance(node, ast.Assign) else [node.target]:
                    if isinstance(target, ast.Name):
                        env[target.id] = value
                    elif isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name):
                        key = _literal(target.slice)
                        current = env.get(target.value.id)
                        if isinstance(current, dict) and isinstance(key, str):
                            current = dict(current)
                            current[key] = _UNKNOWN
                            env[target.value.id] = current
            elif isinstance(node, (ast.AugAssign, ast.Delete)):
                targets = node.targets if isinstance(node, ast.Delete) else [node.target]
                for target in targets:
                    name = target.id if isinstance(target, ast.Name) else target.value.id if isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name) else None
                    if name is not None:
                        env[name] = _UNKNOWN
            else:
                self.calls(node, env)
        return env


def check_completion(inspector, entry):
    """Validate an optional proposal against frozen Python CLI source.

    Unsupported forms raise ValueError, so they cannot grant a READY entry.
    Evidence must cover complete declarations, writes and required-key sources.
    """
    contract = entry.get("completion")
    if contract is None:
        return None
    if not isinstance(contract, dict) or contract.get("schema") != SCHEMA:
        raise ValueError("unsupported completion contract schema")
    if entry.get("kind") != "cli" or not entry.get("path", "").endswith(".py"):
        raise ValueError("completion contract covers only original Python CLI single-file output")
    format_ = contract.get("format")
    keys = contract.get("required_keys")
    if format_ not in {"json", "npz"} or not isinstance(keys, list) or not keys or any(not isinstance(key, str) or not key for key in keys) or len(set(keys)) != len(keys):
        raise ValueError("completion requires json/npz and explicit unique required_keys")
    parameters = [contract.get("input_parameter"), contract.get("output_parameter")]
    if any(not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_]\w*", name) for name in parameters) or parameters[0] == parameters[1]:
        raise ValueError("completion requires distinct explicit input/output placeholders")
    evidence = contract.get("evidence")
    source = source_unit(inspector, entry["path"], entry.get("unit", "file"))
    if not isinstance(evidence, list) or not evidence:
        raise ValueError("completion requires frozen source evidence")
    for row in evidence:
        if not isinstance(row, dict) or row.get("path") != entry["path"] or row.get("unit", "file") != entry.get("unit", "file") or type(row.get("start")) is not int or type(row.get("end")) is not int or not 1 <= row["start"] <= row["end"] <= len(source.splitlines()):
            raise ValueError("completion evidence must reference valid ranges in the original entry")
    tree = ast.parse(source)
    imported = {alias.asname or alias.name for node in tree.body if isinstance(node, ast.Import)
                for alias in node.names if alias.name in {"numpy", "json", "argparse", "os"}}
    def shadowed(node):
        if not isinstance(node, (ast.Name, ast.Attribute, ast.Subscript)) or not isinstance(node.ctx, (ast.Store, ast.Del)):
            return False
        while isinstance(node, (ast.Attribute, ast.Subscript)):
            node = node.value
        return isinstance(node, ast.Name) and node.id in imported | {"open", "dict"}
    if any(shadowed(node) for node in ast.walk(tree)):
        raise ValueError("completion source profile does not support shadowed imports or file primitives")
    argv = entry.get("command", [])
    destinations = {}
    for index, parameter in enumerate(parameters):
        positions = [i for i, item in enumerate(argv) if item == "{" + parameter + "}"]
        if len(positions) != 1 or positions[0] == 0:
            raise ValueError("completion input/output must be exact original argv placeholders")
        flag = argv[positions[0] - 1]
        declarations = [call for call in ast.walk(tree) if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute) and call.func.attr == "add_argument"
            and any(_literal(argument) == flag for argument in call.args)]
        if len(declarations) != 1 or not flag.startswith("-") or not _covered(evidence, declarations[0]):
            raise ValueError("completion input/output option declaration is not covered by source evidence")
        call = declarations[0]
        names = [_literal(arg) for arg in call.args]
        destination = next((_literal(kw.value) for kw in call.keywords if kw.arg == "dest"), None)
        destination = destination or next((name[2:].replace("-", "_") for name in names if isinstance(name, str) and name.startswith("--")), flag.lstrip("-").replace("-", "_"))
        destinations[destination] = _ProfilePath("seampact-input.file", "input") if index == 0 else _ProfilePath("seampact-result." + format_, "output")
    # All relevant bindings and key mutations must be included, rather than
    # accepting an isolated save spelling in an unrelated evidence range.
    profile = _OutputProfile(tree, destinations, "." + format_, evidence)
    profile.scan(tree.body, {})
    writes = [row for row in profile.writes if row[2] == format_]
    if not writes or any(not isinstance(target, _ProfilePath) or target.parameter != "output" or not target.whole or not set(keys) <= actual or not _covered(evidence, call)
                         for call, target, _, actual in writes):
        raise ValueError("completion exact-file write and required keys are not established by the finite source profile (UNKNOWN)")
    relevant = [node for node in ast.walk(tree) if isinstance(node, (ast.Assign, ast.AnnAssign))]
    if any(not _covered(evidence, node) for node in relevant):
        raise ValueError("completion source profile requires evidence covering its assignments and key definitions")
    return dict(contract, source_status="SOURCE_CHECKED_SINGLE_FILE", effect_quality="UNKNOWN")


def _fingerprint(path):
    if not path.is_file():
        return None
    stat = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return dict(sha256=digest.hexdigest(), bytes=stat.st_size, mtime_ns=stat.st_mtime_ns)


def before_completion(contract, arguments, root):
    """Observe original argv paths before execution; root is the original cwd."""
    if contract is None:
        return None
    suffix = "." + contract["format"]
    values = [arguments.get(contract[field]) for field in ("input_parameter", "output_parameter")]
    if any(not isinstance(value, str) or not value or any(char in value for char in "*?[]") for value in values):
        raise ValueError("completion requires explicit single-file input and output paths; directory/glob mapping is UNKNOWN")
    input_path, output_path = [(Path(root) / value).resolve() for value in values]
    if not input_path.is_file() or input_path == output_path or output_path.suffix != suffix or output_path.is_dir():
        raise ValueError("completion requires an existing single input file and a distinct exact " + suffix + " output path")
    return dict(contract=contract, input_path=input_path, output_path=output_path,
                input_before=_fingerprint(input_path), output_before=_fingerprint(output_path))


def _npy(stream):
    if stream.read(6) != b"\x93NUMPY":
        raise ValueError("invalid NPY magic")
    version = stream.read(2)
    width = 2 if version == b"\x01\x00" else 4 if version in {b"\x02\x00", b"\x03\x00"} else 0
    raw = stream.read(width)
    if not width or len(raw) != width:
        raise ValueError("unsupported or truncated NPY header")
    length = int.from_bytes(raw, "little")
    raw = stream.read(length)
    if len(raw) != length:
        raise ValueError("truncated NPY header")
    header = ast.literal_eval(raw.decode("utf-8" if version == b"\x03\x00" else "latin1"))
    shape, dtype = header.get("shape"), header.get("descr")
    if not isinstance(shape, tuple) or any(type(size) is not int or size < 0 for size in shape) or type(header.get("fortran_order")) is not bool:
        raise ValueError("invalid NPY dimensions")
    if not isinstance(dtype, str) or not re.fullmatch(r"[<>=|][biufc](?:1|2|4|8|16)", dtype):
        raise ValueError("only primitive numeric NPY dtypes are supported; object/pickle is never loaded")
    size = int(dtype[2:])
    allowed = {"b": {1}, "i": {1, 2, 4, 8}, "u": {1, 2, 4, 8}, "f": {2, 4, 8, 16}, "c": {8, 16}}
    if size not in allowed[dtype[1]]:
        raise ValueError("invalid numeric NPY dtype size")
    expected = math.prod(shape) * size
    actual = sum(len(chunk) for chunk in iter(lambda: stream.read(1024 * 1024), b""))
    if actual != expected or expected == 0:
        raise ValueError("empty or truncated NPY data")
    return dict(shape=list(shape), dtype=dtype)


def _validity(path, contract):
    if not path.is_file():
        return dict(status="MISSING", reason="expected output file is absent")
    try:
        if contract["format"] == "json":
            def invalid_constant(value):
                raise ValueError("non-finite JSON number")
            value = json.loads(path.read_text(encoding="utf-8"), parse_constant=invalid_constant)
            if not isinstance(value, dict) or not set(contract["required_keys"]) <= value.keys():
                raise ValueError("JSON object or required keys are missing")
            return dict(status="VALID", keys=list(value))
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if len(set(names)) != len(names) or not all(name.endswith(".npy") and "/" not in name and "\\" not in name for name in names):
                raise ValueError("NPZ contains duplicate or unsupported members")
            keys = {name[:-4] for name in names}
            if not set(contract["required_keys"]) <= keys:
                raise ValueError("NPZ required keys are missing")
            arrays = {}
            for name in names:
                with archive.open(name) as stream:
                    arrays[name[:-4]] = _npy(stream)
        return dict(status="VALID", keys=sorted(keys), arrays=arrays)
    except (OSError, ValueError, SyntaxError, UnicodeError, zipfile.BadZipFile, EOFError, struct.error,
            AttributeError, TypeError, RuntimeError, NotImplementedError, OverflowError) as error:
        return dict(status="INVALID", reason=str(error))


def after_completion(snapshot, process):
    """Attach independent process, artifact and cache facts to CompletedProcess."""
    report = dict(schema=SCHEMA, contract_declared=snapshot is not None,
                  process=dict(returncode=process.returncode, succeeded=process.returncode == 0),
                  task_status="UNKNOWN", newly_completed=False, effect_quality="UNKNOWN")
    if snapshot is None:
        report["reason"] = "no single-file completion contract was declared"
    else:
        output = _fingerprint(snapshot["output_path"])
        before = snapshot["output_before"]
        changed = output != before and output is not None
        validity = _validity(snapshot["output_path"], snapshot["contract"])
        input_unchanged = _fingerprint(snapshot["input_path"]) == snapshot["input_before"]
        report.update(result=validity, input_unchanged=input_unchanged,
                      output=dict(preexisting=before is not None, changed=changed,
                                  before=before, after=output),
                      expected_items=1, valid_items=int(validity["status"] == "VALID"),
                      newly_completed_items=0)
        if process.returncode != 0 or validity["status"] != "VALID" or not input_unchanged:
            report.update(task_status="INCOMPLETE", reason="process failure, missing/invalid result, or input changed during execution")
        elif not changed:
            report.update(task_status="UNKNOWN", reason="preexisting valid result was unchanged; its input association and fresh computation are unproved")
        else:
            report.update(task_status="COMPLETE", newly_completed=True, newly_completed_items=1,
                          reason="successful process produced a structurally valid changed result for the explicit single-file contract")
    process.seampact_completion = report
    return report
