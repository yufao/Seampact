"""Worker process hosting one instance of a generated capability package.

The image contains this module, the capability contract, the package and its
frozen source. At start the worker verifies that the package bytes are the ones
the platform froze, then serves three lifecycle operations of the adapter:
initialize once, execute per call, close before the process ends. Instance
identity, intake and in-flight accounting follow the platform's instance
protocol; nothing here decides scheduling.
"""

import copy
import importlib.util
import json
import mimetypes
import os
import re
import shutil
import signal
import sys
import tempfile
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from seampact.capability_contract import (
    IDENTITY_PROTOCOL, WORKER_PROFILE, ContractError, check_bindings, digest, load_package,
    package_digest, validate_value,
)

SPEC_SCHEMA = "capability-worker/1"
CALL_ID = re.compile(r"[0-9a-f]{32}")


class WorkerError(RuntimeError):
    pass


class _Cancelled(Exception):
    """Raised inside the adapter by CallContext.stop_if_cancelled."""


def json_request(url, body=None, timeout=None):
    payload = None if body is None else json.dumps(body, ensure_ascii=False, allow_nan=False).encode()
    request = Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except HTTPError as error:
        try:
            detail = json.load(error).get("error")
        except ValueError:
            detail = None
        raise WorkerError(detail or str(error)) from error


class CallContext:
    """What one execution may use besides its input."""

    def __init__(self, capability, call_id, task_id, output_dir, emit, cancel):
        self.capability, self.call_id, self.task_id = capability, call_id, task_id
        self.output_dir = output_dir
        self._emit, self._cancel = emit, cancel

    def emit(self, kind, data=None):
        """Send incremental information to the caller while the call runs."""
        if not isinstance(kind, str) or not kind:
            raise TypeError("event kind must be a non-empty string")
        json.dumps(data, ensure_ascii=False, allow_nan=False)
        self._emit(kind, data)

    def cancelled(self):
        return self._cancel.is_set()

    def stop_if_cancelled(self):
        """Leave the execution at a point where stopping is safe."""
        if self._cancel.is_set():
            raise _Cancelled()


class CapabilityWorker:
    def __init__(self, spec_path, coordinator, instance_id=None, generation=0, source_root=None):
        self.spec_path = Path(spec_path).resolve()
        self.spec = json.loads(self.spec_path.read_text(encoding="utf-8"))
        if self.spec.get("schema") != SPEC_SCHEMA or self.spec.get("profile") != WORKER_PROFILE:
            raise ValueError("未知的能力实例规格；请用当前平台重新生成运行工件")
        root = self.spec_path.parent
        self.package_root = root / "package"
        self.source_root = Path(source_root).resolve() if source_root else root / "source"
        self.manifest = load_package(self.package_root, self.source_root)
        if package_digest(self.manifest) != self.spec["package_digest"]:
            raise ValueError("运行镜像中的能力包与冻结身份不一致")
        expected = self.manifest["environment"]["python"]
        if tuple(map(int, expected.split("."))) != tuple(sys.version_info[:2]):
            raise ValueError("能力包声明 Python " + expected + "，当前解释器为 " +
                             ".".join(map(str, sys.version_info[:2])))
        self.capabilities = {item["id"]: item for item in self.manifest["capabilities"]}
        self.units = dict(self.spec["units"])
        if sorted(self.units.values()) != sorted(self.capabilities):
            raise ValueError("实例规格登记的能力与能力包不一致")
        self.bindings = self.spec["bindings"]
        missing = check_bindings(self.manifest, self.bindings)
        if missing:
            raise ValueError("缺少必需资源绑定：" + ", ".join(missing))
        if instance_id is not None and not re.fullmatch(r"[0-9a-f]{32}", instance_id):
            raise ValueError("实例身份格式不正确")
        if type(generation) is not int or generation < 0:
            raise ValueError("实例代次必须是非负整数")
        self.instance = instance_id or uuid.uuid4().hex
        self.generation = generation
        self.process_nonce = uuid.uuid4().hex
        self.coordinator = coordinator.rstrip("/")
        self.session = json_request(self.coordinator + "/api/runtime/session", timeout=30)["session"]
        self.concurrency = self.manifest["instance"]["concurrency"]
        self.lock, self.initialize_lock = threading.Lock(), threading.Lock()
        self.accepting, self.in_flight, self.preparing = True, 0, 0
        self.state, self.initialization_error, self.initialization_ms = "NOT_RUN", None, None
        self.initialization_trace = None
        self.owner, self.closed, self.secrets = None, None, []
        self.calls = {}
        self.work = Path(tempfile.mkdtemp(prefix="seampact-calls-"))
        self.server = None
        lock = os.environ.get("SEAMPACT_ENVIRONMENT_LOCK")
        self.environment = dict(python=sys.version.split()[0],
            lock_sha256=digest(Path(lock).read_bytes()) if lock and Path(lock).is_file() else None)

    def identity(self):
        return dict(session=self.session, instance_id=self.instance, generation=self.generation,
                    process_nonce=self.process_nonce)

    def _identify(self, body):
        if any(body.get(key) != value for key, value in self.identity().items()):
            raise WorkerError("运行进程或调用入口已过期；不恢复原会话")

    # Lifecycle of the adapter object.

    def _resolve_bindings(self):
        """The values the adapter receives; secrets come from this process's environment."""
        values, secrets = {}, []
        for identity, binding in self.bindings.items():
            form = binding["form"]
            if form == "value":
                values[identity] = copy.deepcopy(binding["value"])
            elif form == "path":
                if not Path(binding["path"]).exists():
                    raise WorkerError("资源 " + identity + " 的路径在实例内不存在：" + binding["path"])
                values[identity] = binding["path"]
            elif form == "secret":
                value = os.environ.get(binding["env"], "")
                if not value:
                    raise WorkerError("资源 " + identity + " 的凭据未传入实例环境")
                values[identity] = value
                secrets.append(value)
            else:
                key = os.environ.get(binding["api_key_env"], "") if binding.get("api_key_env") else ""
                if binding.get("api_key_env") and not key:
                    raise WorkerError("资源 " + identity + " 的服务凭据未传入实例环境")
                values[identity] = dict(base_url=binding["base_url"], model=binding.get("model"), api_key=key,
                                        parameters=copy.deepcopy(binding.get("parameters") or {}))
                if key:
                    secrets.append(key)
        self.secrets = secrets
        return values

    def _redact(self, text):
        for value in sorted(self.secrets, key=len, reverse=True):
            text = text.replace(value, "[private credential]")
        return text

    def _adapter_module(self, name):
        """Load the generated adapter, and only from this verified package.

        The adapter's identity is the package manifest, not the platform
        code closure, so a same-named module elsewhere must not be picked up.
        """
        spec = importlib.util.find_spec(name)
        origin = Path(spec.origin).resolve() if spec is not None and spec.origin else None
        if origin is None or self.package_root.resolve() not in origin.parents:
            raise WorkerError("适配模块没有从本能力包加载：" + name)
        if name in sys.modules:
            return sys.modules[name]
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(name, None)
            raise
        return module

    def _initialize(self):
        """Import the adapter and create the instance's own state exactly once."""
        with self.initialize_lock:
            before = self.state
            if before == "BOUND":
                return dict(initialization_before=before, initialization_after=before, duration_ms=0.0)
            if before == "FAILED":
                raise WorkerError("实例初始化曾失败，需要启动新实例：" + str(self.initialization_error))
            started = time.perf_counter()
            try:
                bindings = self._resolve_bindings()
                for path in (str(self.source_root), str(self.package_root)):
                    if path not in sys.path:
                        sys.path.insert(0, path)
                implementation = self.manifest["implementation"]
                owner = getattr(self._adapter_module(implementation["module"]), implementation["symbol"])()
                methods = ["initialize", "close"] + [item.get("method", "execute") for item in self.capabilities.values()]
                absent = [name for name in methods if not callable(getattr(owner, name, None))]
                if absent:
                    raise WorkerError("适配类缺少生命周期或执行方法：" + ", ".join(absent))
                owner.initialize(bindings)
            except Exception as error:
                self.state = "FAILED"
                self.initialization_error = self._redact(type(error).__name__ + ": " + str(error))
                self.initialization_trace = self._redact(traceback.format_exc()[-4000:])
                raise
            self.owner, self.state = owner, "BOUND"
            self.initialization_ms = (time.perf_counter() - started) * 1000
            return dict(initialization_before=before, initialization_after="BOUND", duration_ms=self.initialization_ms)

    def close(self):
        """Release what initialize created. Callers first confirm nothing is in flight."""
        with self.initialize_lock:
            if self.closed is None:
                if self.owner is None:
                    self.closed = dict(status="NOT_INITIALIZED")
                else:
                    try:
                        self.owner.close()
                        self.closed = dict(status="CLOSED")
                    except Exception as error:
                        self.closed = dict(status="FAILED", reason=self._redact(type(error).__name__ + ": " + str(error)))
                    self.owner = None
                shutil.rmtree(self.work, ignore_errors=True)
            return self.closed

    # Requests.

    def health(self):
        resources = {}
        try:
            import resource
            resources["peak_rss_kb"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        except (ImportError, AttributeError):
            resources["peak_rss_kb"] = None
        return dict(status="READY", profile=WORKER_PROFILE, identity_protocol=IDENTITY_PROTOCOL, **self.identity(),
            group_id=self.spec["group_id"], source_version=self.spec.get("source_version"),
            installation_id=self.spec["installation_id"], contract_digest=self.spec["contract_digest"],
            package_digest=self.spec["package_digest"], binding_fingerprint=digest(self.spec),
            units=sorted(self.units), missing={}, accepting=self.accepting, in_flight=self.in_flight,
            preparation_in_flight=self.preparing, process_ready=True,
            initialization={unit: self.state for unit in self.units},
            initialization_ready={unit: self.state == "BOUND" for unit in self.units},
            initialization_error=self.initialization_error, initialization_ms=self.initialization_ms,
            environment=self.environment, resources=resources, session_recovery=False)

    def invoke(self, body, emit):
        """Run one call; return its terminal record. ``emit`` sends incremental records."""
        try:
            self._identify(body)
            capability = self.capabilities.get(self.units.get(body.get("unit")))
            if capability is None:
                raise WorkerError("该能力未在本实例登记")
            call_id = body.get("call_id")
            if not isinstance(call_id, str) or not CALL_ID.fullmatch(call_id):
                raise WorkerError("调用身份格式不正确")
        except WorkerError as error:
            return dict(type="rejected", reason=str(error), **self.identity())
        with self.lock:
            if not self.accepting:
                return dict(type="rejected", reason="实例已停止接单", **self.identity())
            if self.in_flight >= self.concurrency:
                return dict(type="rejected", reason="实例已达声明的并发上限", **self.identity())
            if call_id in self.calls:
                return dict(type="rejected", reason="调用身份已使用；不重放", **self.identity())
            call = self.calls[call_id] = dict(cancel=threading.Event(), folder=self.work / call_id)
            self.in_flight += 1
        started = time.perf_counter()
        timing, files = {}, []
        try:
            validate_value(body.get("input"), capability["input"], "input")
            timing.update(self._initialize())
            timing["initialization_ms"] = timing.pop("duration_ms")
            call["folder"].mkdir()
            context = CallContext(capability["id"], call_id, body.get("task_id"), call["folder"], emit, call["cancel"])
            executed = time.perf_counter()
            output = getattr(self.owner, capability.get("method", "execute"))(body["input"], context)
            timing["execute_ms"] = (time.perf_counter() - executed) * 1000
            output = self._check_output(capability, output)
            files = self._collect(call["folder"])
            if capability["output"]["kind"] == "files" and not files:
                raise WorkerError("能力声明交付文件，但本次调用没有写出文件")
            final = dict(type="result", output=output, files=files,
                         cancel=dict(requested=call["cancel"].is_set(), confirmed=False))
        except _Cancelled:
            final = dict(type="cancelled", cancel=dict(requested=True, confirmed=True))
        except ContractError as error:
            final = dict(type="error", error=str(error), error_type="ContractError")
        except Exception as error:
            final = dict(type="error", error=self._redact(str(error)), error_type=type(error).__name__,
                         traceback=self._redact(traceback.format_exc()[-4000:]))
        finally:
            with self.lock:
                self.in_flight -= 1
                if not files:
                    self._discard(call_id)
        timing.setdefault("initialization_before", self.state)
        timing.setdefault("initialization_after", self.state)
        timing["duration_ms"] = (time.perf_counter() - started) * 1000
        return dict(final, timing=timing, **self.identity())

    @staticmethod
    def _check_output(capability, output):
        declared = capability["output"]
        try:
            json.dumps(output, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as error:
            raise WorkerError("能力返回值不是 JSON 值：" + str(error)) from None
        if declared["kind"] == "text" and not isinstance(output, str):
            raise WorkerError("能力声明文本输出，实际返回不是文字")
        if declared["kind"] == "json" and "schema" in declared:
            validate_value(output, declared["schema"], "output")
        return output

    @staticmethod
    def _collect(folder):
        files = []
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                files.append(dict(path=path.relative_to(folder).as_posix(), sha256=digest(path.read_bytes()),
                                  size=path.stat().st_size,
                                  media_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream"))
        return files

    def _discard(self, call_id):
        call = self.calls.pop(call_id, None)
        if call is not None:
            shutil.rmtree(call["folder"], ignore_errors=True)

    def file_path(self, call_id, relative):
        call = self.calls.get(call_id)
        if call is None:
            raise WorkerError("没有这次调用的交付文件")
        folder = call["folder"].resolve()
        target = (folder / relative).resolve()
        if folder not in target.parents or not target.is_file():
            raise WorkerError("没有这个交付文件")
        return target

    def release_call(self, body):
        """The caller has taken (or abandoned) the files of a finished call."""
        self._identify(body)
        with self.lock:
            self._discard(body.get("call_id"))
        return dict(released=True, **self.identity())

    def cancel(self, body):
        """Record a cancellation request. Only the adapter can confirm that work stopped."""
        self._identify(body)
        call = self.calls.get(body.get("call_id"))
        if call is None:
            return dict(requested=False, reason="调用不在途", **self.identity())
        call["cancel"].set()
        return dict(requested=True, mode=self.manifest["instance"]["cancel"], **self.identity())

    def prewarm(self, body):
        self._identify(body)
        with self.lock:
            if not self.accepting:
                raise WorkerError("实例已停止接单；不再接受预热")
            # Importing and initializing is physical work and must drain like a call.
            self.in_flight += 1
            self.preparing += 1
        try:
            try:
                facts = dict(self._initialize(), status="PREPARED", initialization_ready=True)
            except Exception as error:
                facts = dict(status="FAILED", initialization_ready=False, initialization_after=self.state,
                             reason=self._redact(type(error).__name__ + ": " + str(error)),
                             traceback=self.initialization_trace)
            requested = [unit for unit in body.get("units", sorted(self.units))]
            units = {unit: dict(facts) if unit in self.units else dict(status="NOT_PREPARED", initialization_ready=False,
                                                                     reason="该能力未在本实例登记") for unit in requested}
            ready = bool(units) and all(row["status"] == "PREPARED" for row in units.values())
            return dict(**self.identity(), status="PREPARED" if ready else "FAILED", process_ready=True,
                        initialization_ready=ready, units=units)
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
                raise WorkerError("实例仍在接单或有在途调用；先排空")
        closed = self.close()
        if self.server is not None:
            threading.Thread(target=self.server.shutdown, daemon=True).start()
        return dict(**self.identity(), stopping=True, close=closed)


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
            parts = self.path.split("/", 3)
            if len(parts) == 4 and parts[1] == "files":
                try:
                    from urllib.parse import unquote
                    path = worker.file_path(parts[2], unquote(parts[3]))
                except WorkerError as error:
                    return self.send(404, dict(error=str(error), **worker.identity()))
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(path.stat().st_size))
                self.end_headers()
                with path.open("rb") as stream:
                    shutil.copyfileobj(stream, self.wfile)
                return
            self.send(404, dict(error="没有这个实例接口"))

        def stream_invoke(self, body):
            # One JSON record per line; the connection closes after the
            # terminal record. A stream that ends earlier carries no result.
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
            self.end_headers()
            writing = threading.Lock()

            def write(record):
                data = (json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n").encode()
                with writing:
                    try:
                        self.wfile.write(data)
                        self.wfile.flush()
                    except OSError:
                        pass

            write(worker.invoke(body, lambda kind, data: write(dict(type="event", kind=kind, data=data))))

        def do_POST(self):
            try:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            except (ValueError, TypeError, KeyError):
                return self.send(400, dict(error="请求须为 JSON 对象", **worker.identity()))
            if self.path == "/invoke":
                return self.stream_invoke(body)
            action = {"/prewarm": worker.prewarm, "/cancel": worker.cancel, "/calls/release": worker.release_call,
                      "/drain": worker.drain, "/shutdown": worker.shutdown}.get(self.path)
            if action is None:
                return self.send(404, dict(error="没有这个实例接口"))
            try:
                self.send(200, action(body))
            except Exception as error:
                self.send(400, dict(error=str(error), **worker.identity()))

    server = ThreadingHTTPServer((host, port), Handler)
    worker.server = server
    return server


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description="SeamPact capability package worker")
    parser.add_argument("--spec", required=True)
    parser.add_argument("--coordinator", required=True)
    parser.add_argument("--host", choices=["127.0.0.1", "0.0.0.0"], default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8890)
    parser.add_argument("--advertise-url")
    parser.add_argument("--instance-id")
    parser.add_argument("--generation", type=int, default=0)
    parser.add_argument("--source")
    args = parser.parse_args(argv)
    os.umask(0o077)
    sys.stdout.reconfigure(encoding="utf-8")
    worker = CapabilityWorker(args.spec, args.coordinator, args.instance_id, args.generation, args.source)
    # Repository code commonly resolves its files against the repository root.
    if worker.source_root.is_dir():
        os.chdir(worker.source_root)

    def interrupt(signum, frame):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, interrupt)
    server = make_worker_server(worker, args.host, args.port)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        if args.advertise_url:
            json_request(worker.coordinator + "/api/workers/attach", dict(endpoint=args.advertise_url), timeout=120)
        print("[SeamPact] capability_worker_ready " + worker.instance, flush=True)
        while thread.is_alive():
            thread.join(1.0)
    except KeyboardInterrupt:
        server.shutdown()
    finally:
        closed = worker.close()
        print("[SeamPact] capability_worker_closed " + closed["status"], flush=True)
        server.server_close()


if __name__ == "__main__":
    main()
