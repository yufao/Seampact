"""One original native process per checked runtime group; no session recovery."""

import json
import sys
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .deployment import REMOTE_PROFILE, json_request, binding_fingerprint
from .inspection import Inspector, digest, safe_path
from .runtime import OwnerRuntime, ProtocolError
from .store import Store
from .units import UnitCatalog, _matches


class RemoteRuntime:
    # Reuse the existing original-call wrapper: the coordinator remains central,
    # while consume -> original -> finish happen in this worker's native process.
    wrap = OwnerRuntime.wrap
    context = OwnerRuntime.context

    def __init__(self, coordinator, group, instance, session):
        self.coordinator, self.group, self.instance, self.session = coordinator, group, instance, session

    def call(self, method, *args, **kwargs):
        return json_request(self.coordinator + "/api/runtime/worker", dict(method=method, args=args, kwargs=kwargs,
            group_id=self.group, instance_id=self.instance, session=self.session))["result"]


class Worker:
    def __init__(self, spec_path, coordinator):
        if sys.version_info[:2] != (3, 11):
            raise ValueError("当前远程宿主只覆盖Python 3.11，请用生成的运行镜像")
        self.spec_path = Path(spec_path).resolve()
        self.spec = json.loads(self.spec_path.read_text(encoding="utf-8"))
        if self.spec.get("schema") != "native-worker/1":
            raise ValueError("未知运行组格式")
        self.group = self.spec["group"]
        root = self.spec_path.parent
        inspector = Inspector(root / "source", root / "snapshots", self.spec["request"])
        version = digest(json.dumps(inspector.request["files"], sort_keys=True).encode())
        if version != inspector.request["source_version"] or version != self.group["source_version"] or inspector.request.get("source_id") != self.group["source_id"]:
            raise ValueError("运行组的来源身份与冻结文件清单不一致")
        for record in inspector.files.values():
            if digest(safe_path(inspector.root, record["path"]).read_bytes()) != record["sha256"] or digest((inspector.snapshots / record["sha256"]).read_bytes()) != record["sha256"]:
                raise ValueError("运行镜像源码不属于冻结版本")
        self.instance = uuid.uuid4().hex
        self.coordinator = coordinator.rstrip("/")
        self.session = json_request(self.coordinator + "/api/runtime/session")["session"]
        self.runtime = RemoteRuntime(self.coordinator, self.group["id"], self.instance, self.session)
        self.store = Store(":memory:")
        self.catalog = UnitCatalog(root, self.store, self.runtime)
        # Recheck the same rules inside the actual host; edited bundles cannot
        # make an unsupported stateful entry remotely executable.
        report = self.catalog.inspect_candidate(inspector, self.spec["candidate"])
        if any(group["status"] != "REMOTE_CHECKED" for group in report["runtime_groups"]["groups"]):
            raise ValueError("此运行组未满足远程宿主规则")
        units = self.catalog.install(inspector, self.spec["candidate"], self.spec["analysis_id"], self.spec["installation_id"])
        if set(unit["id"] for unit in units) != set(self.group["units"]) or any(unit["status"] != "READY" for unit in units):
            raise ValueError("运行组成员与可绑定能力不一致")
        self.units = {unit["id"] for unit in units}

    def health(self):
        return dict(status="READY", profile=REMOTE_PROFILE["id"], instance_id=self.instance, session=self.session,
            group_id=self.group["id"], source_version=self.group["source_version"],
            binding_fingerprint=binding_fingerprint(self.spec),
            installation_id=self.spec["installation_id"], units=sorted(self.units), session_recovery=False)

    def invoke(self, body):
        if body.get("session") != self.session or body.get("instance_id") != self.instance or body.get("unit") not in self.units:
            raise ProtocolError("运行进程或调用入口已过期；不恢复原会话")
        try:
            with self.runtime.context(body.get("task_id")):
                result = self.catalog.invoke(body["unit"], body["arguments"])
            if type(result) not in {str, int, bool, type(None)}:
                raise ValueError("原返回未满足核对的标量接口；不转换原对象")
            if not _matches(result, self.store.get("unit", body["unit"])["result_schema"]):
                raise ValueError("原实际返回不符合核对的结果类型；后继不使能")
            return dict(result=result, instance_id=self.instance)
        except Exception as error:
            return dict(error=str(error), error_type=type(error).__name__, instance_id=self.instance)


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
            if self.path != "/invoke":
                return self.send(404, dict(error="没有这个运行组接口"))
            try:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                self.send(200, worker.invoke(body))
            except Exception as error:
                self.send(400, dict(error=str(error)))
    return ThreadingHTTPServer((host, port), Handler)
