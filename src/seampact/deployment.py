"""Source-derived runtime groups, their hosts, instances and lifecycle control.

Unknown object/state/control edges keep the original source context together.
The scalar remote rule covers synchronous scalar-value entries only. The native
group host keeps a whole checked group, with its original owners, construction
and controllers, inside one worker process; only declared ports leave it. No
lifecycle operation is granted unless the group contract marks it SUPPORTED.
"""

import ast
import base64
import copy
import hashlib
import io
import json
import math
import re
import shlex
import shutil
import sys
import threading
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from . import backends
from .inspection import digest, safe_path
from .runtime import ProtocolError, _task_context
from .store import now
from .verification import source_unit
from .environments import normalize_environment
from .image_build import worker_dockerfile
from .target_runtime import core_identity, copy_core, environment_selection
from .lifecycle import build_lifecycle_evidence

REMOTE_PROFILE = dict(id="python-native-scalar-worker/2", confirmed=True, python_versions=["3.10", "3.11", "3.12", "3.13"],
    process_per_group=1, replicas=1, preserve_original_lifetimes=True,
    session_recovery=False, codec="JSON_SCALARS", parallel=False,
    source="用户确认：按原对象/共享状态/控制依赖分组，远程条件独立核对，未覆盖继续本地")
GROUP_PROFILE = dict(id="python-native-group-worker/2", process_per_instance=1, python_versions=["3.10", "3.11", "3.12", "3.13"],
    preserve_original_lifetimes=True, session_recovery=False, codec="group-port/1", parallel=False,
    environment="RECHECKED_IN_HOST",
    source="部署前建设方案 P2：整组冻结原源码与核对后的绑定在同一工作进程；原 owner、构造、controller 与内部许可留在组内")
CONTRACT = "runtime-group-contract/1"
IDENTITY_PROTOCOL = "process-identity/1"
PORT_CODEC = dict(id="group-port/1", transport="inline JSON body of one call", visibility="caller of that call",
    validity="that call only", identity="sha256 over byte/array payloads",
    types=["None", "bool", "int", "finite float", "str", "bytes", "list", "tuple", "dict with str keys",
           "numpy.ndarray / numpy scalar without object dtype (npy, allow_pickle=False)"],
    rejected="any other type is reported by name; no str()/JSON conversion and no pickle")
LIVE = {"UNDEPLOYED", "STARTING", "READY", "DRAINING", "UNKNOWN"}


class RemoteGap(ValueError):
    pass


class PortGap(ValueError):
    pass


def json_request(url, body=None, timeout=None):
    payload = None if body is None else json.dumps(body, ensure_ascii=False, allow_nan=False).encode()
    request = Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        # No automatic retry: an absent response is not evidence of non-execution.
        with urlopen(request, timeout=timeout) as response:
            value = json.load(response)
    except HTTPError as error:
        detail = json.load(error)
        failure = ProtocolError(detail.get("error", str(error)))
        failure.answer = detail
        raise failure from error
    return value


def encode_port(value):
    numpy = sys.modules.get("numpy")
    def walk(item):
        kind = type(item)
        if item is None or kind in (bool, int, str):
            return item
        if kind is float:
            if not math.isfinite(item):
                raise PortGap("group-port/1 carries finite floats only")
            return item
        if kind is bytes:
            return {"$bytes": base64.b64encode(item).decode("ascii")}
        if kind is list:
            return [walk(member) for member in item]
        if kind is tuple:
            return {"$tuple": [walk(member) for member in item]}
        if kind is dict:
            if any(type(key) is not str or key.startswith("$") for key in item):
                raise PortGap("group-port/1 dictionaries need str keys without a leading $")
            return {key: walk(member) for key, member in item.items()}
        if numpy is not None and (kind is numpy.ndarray or (isinstance(item, numpy.generic) and kind.__module__ == "numpy")):
            array = numpy.asarray(item)
            if array.dtype.hasobject:
                raise PortGap("group-port/1 does not carry object arrays")
            buffer = io.BytesIO()
            numpy.save(buffer, array, allow_pickle=False)
            data = buffer.getvalue()
            return {"$ndarray": dict(dtype=array.dtype.str, shape=list(array.shape), scalar=kind is not numpy.ndarray,
                                     sha256=hashlib.sha256(data).hexdigest(), npy=base64.b64encode(data).decode("ascii"))}
        raise PortGap("group-port/1 has no encoding for " + kind.__module__ + "." + kind.__qualname__)
    return walk(value)


def decode_port(value):
    if isinstance(value, list):
        return [decode_port(member) for member in value]
    if not isinstance(value, dict):
        return value
    if not any(key.startswith("$") for key in value):
        return {key: decode_port(member) for key, member in value.items()}
    if len(value) != 1:
        raise PortGap("unknown group-port/1 marker")
    key, payload = next(iter(value.items()))
    if key == "$bytes":
        return base64.b64decode(payload, validate=True)
    if key == "$tuple":
        return tuple(decode_port(member) for member in payload)
    if key == "$ndarray":
        try:
            import numpy
        except ImportError:
            raise PortGap("this process has no numpy; the array port cannot be decoded") from None
        data = base64.b64decode(payload["npy"], validate=True)
        if hashlib.sha256(data).hexdigest() != payload["sha256"]:
            raise PortGap("array payload identity differs from its declared sha256")
        array = numpy.load(io.BytesIO(data), allow_pickle=False)
        if array.dtype.str != payload["dtype"] or list(array.shape) != payload["shape"]:
            raise PortGap("array payload dtype/shape differs from its declaration")
        return array[()] if payload.get("scalar") else array
    raise PortGap("unknown group-port/1 marker: " + key)


def worker_spec(binding, group, unit):
    candidate = binding.candidate
    entries = [item for item in candidate["entries"] if item["id"] in group["entries"]]
    selected = {identity for entry in entries for identity in entry.get("boundary_ids", [])}
    candidate = dict(candidate, entries=entries,
        boundaries=[item for item in candidate.get("boundaries", []) if item["id"] in selected], runtime_groups=[])
    contract = group.get("contract") or {}
    return dict(schema="native-worker/1", profile=contract.get("host_profile", REMOTE_PROFILE["id"]),
                contract_digest=contract.get("digest"), group=group, request=binding.inspector.request,
                runtime_environment=group.get("runtime_environment") or binding.inspector.request.get("runtime_environment") or {"python": "3.11"},
                platform_core=core_identity("worker"),
                candidate=candidate, analysis_id=unit["analysis_id"], installation_id=unit["installation_id"])


def binding_fingerprint(spec):
    payload = {key: spec[key] for key in ("request", "candidate", "analysis_id", "installation_id")}
    payload["group"] = {key: spec["group"][key] for key in ("id", "units", "source_id", "source_version")}
    for key in ("runtime_environment", "platform_core"):
        if key in spec:
            payload[key] = spec[key]
    if spec.get("profile", REMOTE_PROFILE["id"]) != REMOTE_PROFILE["id"]:
        payload["host"] = dict(profile=spec["profile"], contract_digest=spec.get("contract_digest"))
    return digest(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode())


class ScalarRule:
    """Abstract types, not generated business code. Unknown constructs fail closed."""
    safe_modules = {"re", "json", "shutil", "pathlib", "typing", "__future__"}
    scalar_types = {"string": "str", "integer": "int", "boolean": "bool", "null": "None"}

    def __init__(self, tree, local_imports=()):
        self.imports = {}
        reserved = {"str", "int", "bool", "isinstance", "len"}
        for node in ast.walk(tree):
            if (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store) and node.id in reserved
                or isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name in reserved
                or isinstance(node, ast.arg) and node.arg in reserved
                or isinstance(node, ast.alias) and (node.asname or node.name) in reserved):
                raise RemoteGap("builtin name rebinding is not covered")
        for statement in tree.body:
            if isinstance(statement, ast.Import):
                for alias in statement.names:
                    if alias.name not in self.safe_modules:
                        raise RemoteGap("module import effects are not covered: " + alias.name)
                    self.imports[alias.asname or alias.name] = alias.name
            elif isinstance(statement, ast.ImportFrom):
                if statement not in local_imports and (statement.level or statement.module not in self.safe_modules):
                    raise RemoteGap("relative/external import lifecycle is not covered")
                if any(alias.name == "*" for alias in statement.names):
                    raise RemoteGap("wildcard imports are not covered")
            elif isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if statement.decorator_list:
                    raise RemoteGap("module callable decorators are not covered")
                for value in [*statement.args.defaults, *[v for v in statement.args.kw_defaults if v is not None]]:
                    if not isinstance(value, ast.Constant) or type(value.value) not in {str, int, bool, type(None)}:
                        raise RemoteGap("effectful or mutable function defaults are not covered")
                for argument in [*statement.args.posonlyargs, *statement.args.args, *statement.args.kwonlyargs]:
                    if argument.annotation and any(isinstance(n, ast.Call) for n in ast.walk(argument.annotation)):
                        raise RemoteGap("annotation evaluation effects are not covered")
                if statement.returns and any(isinstance(n, ast.Call) for n in ast.walk(statement.returns)):
                    raise RemoteGap("return annotation effects are not covered")
            elif isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant):
                continue
            elif isinstance(statement, (ast.Assign, ast.AnnAssign)):
                try:
                    ast.literal_eval(statement.value)
                except (ValueError, TypeError):
                    raise RemoteGap("module initialization effects are not covered")
                targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
                if any(not isinstance(target, ast.Name) for target in targets):
                    raise RemoteGap("module state writes are not covered")
            else:
                raise RemoteGap("module initialization/control is not covered")
        if any(isinstance(node, (ast.Global, ast.Nonlocal)) for node in ast.walk(tree)):
            raise RemoteGap("module has explicit shared-state writers")
        if any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {"eval", "exec", "globals", "locals", "setattr", "delattr", "__import__"} for node in ast.walk(tree)):
            raise RemoteGap("dynamic namespace/state mutation is not covered")
        imported = set(self.imports)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.Delete)):
                targets = node.targets if isinstance(node, (ast.Assign, ast.Delete)) else [node.target]
                if any(any(isinstance(n, ast.Name) and n.id in imported for n in ast.walk(target)) for target in targets):
                    raise RemoteGap("imported module state can be rebound or mutated")
        if any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in imported for node in tree.body):
            raise RemoteGap("imported module name is shadowed")

    def expression(self, node, environment):
        if isinstance(node, ast.Constant) and type(node.value) in {str, int, bool, type(None)}:
            return {str: "str", int: "int", bool: "bool", type(None): "None"}[type(node.value)]
        if isinstance(node, ast.Name) and node.id in environment:
            return environment[node.id]
        if isinstance(node, ast.IfExp):
            self.expression(node.test, environment)
            left, right = self.expression(node.body, environment), self.expression(node.orelse, environment)
            if left == right:
                return left
            if {left, right} <= {"str", "None", "str-or-none"}:
                return "str-or-none"
        if isinstance(node, ast.Compare):
            values = [self.expression(n, environment) for n in [node.left, *node.comparators]]
            if set(values) <= {"str", "int", "bool", "None", "str-or-none"} and all(isinstance(op, (ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE)) for op in node.ops):
                return "bool"
        if isinstance(node, ast.BoolOp):
            values = [self.expression(n, environment) for n in node.values]
            if len(set(values)) == 1:
                return values[0]
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            self.expression(node.operand, environment)
            return "bool"
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = self.expression(node.left, environment), self.expression(node.right, environment)
            if left == right and left in {"str", "int"}:
                return left
            if {left, right} <= {"str", "str-or-none"}:
                return "str"
        if isinstance(node, ast.Subscript) and self.expression(node.value, environment) == "str":
            items = [node.slice] if not isinstance(node.slice, ast.Slice) else [v for v in (node.slice.lower, node.slice.upper, node.slice.step) if v]
            if all(self.expression(value, environment) == "int" for value in items):
                return "str"
        if isinstance(node, ast.Call) and not node.keywords:
            function = node.func
            if isinstance(function, ast.Name):
                if function.id == "isinstance" and len(node.args) == 2 and isinstance(node.args[1], ast.Name) and node.args[1].id in {"str", "int", "bool"}:
                    self.expression(node.args[0], environment)
                    return "bool"
                if function.id == "len" and len(node.args) == 1 and self.expression(node.args[0], environment) in {"str", "str-or-none"}:
                    return "int"
            if isinstance(function, ast.Attribute):
                if isinstance(function.value, ast.Name) and self.imports.get(function.value.id) == "re" and function.attr in {"match", "search", "fullmatch"} and len(node.args) == 2 and isinstance(node.args[0], ast.Constant) and type(node.args[0].value) is str and self.expression(node.args[1], environment) == "str":
                    return "regex-match-or-none"
                receiver = self.expression(function.value, environment)
                if receiver == "regex-match-or-none" and function.attr == "group" and len(node.args) == 1 and isinstance(node.args[0], ast.Constant) and type(node.args[0].value) is int:
                    return "str-or-none"
                if receiver == "str" and not node.args and function.attr in {"strip", "lstrip", "rstrip", "lower", "upper", "casefold"}:
                    return "str"
                if receiver == "str" and not node.args and function.attr in {"isspace", "isdigit", "isalpha", "isalnum", "isascii"}:
                    return "bool"
        raise RemoteGap("value/state/effect rule is missing at source line " + str(getattr(node, "lineno", "?")))

    def statements(self, statements, environment, output):
        returned = False
        for statement in statements:
            if returned:
                break
            if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant) and type(statement.value.value) is str:
                continue
            if isinstance(statement, (ast.Assign, ast.AnnAssign)):
                targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
                value = self.expression(statement.value, environment)
                if any(not isinstance(target, ast.Name) for target in targets):
                    raise RemoteGap("writes through object/container references are not covered")
                for target in targets:
                    environment[target.id] = value
            elif isinstance(statement, ast.Return):
                actual = self.expression(statement.value, environment) if statement.value else "None"
                if actual != output:
                    raise RemoteGap("actual return type is not established by the scalar rule")
                returned = True
            elif isinstance(statement, ast.If):
                self.expression(statement.test, environment)
                left, right = dict(environment), dict(environment)
                left_return = self.statements(statement.body, left, output)
                right_return = self.statements(statement.orelse, right, output)
                if left_return and right_return:
                    returned = True
                elif left_return:
                    environment.update(right)
                elif right_return:
                    environment.update(left)
                else:
                    for key in set(left) | set(right):
                        if left.get(key) != right.get(key):
                            environment.pop(key, None)
                    environment.update({key: value for key, value in left.items() if right.get(key) == value})
            else:
                raise RemoteGap("control/lifecycle rule is missing at source line " + str(statement.lineno))
        return returned


def module_closure(inspector, entry_path):
    """Check source package initialization too, not just the entry's body."""
    modules = {}
    for path in inspector.files:
        if not path.endswith(".py"):
            continue
        parts = list(Path(path).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        if parts and all(part.isidentifier() for part in parts):
            modules[".".join(parts)] = path
            if parts[0] == "src" and len(parts) > 1:
                modules[".".join(parts[1:])] = path
    entry_module = next((module for module, path in modules.items() if path == entry_path and not module.startswith("src.")), None)
    if not entry_module:
        raise RemoteGap("source module path is not established")
    queue, checked, rules = [entry_module], set(), {}
    while queue:
        module = queue.pop()
        if module in checked:
            continue
        checked.add(module)
        path = modules[module]
        tree = ast.parse(source_unit(inspector, path))
        package = module if path.endswith("/__init__.py") else module.rpartition(".")[0]
        for index in range(1, len(module.split("."))):
            parent = ".".join(module.split(".")[:index])
            if parent in modules:
                queue.append(parent)
        local = []
        for statement in tree.body:
            if isinstance(statement, ast.ImportFrom):
                base = statement.module or ""
                if statement.level:
                    segments = package.split(".") if package else []
                    if statement.level > len(segments):
                        raise RemoteGap("relative import leaves the original package")
                    base = ".".join(segments[:len(segments) - statement.level + 1] + ([base] if base else []))
                if base in modules:
                    local.append(statement)
                    queue.append(base)
                    for alias in statement.names:
                        member = base + "." + alias.name
                        if member in modules:
                            queue.append(member)
            elif isinstance(statement, ast.Import):
                if any(alias.name in modules for alias in statement.names):
                    raise RemoteGap("source shadowing of standard modules is not covered")
        rules[path] = (tree, ScalarRule(tree, local))
    return rules[entry_path], sorted(checked)


def check_remote(inspector, entry, checked, boundaries):
    result = dict(status="LOCAL_ONLY", rule="scalar-independent-entry/1", formal_proof=False,
                  codec="JSON_SCALARS", reason=None, evidence=[])
    try:
        if checked["status"] != "READY":
            raise RemoteGap("native entry binding is missing")
        if entry.get("kind") != "function" or entry.get("unit", "file") != "file" or "." in entry["symbol"] or any(entry.get(field) for field in ("constructor", "initializers", "binding_program", "resources")):
            raise RemoteGap("object, construction, resource or notebook lifetime is not covered remotely")
        (tree, rule), closure = module_closure(inspector, entry["path"])
        found = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == entry["symbol"]]
        if len(found) != 1:
            raise RemoteGap("remote rule requires one synchronous top-level original function")
        function = found[0]
        schema = checked["input_schema"]
        if schema.get("additionalProperties") or function.args.vararg or function.args.kwarg:
            raise RemoteGap("open or variadic remote interface is not covered")
        environment = {}
        for parameter in [*function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs]:
            annotation = parameter.annotation
            typename = annotation.id if isinstance(annotation, ast.Name) else annotation.value if isinstance(annotation, ast.Constant) else None
            if typename not in {"str", "int", "bool"}:
                raise RemoteGap("remote inputs require source-anchored immutable scalar types")
            environment[parameter.arg] = typename
        positional = [*function.args.posonlyargs, *function.args.args]
        defaults = dict(zip([argument.arg for argument in positional[len(positional) - len(function.args.defaults):]], function.args.defaults))
        defaults.update({argument.arg: value for argument, value in zip(function.args.kwonlyargs, function.args.kw_defaults) if value is not None})
        values = {name: ast.literal_eval(value) for name, value in defaults.items()}
        values.update(entry.get("fixed_arguments", {}))
        for name, value in values.items():
            if type(value).__name__ != environment.get(name):
                raise RemoteGap("original fixed/default value does not establish its declared scalar type: " + name)
        output = rule.scalar_types.get(checked["result_schema"].get("type"))
        if not output:
            raise RemoteGap("remote return requires an immutable scalar type")
        if not rule.statements(function.body, environment, output):
            raise RemoteGap("not all return paths are covered")
        for identity in entry.get("boundary_ids", []):
            boundary = boundaries[identity]
            if boundary.get("mode") not in {"W", "H"} or boundary.get("rule") or boundary.get("path") != entry["path"] or boundary.get("symbol") != entry["symbol"] or boundary.get("unit", "file") != "file" or isinstance(boundary.get("native_wiring"), dict):
                raise RemoteGap("internal controller boundary requires an additional remote rule")
        result.update(status="REMOTE_CHECKED", reason=None, evidence=[
            dict(path=entry["path"], symbol=entry["symbol"], line=function.lineno,
                 source_sha256=inspector.files[entry["path"]]["sha256"],
                 import_closure=closure,
                 state="NO_CAPTURED_NATIVE_STATE_IN_SUPPORTED_BODY",
                 control="WHOLE_ORIGINAL_SYNC_ENTRY", effects="FINITE_SCALAR_RULE",
                 lifecycle="ORIGINAL_MODULE_LOAD_AND_PER_CALL_LOCALS", failure="RETURN_OR_ERROR_NO_REPLAY")])
    except (RemoteGap, ValueError, KeyError, TypeError, SyntaxError) as error:
        result["reason"] = str(error)
    return result


def prepare_rule(entry):
    if entry.get("kind") == "cli":
        return dict(status="UNSUPPORTED", basis="每次调用启动原进程，没有可提前构造的常驻对象")
    if entry.get("binding_program") is not None:
        return dict(status="UNSUPPORTED", basis="原构造按请求执行（REQUEST_SCOPED），不提前构造，也不改成常驻单例")
    if entry.get("kind") == "method":
        return dict(status="SUPPORTED", scope="OWNER_CONSTRUCTION",
                    basis="原构造与初始化参数已冻结且不取请求输入；常驻 owner 仅构造一次，提前到首个请求前不改变次数与参数；不执行入口")
    return dict(status="SUPPORTED", scope="MODULE_IMPORT", basis="仅导入原模块（首个调用同样导入），不执行入口")


def check_group_host(rows, members, scalar):
    """Which members the declared host can run; environment facts are rechecked inside it."""
    by_id = {row["entry_id"]: row for row in rows}
    result, gaps = {}, {}
    for identity in members:
        row = by_id[identity]
        check, entry = row["check"], row["entry"]
        member = dict(status="HOST_CHECKED", reason=None, prepare=prepare_rule(entry),
                      ports=dict(codec="JSON_SCALARS" if scalar else PORT_CODEC["id"]))
        if check.get("execution_context"):
            member["execution_context"] = check["execution_context"]
        if check.get("import_context"):
            member["import_context"] = check["import_context"]
        schema = check.get("input_schema") or {}
        native_inputs = sorted(name for name, port in schema.get("properties", {}).items()
                               if "x-native-type" in port and name in schema.get("required", []))
        result_schema = check.get("result_schema") or {}
        if check["status"] != "READY" and not check.get("host_condition"):
            member.update(status="MISSING", reason="native entry binding is missing: " + str(check.get("reason")))
        elif native_inputs:
            member.update(status="MISSING", reason="组外端口没有原对象输入的编码，该成员保留本地组合：" + ", ".join(native_inputs))
        elif "x-native-type" in result_schema:
            member.update(status="MISSING", reason="原返回对象 " + str(result_schema["x-native-type"]) +
                          " 没有组外编码；不做 str()/JSON 转换或 pickle，该成员保留本地执行")
        else:
            member["ports"].update(inputs=sorted(schema.get("properties", {})),
                result="SCHEMA_AND_TYPE_CHECKED_AT_RETURN" if result_schema else "TYPE_CHECKED_AT_RETURN")
            if check.get("host_condition"):
                gaps[identity] = check["host_condition"]["gaps"]
        result[identity] = member
    hosted = [identity for identity in members if result[identity]["status"] == "HOST_CHECKED"]
    return dict(profile=REMOTE_PROFILE["id"] if scalar else GROUP_PROFILE["id"],
                status="HOST_CHECKED" if hosted else "MISSING", members=result, host_entries=hosted,
                environment_gaps=gaps)


def lifecycle_conditions(group, source_evidence=None):
    """Separate proof obligations from host availability; no model-generated permission flags.

    The scalar rule or independently built frozen-source evidence discharges
    these conditions. Candidate/group permission flags are never consulted.
    """
    scalar = group["grouping"] == "CHECKED_STATELESS_ENTRIES"
    obligations = dict(
        request_isolation="请求临时状态不串扰；原 owner/会话亲和保留；没有未核对的进程全局或外部共享写入",
        repeatable_initialization="冻结参数可在独立进程重复执行原初始化；不重复不可幂等的外部注册/写入",
        owner_end="原任务/会话/owner 已结束，不以单个 Unit 返回代替；没有后台续接、回调或待兑现义务",
        cleanup="原清理/外部工作已结束；进程退出及后端删除可确认资源归还，不以失联代替释放")
    checked = {name: dict(status="SUPPORTED" if scalar else "UNKNOWN", requirement=requirement,
        basis="scalar-independent-entry/1：原导入闭包及逐调用标量状态已由有限规则核对" if scalar else
              "native group：当前没有覆盖该项的通用源码规则，宿主可调用不能替代生命周期依据")
        for name, requirement in obligations.items()}
    if not scalar and source_evidence is not None:
        for name, item in checked.items():
            evidence = source_evidence["checks"][name]
            item.update(status=evidence["status"], basis=evidence["basis"],
                        evidence_digest=source_evidence["digest"], guards=evidence.get("guards", []))
    def permission(names):
        return dict(status="SUPPORTED" if all(checked[n]["status"] == "SUPPORTED" for n in names) else "UNKNOWN",
                    requires=list(names))
    return dict(schema="lifecycle-conditions/1", checks=checked, formal_proof=False,
        source_evidence=source_evidence,
        replicate=permission(("request_isolation", "repeatable_initialization")),
        reclaim=permission(("request_isolation", "repeatable_initialization", "owner_end", "cleanup")))


def group_contract(group, host, source_evidence=None):
    hosted = bool(host["host_entries"])
    lifecycle = lifecycle_conditions(group, source_evidence)
    replicable = lifecycle["replicate"]["status"] == "SUPPORTED"
    reclaimable = lifecycle["reclaim"]["status"] == "SUPPORTED"
    prepared = [identity for identity in host["host_entries"] if host["members"][identity]["prepare"]["status"] == "SUPPORTED"]
    def operation(allowed, basis, unknown=False):
        return dict(status="SUPPORTED" if allowed else "UNKNOWN" if unknown else "UNSUPPORTED", basis=basis)
    missing = "没有通过宿主核对的成员"
    replica_basis = ("各成员经冻结源码生命周期规则核对：请求状态隔离、初始化可独立重复；新副本是新 owner；回收仍等待任务 lease、根请求、许可及实际释放"
                     if replicable else "请求隔离、状态／外部效果与原初始化可独立重复尚未经规则核对")
    value = dict(schema=CONTRACT, group_id=group["id"], source_id=group["source_id"],
        source_version=group["source_version"], members=list(group["entries"]), root_entries=host["host_entries"],
        host_profile=host["profile"], codec=PORT_CODEC if host["profile"] == GROUP_PROFILE["id"] else "JSON_SCALARS",
        scope=dict(construction="UNCHANGED", request="UNCHANGED", owner="one original owner context per instance process",
                   session="platform session + instance; restart does not recover the original session",
                   internal_boundaries="W/H/R stay in the original context and hand over to the central permit"),
        ports={identity: host["members"][identity]["ports"] for identity in host["host_entries"]},
        prepare={identity: host["members"][identity]["prepare"] for identity in host["host_entries"]},
        lifecycle=lifecycle,
        operations=dict(
            start=operation(hosted, "新实例按原生命周期初始化；不恢复旧活跃任务" if hosted else missing),
            prewarm=operation(bool(prepared), "只准备原生命周期允许提前的构造/导入：" + ", ".join(prepared) if prepared
                              else "没有允许提前准备的成员" if hosted else missing),
            pause_intake=operation(hosted, "平台与实例停止接受新根请求；已开始的 owner 调用与内部许可继续收尾" if hosted else missing),
            drain_stop=operation(hosted, "等待根请求、组内许可与会话收尾后结束实例进程；原关闭流程未识别（UNKNOWN），不调用" if hosted else missing),
            cold_restart=operation(hosted, "停止并释放实例后创建新实例，计入重载成本；不保留现场" if hosted else missing),
            add_replica=operation(hosted and replicable, replica_basis if hosted else missing, unknown=hosted),
            remove_replica=operation(hosted and reclaimable, replica_basis if hosted else missing, unknown=hosted),
            live_restore=operation(False, "不实现保留 GPU 内存、调用栈和会话的休眠恢复")),
        replicas=dict(default=1, replicable=hosted and replicable))
    value["digest"] = digest(json.dumps(value, sort_keys=True, ensure_ascii=False).encode())
    return value


def runtime_groups(inspector, candidate, rows):
    boundaries = {item["id"]: item for item in candidate.get("boundaries", [])}
    remote = {row["entry_id"]: check_remote(inspector, row["entry"], row["check"], boundaries) for row in rows}
    unsafe = {identity for identity, result in remote.items() if result["status"] != "REMOTE_CHECKED"}
    proposed = candidate.get("runtime_groups") or []
    gaps, partitions = [], []
    if proposed:
        try:
            seen = set()
            for group in proposed:
                members = group["entries"]
                if not isinstance(members, list) or not members or any(not isinstance(item, str) for item in members) or seen.intersection(members) or len(set(members)) != len(members):
                    raise RemoteGap("运行组成员须唯一且完整")
                seen.update(members)
                partitions.append(members)
            if seen != set(remote):
                raise RemoteGap("运行组须覆盖本次所有入口，不能丢弃缺失入口")
            if sum(bool(unsafe.intersection(members)) for members in partitions) > 1:
                raise RemoteGap("未决对象/共享状态/控制依赖不能切到多个进程")
        except (RemoteGap, KeyError, TypeError) as error:
            gaps.append(str(error))
            partitions = []
    if not partitions:
        partitions = [[identity] for identity in remote if identity not in unsafe]
        if unsafe:
            partitions.append([row["entry_id"] for row in rows if row["entry_id"] in unsafe])
    groups = []
    for members in partitions:
        can_remote = all(remote[identity]["status"] == "REMOTE_CHECKED" for identity in members)
        key = digest(json.dumps([inspector.request["source_id"], inspector.request["source_version"], sorted(members)]).encode())[:24]
        groups.append(dict(id="rg-" + key, source_id=inspector.request["source_id"], source_version=inspector.request["source_version"],
            entries=members, status="REMOTE_CHECKED" if can_remote else "LOCAL_ONLY",
            grouping="CHECKED_STATELESS_ENTRIES" if can_remote else "CONSERVATIVE_ORIGINAL_SOURCE_CONTEXT",
            same_process=True, object_coalescing=False, construction_lifetime="UNCHANGED",
            request_lifetime="UNCHANGED", replicas=1, session_recovery=False,
            gaps=[remote[identity]["reason"] for identity in members if remote[identity]["reason"]],
            remote_conditions={identity: remote[identity] for identity in members}))
        proposal = next((group for group in proposed if isinstance(group, dict)
            and isinstance(group.get("entries"), list) and all(isinstance(member, str) for member in group["entries"])
            and set(group["entries"]) == set(members)), None)
        if proposal:
            groups[-1]["api_proposal"] = {key: proposal.get(key) for key in ("id", "rationale")}
        host = check_group_host(rows, members, can_remote)
        evidence = None if can_remote else build_lifecycle_evidence(inspector, rows, members)
        groups[-1].update(host=host, host_status=host["status"], contract=group_contract(groups[-1], host, evidence))
    return dict(schema="runtime-group-report/1", profile=REMOTE_PROFILE, profiles=[REMOTE_PROFILE, GROUP_PROFILE],
                groups=groups, proposal_gaps=gaps, proposal_status="REPAIRED_CONSERVATIVELY" if gaps else "CHECKED")


class Deployments:
    """Per-instance registration, routing and the lifecycle operations a group contract allows."""

    def __init__(self, platform):
        self.platform = platform
        self.session = uuid.uuid4().hex
        self.lock = threading.RLock()
        self.instances, self.affinity, self.unit_state = {}, {}, {}
        self.run_locks, self.environment_changes = {}, {}
        self.event_log, self.controller_error, self.listeners = [], None, []
        self.folder = platform.home / "deployments"
        self.folder.mkdir(parents=True, exist_ok=True)
        self.events_path = self.folder / "instance-events.jsonl"
        self.closed = threading.Event()
        platform.catalog.remote_router = self
        for record in platform.store.list("runtime_instance"):
            if record["state"] in LIVE:
                record.update(state="UNKNOWN", accepting=False, stale=True, observed_at=now(),
                    reason="平台会话已重启：原实例会话不恢复；请停止并释放后启动新实例")
                platform.store.save("runtime_instance", record["id"], record)
            self.instances[record["id"]] = record
        self.thread = threading.Thread(target=self._controller, daemon=True, name="seampact-runtime-controller")
        self.thread.start()

    def close(self):
        self.closed.set()

    def controller_state(self):
        with self.lock:
            return dict(session=self.session, error=self.controller_error,
                        locks={group: owner["run_id"] for group, owner in self.run_locks.items()})

    # Configuration, contracts and visible state.

    def backend(self):
        return self.platform.store.get("deployment_backend", "current") or backends.validate_config({"kind": "none"})

    def configure_backend(self, body):
        value = backends.validate_config(body)
        value["updated_at"] = now()
        self.platform.store.save("deployment_backend", "current", value)
        return value

    def _current_groups(self):
        sources = {source["id"]: source for source in self.platform.store.list("harness")}
        for group in self.platform.store.list("runtime_group"):
            source = sources.get(group["source_id"])
            if source and source.get("status") != "DETACHED" and group.get("installation_id") == source.get("installation_id"):
                yield group

    def _group(self, identity):
        group = next((item for item in self._current_groups() if item["id"] == identity), None)
        if not group:
            raise ValueError("没有这个当前运行组")
        if not group.get("contract"):
            raise ValueError("运行组缺少宿主契约；请重新接入分析结果")
        return group

    def _binding(self, group):
        catalog = self.platform.catalog
        for unit_id in group["units"]:
            pair = (catalog.bindings.get(unit_id), catalog.entry_specs.get(unit_id))
            if pair[0] is None:
                pair = catalog.deferred.get(unit_id, (None, None))
            if pair[0] is not None:
                return pair[0], pair[1], self.platform.store.get("unit", unit_id)
        raise ValueError("运行组成员在本进程没有已核对的绑定")

    def _entry(self, unit_id):
        catalog = self.platform.catalog
        return catalog.entry_specs.get(unit_id) or (catalog.deferred.get(unit_id) or (None, {}))[1]

    def _instances(self, group_id, states=None):
        return [record for record in self.instances.values()
                if record["group_id"] == group_id and (states is None or record["state"] in states)]

    @staticmethod
    def _view(record):
        value = {key: copy.deepcopy(item) for key, item in record.items() if key != "events"}
        value["recent_events"] = copy.deepcopy(record.get("events", [])[-8:])
        return value

    def _clear_history_reason(self, record, snapshot):
        if record.get("history_hidden"):
            return "该实例记录已清理"
        if record["state"] not in {"STOPPED", "FAILED"} or record.get("released") is not True:
            return "仅可清理已停止或失败且已确认释放的实例"
        if record["group_id"] in self.run_locks:
            return "研究运行正在使用该运行组，结束后再清理"
        if record.get("in_flight") or record.get("pending_stop"):
            return "实例仍有在途工作或待完成的停止操作"
        if record["id"] in self.affinity.values():
            return "实例仍被任务绑定"
        if any(root.get("host") == record["id"] and root["state"] in {"ACTIVE", "UNKNOWN"}
               for root in snapshot["roots"].values()):
            return "实例仍有关联的活跃或未决根请求"
        if any((call.get("unit") or {}).get("instance_id") == record["id"]
               and call["physical"] in {"ENABLED", "RESERVED", "AUTHORIZED", "STARTED", "UNKNOWN"}
               for call in snapshot["calls"].values()):
            return "实例仍有关联的活跃或未决调用"
        return None

    def clear_history(self, identity, body):
        """Hide released terminal instances while retaining identity and research evidence."""
        def action():
            with self.lock:
                self._guard(identity, None)
                if not self.platform.store.get("runtime_group", identity):
                    raise ValueError("没有这个运行组")
                instance_id = body.get("instance_id")
                records = self._instances(identity)
                if instance_id is not None:
                    records = [item for item in records if item["id"] == instance_id]
                    if not records:
                        raise ValueError("该运行组没有这个实例")
                snapshot = self.platform.runtime.snapshot()
                hidden, skipped = [], []
                for record in records:
                    if record.get("history_hidden"):
                        continue
                    reason = self._clear_history_reason(record, snapshot)
                    if reason:
                        if instance_id is not None:
                            raise ValueError(reason)
                        skipped.append(dict(instance_id=record["id"], reason=reason))
                        continue
                    record.update(history_hidden=True, history_hidden_at=now())
                    self.platform.store.save("runtime_instance", record["id"], record)
                    hidden.append(record["id"])
                return dict(group_id=identity, hidden_instance_ids=hidden, count=len(hidden), skipped=skipped)
        return self._once(body, "clear_history:" + identity + ":" + str(body.get("instance_id")), action)

    def public(self):
        artifacts = {item["id"]: item for item in self.platform.store.list("deployment")}
        result = []
        with self.lock:
            snapshot = self.platform.runtime.snapshot()
            groups = {group["id"]: group for group in self._current_groups()}
            # Keep older backend objects visible for release after an
            # environment edit, source detachment or platform-code update.
            for group in self.platform.store.list("runtime_group"):
                if group["id"] not in groups and any(self._held(item) for item in self._instances(group["id"])):
                    group["environment_gap"] = "旧接入版本的实例尚待释放；该组只允许排空停止"
                    groups[group["id"]] = group
            for group in groups.values():
                records = sorted((item for item in self._instances(group["id"]) if not item.get("history_hidden")),
                                 key=lambda item: item["generation"], reverse=True)
                shown = [item for item in records if item["state"] in LIVE] + [item for item in records if item["state"] not in LIVE][:3]
                views = []
                for item in shown:
                    reason = self._clear_history_reason(item, snapshot)
                    views.append(dict(self._view(item), can_cleanup=reason is None, cleanup_reason=reason))
                ready = [item for item in shown if item["state"] == "READY" and item["accepting"]]
                lock = self.run_locks.get(group["id"])
                clearable = sum(self._clear_history_reason(item, snapshot) is None for item in records)
                group.update(execution="REMOTE" if ready else "LOCAL", instance=self._view(ready[0]) if ready else None,
                    instances=views, artifact=artifacts.get(group["id"]),
                    artifact_gap=self._artifact_gap(group, artifacts.get(group["id"])) if group.get("contract") else "运行组缺少宿主契约",
                    history_cleanup=dict(count=clearable, enabled=bool(clearable),
                                         reason="研究运行正在使用该运行组，结束后再清理" if lock else
                                         None if clearable else "没有可清理的已释放实例记录"),
                    locked_by_run=lock["run_id"] if lock else None,
                    actions=self._actions(group, artifacts.get(group["id"])) if group.get("contract") else {})
                if group.get("environment_gap"):
                    for name, action in group["actions"].items():
                        if name != "drain_stop":
                            action.update(enabled=False, reason=group["environment_gap"])
                result.append(group)
        return result

    def _artifact_gap(self, group, artifact):
        if not artifact:
            return "先生成构建工件并构建镜像"
        if (artifact.get("contract_digest") != group["contract"]["digest"]
                or artifact.get("installation_id") != group.get("installation_id")):
            return "构建工件与当前运行组契约/接入版本不一致，请重新生成"
        if artifact.get("platform_core") != core_identity("worker"):
            return "构建工件与当前平台核心代码不同，请重新生成并构建 worker"
        return None

    def _start_gap(self, group, artifact):
        artifact_gap = self._artifact_gap(group, artifact)
        if artifact_gap:
            return artifact_gap
        if any(artifact.get(field, {}).get("status") == "RUNNING" for field in ("build", "publish")):
            return "该运行组正在构建或发布镜像"
        if (artifact.get("build") or {}).get("status") not in {"BUILT", "OPERATOR_REPORTED"}:
            return "镜像尚未构建成功"
        if (artifact.get("publish") or {}).get("status") not in {"PUBLISHED", "NOT_REQUIRED", "OPERATOR_REPORTED"}:
            return "镜像尚未对目标后端可达"
        if self.backend()["kind"] == "none":
            return "未配置部署后端"
        return None

    def _actions(self, group, artifact):
        contract = group["contract"]
        lock = self.run_locks.get(group["id"])
        live = [item for item in self._instances(group["id"]) if item["state"] in LIVE]
        held = [item for item in self._instances(group["id"]) if self._held(item)]
        ready = [item for item in live if item["state"] == "READY"]
        def gate(name, reason=None):
            operation = contract["operations"][name]
            if operation["status"] != "SUPPORTED":
                return dict(enabled=False, reason=operation["status"] + "：" + operation["basis"])
            if lock:
                return dict(enabled=False, reason="运行 " + lock["run_id"] + " 正在使用该组；手动启停会改变冻结执行条件")
            return dict(enabled=reason is None, reason=reason)
        built = (artifact or {}).get("build", {}).get("status") in {"BUILT", "OPERATOR_REPORTED"}
        artifact_gap = self._artifact_gap(group, artifact)
        gap = self._start_gap(group, artifact)
        single = None if not held else "已有实例或待释放对象；该组只允许一个实例，先排空停止" if not contract["replicas"]["replicable"] else "已有实例或待释放对象；请先核对，再用增加副本"
        editing = ("运行 " + lock["run_id"] + " 正在使用该组" if lock else
                   "已有实例或待释放对象，请先排空停止" if held else
                   "该运行组正在构建或发布镜像" if any((artifact or {}).get(field, {}).get("status") == "RUNNING"
                                                           for field in ("build", "publish")) else None)
        return dict(
            export=dict(enabled=not editing and group.get("host_status") == "HOST_CHECKED",
                        reason=editing or (None if group.get("host_status") == "HOST_CHECKED" else "没有通过宿主核对的成员")),
            build=dict(enabled=not editing and artifact_gap is None, reason=editing or artifact_gap),
            publish=dict(enabled=not editing and artifact_gap is None and built,
                         reason=editing or artifact_gap or (None if built else "镜像尚未构建成功")),
            start=gate("start", gap or single), prewarm=gate("prewarm", None if ready else "没有就绪实例"),
            pause_intake=gate("pause_intake", None if any(item["accepting"] for item in ready) else "没有正在接单的实例"),
            drain_stop=dict(enabled=bool(held) and not lock, reason=None if held and not lock else
                            "没有需释放的实例" if not held else "运行 " + lock["run_id"] + " 正在使用该组"),
            add_replica=gate("add_replica", gap or (None if live else "先启动第一个实例")),
            remove_replica=gate("remove_replica", None if len(ready) > 1 else "只有一个就绪实例"))

    def dispatch(self, unit, deferred):
        """Immediate dispatchability; semantic legality is decided by the scene and rules."""
        group_id = unit.get("runtime_group")
        with self.lock:
            ready = [item for item in self._instances(group_id, {"READY"})
                     if item["accepting"] and unit["id"] in item.get("units", [])] if group_id else []
        if ready:
            return dict(status="READY_INSTANCE", group_id=group_id, instance_id=ready[0]["id"])
        if unit.get("status") == "READY" and not deferred:
            return dict(status="READY_LOCAL")
        host = (unit.get("operations") or {}).get("group_host") or {}
        if not group_id or host.get("status") != "HOST_CHECKED":
            return dict(status="UNAVAILABLE", reason=host.get("reason") or unit.get("reason"))
        group = self.platform.store.get("runtime_group", group_id)
        gap = self._start_gap(group, self.platform.store.get("deployment", group_id)) if group and group.get("contract") else "运行组没有宿主契约"
        return dict(status="COLD_STARTABLE", group_id=group_id) if gap is None else dict(status="UNAVAILABLE", group_id=group_id, reason=gap)

    def queue(self, group_id):
        return sum(1 for call in self.platform.runtime.snapshot()["calls"].values()
                   if (call.get("unit") or {}).get("runtime_group") == group_id and call["physical"] in {"ENABLED", "RESERVED"})

    def start_gap(self, group_id, expected_image=None):
        group = self.platform.store.get("runtime_group", group_id)
        if not group or not group.get("contract"):
            return "运行组不存在或缺少宿主契约"
        artifact = self.platform.store.get("deployment", group_id)
        gap = self._start_gap(group, artifact)
        reference = ((artifact or {}).get("publish") or {}).get("reference") or (artifact or {}).get("image")
        if gap is None and expected_image and reference != expected_image:
            gap = "当前镜像与本次运行冻结的镜像不一致"
        return gap

    def runtime_fact(self, group_id, unit_id, expected_image=None):
        """State visible to a scheduling event; unknown parts stay None."""
        task_id = _task_context.get()
        retiring = lambda item: (item.get("pending_stop") or {}).get("reason") == "idle_retire/1"
        with self.lock:
            bound = self.instances.get(self.affinity.get((task_id, group_id))) if task_id else None
            if (bound and (retiring(bound) or bound["state"] == "STOPPED")
                    and self._idle_rebuild_allowed(self._group(group_id))):
                bound = None
            ready = [item for item in self._instances(group_id, {"READY"}) if item["accepting"] and unit_id in item.get("units", [])]
            record = bound if bound and bound["state"] == "READY" and unit_id in bound.get("units", []) else min(
                ready, key=lambda item: (item["in_flight"], item["generation"])) if ready and not bound else None
            if record is None and bound:
                return dict(local=False, ready_instance=False, warm=False, cold_startable=False, accepting=False,
                            busy=None, queue=None, state_age_ms=None,
                            reason="本任务绑定的实例状态为 " + bound["state"] + "，或未登记该成员")
            starting = any(item["state"] in {"UNDEPLOYED", "STARTING"} or retiring(item)
                           for item in self._instances(group_id, LIVE))
            if record is not None:
                fact = dict(local=False, ready_instance=True, warm=record["initialization"].get(unit_id) == "BOUND",
                            cold_startable=False, accepting=record["accepting"], busy=record["in_flight"] > 0,
                            state_age_ms=(time.time() - record.get("observed_wall", time.time())) * 1000)
        if record is None:
            gap = self.start_gap(group_id, expected_image)
            fact = dict(local=False, ready_instance=False, warm=False, cold_startable=gap is None or starting,
                        accepting=False, busy=False, state_age_ms=0.0, reason=gap)
        fact["queue"] = self.queue(group_id)
        return fact

    # Runs freeze placement; manual operations cannot silently change it.

    def lock_groups(self, run_id, placements, control):
        with self.lock:
            for group_id in placements:
                if control.get("mode") == "idle_retire/1" and not self._idle_rebuild_allowed(self._group(group_id)):
                    raise ValueError("原生有状态运行组须使用 fixed_resident；尚未核对任务/owner 结束及可重建契约：" + group_id)
                owner = self.run_locks.get(group_id)
                if owner and owner["run_id"] != run_id:
                    raise ValueError("运行组正被运行 " + owner["run_id"] + " 使用")
            for group_id, placement in placements.items():
                self.run_locks[group_id] = dict(run_id=run_id, placement=copy.deepcopy(placement),
                                                control=copy.deepcopy(control), locked_wall=time.time())
            self.environment_changes.setdefault(run_id, [])

    def live_instances(self, group_id):
        with self.lock:
            return [self._view(item) for item in self._instances(group_id) if self._held(item)]

    def ensure_ready(self, placement, unit_id):
        return self._view(self._ensure(placement, unit_id))

    def release_run(self, group_id, run_id):
        with self.lock:
            owned = [item["id"] for item in self._instances(group_id) if self._held(item) and item.get("run_id") == run_id]
        if not owned:
            return dict(group_id=group_id, instances=[])
        results = [self.drain_stop(group_id, dict(instance_id=identity), run_id=run_id, reason="run_window_end")
                   for identity in owned]
        return dict(group_id=group_id, instances=[item for result in results for item in result["instances"]])

    def unlock(self, run_id):
        with self.lock:
            for group_id in [group for group, owner in self.run_locks.items() if owner["run_id"] == run_id]:
                del self.run_locks[group_id]

    # Joint windows: the window is the single controller of these groups' instances.

    def group_view(self, group_id, expected_image=None):
        """Contract facts and held instances a joint decision may read."""
        fields = ("id", "state", "accepting", "in_flight", "units", "initialization", "generation", "run_id",
                  "start_wall", "ready_wall", "last_activity_wall", "pending_stop", "released")
        with self.lock:
            group = self._group(group_id)
            contract = group["contract"]
            instances = [{key: copy.deepcopy(item.get(key)) for key in fields}
                         for item in self._instances(group_id) if self._held(item)]
            leases = {}
            for (task_id, identity), instance_id in self.affinity.items():
                if identity == group_id and task_id:
                    leases.setdefault(instance_id, []).append(task_id)
            return dict(group_id=group_id, units=list(group["units"]),
                        operations={name: item["status"] for name, item in contract["operations"].items()},
                        prepare={entry: rule["status"] for entry, rule in contract["prepare"].items()},
                        replicable=contract["replicas"]["replicable"] is True, reclaim=self._idle_rebuild_allowed(group),
                        start_gap=self.start_gap(group_id, expected_image), instances=instances, leases=leases)

    def bound(self, task_id, group_id):
        with self.lock:
            return self.affinity.get((task_id, group_id))

    def assign(self, task_id, group_id, instance_id):
        """Bind a task's owner lease to the instance its first root request was dispatched to."""
        with self.lock:
            current = self.affinity.get((task_id, group_id))
            record = self.instances.get(instance_id)
            if current not in (None, instance_id):
                raise ProtocolError("任务已绑定同组另一实例；原 owner 状态不迁移")
            if (not record or record["group_id"] != group_id or record["state"] != "READY" or not record["accepting"]
                    or record.get("pending_stop")):
                raise ProtocolError("派发目标实例已不可接单")
            self.affinity[(task_id, group_id)] = instance_id
            return current is None

    def unassign(self, task_id, group_id):
        with self.lock:
            self.affinity.pop((task_id, group_id), None)

    def release_task(self, task_id):
        """A finished task no longer holds an owner lease on any instance."""
        with self.lock:
            for key in [key for key in self.affinity if key[0] == task_id]:
                del self.affinity[key]

    def instance_fact(self, instance_id):
        """Current outcome facts of one instance, held or already released."""
        with self.lock:
            record = self.instances.get(instance_id)
            if record is None:
                return None
            return dict(id=record["id"], state=record["state"], held=self._held(record),
                        released=record.get("released"), start_wall=record.get("start_wall"),
                        pending_stop=copy.deepcopy(record.get("pending_stop")),
                        drain_blocked=record.get("drain_blocked"), reason=record.get("reason"))

    def _guard(self, group_id, run_id):
        owner = self.run_locks.get(group_id)
        if owner and owner["run_id"] != run_id:
            raise ValueError("运行 " + owner["run_id"] + " 正在使用该运行组；手动启停会改变冻结执行条件，结束后再操作")

    def _environment_change(self, record, kind):
        owner = self.run_locks.get(record["group_id"])
        if owner:
            self.environment_changes.setdefault(owner["run_id"], []).append(dict(
                at=now(), kind=kind, group_id=record["group_id"], instance_id=record["id"], reason=record.get("reason")))

    def _event(self, record, kind, **facts):
        event = dict(at=now(), wall=time.time(), kind=kind, group_id=record["group_id"], instance_id=record["id"],
                     generation=record["generation"], state=record["state"], **facts)
        record.setdefault("events", []).append(event)
        del record["events"][:-40]
        self.event_log.append(event)
        del self.event_log[:-100000]
        self.platform.store.save("runtime_instance", record["id"], record)
        with self.events_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n")
        for listener in list(self.listeners):
            listener(event)
        return event

    def _once(self, body, kind, action):
        """Repeated submission of one operation identity returns its first result."""
        operation = (body or {}).get("operation_id")
        if operation is not None:
            if not isinstance(operation, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", operation):
                raise ValueError("operation_id 格式不正确")
            previous = self.platform.store.get("runtime_operation", operation)
            if previous:
                if previous["kind"] != kind:
                    raise ValueError("该操作身份已用于另一操作")
                return dict(previous["result"], repeated_operation=True)
        result = action()
        if operation is not None:
            self.platform.store.save("runtime_operation", operation, dict(kind=kind, at=now(), result=result))
        return result

    # Build artifacts, images and image reachability.

    def _environment(self, value, binding):
        environment = normalize_environment(value, binding.inspector)
        checked_python = (binding.inspector.request.get("runtime_environment") or {}).get("python", "3.11")
        if environment["python"] != checked_python:
            raise ValueError("所选 Python 与源码核对解释器不同；请先在来源配置准备该解释器并零模型重新核对候选")
        return environment

    def _artifact_guard(self, identity):
        self._guard(identity, None)
        if any(self._held(item) for item in self._instances(identity)):
            raise ValueError("请先排空并停止该运行组实例，再修改或重建镜像")
        artifact = self.platform.store.get("deployment", identity) or {}
        if any(artifact.get(field, {}).get("status") == "RUNNING" for field in ("build", "publish")):
            raise ValueError("该运行组正在构建或发布镜像，请结束后再操作")

    def export(self, identity, body):
        with self.platform.source_lock, self.lock:
            return self._export(identity, body)

    def _export(self, identity, body):
        self._artifact_guard(identity)
        group = self._group(identity)
        if group["host"]["status"] != "HOST_CHECKED":
            raise ValueError("运行组没有通过宿主核对的成员，继续本地执行；请查看缺口")
        binding, _, unit = self._binding(group)
        environment = self._environment(body.get("environment"), binding)
        group["runtime_environment"] = environment_selection(environment)
        destination = self.folder / (identity + "-" + uuid.uuid4().hex[:8])
        destination.mkdir(parents=True)
        original, snapshots = destination / "source", destination / "snapshots"
        original.mkdir()
        snapshots.mkdir()
        for record in binding.inspector.files.values():
            data = (binding.inspector.snapshots / record["sha256"]).read_bytes()
            if digest(data) != record["sha256"]:
                raise ValueError("冻结源码内容已改变")
            target = safe_path(original, record["path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            (snapshots / record["sha256"]).write_bytes(data)
        spec = worker_spec(binding, group, unit)
        copy_core(destination / "core" / "seampact", spec["platform_core"])
        fingerprint = binding_fingerprint(spec)
        image = "seampact-worker-" + identity[3:15] + ":" + digest(
            json.dumps([fingerprint, environment], sort_keys=True).encode())[:16]
        (destination / "worker.json").write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
        (destination / "environment.json").write_text(json.dumps(environment, ensure_ascii=False, indent=2), encoding="utf-8")
        check_python = ("import sys; from seampact.environments import check_python_version; "
            "assert '.'.join(map(str,sys.version_info[:2])) == " + repr(environment["python"]) + "; "
            "r=check_python_version('.'.join(map(str,sys.version_info[:3])), " + repr(environment["python_requirements"]) +
            "); assert r['status'] == 'SUPPORTED', r['missing']")
        reuse = worker_dockerfile(destination, binding.inspector, environment, check_python)
        archive = shutil.make_archive(str(destination), "zip", root_dir=destination)
        record = dict(id=identity, status="ARTIFACT_READY", folder=str(destination), archive=archive, image=image,
            profile=spec["profile"], contract_digest=group["contract"]["digest"], binding_fingerprint=fingerprint,
            installation_id=group["installation_id"], analysis_id=group.get("analysis_id"), environment=environment,
            source_runtime_environment=environment_selection(binding.inspector.request.get("runtime_environment") or {}),
            platform_core=spec["platform_core"], build_reuse=reuse,
            created_at=now(), source_version=group["source_version"], build=dict(status="NOT_RUN"),
            publish=dict(status="NOT_RUN"), deployment="NOT_APPLIED", copies_original_code=True, resource_downloads=0)
        self.platform.store.save("runtime_group", identity, group)
        self.platform.store.save("deployment", identity, record)
        return record

    def _artifact(self, identity):
        artifact = self.platform.store.get("deployment", identity)
        if not artifact or artifact.get("status") != "ARTIFACT_READY":
            raise ValueError("先生成该运行组的构建工件")
        gap = self._artifact_gap(self._group(identity), artifact)
        if gap:
            raise ValueError(gap)
        return artifact

    def _background(self, identity, field, work):
        artifact = self._artifact(identity)
        if (artifact.get(field) or {}).get("status") == "RUNNING":
            raise ValueError("该操作仍在进行")
        artifact[field] = dict(status="RUNNING", started_at=now())
        self.platform.store.save("deployment", identity, artifact)
        def run():
            try:
                result = work(artifact)
            except Exception as error:
                result = dict(status="FAILED", reason=str(error))
            current = self.platform.store.get("deployment", identity)
            current[field] = dict(result, finished_at=now())
            if field == "build" and result.get("status") == "BUILT":
                current["publish"] = dict(status="NOT_RUN")
            self.platform.store.save("deployment", identity, current)
            print("[SeamPact] image_" + field + " " + identity + " " + result["status"], flush=True)
        threading.Thread(target=run, daemon=True).start()
        return artifact

    def build(self, identity, body):
        with self.platform.source_lock, self.lock:
            return self._build(identity, body)

    def _build(self, identity, body):
        def action():
            self._artifact_guard(identity)
            image = body.get("operator_reported_image")
            if image is not None:
                if not isinstance(image, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/:@-]*", image):
                    raise ValueError("镜像引用格式不正确")
                artifact = self._artifact(identity)
                artifact.update(image=image, build=dict(status="OPERATOR_REPORTED", image=image, image_id=None, at=now(),
                    reason="镜像由操作者在平台外构建；平台未执行或核对构建"), publish=dict(status="NOT_RUN"))
                self.platform.store.save("deployment", identity, artifact)
                return artifact
            config = self.backend()
            return self._background(identity, "build", lambda artifact: backends.build(
                artifact["folder"], artifact["image"], artifact["environment"], config,
                dependency_image=artifact["build_reuse"]["dependency_image"]))
        return self._once(body, "build:" + identity, action)

    def publish(self, identity, body):
        with self.platform.source_lock, self.lock:
            return self._publish(identity, body)

    def _publish(self, identity, body):
        def action():
            self._artifact_guard(identity)
            artifact = self._artifact(identity)
            if (artifact.get("build") or {}).get("status") not in {"BUILT", "OPERATOR_REPORTED"}:
                raise ValueError("镜像尚未构建成功")
            config = self.backend()
            if config["kind"] == "none":
                raise ValueError("先配置部署后端")
            return self._background(identity, "publish", lambda item: backends.publish(
                item["image"], config, image_id=item.get("build", {}).get("image_id")))
        return self._once(body, "publish:" + identity, action)

    # Instances: start, registration, intake, drain and stop.

    def _next_generation(self, group_id):
        state = self.platform.store.get("runtime_group_state", group_id) or dict(generation=0)
        state["generation"] += 1
        self.platform.store.save("runtime_group_state", group_id, state)
        return state["generation"]

    def start(self, identity, body, run_id=None, replica=False):
        return self._once(body, ("add_replica:" if replica else "start:") + identity,
                          lambda: self._start(identity, run_id, replica, (body or {}).get("expected_image")))

    def _start(self, identity, run_id, replica, expected_image=None):
        with self.platform.source_lock, self.lock:
            self._guard(identity, run_id)
            group = self._group(identity)
            contract = group["contract"]
            operation = contract["operations"]["add_replica" if replica else "start"]
            if operation["status"] != "SUPPORTED":
                raise ValueError(operation["status"] + "：" + operation["basis"])
            artifact = self.platform.store.get("deployment", identity)
            gap = self._start_gap(group, artifact)
            if gap:
                raise ValueError(gap)
            reference = (artifact.get("publish") or {}).get("reference") or artifact["image"]
            if expected_image is not None and expected_image != reference:
                raise ValueError("运行组镜像已变化；冻结运行不能改用新镜像")
            live = [item for item in self._instances(identity) if self._held(item)]
            if replica and not live:
                raise ValueError("先启动第一个实例")
            if not replica and live:
                raise ValueError("已有实例；该组只允许一个实例，先排空停止" if not contract["replicas"]["replicable"]
                                 else "已有实例；请用增加副本")
            config = self.backend()
            generation = self._next_generation(identity)
            record = dict(id=uuid.uuid4().hex, group_id=identity, generation=generation, name=identity + "-g" + str(generation),
                session=self.session, profile=contract["host_profile"], contract_digest=contract["digest"],
                binding_fingerprint=artifact["binding_fingerprint"], image=reference,
                image_id=(artifact.get("build") or {}).get("image_id"), backend=config["kind"], origin="PLATFORM_STARTED",
                run_id=run_id, state="UNDEPLOYED", desired="RUNNING", accepting=False, in_flight=0, units=[],
                initialization={}, created_at=now(), start_wall=time.time(), events=[])
            self.instances[record["id"]] = record
            self._event(record, "start_requested", replica=replica)
        result = backends.start(record, reference, artifact["folder"], config)
        with self.lock:
            record["apply"] = {key: item for key, item in result.items() if key != "handle"}
            # A manual apply can create the same object later. Retain its identity
            # before registration; shutting down its worker cannot release it.
            if result.get("handle"):
                record["handle"] = result["handle"]
            if result["status"] == "APPLIED":
                if record["state"] == "UNDEPLOYED":
                    record["state"] = "STARTING"
            elif result["status"] == "BACKEND_UNAVAILABLE":
                record["reason"] = result["reason"] + "；可按 manual_commands 手动执行，进程登记后绑定此实例"
            else:
                record.update(state="FAILED", desired="STOPPED", accepting=False, reason=result.get("reason"), stopped_wall=time.time())
            self._event(record, "apply", status=result["status"],
                        duration_ms=sum(step["duration_ms"] for step in result.get("steps", [])))
            return self._view(record)

    def attach(self, endpoint):
        endpoint = endpoint.rstrip("/")
        if urlsplit(endpoint).scheme not in {"http", "https"} or not urlsplit(endpoint).netloc:
            raise ValueError("运行进程地址须为HTTP地址")
        health = json_request(endpoint + "/health", timeout=15)
        if (health.get("identity_protocol") != IDENTITY_PROTOCOL
                or not isinstance(health.get("process_nonce"), str) or not health["process_nonce"]
                or type(health.get("generation")) is not int or health["generation"] < 0):
            raise ValueError("运行进程缺少同版身份协议、有效代次或进程身份；请重建同版 worker")
        group = self._group(health.get("group_id"))
        contract = group["contract"]
        binding, _, unit = self._binding(group)
        expected = worker_spec(binding, self.platform.store.get("runtime_group", group["id"]), unit)
        if (health.get("session") != self.session or health.get("profile") != contract["host_profile"]
                or health.get("source_version") != group["source_version"]
                or health.get("installation_id") != group["installation_id"]
                or health.get("binding_fingerprint") != binding_fingerprint(expected)
                or (contract["host_profile"] == GROUP_PROFILE["id"] and health.get("contract_digest") != contract["digest"])):
            raise ValueError("运行进程与当前源码/接入版本/宿主/契约/平台会话不一致")
        units = list(health.get("units", []))
        if not units or set(units) - set(group["units"]):
            raise ValueError("实例登记的可执行成员不属于该运行组")
        unknown = False
        with self.lock:
            record = self.instances.get(health.get("instance_id"))
            if record:
                if record["group_id"] != group["id"] or record.get("stale") or record["state"] in {"STOPPED", "FAILED"}:
                    raise ValueError("该实例已结束或已过期；重建不恢复原会话，请启动新实例")
                if record.get("process_nonce") and record["process_nonce"] != health.get("process_nonce"):
                    record.update(state="FAILED", desired="STOPPED", accepting=False,
                                  reason="同一实例身份的进程已被替换（容器/Pod 重建）；不恢复原会话")
                    self._event(record, "process_replaced")
                    self._environment_change(record, "process_replaced")
                    unknown = True
                elif health["generation"] != record["generation"]:
                    raise ValueError("实例代次不一致")
            else:
                self._guard(group["id"], None)
                live = [item for item in self._instances(group["id"]) if item["state"] in LIVE]
                if live and not contract["replicas"]["replicable"]:
                    raise ValueError("该运行组只允许一个实例；先排空停止现有实例")
                # An operator-started worker must know its own generation. Do
                # not fabricate a different one only on the coordinator.
                generation = health["generation"]
                record = dict(id=health["instance_id"], group_id=group["id"], generation=generation,
                    name=group["id"] + "-g" + str(generation), session=self.session, profile=contract["host_profile"],
                    contract_digest=contract["digest"], binding_fingerprint=health["binding_fingerprint"],
                    image="OPERATOR_STARTED", image_id=None, backend="operator", origin="OPERATOR_ATTACHED", run_id=None,
                    state="STARTING", desired="RUNNING", accepting=False, in_flight=0, units=[], initialization={},
                    generation_origin="WORKER_REPORTED", created_at=now(), start_wall=None, events=[])
                self.instances[record["id"]] = record
            if not unknown:
                record.update(endpoint=endpoint, process_nonce=health.get("process_nonce"), state="READY", accepting=True,
                    units=units, missing=health.get("missing", {}), initialization=dict(health.get("initialization", {})),
                    environment=health.get("environment"), resources=health.get("resources"), ready_wall=time.time(),
                    observed_wall=time.time(), last_activity_wall=time.time(), registered_at=now(), health_failures=0, reason=None)
                self._event(record, "registered", start_to_ready_ms=(record["ready_wall"] - record["start_wall"]) * 1000
                            if record.get("start_wall") else None)
        if unknown:
            self.platform.runtime.call("remote_unknown", record["group_id"], record["id"])
            raise ValueError(record["reason"])
        print("[SeamPact] worker_attached " + group["id"] + " " + record["id"], flush=True)
        return self._view(record)

    @staticmethod
    def _identity(record):
        return dict(session=record["session"], instance_id=record["id"], generation=record["generation"],
                    process_nonce=record.get("process_nonce"))

    def _check_identity(self, record, response):
        if not record.get("process_nonce") or any(response.get(key) != value for key, value in self._identity(record).items()):
            raise ProtocolError("运行进程身份不一致；不接收其他会话、代次或进程的结果，也不重放")

    def _notify(self, record, path):
        try:
            answer = json_request(record["endpoint"] + path, self._identity(record), timeout=15)
            self._check_identity(record, answer)
            return answer
        except Exception as error:
            return dict(error=str(error))

    def pause(self, identity, body, run_id=None):
        def action():
            with self.lock:
                self._guard(identity, run_id)
                operation = self._group(identity)["contract"]["operations"]["pause_intake"]
                if operation["status"] != "SUPPORTED":
                    raise ValueError(operation["status"] + "：" + operation["basis"])
                targets = [item for item in self._instances(identity, {"READY"}) if body.get("instance_id") in (None, item["id"])]
                if not targets:
                    raise ValueError("没有可停止接单的就绪实例")
                for item in targets:
                    item.update(accepting=False, state="DRAINING")
                    self._event(item, "intake_paused")
            for item in targets:
                answer = self._notify(item, "/drain")
                if "error" in answer:
                    item["drain_notice"] = "实例未确认停止接单（平台已停止派发）：" + answer["error"]
            return dict(group_id=identity, instances=[self._view(item) for item in targets])
        return self._once(body, "pause:" + identity, action)

    def local(self, identity):
        result = self.pause(identity, {})
        return dict(id=identity, execution="LOCAL", paused=result["instances"])

    def drain_stop(self, identity, body, run_id=None, reason="operator"):
        def action():
            with self.lock:
                self._guard(identity, run_id)
                group = self.platform.store.get("runtime_group", identity)
                if not group or not group.get("contract"):
                    raise ValueError("没有可释放的运行组契约")
                targets = [item for item in self._instances(identity) if self._held(item)
                           and body.get("instance_id") in (None, item["id"])]
                if not targets:
                    raise ValueError("没有可停止的实例")
                for item in targets:
                    if item["state"] == "UNKNOWN" and not body.get("release_unknown"):
                        raise ValueError("实例存在未决（UNKNOWN）结果，不视为可安全结束；确认后以 release_unknown 停止并释放，未决调用保持 UNKNOWN")
                if group["contract"]["operations"]["drain_stop"]["status"] != "SUPPORTED" and any(
                        item["state"] in {"READY", "DRAINING"} for item in targets):
                    raise ValueError(group["contract"]["operations"]["drain_stop"]["basis"])
                for item in targets:
                    item.update(accepting=False, desired="STOPPED", released=False, pending_stop=dict(reason=reason, requested_at=now(),
                                                                   release_unknown=bool(body.get("release_unknown"))))
                    if item["state"] == "READY":
                        item["state"] = "DRAINING"
                    self._event(item, "drain_requested", reason=reason)
            for item in targets:
                if item.get("endpoint") and item["state"] == "DRAINING":
                    self._notify(item, "/drain")
            self._advance_stops()
            return dict(group_id=identity, instances=[self._view(item) for item in targets])
        return self._once(body, "drain_stop:" + identity + ":" + str(body.get("instance_id")), action)

    def add_replica(self, identity, body):
        return self.start(identity, body, replica=True)

    def remove_replica(self, identity, body):
        with self.lock:
            self._guard(identity, None)
            operation = self._group(identity)["contract"]["operations"]["remove_replica"]
            if operation["status"] != "SUPPORTED":
                raise ValueError(operation["status"] + "：" + operation["basis"])
            ready = [item for item in self._instances(identity, {"READY"}) if item["accepting"]]
            if len(ready) < 2:
                raise ValueError("只有一个就绪实例；停止请用排空后停止")
            chosen = next((item for item in ready if item["id"] == body.get("instance_id")), None) or min(
                ready, key=lambda item: (item["in_flight"], -item["generation"]))
        return self.drain_stop(identity, dict(body, instance_id=chosen["id"]), reason="remove_replica")

    def prewarm(self, identity, body, run_id=None):
        def action():
            with self.lock:
                self._guard(identity, run_id)
                group = self._group(identity)
                operation = group["contract"]["operations"]["prewarm"]
                if operation["status"] != "SUPPORTED":
                    raise ValueError(operation["status"] + "：" + operation["basis"])
                allowed = {entry for entry, rule in group["contract"]["prepare"].items() if rule["status"] == "SUPPORTED"}
                units = [unit_id for unit_id in group["units"]
                         if (self.platform.store.get("unit", unit_id) or {}).get("entry_id") in allowed]
                requested = body.get("units", units)
                if not isinstance(requested, list) or not requested or set(requested) - set(units):
                    raise ValueError("预热成员须为契约允许提前准备的非空成员列表")
                units = list(dict.fromkeys(requested))
                targets = [item for item in self._instances(identity, {"READY"}) if body.get("instance_id") in (None, item["id"])]
                if not targets:
                    raise ValueError("没有就绪实例")
            results = []
            for item in targets:
                started = time.perf_counter()
                with self.lock:
                    if item["state"] != "READY" or not item["accepting"]:
                        raise ProtocolError("实例已停止接单；不能开始预热")
                    item["in_flight"] += 1
                    item["last_activity_wall"] = time.time()
                try:
                    answer = json_request(item["endpoint"] + "/prewarm", dict(self._identity(item), units=units))
                    self._check_identity(item, answer)
                except Exception as error:
                    # A refusal carrying this process's identity is an answer; a lost or foreign response is not.
                    reply = getattr(error, "answer", None)
                    answered = isinstance(reply, dict) and bool(item.get("process_nonce")) and all(
                        reply.get(key) == value for key, value in self._identity(item).items())
                    answer = dict(status="FAILED", initialization_ready=False, error=str(error), answered=answered)
                finally:
                    with self.lock:
                        item["in_flight"] -= 1
                        item["last_activity_wall"] = time.time()
                with self.lock:
                    for unit_id, row in (answer.get("units") or {}).items():
                        if row.get("initialization_after"):
                            item["initialization"][unit_id] = row["initialization_after"]
                    item["observed_wall"] = time.time()
                    self._event(item, "prewarm", duration_ms=(time.perf_counter() - started) * 1000,
                                units=answer.get("units"), error=answer.get("error"))
                missing = [uid for uid in units if (answer.get("units", {}).get(uid) or {}).get("status") != "PREPARED"
                           or (answer.get("units", {}).get(uid) or {}).get("initialization_after") != "BOUND"]
                results.append(dict(answer, instance_id=item["id"], required_units=units, unprepared_units=missing))
            ready = all(not result["unprepared_units"] and result.get("initialization_ready") is True for result in results)
            return dict(group_id=identity, status="PREPARED" if ready else "FAILED", initialization_ready=ready, results=results)
        return self._once(body, "prewarm:" + identity, action)

    def status(self, identity):
        config = self.backend()
        with self.lock:
            targets = [item for item in self._instances(identity) if item["state"] in LIVE]
        for item in targets:
            observed = backends.observe(item, config) if config["kind"] != "none" or item.get("handle") else dict(
                status="UNKNOWN", reason="未配置部署后端")
            with self.lock:
                item.update(observed=observed, observed_at=now())
                self.platform.store.save("runtime_instance", item["id"], item)
            if item.get("endpoint") and item["state"] in {"READY", "DRAINING"}:
                self._poll(item)
        return dict(group_id=identity, desired_vs_observed="desired is the platform request; observed is the backend/worker report",
                    instances=[self._view(item) for item in targets])

    def mark_source_detached(self, source_id):
        with self.lock:
            for item in self.instances.values():
                group = self.platform.store.get("runtime_group", item["group_id"]) or {}
                if group.get("source_id") == source_id and item["state"] in LIVE:
                    # Started receipts finish; catalog/source masks stop new calls.
                    item.update(accepting=False, source_detached=True)
                    self.platform.store.save("runtime_instance", item["id"], item)

    @staticmethod
    def _held(record):
        """Live, or failed while its backend object has not been released."""
        return record["state"] in LIVE or (record["state"] == "FAILED" and bool(record.get("handle") or record.get("endpoint"))
                                           and not record.get("released"))

    def _drained(self, record):
        if record["in_flight"]:
            return False
        if any(task_id and instance_id == record["id"] for (task_id, _), instance_id in self.affinity.items()):
            record["drain_blocked"] = "原任务/owner lease 尚未结束；Unit 返回不能代替 owner 结束"
            return False
        snapshot = self.platform.runtime.snapshot()
        if any(root["host"] == record["id"] and root["state"] in {"ACTIVE", "UNKNOWN"} for root in snapshot["roots"].values()):
            return False
        calls = [call for call in snapshot["calls"].values()
                 if (call.get("unit") or {}).get("instance_id") == record["id"]]
        if any(call["physical"] == "UNKNOWN" for call in calls) and not record["pending_stop"].get("release_unknown"):
            record["drain_blocked"] = "存在 UNKNOWN 调用；不视为可安全结束"
            return False
        if any(call["physical"] in {"ENABLED", "RESERVED", "AUTHORIZED", "STARTED"} for call in calls):
            return False
        if record.get("endpoint") and record["state"] == "DRAINING":
            try:
                health = json_request(record["endpoint"] + "/health", timeout=10)
                self._check_identity(record, health)
            except Exception:
                return bool(record["pending_stop"].get("release_unknown"))
            if health.get("in_flight"):
                return False
        return True

    def _advance_stops(self):
        with self.lock:
            pending = [item for item in self.instances.values() if item.get("pending_stop") and self._held(item)
                       and not item["pending_stop"].get("blocked")]
        for item in pending:
            if item["state"] in {"READY", "DRAINING"} and not self._drained(item):
                continue
            started = time.perf_counter()
            if item.get("handle"):
                result = backends.stop(item, self.backend())
            elif item.get("backend") in {"docker", "kubernetes"}:
                result = dict(status="STOP_PENDING", reason="缺少可确认的后端对象身份；须人工核对并释放，worker 退出不等于后端对象删除")
            elif item.get("endpoint"):
                answer = self._notify(item, "/shutdown") if item["state"] in {"READY", "DRAINING"} else dict(
                    error="无法确认原 worker 已退出")
                result = dict(status="STOP_PENDING", process_shutdown_requested="error" not in answer,
                    reason="平台没有后端对象句柄；仍须人工核对资源释放，worker 退出或失联不能确认 Deployment/容器已删除",
                    worker_error=answer.get("error"))
            else:
                result = dict(status="STOPPED", reason="没有已应用的后端对象或可达进程")
            with self.lock:
                if result["status"] == "STOPPED":
                    item.update(state="FAILED" if item["state"] == "FAILED" else "STOPPED", desired="STOPPED",
                                accepting=False, released=True, stopped_wall=item.get("stopped_wall") or time.time(),
                                stopped_at=now(), stop_result=result)
                    if self._idle_rebuild_allowed(self.platform.store.get("runtime_group", item["group_id"]) or {}):
                        for key in [key for key, value in self.affinity.items() if value == item["id"]]:
                            del self.affinity[key]
                    self._event(item, "stopped", reason=item["pending_stop"]["reason"],
                                duration_ms=(time.perf_counter() - started) * 1000)
                    item["pending_stop"] = None
                else:
                    item.update(desired="STOPPED", released=False)
                    item["pending_stop"]["blocked"] = result.get("reason") or result["status"]
                    item["stop_result"] = result
                    self._event(item, "stop_blocked", status=result["status"])

    def _poll(self, record):
        unknown = False
        try:
            health = json_request(record["endpoint"] + "/health", timeout=10)
        except Exception as error:
            with self.lock:
                record["health_failures"] = record.get("health_failures", 0) + 1
                if record["health_failures"] >= 3 and record["state"] in {"READY", "DRAINING"}:
                    record.update(state="UNKNOWN", accepting=False, reason="连续 3 次健康检查无响应：" + str(error))
                    self._event(record, "unknown", reason=record["reason"])
                    self._environment_change(record, "health_lost")
                    unknown = True
        else:
            with self.lock:
                if any(health.get(key) != value for key, value in self._identity(record).items()):
                    record.update(state="FAILED", desired="STOPPED", accepting=False,
                                  reason="同一地址的进程已被替换；不恢复原会话")
                    self._event(record, "process_replaced")
                    self._environment_change(record, "process_replaced")
                    unknown = True
                else:
                    record.update(health_failures=0, observed_wall=time.time(), worker_in_flight=health.get("in_flight"),
                                  initialization=dict(health.get("initialization", {})), resources=health.get("resources"))
                    self.platform.store.save("runtime_instance", record["id"], record)
        if unknown:
            self.platform.runtime.call("remote_unknown", record["group_id"], record["id"])

    def _observe_starting(self):
        config = self.backend()
        with self.lock:
            for item in self.instances.values():
                if (item["state"] == "UNDEPLOYED" and item.get("session") == self.session and item.get("start_wall")
                        and time.time() - item["start_wall"] > config["start_timeout_s"]):
                    item.update(state="FAILED", desired="STOPPED", reason="启动未在时限内完成登记（含手动命令未执行）")
                    self._event(item, "start_failed")
            starting = [item for item in self.instances.values() if item["state"] == "STARTING" and item.get("handle")
                        and item.get("session") == self.session]
        for item in starting:
            observed = backends.observe(item, config)
            with self.lock:
                if item["state"] != "STARTING":
                    continue
                item["observed"] = observed
                if observed["status"] in {"EXITED", "ABSENT", "FAILED"}:
                    item.update(state="FAILED", desired="STOPPED", reason="后端报告实例未运行：" + str(observed.get("reason") or observed["status"]))
                elif time.time() - item["start_wall"] > config["start_timeout_s"]:
                    item.update(state="FAILED", reason="启动超时，进程未登记；后端对象需排空停止释放")
                else:
                    continue
                self._event(item, "start_failed", observed=observed)

    def _retire_idle(self):
        due = []
        with self.lock:
            for group_id, owner in self.run_locks.items():
                control = owner["control"]
                if control.get("mode") != "idle_retire/1":
                    continue
                if not self._idle_rebuild_allowed(self._group(group_id)):
                    continue
                for item in self._instances(group_id, {"READY"}):
                    idle = time.time() - item.get("last_activity_wall", item.get("ready_wall", time.time()))
                    leased = any(task_id and instance_id == item["id"]
                                 for (task_id, _), instance_id in self.affinity.items())
                    if not leased and not item["in_flight"] and not item.get("pending_stop") and idle * 1000 >= control["idle_ms"]:
                        due.append((group_id, item["id"], owner["run_id"]))
        for group_id, instance_id, run_id in due:
            try:
                self.drain_stop(group_id, dict(instance_id=instance_id), run_id=run_id, reason="idle_retire/1")
            except ValueError:
                pass

    def _controller(self):
        last = 0.0
        while not self.closed.wait(1.0):
            try:
                self._advance_stops()
                self._retire_idle()
                if time.monotonic() - last >= 10:
                    last = time.monotonic()
                    with self.lock:
                        polled = [item for item in self.instances.values() if item["state"] in {"READY", "DRAINING"}
                                  and item.get("endpoint") and item.get("session") == self.session]
                    for item in polled:
                        self._poll(item)
                    self._observe_starting()
            except Exception as error:
                if self.closed.is_set():
                    return
                self.controller_error = dict(at=now(), error=type(error).__name__ + ": " + str(error))

    # Calls: routing with owner affinity, encoded ports, no replay.

    def group_busy(self, identity):
        return any((call.get("unit") or {}).get("runtime_group") == identity and call["physical"] in {"ENABLED", "RESERVED", "AUTHORIZED", "STARTED", "UNKNOWN"} for call in self.platform.runtime.snapshot()["calls"].values())

    @staticmethod
    def _idle_rebuild_allowed(group):
        lifecycle = group.get("contract", {}).get("lifecycle")
        if lifecycle is not None:
            return lifecycle.get("reclaim", {}).get("status") == "SUPPORTED"
        # Compatibility for previously checked scalar artifacts only.
        return (group.get("grouping") == "CHECKED_STATELESS_ENTRIES"
                and group.get("contract", {}).get("replicas", {}).get("replicable") is True)

    def _route(self, group_id, unit_id, task_id):
        key = (task_id, group_id)
        bound = self.instances.get(self.affinity.get(key)) if task_id else None
        if bound:
            if bound["state"] == "READY":
                if unit_id in bound.get("units", []):
                    return bound
                raise ProtocolError("本任务绑定的实例未登记该成员（宿主环境未满足）；同组成员不改派到其他实例")
            if bound["state"] in {"UNKNOWN", "FAILED", "DRAINING", "STOPPED"}:
                raise ProtocolError("本任务绑定的实例" + ("正在排空，不接受新根请求" if bound["state"] == "DRAINING" else
                                    "已不可用（" + bound["state"] + "）") + "；原 owner 状态不迁移到其他实例，也不重放")
        ready = [item for item in self._instances(group_id, {"READY"}) if item["accepting"] and unit_id in item.get("units", [])]
        if not ready:
            return None
        chosen = min(ready, key=lambda item: (item["in_flight"], item["generation"]))
        if task_id:
            self.affinity[key] = chosen["id"]
        return chosen

    def invoke(self, unit, arguments, native_arguments=()):
        group_id = unit.get("runtime_group")
        if not group_id:
            return False, None
        with self.lock:
            record = self._route(group_id, unit["id"], _task_context.get())
        if record is None:
            return False, None
        return True, self._call(record, unit, arguments, native_arguments)[0]

    def invoke_placed(self, placement, unit, arguments, native_arguments=()):
        """Functional mode: a fixed controller prepares an instance, then the call is dispatched."""
        waited = None
        with self.lock:
            record = self._route(placement["group_id"], unit["id"], _task_context.get())
        if record is None:
            started = time.perf_counter()
            record = self._ensure(placement, unit["id"])
            waited = (time.perf_counter() - started) * 1000
        result, facts = self._call(record, unit, arguments, native_arguments)
        facts.update(start_wait_ms=waited, cold_instance=waited is not None)
        return result, facts

    def _ensure(self, placement, unit_id):
        group = self._group(placement["group_id"])
        if group["contract"]["digest"] != placement["contract_digest"]:
            raise ProtocolError("运行组契约已变化；冻结运行不能改用新契约")
        deadline = time.monotonic() + placement["start_timeout_s"]
        record = None
        while record is None and time.monotonic() < deadline:
            with self.lock:
                pending = [item for item in self._instances(group["id"], {"UNDEPLOYED", "STARTING"})
                           if item.get("run_id") == placement["run_id"]]
                retiring = [item for item in self._instances(group["id"], {"DRAINING"}) if item.get("pending_stop")]
            if pending:
                record = pending[0]
            elif not retiring:
                record = self.instances[self._start(group["id"], placement["run_id"], False, placement.get("image"))["id"]]
            elif self.closed.wait(0.5):
                break
        while record is not None and time.monotonic() < deadline:
            with self.lock:
                if record["state"] == "READY":
                    if unit_id not in record["units"]:
                        raise ProtocolError("实例宿主环境未满足该成员：" + str(record.get("missing", {}).get(unit_id)))
                    routed = self._route(group["id"], unit_id, _task_context.get())
                    if routed is not None:
                        return routed
                if record["state"] in {"FAILED", "STOPPED", "UNKNOWN"} or (
                        record["state"] == "UNDEPLOYED" and record.get("apply", {}).get("status") != "BACKEND_UNAVAILABLE"
                        and record.get("apply")):
                    raise ProtocolError("实例未能就绪：" + str(record.get("reason") or record["state"]))
            if self.closed.wait(0.5):
                break
        raise ProtocolError("实例在 " + str(placement["start_timeout_s"]) + " 秒内未就绪：" +
                            (str(record.get("reason") or record["state"]) if record else "原实例仍在排空"))

    def _call(self, record, unit, arguments, native_arguments):
        if native_arguments:
            raise ValueError("组外端口不传递原对象引用；需要原对象输入的组合留在本地")
        if record["profile"] == REMOTE_PROFILE["id"]:
            if any(type(value) not in {str, int, bool, type(None)} for value in arguments.values()):
                raise ValueError("远程输入须为原类型的内建不可变标量，不转换原对象或子类")
            payload = arguments
        else:
            payload = encode_port(arguments)
        with self.lock:
            if record["state"] != "READY" or not record.get("accepting") or record.get("source_detached"):
                raise ProtocolError("实例已停止接单或来源已摘除，不能开始新的根请求")
            record["in_flight"] += 1
            record["last_activity_wall"] = time.time()
        started = time.perf_counter()
        try:
            response = json_request(record["endpoint"] + "/invoke", dict(unit=unit["id"], arguments=payload,
                task_id=_task_context.get(), **self._identity(record)))
            self._check_identity(record, response)
        except (OSError, ValueError, ProtocolError) as error:
            with self.lock:
                record.update(state="UNKNOWN", accepting=False, reason="调用响应未取得：" + str(error))
                self._event(record, "unknown", unit_id=unit["id"], reason=record["reason"])
                self._environment_change(record, "response_lost")
            self.platform.runtime.call("remote_unknown", record["group_id"], record["id"])
            raise ProtocolError("远程响应未取得；调用不重放，未收尾执行保留为UNKNOWN：" + str(error)) from error
        finally:
            with self.lock:
                record["in_flight"] -= 1
                record["last_activity_wall"] = time.time()
        elapsed = (time.perf_counter() - started) * 1000
        if "rejected" in response:
            raise ProtocolError("实例拒绝该根请求（未执行）：" + str(response["rejected"]))
        timing = response.get("timing") or {}
        worker_ms = timing.get("duration_ms")
        facts = dict(execution="REMOTE", group_id=record["group_id"], instance_id=record["id"],
            generation=record["generation"], session=record["session"], process_nonce=record["process_nonce"],
            round_trip_ms=elapsed, worker_ms=worker_ms,
            transport_ms=elapsed - worker_ms if type(worker_ms) in (int, float) else None,
            initialization_before=timing.get("initialization_before"),
            initialization_after=timing.get("initialization_after"), binding_duration_ms=timing.get("binding_duration_ms"))
        with self.lock:
            if facts["initialization_after"]:
                record["initialization"][unit["id"]] = facts["initialization_after"]
            record["observed_wall"] = time.time()
            self.unit_state[unit["id"]] = facts
            self._event(record, "call", unit_id=unit["id"], status="ERROR" if "error" in response else "RETURN",
                        round_trip_ms=elapsed, worker_ms=worker_ms, initialization_before=facts["initialization_before"])
        stored = self.platform.store.get("unit", unit["id"])
        if stored:
            stored.update(initialization=facts["initialization_after"] or stored.get("initialization"),
                          last_initialization_before=facts["initialization_before"],
                          last_binding_duration_ms=facts["binding_duration_ms"], last_execution_instance=record["id"])
            self.platform.store.save("unit", unit["id"], stored)
        if "error" in response:
            raise ProtocolError("原远程入口 " + response.get("error_type", "Error") + ": " + response["error"])
        result = response["result"] if record["profile"] == REMOTE_PROFILE["id"] else decode_port(response["result"])
        return result, facts

    # Run-window evidence for cost ledgers.

    def events_between(self, group_ids, start, end):
        with self.lock:
            events = [event for event in self.event_log if event["group_id"] in group_ids and start <= event["wall"] <= end]
        summary = {}
        for event in events:
            row = summary.setdefault(event["kind"], dict(count=0, duration_ms=0.0))
            row["count"] += 1
            for key in ("duration_ms", "round_trip_ms", "start_to_ready_ms"):
                if type(event.get(key)) in (int, float):
                    row["duration_ms"] += event[key]
        return dict(kinds=summary, instances=sorted({event["instance_id"] for event in events}))

    def residency(self, group_ids, start, end):
        rows = []
        with self.lock:
            for item in self.instances.values():
                begin = item.get("start_wall") or item.get("ready_wall")
                if item["group_id"] not in group_ids or begin is None:
                    continue
                observed = item.get("stopped_wall") is not None or item["state"] in {"UNDEPLOYED", "STARTING", "READY", "DRAINING"}
                finish = item.get("stopped_wall") or end
                resident = max(0.0, min(finish, end) - max(begin, start)) * 1000
                ready = max(0.0, min(finish, end) - max(item.get("ready_wall") or finish, start)) * 1000
                busy = sum(event.get("worker_ms") or 0 for event in self.event_log if event["instance_id"] == item["id"]
                           and event["kind"] == "call" and start <= event["wall"] <= end)
                rows.append(dict(instance_id=item["id"], group_id=item["group_id"], generation=item["generation"],
                                 resident_ms=resident, ready_ms=ready, call_ms=busy, idle_ready_ms=max(0.0, ready - busy),
                                 end_observed=observed, peak_rss_kb=(item.get("resources") or {}).get("peak_rss_kb")))
        return dict(allocation="UNALLOCATED_WINDOW_TOTAL", window=dict(start=start, end=end), instances=rows,
                    resident_ms=sum(row["resident_ms"] for row in rows), ready_ms=sum(row["ready_ms"] for row in rows),
                    idle_ready_ms=sum(row["idle_ready_ms"] for row in rows),
                    memory_integral=dict(status="UNKNOWN", reason="未测量驻留内存积分；实例只报告可得的峰值 RSS"),
                    gpu_time=dict(status="UNKNOWN", reason="wall time is not GPU kernel time"),
                    note="启动与空闲驻留跨任务发生；此为不分摊的窗口总量，单任务账本只计其实际等待")

    def protocol(self, body):
        with self.lock:
            record = self.instances.get(body.get("instance_id"))
            valid = (record is not None and record["group_id"] == body.get("group_id") and body.get("session") == self.session
                     and record.get("session") == self.session and bool(record.get("process_nonce"))
                     and all(body.get(key) == value for key, value in self._identity(record).items())
                     and record["state"] not in {"STOPPED", "FAILED"})
        if not valid:
            raise ProtocolError("未登记或已过期的运行进程；重启不恢复原会话")
        method, args, kwargs = body.get("method"), body.get("args", []), body.get("kwargs", {})
        if method not in {"register", "enable", "wait", "consume", "finish", "cancel", "label"}:
            raise ProtocolError("未支持的运行进程协议操作")
        snapshot = self.platform.runtime.snapshot()
        if method == "register":
            unit = kwargs.get("unit") or {}
            group = self.platform.store.get("runtime_group", record["group_id"])
            if unit.get("source_id") != group["source_id"]:
                raise ProtocolError("登记的边界不属于这个运行组来源")
            allowed = set(group["units"])
            for identity in group["units"]:
                allowed.update(group["source_version"][:16] + ":" + boundary
                               for boundary in self._entry(identity).get("boundary_ids", []))
            if unit.get("id") not in allowed:
                raise ProtocolError("运行边界不属于核对后的组成员")
            kwargs["unit"] = dict(unit, runtime_group=record["group_id"], execution="REMOTE", **self._identity(record))
        else:
            item = snapshot["gates"].get(args[0]) if method in {"enable", "label"} else snapshot["calls"].get(args[0])
            if not item or any((item.get("unit") or {}).get(key) != value for key, value in self._identity(record).items()):
                raise ProtocolError("边界/调用不属于当前运行进程")
            if method == "label":
                args[1] = dict(args[1], runtime_group=record["group_id"], execution="REMOTE", **self._identity(record))
        return dict(result=self.platform.runtime.call(method, *args, **kwargs), **self._identity(record))
