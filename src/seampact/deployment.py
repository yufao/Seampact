"""Source-derived runtime groups and a finite, independently checked remote host.

Unknown object/state/control edges keep the original source context together.
The first remote rule covers synchronous scalar-value entries only. It does not
infer independent export from a JSON schema or from the analyst's assertions.
"""

import ast
import json
import shutil
import uuid
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .inspection import digest, safe_path
from .runtime import ProtocolError, _task_context
from .store import now
from .verification import source_unit

REMOTE_PROFILE = dict(id="python311-scalar-worker/1", confirmed=True,
    process_per_group=1, replicas=1, preserve_original_lifetimes=True,
    session_recovery=False, codec="JSON_SCALARS", parallel=False,
    source="用户确认：按原对象/共享状态/控制依赖分组，远程条件独立核对，未覆盖继续本地")


class RemoteGap(ValueError):
    pass


def json_request(url, body=None):
    payload = None if body is None else json.dumps(body, ensure_ascii=False, allow_nan=False).encode()
    request = Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        # No automatic retry: an absent response is not evidence of non-execution.
        with urlopen(request) as response:
            value = json.load(response)
    except HTTPError as error:
        detail = json.load(error)
        raise ProtocolError(detail.get("error", str(error))) from error
    return value


def worker_spec(binding, group, unit):
    candidate = binding.candidate
    entries = [item for item in candidate["entries"] if item["id"] in group["entries"]]
    selected = {identity for entry in entries for identity in entry.get("boundary_ids", [])}
    candidate = dict(candidate, entries=entries,
        boundaries=[item for item in candidate.get("boundaries", []) if item["id"] in selected], runtime_groups=[])
    return dict(schema="native-worker/1", group=group, request=binding.inspector.request,
                candidate=candidate, analysis_id=unit["analysis_id"], installation_id=unit["installation_id"])


def binding_fingerprint(spec):
    payload = {key: spec[key] for key in ("request", "candidate", "analysis_id", "installation_id")}
    payload["group"] = {key: spec["group"][key] for key in ("id", "units", "source_id", "source_version")}
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
    return dict(schema="runtime-group-report/1", profile=REMOTE_PROFILE, groups=groups,
                proposal_gaps=gaps, proposal_status="REPAIRED_CONSERVATIVELY" if gaps else "CHECKED")


class Deployments:
    def __init__(self, platform):
        self.platform = platform
        self.session = uuid.uuid4().hex
        self.routes = {}
        self.platform.catalog.remote_router = self

    def public(self):
        groups = self.platform.store.list("runtime_group")
        sources = {source["id"]: source for source in self.platform.store.list("harness")}
        result = []
        for group in groups:
            source = sources.get(group["source_id"])
            if not source or source.get("status") == "DETACHED" or group.get("installation_id") != source.get("installation_id"):
                continue
            route = self.routes.get(group["id"])
            current = route and set(route["units"]) == set(group["units"])
            group["execution"] = "REMOTE" if current else "LOCAL"
            group["instance"] = route if current else None
            result.append(group)
        return result

    def export(self, identity, body):
        group = next((item for item in self.public() if item["id"] == identity), None)
        if not group or group["status"] != "REMOTE_CHECKED":
            raise ValueError("运行组的远程条件尚未覆盖，继续本地执行；请查看缺口")
        if self.platform.catalog.source_running(group["source_id"]):
            raise ValueError("原生调用仍在运行，结束后导出")
        platform_url = body.get("platform_url", "http://seampact-platform:8880").rstrip("/")
        from urllib.parse import urlsplit
        if urlsplit(platform_url).scheme not in {"http", "https"} or not urlsplit(platform_url).netloc:
            raise ValueError("平台地址须为运行进程可访问的HTTP地址")
        namespace = body.get("namespace", "seampact")
        import re
        if not isinstance(namespace, str) or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", namespace):
            raise ValueError("K8S命名空间格式不正确")
        unit = self.platform.store.get("unit", group["units"][0])
        binding = self.platform.catalog.bindings[unit["id"]]
        destination = self.platform.home / "deployments" / (identity + "-" + uuid.uuid4().hex[:8])
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
        shutil.copytree(Path(__file__).parent, destination / "core" / "seampact", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        image = "seampact-worker:" + identity[3:]
        spec = worker_spec(binding, group, unit)
        (destination / "worker.json").write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
        (destination / "Dockerfile").write_text(
            'FROM python:3.11-slim\nWORKDIR /work\nCOPY core /app\nCOPY source /work/source\nCOPY snapshots /work/snapshots\nCOPY worker.json /work/worker.json\nENV PYTHONPATH=/app PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1\nENTRYPOINT ["python","-B","-m","seampact","worker","--spec","/work/worker.json","--host","0.0.0.0","--port","8890"]\n', encoding="utf-8")
        labels = {"app": identity}
        manifest = dict(apiVersion="v1", kind="List", items=[
            dict(apiVersion="apps/v1", kind="Deployment", metadata=dict(name=identity, namespace=namespace),
                spec=dict(replicas=1, strategy=dict(type="Recreate"), selector=dict(matchLabels=labels),
                    template=dict(metadata=dict(labels=labels), spec=dict(containers=[dict(name="owner", image=image,
                        imagePullPolicy="IfNotPresent", args=["--coordinator", platform_url, "--advertise-url", "http://" + identity + ":8890"],
                        ports=[dict(containerPort=8890)])])))),
            dict(apiVersion="v1", kind="Service", metadata=dict(name=identity, namespace=namespace),
                spec=dict(selector=labels, ports=[dict(port=8890, targetPort=8890)]))])
        (destination / "k8s.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        archive = shutil.make_archive(str(destination), "zip", root_dir=destination)
        record = dict(id=identity, status="EXPORTED", folder=str(destination), image=image,
            archive=archive,
            platform_url=platform_url, namespace=namespace, created_at=now(), source_version=group["source_version"],
            deployment="NOT_APPLIED", copies_original_code=True, resource_downloads=0)
        self.platform.store.save("deployment", identity, record)
        return record

    def attach(self, endpoint):
        endpoint = endpoint.rstrip("/")
        health = json_request(endpoint + "/health")
        group = next((item for item in self.public() if item["id"] == health.get("group_id")), None)
        if not group or group["status"] != "REMOTE_CHECKED" or health.get("session") != self.session or health.get("profile") != REMOTE_PROFILE["id"] or health.get("source_version") != group["source_version"] or health.get("installation_id") != group["installation_id"] or set(health.get("units", [])) != set(group["units"]):
            raise ValueError("运行进程与当前源码/接入版本/宿主/平台会话不一致")
        unit = self.platform.store.get("unit", group["units"][0])
        expected = worker_spec(self.platform.catalog.bindings[unit["id"]], group, unit)
        if health.get("binding_fingerprint") != binding_fingerprint(expected):
            raise ValueError("运行进程的原源码、入口接线或构造配置与能力池不一致")
        prior = self.routes.get(group["id"])
        if self.platform.catalog.source_running(group["source_id"]):
            raise ValueError("本地原调用仍在执行，结束后再切换运行位置")
        if prior and prior["instance_id"] != health["instance_id"] and self.group_busy(group["id"]):
            raise ValueError("旧运行进程仍有未收尾调用，不能假设重启恢复原会话")
        route = dict(endpoint=endpoint, instance_id=health["instance_id"], session=self.session, status="ONLINE",
            units=health["units"], group_id=group["id"], attached_at=now())
        self.routes[group["id"]] = route
        print("[SeamPact] worker_attached " + group["id"], flush=True)
        return route

    def group_busy(self, identity):
        return any((call.get("unit") or {}).get("runtime_group") == identity and call["physical"] in {"ENABLED", "RESERVED", "AUTHORIZED", "STARTED", "UNKNOWN"} for call in self.platform.runtime.snapshot()["calls"].values())

    def local(self, identity):
        if self.group_busy(identity):
            raise ValueError("远程调用未收尾，不能切换执行位置")
        self.routes.pop(identity, None)
        return dict(id=identity, execution="LOCAL")

    def invoke(self, unit, arguments):
        route = self.routes.get(unit.get("runtime_group"))
        if not route:
            return False, None
        if unit["id"] not in route["units"]:
            return False, None
        if route["status"] != "ONLINE":
            raise ProtocolError("远程执行结果未决，不能继续派发或自动重放")
        if any(type(value) not in {str, int, bool, type(None)} for value in arguments.values()):
            raise ValueError("远程输入须为原类型的内建不可变标量，不转换原对象或子类")
        try:
            response = json_request(route["endpoint"] + "/invoke", dict(unit=unit["id"], arguments=arguments,
                task_id=_task_context.get(), session=self.session, instance_id=route["instance_id"]))
        except (OSError, ValueError, ProtocolError) as error:
            route["status"] = "UNKNOWN"
            self.platform.runtime.call("remote_unknown", route["group_id"], route["instance_id"])
            raise ProtocolError("远程响应未取得；调用不重放，未收尾执行保留为UNKNOWN：" + str(error)) from error
        if response.get("instance_id") != route["instance_id"]:
            raise ProtocolError("运行进程已替换，本次结果不属于原实例；不会重放调用")
        if "error" in response:
            raise ProtocolError("原远程入口 " + response.get("error_type", "Error") + ": " + response["error"])
        return True, response["result"]

    def protocol(self, body):
        route = self.routes.get(body.get("group_id"))
        if not route or body.get("instance_id") != route["instance_id"] or body.get("session") != self.session:
            raise ProtocolError("未登记或已过期的运行进程；重启不恢复原会话")
        method, args, kwargs = body.get("method"), body.get("args", []), body.get("kwargs", {})
        if method not in {"register", "enable", "wait", "consume", "finish", "cancel", "label"}:
            raise ProtocolError("未支持的运行进程协议操作")
        snapshot = self.platform.runtime.snapshot()
        if method == "register":
            unit = kwargs.get("unit") or {}
            group = self.platform.store.get("runtime_group", route["group_id"])
            if unit.get("source_id") != group["source_id"]:
                raise ProtocolError("登记的边界不属于这个运行组来源")
            allowed = set(group["units"])
            for identity in group["units"]:
                spec = self.platform.catalog.entry_specs.get(identity, {})
                allowed.update(group["source_version"][:16] + ":" + boundary for boundary in spec.get("boundary_ids", []))
            if unit.get("id") not in allowed:
                raise ProtocolError("运行边界不属于核对后的组成员")
            kwargs["unit"] = dict(unit, runtime_group=route["group_id"], instance_id=route["instance_id"], execution="REMOTE")
        else:
            record = snapshot["gates"].get(args[0]) if method in {"enable", "label"} else snapshot["calls"].get(args[0])
            if not record or (record.get("unit") or {}).get("instance_id") != route["instance_id"]:
                raise ProtocolError("边界/调用不属于当前运行进程")
            if method == "label":
                args[1] = dict(args[1], runtime_group=route["group_id"], instance_id=route["instance_id"], execution="REMOTE")
        return dict(result=self.platform.runtime.call(method, *args, **kwargs))
