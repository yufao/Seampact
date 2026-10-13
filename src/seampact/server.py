"""Control panel with explicit container binding and authenticated local UI."""

import json
import hmac
import os
import secrets
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from . import __version__


def make_server(platform, port=8874, require_session=None, host="127.0.0.1", public_port=None):
    ui = Path(__file__).with_name("ui")
    if host not in {"127.0.0.1","0.0.0.0"}:
        raise ValueError("supported bind addresses are local loopback or explicit container wildcard")
    if public_port is not None and (type(public_port) is not int or not 1<=public_port<=65535):
        raise ValueError("public browser port must be in 1..65535")
    require_session = (os.name != "nt" or host=="0.0.0.0") if require_session is None else require_session
    if host=="0.0.0.0" and not require_session:
        raise ValueError("container/wildcard binding requires startup authentication")
    access_token = secrets.token_urlsafe(32) if require_session else None

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            # Request paths and bodies may contain credentials or private inputs.
            pass

        def send(self, status, value, mime="application/json; charset=utf-8"):
            data = json.dumps(value, ensure_ascii=False).encode() if isinstance(value, (dict, list)) else value
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(data)

        def origin_ok(self):
            # Localhost DNS rebinding and cross-site browser requests cannot mutate.
            ports={self.server.server_port,self.server.public_port}
            allowed = {f"{hostname}:{p}" for hostname in ("127.0.0.1","localhost") for p in ports}
            if self.headers.get("Host") not in allowed:
                return False
            origin = self.headers.get("Origin")
            return origin is None or origin in {"http://"+h for h in allowed}

        def session_ok(self):
            if not require_session:
                return True
            try:
                cookies = SimpleCookie(self.headers.get("Cookie", ""))
                token = cookies.get("seampact_session")
                return bool(token and hmac.compare_digest(token.value, access_token))
            except Exception:
                return False

        def do_GET(self):
            if not self.origin_ok():
                return self.send(403, dict(error="local same-origin access required"))
            path = urlsplit(self.path).path
            if path=="/health":
                # No source, credential, ledger or paid-call information leaks.
                return self.send(200,dict(status="ok",version=__version__,session_required=require_session))
            if require_session and path == "/":
                provided = parse_qs(urlsplit(self.path).query).get("access", [""])[0]
                if hmac.compare_digest(provided.encode(), access_token.encode()):
                    self.send_response(303)
                    self.send_header("Location", "/")
                    self.send_header("Set-Cookie", "seampact_session="+access_token+"; HttpOnly; SameSite=Strict; Path=/")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Referrer-Policy", "no-referrer")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if not self.session_ok():
                    return self.send(200, ('<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
                        '<title>SeamPact 登录</title><h1>SeamPact</h1><p>使用启动终端显示的本次访问令牌。</p>'
                        '<form method="get"><input type="password" name="access" autocomplete="off" required>'
                        '<button>进入平台</button></form></html>').encode(), "text/html; charset=utf-8")
            if not self.session_ok():
                return self.send(403, dict(error="startup access session required"))
            try:
                if path == "/api/state":
                    return self.send(200, platform.state())
                if path == "/api/events":
                    return self.send(200, platform.store.events())
                if path.startswith("/api/analysis/"):
                    job = platform.store.get("analysis", path.rsplit("/",1)[1])
                    return self.send(200 if job else 404, job or dict(error="analysis not found"))
                if path.startswith("/api/run/"):
                    job = platform.tasks.get(path.rsplit("/",1)[1])
                    return self.send(200 if job else 404, job or dict(error="task run not found"))
                if path.startswith("/api/package/"):
                    package = platform.store.get("package", path.rsplit("/",1)[1])
                    if not package:
                        return self.send(404, dict(error="package not found"))
                    return self.send(200, (platform.home / "packages" / package["archive"]).read_bytes(), "application/zip")
                static = {"/":"index.html", "/app.js":"app.js", "/style.css":"style.css"}.get(path)
                if static:
                    mime = {"index.html":"text/html", "app.js":"text/javascript", "style.css":"text/css"}[static]
                    return self.send(200, (ui / static).read_bytes(), mime + "; charset=utf-8")
                self.send(404, dict(error="not found"))
            except Exception:
                self.send(500, dict(error="read failed; inspect local status"))

        def do_POST(self):
            if not self.session_ok() or not self.origin_ok() or self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                return self.send(403, dict(error="same-origin JSON required"))
            try:
                # Control-message transport bound, unrelated to model context/use quotas.
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 2_000_000:
                    raise ValueError("invalid control message size")
                body = json.loads(self.rfile.read(size))
                path = urlsplit(self.path).path
                if path == "/api/freeze":
                    result = platform.freeze(body["source_id"], body["observation"])
                elif path == "/api/analyze":
                    result = dict(id=platform.start_analysis(body["request_id"], body["provider"]))
                elif path == "/api/cancel":
                    result = platform.analysis.cancel(body["id"])
                elif path == "/api/build":
                    result = platform.build(body["id"])
                elif path == "/api/profile":
                    platform.profiles.save(body["provider"], body)
                    result = platform.profiles.public()
                elif path == "/api/discover":
                    result = platform.profiles.discover(body["provider"])
                elif path == "/api/demo":
                    result = dict(id=platform.demo())
                elif path == "/api/run":
                    result = dict(id=platform.tasks.start(body))
                else:
                    return self.send(404, dict(error="not found"))
                self.send(200, result)
            except (KeyError, ValueError, TypeError) as error:
                self.send(400, dict(error=str(error)))
            except Exception:
                self.send(500, dict(error="operation failed; no automatic provider retry"))

    server = ThreadingHTTPServer((host, port), Handler)
    server.access_token = access_token
    server.public_port = public_port or server.server_port
    return server
