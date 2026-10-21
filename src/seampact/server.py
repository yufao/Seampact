"""Local research prototype API and standard conversation UI."""

import json
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


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
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = urlsplit(self.path).path
            try:
                if path == "/health":
                    return self.send(200, dict(status="ok", version=platform.state()["version"]))
                if path == "/api/state":
                    return self.send(200, platform.state())
                if path == "/api/runtime/session":
                    return self.send(200, dict(session=platform.deployments.session))
                if path.startswith("/api/analysis/"):
                    return self.send(200, platform.store.get("analysis", path.rsplit("/", 1)[1]))
                if path.startswith("/api/preprocessing/"):
                    return self.send(200, platform.store.get("preprocessing", path.rsplit("/", 1)[1]))
                if path.startswith("/api/scheduler/runs/"):
                    return self.send(200, platform.training.detail(path.rsplit("/", 1)[1]))
                filename = {"/": "index.html", "/inspect": "index.html", "/app.js": "app.js", "/style.css": "style.css"}.get(path)
                if not filename:
                    return self.send(404, dict(error="没有这个接口"))
                mime = {"index.html": "text/html", "app.js": "text/javascript", "style.css": "text/css"}[filename]
                self.send(200, (ui / filename).read_bytes(), mime + "; charset=utf-8")
            except Exception as error:
                self.send(500, dict(error=str(error)))

        def do_POST(self):
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode())
                path = urlsplit(self.path).path
                if path == "/api/models":
                    platform.profiles.save_role(body["role"], body)
                    result = platform.profiles.public_roles()
                elif path == "/api/models/select":
                    result = platform.profiles.select_role(body["role"], body["connection_id"])
                elif path == "/api/connections":
                    result = platform.profiles.save_connection(body)
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
                elif path == "/api/units/policy":
                    result = platform.catalog.scheduling_policy(body)
                elif path == "/api/units/capability":
                    result = platform.catalog.set_capability(body)
                elif path == "/api/analyze":
                    result = platform.analyze(body["harness_id"], body.get("preprocessing", "api"),
                                              body.get("preprocessing_id"))
                elif path == "/api/analysis/resume":
                    result = platform.resume_analysis(body["analysis_id"])
                elif path == "/api/harnesses/detach":
                    result = platform.detach_harness(body["harness_id"])
                elif path == "/api/harnesses/clear":
                    result = platform.clear_pool()
                elif path == "/api/harnesses/restore":
                    result = platform.restore_harness(body["harness_id"])
                elif path == "/api/harnesses/configure":
                    result = platform.configure_harness(body)
                elif path == "/api/chat":
                    result = (platform.training.start_chat(body) if "scene_task_id" in body
                              else platform.chat.start(body))
                elif path == "/api/scheduler/config":
                    result = platform.training.configure(body)
                elif path == "/api/scheduler/runs":
                    result = platform.training.start(body)
                elif path == "/api/scheduler/runs/stop":
                    result = platform.training.stop(body["run_id"])
                elif path == "/api/scheduler/activate":
                    result = platform.training.activate(body["run_id"])
                elif path == "/api/analysis/export":
                    result = platform.export_analysis(body["analysis_id"])
                elif path == "/api/analysis/import":
                    result = platform.import_analysis(body)
                elif path == "/api/deployments/backend":
                    result = platform.deployments.configure_backend(body)
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
                else:
                    return self.send(404, dict(error="没有这个接口"))
                self.send(200, result)
            except Exception as error:
                self.send(400, dict(error=str(error)))

    server = PlatformHTTPServer((host, port), Handler)
    server.access_token = None
    server.public_port = public_port or server.server_port
    return server
