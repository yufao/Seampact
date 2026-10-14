"""Local research prototype API and standard conversation UI."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


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
                elif path == "/api/harnesses":
                    result = platform.add_harness(body)
                elif path == "/api/analyze":
                    result = platform.analyze(body["harness_id"])
                elif path == "/api/harnesses/detach":
                    result = platform.detach_harness(body["harness_id"])
                elif path == "/api/harnesses/restore":
                    result = platform.restore_harness(body["harness_id"])
                elif path == "/api/chat":
                    result = platform.chat.start(body)
                elif path == "/api/deployments/export":
                    result = platform.deployments.export(body["group_id"], body)
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

    server = ThreadingHTTPServer((host, port), Handler)
    server.access_token = None
    server.public_port = public_port or server.server_port
    return server
