"""Local research prototype API and standard conversation UI."""

import base64
import binascii
import json
import shutil
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from seampact import __version__
from seampact.hosting.operation_preflight import preflight
from seampact.observability.resource_observation import resolve_gpu_devices


class PlatformHTTPServer(ThreadingHTTPServer):
    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.allow_reuse_address = False
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def make_server(platform, port=8880, host="127.0.0.1", public_port=None):
    ui = Path(__file__).with_name("ui")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def send(self, status, value, mime="application/json; charset=utf-8"):
            data = json.dumps(value, ensure_ascii=False).encode() if isinstance(value, (dict, list)) else value
            try:
                self.send_response(status)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                # Navigation or a closed tab can discard a polling response.
                # The request's work is already done; do not write another
                # error response to the disconnected browser socket.
                self.close_connection = True

        def do_GET(self):
            path = urlsplit(self.path).path
            try:
                if path == "/health":
                    return self.send(200, dict(status="ok", version=__version__))
                if path == "/api/state":
                    return self.send(200, platform.state())
                if path == "/api/projects":
                    projects = [dict(item, status="ARCHIVED" if item.get("archived") else "ACTIVE")
                                for item in platform.projects.list()]
                    return self.send(200, dict(projects=projects, default_project_id="default"))
                if path == "/api/projects/application":
                    query = parse_qs(urlsplit(self.path).query)
                    return self.send(200, platform.project_applications.view(query.get("project_id", ["default"])[0]))
                if path == "/api/experiments/office/privacy":
                    from seampact.office_privacy import privacy_cases
                    return self.send(200, dict(cases=[{key: case[key] for key in
                        ("id", "family_id", "variant", "title")} for case in privacy_cases()],
                        transport="simulated_only", enabled_by_default=False))
                if path in {"/api/project-files", "/api/project-file"}:
                    query = parse_qs(urlsplit(self.path).query)
                    project_id = query.get("project_id", ["default"])[0]
                    conversation_id = query.get("conversation_id", [None])[0]
                    area = query.get("area", ["data"])[0]
                    relative = query.get("path", [""])[0]
                    if area not in {"data", "workspace"}:
                        raise ValueError("请选择项目资料或会话文件")
                    if path == "/api/project-files":
                        return self.send(200, platform.projects.list_files(project_id, conversation_id,
                                                                          area, relative))
                    # Keep managed cleanup from removing the file while it is sent.
                    with platform.projects.lock:
                        return self.send_file(platform.projects.file_path(project_id, conversation_id,
                                                                          area, relative))
                if path == "/api/generations":
                    return self.send(200, platform.generation.snapshot())
                if path.startswith("/api/generations/"):
                    return self.send(200, platform.generation.detail(path.rsplit("/", 1)[1]))
                if path == "/api/execution":
                    return self.send(200, platform.execution.status())
                if path == "/api/operations/preflight":
                    query = parse_qs(urlsplit(self.path).query)
                    return self.send(200, preflight(platform, query.get("operation", ["update"])[0],
                                                   query.get("source_id", [None])[0]))
                if path == "/api/monitor":
                    return self.send(200, platform.monitor.status())
                if path.startswith("/api/monitor/sessions/"):
                    query = parse_qs(urlsplit(self.path).query)
                    identity = path.split("/")[4]
                    if path.endswith("/export"):
                        format = query.get("format", ["json"])[0]
                        if format == "jsonl":
                            return self.send_record(platform.monitor.export_path(identity), identity + ".jsonl")
                        return self.send(200, platform.monitor.export(identity, format))
                    return self.send(200, platform.monitor.detail(identity,
                        offset=int(query["offset"][0]) if "offset" in query else None,
                        limit=int(query.get("limit", ["100"])[0])))
                if path == "/api/tasks":
                    query = parse_qs(urlsplit(self.path).query)
                    return self.send(200, platform.tasks(
                        before=int(query["before"][0]) if "before" in query else None,
                        limit=int(query.get("limit", ["30"])[0])))
                if path.startswith("/api/tasks/") and path.endswith("/activity"):
                    return self.send(200, platform.task_activity(path.split("/")[-2]))
                if path.startswith("/api/tasks/"):
                    return self.send(200, platform.task(path.rsplit("/", 1)[1]))
                if path == "/api/chats":
                    query = parse_qs(urlsplit(self.path).query)
                    limit, offset = int(query.get("limit", ["0"])[0]), int(query.get("offset", ["0"])[0])
                    if limit < 0 or offset < 0:
                        raise ValueError("limit 与 offset 不能为负数")
                    return self.send(200, platform.chat.conversations(query.get("q", [""])[0], limit or None,
                        offset, project_id=query.get("project_id", [None])[0]))
                if path.startswith("/api/chat/"):
                    parts = path.split("/")
                    if len(parts) == 5 and parts[4] == "events":
                        query = parse_qs(urlsplit(self.path).query)
                        after = int(query.get("after", ["0"])[0])
                        if after < 0:
                            raise ValueError("事件序号不能为负数")
                        return self.send(200, platform.chat.stream_events(parts[3], after=after))
                    if len(parts) == 5 and parts[4] == "task":
                        task = platform.store.get("task", parts[3])
                        if not task:
                            raise ValueError("没有这个任务")
                        return self.send(200, platform.chat.snapshot(task))
                if path.startswith("/api/chats/"):
                    return self.send(200, platform.chat.conversation(path.rsplit("/", 1)[1]))
                if path == "/api/runtime/session":
                    return self.send(200, dict(session=platform.deployments.session))
                if path.startswith("/api/artifacts/"):
                    identity = path.rsplit("/", 1)[1]
                    target = (platform.capabilities.artifacts.path(identity)
                              if len(identity) == 64 and set(identity) <= set("0123456789abcdef") else None)
                    if target is None or not target.is_file():
                        raise ValueError("没有这个交付文件")
                    return self.send_file(target)
                if path == "/api/images":
                    return self.send(200, dict(**platform.images.snapshot(), **platform.images.inventory()))
                if path.startswith("/api/analysis/revisions/"):
                    return self.send(200, dict(revision=platform.revision_detail(path.rsplit("/", 1)[1])))
                if path.startswith("/api/analysis/") and path.endswith("/snapshot"):
                    return self.send(200, platform.analysis_snapshot(path.split("/")[-2]))
                if path.startswith("/api/analysis/"):
                    return self.send(200, platform.store.get("analysis", path.rsplit("/", 1)[1]))
                if path.startswith("/api/preprocessing/") and path.endswith("/summary"):
                    return self.send(200, platform.preprocessing_summary(path.split("/")[-2]))
                if path.startswith("/api/preprocessing/"):
                    return self.send(200, platform.store.get("preprocessing", path.rsplit("/", 1)[1]))
                if path.startswith("/api/scheduler/runs/"):
                    query = parse_qs(urlsplit(self.path).query)
                    if query.get("view", ["full"])[0] == "summary":
                        return self.send(200, platform.training.progress(path.rsplit("/", 1)[1],
                            int(query.get("history_after", ["0"])[0]), int(query.get("curves_after", ["0"])[0])))
                    return self.send(200, platform.training.detail(path.rsplit("/", 1)[1]))
                filename = {"/": "index.html", "/inspect": "index.html", "/configuration": "index.html",
                            "/app.js": "app.js", "/style.css": "style.css"}.get(path)
                if not filename:
                    return self.send(404, dict(error="没有这个接口"))
                mime = {"index.html": "text/html", "app.js": "text/javascript", "style.css": "text/css"}[filename]
                self.send(200, (ui / filename).read_bytes(), mime + "; charset=utf-8")
            except FileNotFoundError:
                self.send(404, dict(error="没有这个项目或文件"))
            except ValueError as error:
                self.send(400, dict(error=str(error)))
            except Exception as error:
                self.send(500, dict(error=str(error)))

        def send_file(self, path):
            try:
                with path.open("rb") as stream:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(path.stat().st_size))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    shutil.copyfileobj(stream, self.wfile)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                self.close_connection = True

        def send_record(self, path, filename):
            # Freeze byte count for active records; future samples remain in the session.
            try:
                with path.open("rb") as stream:
                    remaining = path.stat().st_size
                    # An active recorder may be appending a sample. Export only
                    # whole JSONL records that existed at this observation.
                    end = remaining
                    while end:
                        begin = max(0, end - 65536)
                        stream.seek(begin)
                        block = stream.read(end - begin)
                        newline = block.rfind(b"\n")
                        if newline >= 0:
                            remaining = begin + newline + 1; break
                        end = begin
                    else:
                        remaining = 0
                    stream.seek(0)
                    self.send_response(200)
                    self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
                    self.send_header("Content-Disposition", 'attachment; filename="' + filename + '"')
                    self.send_header("Content-Length", str(remaining))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    while remaining:
                        block = stream.read(min(65536, remaining))
                        if not block: break
                        self.wfile.write(block); remaining -= len(block)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                self.close_connection = True

        def do_POST(self):
            try:
                path = urlsplit(self.path).path
                length = int(self.headers.get("Content-Length", 0))
                if length < 0 or path == "/api/project-files" and length > 23 * 1024 * 1024:
                    self.close_connection = True
                    return self.send(413, dict(error="单次文件上传最多16 MiB"))
                body = json.loads(self.rfile.read(length).decode())
                if not isinstance(body, dict):
                    raise ValueError("请求内容需要对象")
                retired = {
                    "/api/harnesses/bind-dependency", "/api/harnesses/check-dependency",
                    "/api/harnesses/environment", "/api/units/policy", "/api/units/capability",
                    "/api/analyze-update", "/api/analysis/resume", "/api/analysis/cancel",
                    "/api/analysis/revisions", "/api/analysis/revisions/save", "/api/analysis/revisions/check",
                    "/api/analysis/revisions/refine", "/api/analysis/revisions/install", "/api/analysis/revisions/feedback",
                    "/api/harnesses/configure", "/api/harnesses/delete", "/api/plans", "/api/analysis/import",
                    "/api/scheduler/config", "/api/scheduler/runs", "/api/scheduler/runs/stop", "/api/scheduler/activate"}
                if path in retired:
                    return self.send(410, dict(error="此旧执行入口已停用；请使用仓库生成、能力包和普通 Agent 入口。历史记录仍可读取。"))
                if path == "/api/projects":
                    result = platform.projects.create(body)
                elif path == "/api/projects/application":
                    result = platform.project_applications.configure(body.get("project_id", "default"), body)
                elif path == "/api/projects/cleanup":
                    result = platform.cleanup_project(body)
                elif path == "/api/tasks/workspace/reconcile":
                    result = platform.chat.reconcile_task_workspace(body)
                elif path == "/api/projects/archive":
                    result = platform.projects.archive(body.get("project_id", "default"))
                elif path == "/api/project-files/delete":
                    area = body.get("area", "data")
                    if area not in {"data", "workspace"}:
                        raise ValueError("请选择项目资料或会话文件")
                    result = platform.projects.delete_file(body.get("project_id", "default"),
                        body.get("conversation_id"), area, body.get("path", ""))
                elif path == "/api/project-files":
                    area = body.get("area", "data")
                    if area not in {"data", "workspace"}:
                        raise ValueError("请选择项目资料或会话文件")
                    encoded = body.get("content_base64")
                    if not isinstance(encoded, str):
                        raise ValueError("请选择要上传的文件")
                    try:
                        data = base64.b64decode(encoded, validate=True)
                    except (ValueError, binascii.Error):
                        raise ValueError("文件内容编码无效") from None
                    if len(data) > 16 * 1024 * 1024:
                        raise ValueError("单次文件上传最多16 MiB")
                    with platform.projects.lock:
                        # A browser upload must not race a model editing the same files.
                        project_id = body.get("project_id", "default")
                        platform.projects.require_idle(project_id, body.get("conversation_id") if area == "workspace" else None)
                        result = platform.projects.put_file(project_id, body.get("conversation_id"), area,
                                                            body.get("path", ""), data)
                elif path == "/api/models":
                    platform.profiles.save_role(body["role"], body)
                    result = platform.profiles.public_roles()
                elif path == "/api/models/select":
                    result = platform.profiles.select_role(body["role"], body["connection_id"])
                elif path == "/api/connections":
                    result = platform.profiles.save_connection(body)
                elif path == "/api/managed-services":
                    result = platform.managed_services.configure(body)
                elif path == "/api/credentials":
                    result = platform.profiles.save_credential(body)
                elif path == "/api/connections/check":
                    result = platform.resources.check_connection(body["connection_id"])
                elif path == "/api/connections/invoke":
                    result = platform.resources.invoke_connection(body)
                elif path == "/api/weights":
                    result = platform.resources.save_weight(body)
                elif path == "/api/resources/check":
                    result = platform.resources.check_weight(body["resource_id"])
                elif path == "/api/harnesses/bind-dependency":
                    result = platform.bind_dependency(body)
                elif path == "/api/harnesses/check-dependency":
                    result = platform.dependencies.check_fixed(body)
                elif path == "/api/harnesses":
                    result = platform.add_harness(body)
                elif path == "/api/harnesses/environment":
                    result = platform.configure_environment(body)
                elif path == "/api/capabilities/install":
                    result = platform.capabilities.install(body["path"], body.get("bindings"))
                elif path == "/api/capabilities/bind":
                    result = platform.capabilities.bind(body["group_id"], body["bindings"])
                    if result["availability"]["status"] != "NEEDS_INPUT":
                        result["preparation"] = platform.capabilities.prepare(body["group_id"])
                elif path == "/api/capabilities/prepare":
                    result = platform.capabilities.prepare(body["group_id"], body)
                elif path == "/api/capabilities/invoke":
                    from seampact.capability_diagnostics import invoke
                    result = invoke(platform, body)
                elif path == "/api/capabilities/detach":
                    result = platform.capabilities.detach(body["group_id"])
                elif path == "/api/units/policy":
                    result = platform.catalog.scheduling_policy(body)
                elif path == "/api/units/capability":
                    result = platform.catalog.set_capability(body)
                elif path == "/api/analyze":
                    result = platform.generation.start(body["harness_id"])
                elif path == "/api/generations":
                    result = platform.generation.start(body["source_id"],
                        improve_from=body.get("improve_from"), configuration=body.get("configuration"))
                elif path == "/api/generations/cancel":
                    result = platform.generation.cancel(body["id"])
                elif path == "/api/execution/config":
                    result = platform.execution.configure(body)
                elif path == "/api/analyze-update":
                    result = platform.analyze_update(body["harness_id"])
                elif path == "/api/analysis/resume":
                    result = platform.resume_analysis(body["analysis_id"])
                elif path == "/api/analysis/cancel":
                    result = platform.cancel_analysis(body["analysis_id"])
                elif path == "/api/analysis/revisions":
                    result = platform.create_revision(body)
                elif path == "/api/analysis/revisions/save":
                    result = platform.save_revision(body)
                elif path == "/api/analysis/revisions/check":
                    result = platform.check_revision(body)
                elif path == "/api/analysis/revisions/refine":
                    result = platform.refine_revision(body)
                elif path == "/api/analysis/revisions/install":
                    result = platform.install_revision(body)
                elif path == "/api/analysis/revisions/feedback":
                    result = platform.feedback_revision(body)
                elif path == "/api/harnesses/detach":
                    result = platform.detach_harness(body["harness_id"])
                elif path == "/api/harnesses/delete":
                    result = platform.delete_harness(body["harness_id"])
                elif path == "/api/harnesses/clear":
                    result = platform.clear_pool()
                elif path == "/api/harnesses/restore":
                    result = platform.restore_harness(body["harness_id"])
                elif path == "/api/harnesses/configure":
                    result = platform.configure_harness(body)
                elif path == "/api/chat":
                    if body.get("scene_task_id"):
                        raise ValueError("旧场景执行已停用，请直接提交普通 Agent 任务")
                    result = platform.chat.start(body)
                elif path == "/api/chat/cancel":
                    result = platform.chat.cancel(body["task_id"])
                elif path == "/api/experiments/office/native":
                    from seampact.office_experiment import start_native_task
                    result = start_native_task(platform, body)
                elif path == "/api/experiments/office/spreadsheet":
                    from seampact.office_spreadsheet import start_spreadsheet_task
                    result = start_spreadsheet_task(platform, body)
                elif path == "/api/experiments/office/spreadsheet/score":
                    from seampact.office_spreadsheet import apply_native_score
                    result = apply_native_score(platform, body)
                elif path == "/api/experiments/office/privacy":
                    from seampact.office_privacy import start_privacy_task
                    result = start_privacy_task(platform, body)
                elif path == "/api/experiments/office/pilot":
                    from seampact.office_pilot import handle
                    result = handle(platform, body)
                elif path == "/api/plans":
                    result = platform.chat.start_plan(body)
                elif path == "/api/monitor/start":
                    result = platform.monitor.start(body)
                elif path == "/api/monitor/stop":
                    result = platform.monitor.stop(body["session_id"])
                elif path == "/api/monitor/gpus":
                    result = platform.monitor.configure_gpus(body)
                elif path == "/api/gpus/probe":
                    if not isinstance(body, dict) or set(body) != {"devices"}:
                        raise ValueError("GPU 探查只接收 devices 列表")
                    devices = resolve_gpu_devices(body["devices"])
                    result = dict(devices=devices, gpu_uuids=[device["gpu_uuid"] for device in devices])
                elif path == "/api/deployments/gpus":
                    result = platform.deployments.configure_gpus(body)
                elif path == "/api/managed-services/gpus":
                    result = platform.managed_services.set_gpu_devices(body)
                elif path == "/api/tasks/delete":
                    result = platform.delete_tasks(body)
                elif path == "/api/chats/delete":
                    result = platform.chat.delete_conversation(body["conversation_id"])
                elif path == "/api/chats/restore":
                    result = platform.chat.restore_conversation(body["conversation_id"])
                elif path == "/api/scheduler/config":
                    result = platform.training.configure(body)
                elif path == "/api/scheduler/runs":
                    result = platform.training.start(body)
                elif path == "/api/scheduler/runs/stop":
                    result = (platform.training.stop(body["run_id"], after_windows=body["after_windows"])
                              if "after_windows" in body else platform.training.stop(body["run_id"]))
                elif path == "/api/scheduler/activate":
                    result = platform.training.activate(body["run_id"])
                elif path == "/api/analysis/export":
                    result = platform.export_analysis(body["analysis_id"])
                elif path == "/api/analysis/import":
                    result = platform.import_analysis(body)
                elif path == "/api/deployments/backend":
                    result = platform.deployments.configure_backend(body)
                elif path == "/api/images/prepare":
                    result = platform.images.prepare(body)
                elif path == "/api/images/configure":
                    result = platform.images.configure(body)
                elif path == "/api/images/begin":
                    result = platform.images.begin(body["retention_scope_id"])
                elif path == "/api/images/verify":
                    result = platform.images.verify(body["retention_scope_id"])
                elif path == "/api/images/close":
                    result = platform.images.close(body["retention_scope_id"])
                elif path == "/api/images/reconcile":
                    result = platform.images.reconcile(body)
                elif path == "/api/images/usage":
                    result = platform.images.usage(body)
                elif path == "/api/images/result":
                    result = platform.images.result(body)
                elif path == "/api/images/backup":
                    result = platform.images.backup(body)
                elif path == "/api/images/restore":
                    result = platform.images.restore(body)
                elif path == "/api/images/import-backup":
                    result = platform.images.import_backup(body)
                elif path == "/api/images/delete":
                    result = platform.images.delete(body)
                elif path == "/api/deployments/export":
                    result = platform.deployments.export(body["group_id"], body)
                elif path == "/api/deployments/build":
                    result = platform.deployments.build(body["group_id"], body)
                elif path == "/api/deployments/publish":
                    result = platform.deployments.publish(body["group_id"], body)
                elif path == "/api/deployments/start":
                    result = platform.deployments.start(body["group_id"], body)
                elif path == "/api/deployments/prewarm":
                    result = platform.deployments.prewarm(body["group_id"], body)
                elif path == "/api/deployments/pause":
                    result = platform.deployments.pause(body["group_id"], body)
                elif path == "/api/deployments/drain-stop":
                    result = platform.deployments.drain_stop(body["group_id"], body)
                elif path == "/api/deployments/clear-history":
                    result = platform.deployments.clear_history(body["group_id"], body)
                elif path == "/api/deployments/add-replica":
                    result = platform.deployments.add_replica(body["group_id"], body)
                elif path == "/api/deployments/remove-replica":
                    result = platform.deployments.remove_replica(body["group_id"], body)
                elif path == "/api/deployments/status":
                    result = platform.deployments.status(body["group_id"])
                elif path == "/api/workers/attach":
                    result = platform.deployments.attach(body["endpoint"])
                elif path == "/api/deployments/local":
                    result = platform.deployments.local(body["group_id"])
                elif path == "/api/runtime/worker":
                    result = platform.deployments.protocol(body)
                elif path == '/api/runtime/birth':
                    result = platform.deployments.managed_birth(body)
                else:
                    return self.send(404, dict(error="没有这个接口"))
                self.send(200, result)
            except FileNotFoundError:
                self.send(404, dict(error="没有这个项目或文件"))
            except Exception as error:
                self.send(400, dict(error=str(error)))

    server = PlatformHTTPServer((host, port), Handler)
    server.access_token = None
    server.public_port = public_port or server.server_port
    return server
