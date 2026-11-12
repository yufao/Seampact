"""Bounded Python execution in a locally registered immutable Docker image."""

import re
import subprocess
import threading
import time
import uuid

from .providers import RequestCancelled

_IMAGE = re.compile(r"(?:sha256:[0-9a-f]{64}|[^\s@]+@sha256:[0-9a-f]{64})\Z")
_MAX_OUTPUT = 65536


class DockerPythonExecutor:
    def __init__(self, image, docker="docker"):
        if not isinstance(image, str) or not _IMAGE.fullmatch(image):
            raise ValueError("Python 执行镜像必须使用固定的 SHA-256 镜像 ID 或摘要")
        self.image, self.docker = image, docker
        self.lock = threading.RLock()
        self.active = {}

    def _command(self, *args, timeout=15):
        try:
            return subprocess.run([self.docker, *args], capture_output=True, timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError("Docker 执行服务不可用或未响应") from exc

    def _running(self, name):
        result = self._command("ps", "-aq", "--no-trunc", "--filter", "name=^/" + name + "$")
        if result.returncode:
            return None
        if not result.stdout.strip():
            return False
        result = self._command("inspect", "--format", "{{.State.Running}}", name)
        if result.returncode:
            return None
        state = result.stdout.strip()
        return True if state == b"true" else False if state == b"false" else None

    def is_drained(self, task):
        for call in task.get("python_calls", []):
            if call.get("stopped") is True:
                continue
            # A timed-out create request may still be processed by the daemon.
            if call.get("create_uncertain"):
                return False
            try:
                if self._running(call["container"]) is not False:
                    return False
                call["stopped"] = True
                self._command("rm", "-f", call["container"])
            except (KeyError, ValueError):
                return False
        return True

    def cancel(self, call_id):
        with self.lock:
            state = self.active.get(call_id)
            if state is None:
                return dict(status="UNKNOWN")
            state["cancelled"] = True
            name = state["container"]
        try:
            self._command("kill", name)
            stopped = self._running(name) is False
            return dict(status="CONFIRMED" if stopped else "UNKNOWN")
        except ValueError:
            return dict(status="UNKNOWN")

    def run(self, projects, scope, code, timeout_s, *, control, task, emit):
        if not isinstance(code, str) or len(code.encode("utf-8")) > 131072:
            raise ValueError("Python 代码必须是最多 128 KiB 的文本")
        if type(timeout_s) is not int or not 1 <= timeout_s <= 120:
            raise ValueError("执行时限必须是 1 到 120 秒的整数")
        control.check()
        mounts = projects.container_mounts(scope["task_id"])
        with projects.lock:
            current = projects.task_scope(scope["task_id"])
            if not current or current.get("status") != "ACTIVE" or any(
                    current.get(k) != scope.get(k) for k in ("project_id", "conversation_id", "task_id")):
                raise ValueError("会话工作区没有本任务的有效租约")
        image = self._command("image", "inspect", self.image)
        if image.returncode:
            raise ValueError("固定的 Python 执行镜像尚未在本地登记；请先准备镜像")
        call_id = "python-" + uuid.uuid4().hex
        name = "seampact-" + call_id
        record = dict(call_id=call_id, container=name, stopped=False, status="STARTING")
        task.setdefault("python_calls", []).append(record)
        emit("python_execution", call_id=call_id, status="STARTING")
        with self.lock:
            self.active[call_id] = dict(container=name, cancelled=False)
        control.attach_call(call_id, self.cancel)
        proc = None
        try:
            control.check()
            args = ["create", "--name", name, "--interactive", "--network", "none", "--read-only",
                    "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--cpus", "1",
                    "--memory", "512m", "--pids-limit", "64", "--log-driver", "none",
                    "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=64m", "--workdir", "/workspace"]
            for mount in mounts:
                args += ["--mount", "type=bind,src=" + mount["source"] + ",dst=" + mount["target"] +
                         (",readonly" if mount["read_only"] else "")]
            args += [self.image, "python", "-I", "-"]
            record["create_uncertain"] = True
            emit("python_execution", call_id=call_id, status="CREATING")
            created = self._command(*args)
            record["create_uncertain"] = False
            if created.returncode:
                raise ValueError("无法创建隔离 Python 容器")
            control.check()
            proc = subprocess.Popen([self.docker, "start", "--attach", "--interactive", name],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            output = [bytearray(), bytearray()]
            truncated = [False, False]

            def read_stream(stream, index):
                while True:
                    block = stream.read(8192)
                    if not block:
                        break
                    room = max(0, _MAX_OUTPUT - len(output[index]))
                    output[index].extend(block[:room])
                    truncated[index] |= len(block) > room

            readers = [threading.Thread(target=read_stream, args=(stream, i), daemon=True)
                       for i, stream in enumerate((proc.stdout, proc.stderr))]
            for reader in readers:
                reader.start()

            def send_code():
                try:
                    proc.stdin.write(code.encode("utf-8"))
                    proc.stdin.close()
                except (BrokenPipeError, OSError):
                    pass

            threading.Thread(target=send_code, daemon=True).start()
            deadline = time.monotonic() + timeout_s
            timed_out = False
            while proc.poll() is None:
                if control.cancelled.is_set() or time.monotonic() >= deadline:
                    timed_out = not control.cancelled.is_set()
                    self.cancel(call_id)
                    break
                time.sleep(0.05)
            if proc.poll() is None:
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
            for reader in readers:
                reader.join(timeout=2)
            control.check()
            record["status"] = "TIMEOUT" if timed_out else "COMPLETED"
            return dict(status=record["status"], exit_code=proc.returncode,
                        stdout=bytes(output[0]).decode("utf-8", "replace"),
                        stderr=bytes(output[1]).decode("utf-8", "replace"),
                        stdout_truncated=truncated[0], stderr_truncated=truncated[1])
        except RequestCancelled:
            record["status"] = "CANCELLED"
            raise
        except Exception:
            record["status"] = "FAILED"
            raise
        finally:
            try:
                if self._running(name) is True:
                    self._command("kill", name)
                record["stopped"] = not record.get("create_uncertain") and self._running(name) is False
                if record["stopped"]:
                    self._command("rm", "-f", name)
            except ValueError:
                record["stopped"] = False
            emit("python_execution", call_id=call_id, status=record["status"], stopped=record["stopped"])
            control.tool_outcome(dict(call_id=call_id, outcome="result" if record["stopped"] else "unknown",
                cancel=dict(requested=control.cancelled.is_set(), confirmed=record["stopped"])))
            with control.lock:
                control.calls.pop(call_id, None)
            with self.lock:
                self.active.pop(call_id, None)
            if not record["stopped"]:
                raise RuntimeError("Python 容器状态未确认；已保留项目任务租约")
