"""One original native process per runtime-group instance; no session recovery.

The worker re-checks the frozen source and every binding rule in its own
environment. Files, packages and credentials are facts of this host, so they are
checked here rather than trusted from the platform's record.
"""

import asyncio
import copy
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import Future
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from seampact.hosting.deployment import GROUP_PROFILE, IDENTITY_PROTOCOL, REMOTE_PROFILE, PortGap, binding_fingerprint, decode_port, encode_port, json_request
from seampact.source.inspection import Inspector, digest, python_runtime_issue, safe_path
from seampact.resources import ResourceRegistry
from seampact.runtime import OwnerRuntime, ProtocolError
from seampact.store import Store
from seampact.units import UnitCatalog, _result_matches
from seampact.source.record_ports import FrozenRecordPorts
from seampact.hosting.background import census as python_thread_census, required as python_work_required, quiescent as python_work_quiescent, cleanup_obligations


class RemoteRuntime:
    # Reuse the existing original-call wrapper: the coordinator remains central,
    # while consume -> original -> finish happen in this worker's native process.
    wrap = OwnerRuntime.wrap
    context = OwnerRuntime.context

    def __init__(self, coordinator, group, instance, session, generation, process_nonce):
        self.coordinator, self.group, self.instance, self.session = coordinator, group, instance, session
        self.generation, self.process_nonce = generation, process_nonce

    def identity(self):
        return dict(session=self.session, instance_id=self.instance, generation=self.generation,
                    process_nonce=self.process_nonce)

    def call(self, method, *args, **kwargs):
        answer = json_request(self.coordinator + "/api/runtime/worker", dict(method=method, args=args, kwargs=kwargs,
            group_id=self.group, **self.identity()))
        if any(answer.get(key) != value for key, value in self.identity().items()):
            raise ProtocolError("中央许可响应不属于当前运行进程；不恢复原会话")
        return answer["result"]

    def submit(self, method, *args, **kwargs):
        # HTTP wait can outlive the native coroutine's cancellation. Keep it
        # independent so the same wrapper can still send cancel/finish to the
        # central coordinator; no local permit or scheduling policy is added.
        future = Future()
        def communicate():
            if not future.set_running_or_notify_cancel():
                return
            try:
                future.set_result(self.call(method, *args, **kwargs))
            except BaseException as error:
                future.set_exception(error)
        threading.Thread(target=communicate, daemon=True, name="seampact-worker-permit").start()
        return future

    async def acall(self, method, *args, **kwargs):
        return await asyncio.wrap_future(self.submit(method, *args, **kwargs))


class HostProfiles:
    """Connection snapshots frozen in the binding; credentials come from this host's environment."""
    def __init__(self, selections):
        self.lock = threading.RLock()
        self.connections = {selection["connection"]["id"]: selection["connection"]
                            for selection in selections.values() if selection.get("connection")}

    def connection_profile(self, connection_id):
        return copy.deepcopy(self.connections[connection_id])

    def connection_key(self, connection_id):
        auth = self.connections[connection_id].get("auth") or {"mode": "none"}
        if auth["mode"] == "none":
            return ""
        if auth["mode"] == "environment":
            return os.environ.get(auth.get("env", ""), "")
        name = "SEAMPACT_CREDENTIAL_" + re.sub(r"[^A-Za-z0-9]", "_", connection_id).upper()
        return os.environ.get(name) or os.environ.get(auth.get("fallback_env") or "", "")


class HostDependencies:
    def __init__(self, request):
        self.profiles = HostProfiles(request.get("dependency_bindings", {}))

    def record_execution(self, *args, **kwargs):
        return None


def host_weight_check(root):
    registry = ResourceRegistry(root, Store(":memory:"), None)
    def check(resource):
        record = copy.deepcopy(resource)
        record.setdefault("required_files", [])
        try:
            registry.store.save("weight_resource", record["id"], record)
            return registry.check_weight(record["id"])["check"]
        except (ValueError, KeyError, OSError) as error:
            return dict(status="missing", reason=str(error))
    return check


class Worker:
    def __init__(self, spec_path, coordinator, instance_id=None, generation=0):
        issue = python_runtime_issue()
        if issue:
            raise ValueError(issue)
        self.spec_path = Path(spec_path).resolve()
        self.spec = json.loads(self.spec_path.read_text(encoding="utf-8"))
        if self.spec.get("schema") != "native-worker/1":
            raise ValueError("未知运行组格式")
        environment = self.spec.get("runtime_environment") or {"python": "3.11"}
        expected = environment.get("python") if isinstance(environment, dict) else None
        if not isinstance(expected, str) or not re.fullmatch(r"3\.(?:10|11|12|13)(?:\.\d+)?", expected):
            raise ValueError("运行组必须声明 Python 3.10–3.13 中的一个解释器版本")
        if tuple(map(int, expected.split(".")[:2])) != tuple(sys.version_info[:2]):
            raise ValueError("运行组声明 Python " + expected + "，当前解释器为 " +
                             ".".join(map(str, sys.version_info[:2])) + "；请使用匹配的运行镜像")
        from seampact.hosting.environments import check_python_version
        checked_python = check_python_version(".".join(map(str, sys.version_info[:3])),
                                              environment.get("python_requirements", []))
        if checked_python["status"] != "SUPPORTED":
            raise ValueError("运行组实际 Python 不满足原库声明：" + "; ".join(checked_python["missing"]))
        self.profile = self.spec.get("profile", REMOTE_PROFILE["id"])
        if self.profile not in {REMOTE_PROFILE["id"], GROUP_PROFILE["id"]}:
            raise ValueError("旧版或未知运行组宿主规格；请用当前平台重新生成运行工件")
        from seampact.target_runtime import core_identity
        if not isinstance(self.spec.get("platform_core"), dict):
            raise ValueError("运行工件缺少平台核对规则版本；请用当前平台重新生成运行工件")
        if self.spec["platform_core"] != core_identity("worker"):
            raise ValueError("运行镜像的平台核对代码与冻结工件不一致；请重新生成并构建运行镜像")
        self.group = self.spec["group"]
        root = self.spec_path.parent
        inspector = Inspector(root / "source", root / "snapshots", self.spec["request"])
        self.record_ports = FrozenRecordPorts(inspector)
        withheld = inspector.request.get("withheld_sources")
        version = digest(json.dumps(dict(files=inspector.request["files"], withheld_sources=withheld) if withheld
                                    else inspector.request["files"], sort_keys=True).encode())
        if version != inspector.request["source_version"] or version != self.group["source_version"] or inspector.request.get("source_id") != self.group["source_id"]:
            raise ValueError("运行组的来源身份与冻结文件清单不一致")
        for record in inspector.files.values():
            if digest(safe_path(inspector.root, record["path"]).read_bytes()) != record["sha256"] or digest((inspector.snapshots / record["sha256"]).read_bytes()) != record["sha256"]:
                raise ValueError("运行镜像源码不属于冻结版本")
        if instance_id is not None and not re.fullmatch(r"[0-9a-f]{32}", instance_id):
            raise ValueError("实例身份格式不正确")
        if type(generation) is not int or generation < 0:
            raise ValueError("实例代次必须是非负整数")
        self.instance = instance_id or uuid.uuid4().hex
        self.generation = generation
        self.process_nonce = uuid.uuid4().hex
        self.coordinator = coordinator.rstrip("/")
        self.session = json_request(self.coordinator + "/api/runtime/session", timeout=30)["session"]
        self.runtime = RemoteRuntime(self.coordinator, self.group["id"], self.instance, self.session,
                                     self.generation, self.process_nonce)
        self.store = Store(":memory:")
        self.catalog = UnitCatalog(root, self.store, self.runtime)
        if self.profile == GROUP_PROFILE["id"]:
            self.catalog.resource_registry = HostDependencies(inspector.request)
            self.catalog.host_check = host_weight_check(root)
        # Recheck the same rules inside the actual host; edited bundles cannot
        # make an unsupported entry remotely executable.
        report = self.catalog.inspect_candidate(inspector, self.spec["candidate"])
        group = next((item for item in report["runtime_groups"]["groups"] if item["id"] == self.group["id"]), None)
        if group is None:
            raise ValueError("宿主重新核对后的运行组划分与工件不一致")
        units = self.catalog.install(inspector, self.spec["candidate"], self.spec["analysis_id"], self.spec["installation_id"])
        if set(unit["id"] for unit in units) != set(self.group["units"]):
            raise ValueError("运行组成员与可绑定能力不一致")
        self.guarded_units = set()
        if self.profile == REMOTE_PROFILE["id"]:
            if group["status"] != "REMOTE_CHECKED" or any(unit["status"] != "READY" for unit in units):
                raise ValueError("此运行组未满足远程宿主规则")
            self.units, self.missing = {unit["id"] for unit in units}, {}
        else:
            if group["contract"]["digest"] != self.spec.get("contract_digest"):
                raise ValueError("宿主重新核对后的运行组契约与工件不一致")
            hosted = set(group["host"]["host_entries"])
            structural = set(group['host'].get('structure_entries', []))
            self.units = {unit["id"] for unit in units if unit["status"] in {'READY', 'CALL_CONDITION_DEFERRED'} and unit["entry_id"] in hosted}
            self.guarded_units = {unit['id'] for unit in units if unit.get('status') == 'STRUCTURE_ONLY'
                                  and unit['entry_id'] in structural}
            self.missing = {unit["id"]: unit.get("reason") or group["host"]["members"][unit["entry_id"]].get("reason")
                            for unit in units if unit["id"] not in self.units}
            if not self.units and not self.guarded_units:
                raise ValueError("宿主环境中没有可执行成员：" + json.dumps(self.missing, ensure_ascii=False))
            if self.guarded_units and (self.units or len(self.guarded_units) != 1 or len(units) != 1):
                raise ValueError('guarded material requires one separate structural root')
        self.prepare_rules = {unit["id"]: group["host"]["members"][unit["entry_id"]]["prepare"] for unit in units}
        self.catalog._worker_call_identity = self.identity()
        installed_group = self.store.get("runtime_group", self.group["id"])
        self.resource_observations = dict(schema="worker-resource-startup-observation/1", scope="STARTUP_SNAPSHOT",
            observed_wall=time.time(), process_identity=self.identity(),
            entries=installed_group.get("resource_observations", []),
            validity="installation-time asset check only; not a continuing disk identity guarantee")
        self.accepting, self.in_flight, self.preparing, self.lock = True, 0, 0, threading.Lock()
        self.host_threads = {id(threading.current_thread()): threading.current_thread()}
        self.server = None
        lock = os.environ.get("SEAMPACT_ENVIRONMENT_LOCK")
        self.environment = dict(python=sys.version.split()[0], revision=os.environ.get("SEAMPACT_REVISION", "UNKNOWN"),
            lock_sha256=hashlib.sha256(Path(lock).read_bytes()).hexdigest() if lock and Path(lock).is_file() else None)
        self._guarded_scopes = {}
        if self.guarded_units:
            from seampact.source.value_conditioned_process import begin_worker_scope
            for unit_id in self.guarded_units:
                self._guarded_scopes[unit_id] = begin_worker_scope(self, unit_id)

    def _initialization(self):
        result = {}
        for unit_id in self.units:
            binding, entry = ((self.catalog.bindings[unit_id], self.catalog.entry_specs[unit_id])
                              if unit_id in self.catalog.bindings else self.catalog.deferred[unit_id])
            result[unit_id] = binding.initialization_state(entry, self.store.get("unit", unit_id))
        return result

    def identity(self):
        return dict(session=self.session, instance_id=self.instance, generation=self.generation,
                    process_nonce=self.process_nonce)

    @contextmanager
    def hosting_thread(self):
        current = threading.current_thread()
        with self.lock:
            self.host_threads[id(current)] = current
        try:
            yield
        finally:
            with self.lock:
                self.host_threads.pop(id(current), None)

    def _python_threads(self):
        with self.lock:
            known = list(getattr(self, "host_threads", {id(threading.main_thread()): threading.main_thread()}).values())
        # These are exact platform-owned loop objects, not names supplied by a
        # source. Source/SDK threads are deliberately not put on this list.
        for binding in getattr(self.catalog, "bindings", {}).values():
            thread = getattr(binding, "thread", None)
            if thread is not None:
                known.append(thread)
        return python_thread_census(known)

    def health(self):
        resources = {}
        try:
            import resource
            resources["peak_rss_kb"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        except (ImportError, AttributeError):
            resources["peak_rss_kb"] = None
        initialization = self._initialization()
        return dict(status="READY", profile=self.profile, identity_protocol=IDENTITY_PROTOCOL,
            **self.identity(), group_id=self.group["id"],
            source_version=self.group["source_version"], binding_fingerprint=binding_fingerprint(self.spec),
            contract_digest=self.spec.get("contract_digest"), installation_id=self.spec["installation_id"],
            units=sorted(self.units), missing=self.missing, accepting=self.accepting, in_flight=self.in_flight,
            guarded_units=sorted(getattr(self, 'guarded_units', set())),
            preparation_in_flight=self.preparing, process_ready=True,
            initialization_ready={unit_id: state == "BOUND" for unit_id, state in initialization.items()},
            initialization=initialization, environment=self.environment, resources=resources,
            session_recovery=False, python_threads=self._python_threads(), resource_observations=self.resource_observations,
            sdk_cleanup=cleanup_obligations(self.group.get("contract") or {}))

    def _identify(self, body):
        if any(body.get(key) != value for key, value in self.identity().items()):
            raise ProtocolError("运行进程或调用入口已过期；不恢复原会话")

    def invoke(self, body):
        try:
            self._identify(body)
            if body.get("unit") not in self.units:
                raise ProtocolError("该入口未在本实例登记")
        except ProtocolError as error:
            return dict(rejected=str(error), **self.identity())
        with self.lock:
            if not self.accepting:
                return dict(rejected="实例已停止接单", **self.identity())
            selected = self.store.get('unit', body['unit']) or {}
            if selected.get('call_condition') and (self.in_flight or self.preparing):
                return dict(rejected='conditional original owner is busy; call state is UNKNOWN', **self.identity())
            self.in_flight += 1
        unit_id = body["unit"]
        started = time.perf_counter()
        timing = {}
        try:
            unit = self.store.get('unit', unit_id)
            if unit.get('call_condition'):
                from seampact.source.call_value_conditions import ordinary
                ordinary(body['arguments'])
            arguments = body["arguments"] if self.profile == REMOTE_PROFILE["id"] else decode_port(body["arguments"])
            self.catalog.require_call_conditions(unit_id, arguments, instance_identity=self.identity())
            with self.runtime.context(body.get("task_id")):
                result = self.catalog.invoke(unit_id, arguments)
            unit = self.store.get("unit", unit_id)
            timing = dict(duration_ms=(time.perf_counter() - started) * 1000,
                          initialization_before=unit.get("last_initialization_before"),
                          initialization_after=unit.get("initialization"),
                          binding_duration_ms=unit.get("last_binding_duration_ms"))
            if self.profile == REMOTE_PROFILE["id"]:
                if type(result) not in {str, int, bool, type(None)}:
                    raise ValueError("原返回未满足核对的标量接口；不转换原对象")
                encoded = result
            else:
                encoded = encode_port(result, self.record_ports)
            schema = unit.get("result_schema") or {}
            actual_entry = self.catalog.entry_specs.get(unit_id)
            if actual_entry is None and unit_id in self.catalog.deferred:
                actual_entry = self.catalog.deferred[unit_id][1]
            if actual_entry is None or actual_entry.get('kind') != unit.get('kind'):
                raise ValueError('原实际入口与核对的结果端口种类不一致；后继不使能')
            cli_result = (self.profile == GROUP_PROFILE["id"] and
                unit.get('kind') == "cli" and
                schema == {"x-native-type": {"module": "subprocess", "symbol": "CompletedProcess"}} and
                type(result) is subprocess.CompletedProcess)
            if schema and not (_result_matches(result, schema) or cli_result):
                raise ValueError("原实际返回不符合核对的结果类型；后继不使能")
            return dict(result=encoded, **self.identity(), timing=timing)
        except Exception as error:
            timing.setdefault("duration_ms", (time.perf_counter() - started) * 1000)
            kind = "PortGap" if isinstance(error, PortGap) else type(error).__name__
            return dict(error=str(error), error_type=kind, **self.identity(), timing=timing)
        finally:
            with self.lock:
                self.in_flight -= 1

    def check_call(self, body):
        """Pure pre-action check; never import/prepare/invoke original source."""
        self._identify(body)
        unit_id = body.get('unit')
        with self.lock:
            guarded = unit_id in getattr(self, 'guarded_units', set())
            if not self.accepting or (unit_id not in self.units and not guarded) or self.in_flight or self.preparing:
                code = ('WORKER_NONACCEPTING' if not self.accepting else
                        'MEMBER_MISSING' if unit_id not in self.units and not guarded else 'WORKER_BUSY')
                return dict(status='UNKNOWN', allowed=False, reason_code=code,
                            reason='worker/member is not accepting', **self.identity())
            # The pure check and a concurrent import/invoke must not observe
            # each other's partially changed original module state.
            from seampact.source.call_value_conditions import ordinary
            try:
                values = ordinary(body.get('arguments'))
                evidence = self.catalog.check_call_conditions(unit_id, values, instance_identity=self.identity())
                if guarded:
                    return self._check_guarded_worker(unit_id, values, evidence)
                return dict(evidence, **self.identity())
            except (ValueError, TypeError, KeyError) as error:
                return dict(status='UNKNOWN', allowed=False, reason=str(error), **self.identity())

    def _check_guarded_worker(self, unit_id, arguments, source_proof):
        """Read partial worker facts; neither this route nor H authorizes use."""
        if source_proof.get('status') != 'CONTROLLER_SOURCE_CHECKED':
            return dict(source_proof, **self.identity())
        from seampact.units import _native_context_lock
        from seampact.source.value_conditioned_process import verify_scope
        from seampact.source.value_conditioned_worker import check_worker_targets
        binding, entry = self.catalog.structural[unit_id]
        unit = self.store.get('unit', unit_id)
        structure = unit['structure_admission']
        proof = source_proof['source_proof']
        with binding.lock, _native_context_lock:
            process = verify_scope(self._guarded_scopes.get(unit_id), binding, unit, structure, self.identity())
            targets = check_worker_targets(binding, entry, unit, structure, proof, arguments, self.identity())
        return dict(status='UNKNOWN', allowed=False, can_start=False, reason_code='WORKER_BINDING_UNKNOWN',
            reason='guarded source facts do not grant original prefix, call or resource permission',
            source_proof=proof, process_proof=process, target_proof=targets, **self.identity())

    def import_only(self, body):
        """Run only the independently checked original import prefix."""
        self._identify(body)
        unit_id, plan_digest = body.get('unit'), body.get('plan_digest')
        with self.lock:
            if not self.accepting or unit_id not in self.units or self.in_flight or self.preparing:
                return dict(status='FAILED', reason='worker/member is not idle and accepting', **self.identity())
            self.in_flight += 1
            self.preparing += 1
        started = time.perf_counter()
        try:
            result = self.catalog.prepare_import_only(unit_id, plan_digest)
            return dict(result, **self.identity(), duration_ms=(time.perf_counter() - started) * 1000)
        except Exception as error:
            return dict(status='FAILED', reason=str(error), **self.identity(),
                        duration_ms=(time.perf_counter() - started) * 1000)
        finally:
            with self.lock:
                self.in_flight -= 1
                self.preparing -= 1

    def prewarm(self, body):
        self._identify(body)
        with self.lock:
            if not self.accepting:
                raise ProtocolError("实例已停止接单；不再接受预热")
            if self.in_flight and any((self.store.get('unit', uid) or {}).get('call_condition') for uid in body.get('units', [])):
                raise ProtocolError('conditional original owner is busy; preparation state is UNKNOWN')
            # Preparing may import code or construct the original owner. It is
            # physical work even without an /invoke request and must drain.
            self.in_flight += 1
            self.preparing += 1
        try:
            results = {}
            for unit_id in body.get("units", []):
                rule = self.prepare_rules.get(unit_id, {})
                if unit_id not in self.units:
                    results[unit_id] = dict(status="NOT_PREPARED", initialization_ready=False,
                                           reason=self.missing.get(unit_id) or "该成员未在本实例登记")
                    continue
                if rule.get("status") != "SUPPORTED":
                    results[unit_id] = dict(status="NOT_PREPARED", initialization_ready=False,
                                           reason=rule.get("basis") or "该成员不支持提前准备")
                    continue
                binding, entry = ((self.catalog.bindings[unit_id], self.catalog.entry_specs[unit_id])
                                  if unit_id in self.catalog.bindings else self.catalog.deferred[unit_id])
                unit = self.store.get("unit", unit_id)
                try:
                    self.catalog.require_initialization_conditions(unit_id)
                    with self.runtime.context(None):
                        facts = binding.prepare(entry, unit)
                    unit.update(initialization=facts["initialization_after"])
                    self.store.save("unit", unit_id, unit)
                    ready = facts["initialization_after"] == "BOUND"
                    results[unit_id] = dict(status="PREPARED" if ready else "FAILED", scope=rule.get("scope"),
                                           initialization_ready=ready, **facts)
                    if not ready:
                        results[unit_id]["reason"] = "允许提前完成的初始化未达到 BOUND；不能标记为暖态"
                except Exception as error:
                    results[unit_id] = dict(status="FAILED", scope=rule.get("scope"),
                                           initialization_ready=False, reason=str(error))
            ready = bool(results) and all(row["status"] == "PREPARED" for row in results.values())
            return dict(**self.identity(), status="PREPARED" if ready else "FAILED", process_ready=True,
                        initialization_ready=ready, units=results)
        finally:
            with self.lock:
                self.in_flight -= 1
                self.preparing -= 1

    def drain(self, body):
        self._identify(body)
        with self.lock:
            self.accepting = False
            return dict(**self.identity(), accepting=False, in_flight=self.in_flight,
                        preparation_in_flight=self.preparing)

    def shutdown(self, body):
        self._identify(body)
        with self.lock:
            if self.accepting or self.in_flight:
                raise ProtocolError("实例仍在接单或有在途调用；先排空")
        if python_work_required(self.group.get("contract") or {}) and not python_work_quiescent(self._python_threads()):
            raise ProtocolError("原调用返回后仍有未归属的 Python 后台线程；结束操作尚未排空")
        if self.server is not None:
            threading.Thread(target=self.server.shutdown, daemon=True).start()
        return dict(**self.identity(), stopping=True)


def make_worker_server(worker, host, port):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def send(self, status, value):
            data = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/health":
                return self.send(200, worker.health())
            self.send(404, dict(error="没有这个运行组接口"))

        def do_POST(self):
            action = {"/invoke": worker.invoke, "/check_call": worker.check_call, "/import_only": worker.import_only,
                      "/prewarm": worker.prewarm, "/drain": worker.drain,
                      "/shutdown": worker.shutdown}.get(self.path)
            if action is None:
                return self.send(404, dict(error="没有这个运行组接口"))
            try:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                self.send(200, action(body))
            except Exception as error:
                self.send(400, dict(error=str(error), **worker.identity()))
    class Server(ThreadingHTTPServer):
        def process_request_thread(self, request, client_address):
            with worker.hosting_thread():
                super().process_request_thread(request, client_address)
    server = Server((host, port), Handler)
    worker.server = server
    return server


def main(argv=None):
    """Standalone image entrypoint; coordinator/server imports are unnecessary."""
    import argparse
    parser = argparse.ArgumentParser(description="SeamPact native runtime-group worker")
    parser.add_argument("--spec", required=True)
    parser.add_argument("--coordinator", required=True)
    parser.add_argument("--host", choices=["127.0.0.1", "0.0.0.0"], default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8880)
    parser.add_argument("--advertise-url")
    parser.add_argument("--instance-id")
    parser.add_argument("--generation", type=int, default=0)
    args = parser.parse_args(argv)
    os.umask(0o077)
    sys.stdout.reconfigure(encoding="utf-8")
    worker = Worker(args.spec, args.coordinator, args.instance_id, args.generation)
    server = make_worker_server(worker, args.host, args.port)
    if args.advertise_url:
        def serve():
            with worker.hosting_thread():
                server.serve_forever()
        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        try:
            json_request(worker.coordinator + "/api/workers/attach", dict(endpoint=args.advertise_url), timeout=120)
            if worker.guarded_units:
                from seampact.source.value_conditioned_process import bind_worker_birth
                for token in worker._guarded_scopes.values():
                    birth = bind_worker_birth(token)
                    if birth.get('status') != 'MANAGED_BIRTH_BOUND':
                        raise ValueError('normal managed worker birth is not independently checked')
            print("[SeamPact] worker_ready " + worker.instance, flush=True)
            thread.join()
        except KeyboardInterrupt:
            server.shutdown()
    else:
        print("[SeamPact] worker_ready " + worker.instance, flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    server.server_close()


if __name__ == "__main__":
    main()
