"""Bounded Python execution in a locally registered immutable Docker image."""

import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import subprocess
import threading
import time
import uuid

from .providers import RequestCancelled

_IMAGE = re.compile(r"(?:sha256:[0-9a-f]{64}|[^\s@]+@sha256:[0-9a-f]{64})\Z")
_MAX_OUTPUT = 65536
ISOLATION_RECEIPT = "seampact.python-isolation-receipt/1"
_SANDBOX_FIELDS = ("NetworkMode", "ReadonlyRootfs", "Privileged", "CapAdd", "CapDrop", "SecurityOpt",
                   "NanoCpus", "Memory", "PidsLimit", "LogConfig", "Tmpfs")
_INSPECT_CREATED = ('{"Id":{{json .Id}},"Name":{{json .Name}},"Image":{{json .Image}},'
    '"State":{{json .State}},"Config":{"Cmd":{{json .Config.Cmd}},'
    '"OpenStdin":{{json .Config.OpenStdin}},"WorkingDir":{{json .Config.WorkingDir}}},"HostConfig":{' +
    ','.join('"' + key + '":{{json .HostConfig.' + key + '}}' for key in _SANDBOX_FIELDS) +
    '},"Mounts":{{json .Mounts}}}')


def mount_source_identity(value):
    """Compare native Windows binds with Docker Desktop's explicit host mapping."""
    if not isinstance(value, str) or not value:
        raise ValueError("Docker mount source must be an absolute path")
    windows = PureWindowsPath(value)
    if re.fullmatch(r"[A-Za-z]:", windows.drive) and windows.is_absolute():
        if ".." in windows.parts:
            raise ValueError("Docker mount source cannot traverse parents")
        return ("windows", windows.drive[0].lower(), *(part.casefold() for part in windows.parts[1:]))
    path = PurePosixPath(value)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("Docker mount source must be an absolute path")
    for prefix in (PurePosixPath("/run/desktop/mnt/host"), PurePosixPath("/host_mnt")):
        if path.is_relative_to(prefix):
            parts = path.relative_to(prefix).parts
            if parts and re.fullmatch(r"[A-Za-z]", parts[0]):
                return ("windows", parts[0].lower(), *(part.casefold() for part in parts[1:]))
    return ("posix", *path.parts[1:])


def _create_failure(result):
    """Retain actionable Docker failure categories without exposing daemon output."""
    text = (result.stderr or b"").decode("utf-8", "replace").lower()
    if "bind source path does not exist" in text:
        reason = "Docker bind source path does not exist"
    elif "invalid mount config" in text:
        reason = "Docker rejected the bind mount configuration"
    elif "permission denied" in text:
        reason = "Docker denied access to a required resource"
    elif "no such image" in text:
        reason = "Docker image does not exist on the selected daemon"
    else:
        reason = "Docker create failed; inspect the daemon locally"
    return dict(exit_code=result.returncode, reason=reason)


def _daemon_path(source, mounts, *, writable):
    """Translate a platform-container path through its verified Docker mount."""
    source = PurePosixPath(source)
    if not source.is_absolute() or ".." in source.parts:
        raise ValueError("隔离执行的源路径必须是绝对路径")
    candidates = []
    for mount in mounts:
        target = mount.get("Destination")
        if not isinstance(target, str):
            continue
        target = PurePosixPath(target)
        if target.is_absolute() and source.is_relative_to(target):
            candidates.append((len(target.parts), target, mount))
    if not candidates:
        raise ValueError("隔离执行源路径没有平台容器的 Docker 宿主挂载")
    candidates.sort(key=lambda row: row[0], reverse=True)
    if len(candidates) > 1 and candidates[0][0] == candidates[1][0]:
        raise ValueError("隔离执行源路径的 Docker 挂载不唯一")
    _, target, mapping = candidates[0]
    host = mapping.get("Source")
    if (mapping.get("Type") not in ("bind", "volume") or not isinstance(host, str)
            or not PurePosixPath(host).is_absolute() or ".." in PurePosixPath(host).parts
            or (writable and not mapping.get("RW"))):
        raise ValueError("隔离执行源路径的 Docker 宿主挂载不可用")
    return str(PurePosixPath(host) / source.relative_to(target))


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

    def _mount_sources(self, mounts):
        if not Path("/.dockerenv").exists():
            return mounts
        identity = os.environ.get("SEAMPACT_CONTAINER_NAME") or os.environ.get("HOSTNAME")
        if not identity:
            raise ValueError("无法确定平台容器，不能安全映射隔离执行路径")
        result = self._command("inspect", "--format", "{{json .Mounts}}", identity)
        if result.returncode:
            raise ValueError("无法核实平台容器的 Docker 宿主挂载")
        try:
            own_mounts = json.loads(result.stdout)
            if not isinstance(own_mounts, list) or not all(isinstance(row, dict) for row in own_mounts):
                raise ValueError("invalid mount list")
            return [dict(mount, source=_daemon_path(str(Path(mount["source"]).resolve(strict=True)),
                         own_mounts, writable=not mount["read_only"])) for mount in mounts]
        except (KeyError, TypeError, OSError, json.JSONDecodeError) as exc:
            raise ValueError("无法核实隔离执行的 Docker 宿主路径") from exc

    def _created_receipt(self, name, mounts):
        """Inspect the exact unstarted container, retaining no environment values.

        Absence, daemon errors and partial/mismatching receipts remain unknown:
        the original create request may still complete after this inspection.
        """
        try:
            image = self._command("image", "inspect", "--format", "{{.Id}}", self.image)
            actual_image = image.stdout.decode("ascii").strip()
            if image.returncode or not re.fullmatch(r"sha256:[0-9a-f]{64}", actual_image):
                return None
            result = self._command("inspect", "--format", _INSPECT_CREATED, name)
            if result.returncode:
                return None
            rows = json.loads(result.stdout)
            if isinstance(rows, dict):
                rows = [rows]
            if not isinstance(rows, list) or len(rows) != 1:
                return None
            row = rows[0]
            state, config, host = row["State"], row["Config"], row["HostConfig"]
            actual_mounts = {(m["Type"], mount_source_identity(m["Source"]), m["Destination"], m["RW"]) for m in row["Mounts"]}
            expected_mounts = {("bind", mount_source_identity(m["source"]), m["target"], not m["read_only"]) for m in mounts}
            tmpfs = host.get("Tmpfs") or {}
            options = set(tmpfs.get("/tmp", "").split(","))
            security = set(host.get("SecurityOpt") or [])
            valid = (row.get("Name") == "/" + name and row.get("Image") == actual_image
                    and isinstance(row.get("Id"), str) and re.fullmatch(r"[0-9a-f]{64}", row["Id"])
                    and state.get("Status") == "created" and state.get("Running") is False
                    and state.get("Pid") == 0 and state.get("StartedAt") == "0001-01-01T00:00:00Z"
                    and config.get("Cmd") == ["python", "-I", "-"] and config.get("OpenStdin") is True
                    and config.get("WorkingDir") == "/workspace"
                    and host.get("NetworkMode") == "none" and host.get("ReadonlyRootfs") is True
                    and host.get("Privileged") is False and not host.get("CapAdd")
                    and set(host.get("CapDrop") or []) == {"ALL"}
                    and security in ({"no-new-privileges"}, {"no-new-privileges=true"})
                    and host.get("NanoCpus") == 1000000000 and host.get("Memory") == 512 * 1024 * 1024
                    and host.get("PidsLimit") == 64 and (host.get("LogConfig") or {}).get("Type") == "none"
                    and set(tmpfs) == {"/tmp"} and options == {"rw", "nosuid", "nodev", "noexec", "size=64m"}
                    and len(row["Mounts"]) == len(mounts) == len(expected_mounts)
                    and actual_mounts == expected_mounts)
            if not valid:
                return None
            return dict(schema=ISOLATION_RECEIPT, verified_before_start=True, container_id=row["Id"],
                container_name=name, image=actual_image, command=config["Cmd"], working_directory=config["WorkingDir"],
                mounts=[{key: mount[key] for key in ("Type", "Source", "Destination", "RW")} for mount in row["Mounts"]],
                sandbox={key: host.get(key) for key in _SANDBOX_FIELDS})
        except (ValueError, KeyError, TypeError, UnicodeError, AttributeError):
            return None

    def _verified_created(self, name, mounts):
        return self._created_receipt(name, mounts) is not None

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
            daemon_mounts = self._mount_sources(mounts)
            for mount in daemon_mounts:
                args += ["--mount", "type=bind,src=" + mount["source"] + ",dst=" + mount["target"] +
                         (",readonly" if mount["read_only"] else "")]
            args += [self.image, "python", "-I", "-"]
            record["create_uncertain"] = True
            emit("python_execution", call_id=call_id, status="CREATING")
            try:
                created = self._command(*args)
            except ValueError:
                if not self._verified_created(name, daemon_mounts):
                    raise
                record["create_recovered"] = "verified_unstarted_container"
                emit("python_execution", call_id=call_id, status="CREATE_RECOVERED")
                created = None
            record["create_uncertain"] = False
            if created is not None and created.returncode:
                record["create_error"] = _create_failure(created)
                raise ValueError("无法创建隔离 Python 容器")
            control.check()
            receipt = self._created_receipt(name, daemon_mounts)
            if receipt is None:
                raise ValueError("无法核实隔离 Python 容器的实际镜像、挂载与执行限制")
            receipt.update(scope={key: scope[key] for key in ("project_id", "conversation_id", "task_id")},
                requested_mounts=[dict(mount) for mount in mounts], daemon_mounts=[dict(mount) for mount in daemon_mounts])
            record["isolation_receipt"] = receipt
            emit("python_execution", call_id=call_id, status="ISOLATION_VERIFIED")
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
                if record.get("create_uncertain"):
                    # A name without a verified create receipt is not authority
                    # to kill or remove a possibly unrelated container.
                    record["stopped"] = False
                else:
                    if self._running(name) is True:
                        self._command("kill", name)
                    record["stopped"] = self._running(name) is False
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
