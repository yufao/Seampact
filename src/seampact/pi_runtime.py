"""Local transport for the pinned Pi Agent core; Pi owns the Agent loop."""

import json
import os
import queue
import shutil
import subprocess
import threading
from collections import deque
from pathlib import Path


class PiHost:
    def run(self, initial, *, context, model, tool, event, control, identity):
        executable = shutil.which("node")
        entry = Path(__file__).with_name("pi_host") / "bridge.mjs"
        if not executable or not entry.is_file():
            raise RuntimeError("Pi 宿主未安装：需要 Node.js 和 pi_host/bridge.mjs")
        process = subprocess.Popen(
            [executable, str(entry)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
            bufsize=1, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0)
        incoming, errors = queue.Queue(), deque(maxlen=24)
        write_lock, stopping = threading.Lock(), threading.Event()

        def send(value):
            with write_lock:
                if process.poll() is not None:
                    raise RuntimeError("Pi 宿主进程已退出")
                process.stdin.write(json.dumps(value, ensure_ascii=False) + "\n")
                process.stdin.flush()

        def read():
            try:
                for line in process.stdout:
                    incoming.put(json.loads(line))
            except Exception as error:
                incoming.put(dict(type="bridge_error", error=str(error)))
            finally:
                incoming.put(dict(type="closed"))

        def read_errors():
            for line in process.stderr:
                errors.append(line.rstrip()[:1000])

        def cancel():
            while not stopping.wait(0.1):
                if control.cancelled.is_set():
                    try:
                        send(dict(type="abort"))
                    except (OSError, RuntimeError):
                        pass
                    return

        readers = [threading.Thread(target=read, daemon=True, name="seampact-pi-reader"),
                   threading.Thread(target=read_errors, daemon=True, name="seampact-pi-errors"),
                   threading.Thread(target=cancel, daemon=True, name="seampact-pi-cancel")]
        for reader in readers:
            reader.start()
        try:
            send(dict(type="start", **initial))
            while True:
                control.check()
                try:
                    value = incoming.get(timeout=0.2)
                except queue.Empty:
                    continue
                kind = value.get("type")
                if kind == "ready":
                    identity(value["identity"])
                elif kind == "event":
                    event(value["event"])
                elif kind == "request":
                    request_id, params = value["id"], value.get("params", {})
                    method = value["method"]
                    if method == "context":
                        result = context(params)
                    elif method == "model":
                        result = model(params, lambda text: send(dict(type="model_delta", id=request_id, text=text)))
                    elif method == "tool":
                        result = tool(params)
                    else:
                        raise RuntimeError("Pi 宿主请求了未知平台操作：" + str(method))
                    control.check()
                    send(dict(type="reply", id=request_id, result=result))
                elif kind == "done":
                    control.check()
                    if value.get("error"):
                        raise RuntimeError(str(value["error"]))
                    if value.get("aborted"):
                        raise RuntimeError("Pi 宿主中止，未形成完整结果")
                    return value
                elif kind in {"closed", "bridge_error"}:
                    raise RuntimeError(value.get("error") or "Pi 宿主提前退出：" + "\n".join(errors)[-4000:])
        finally:
            stopping.set()
            if process.poll() is None:
                try:
                    send(dict(type="abort"))
                    process.stdin.close()
                    process.wait(timeout=2)
                except (OSError, RuntimeError, subprocess.TimeoutExpired):
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
            for reader in readers:
                reader.join(timeout=0.3)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None and not stream.closed:
                    stream.close()
