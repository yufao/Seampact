"""What the instance manager needs from a capability package.

The instance controller in deployment.py owns identity, intake, in-flight
counts, draining and confirmed release. This module supplies the package side:
the operations an instance supports, its build artifact, the credentials passed
at start, and one call with incremental events and delivered files.
"""

import copy
import contextvars
import json
import os
import shutil
import time
import uuid
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from urllib.parse import quote
from urllib.request import Request, urlopen

from seampact.capability_contract import (
    MANIFEST, SOURCE_PREFIX, WORKER_PROFILE, check_bindings, digest, package_path, project_binding_path, secret_names,
)
from seampact.capability_worker import SPEC_SCHEMA
from seampact.hosting.environments import normalize_environment
from seampact.hosting.image_build import worker_dockerfile
from seampact.runtime import ProtocolError, _task_context
from seampact.store import now
from seampact.target_runtime import copy_core, core_identity, environment_selection

GROUP_KIND = "capability-package"
CONTRACT = "capability-instance-contract/1"
CORE_SCOPE = "capability_worker"
IMAGE_RUNTIME = dict(
    copies=["COPY package /work/package", "COPY worker.json /work/worker.json"],
    entrypoint=["python", "-B", "-m", "seampact.capability_worker", "--spec", "/work/worker.json",
                "--host", "0.0.0.0", "--port", "8890"])
_project_context = contextvars.ContextVar("seampact_capability_project", default=None)


@contextmanager
def project_scope(project_id):
    """Explicit platform scope for preparation/diagnostics outside a chat task."""
    project_binding_path(dict(form="project", project_id=project_id, area="data", read_only=True))
    token = _project_context.set(project_id)
    try:
        yield
    finally:
        _project_context.reset(token)


def check_project_scope(platform, group, task_id=None):
    """A worker bound to a project may only receive that project's calls."""
    projects = {binding["project_id"] for binding in group.get("bindings", {}).values()
                if binding.get("form") == "project"}
    if group.get("project_owner"):
        projects.add(group["project_owner"])
    if not projects:
        return
    if len(projects) != 1:
        raise ProtocolError("能力实例绑定的项目不唯一")
    task_id = _task_context.get() if task_id is None else task_id
    if task_id is not None:
        lease = platform.projects.task_scope(task_id)
        if not lease or lease.get("status") != "ACTIVE":
            raise ProtocolError("项目能力调用需要活动任务租约")
        project_id = lease["project_id"]
    else:
        project_id = _project_context.get()
        if project_id is None:
            raise ProtocolError("项目能力准备或诊断需要明确的项目范围")
    if projects != {project_id}:
        raise ProtocolError("能力实例与当前任务属于不同项目")
    if platform.projects.get(project_id).get("archived"):
        raise ProtocolError("项目已归档，无法调用项目能力")


class CapabilityError(RuntimeError):
    """The call reached the adapter and did not produce a deliverable result."""

    def __init__(self, message, *, kind, facts, trace=None):
        super().__init__(message)
        self.kind, self.facts, self.trace = kind, facts, trace


def core():
    return core_identity(CORE_SCOPE)


def group_identity(manifest, project_id=None, *, isolated_global=False):
    """Stable source/package identity, additionally scoped when project resources are bound."""
    identity = [manifest["source"].get("id"), manifest["name"]]
    if project_id is not None:
        identity.extend(["project", project_id])
    elif isolated_global:
        identity.append("global")
    return "cap-" + digest(identity)[:24]


def default_gpu_selection(group):
    """Return [] for a declared CPU-only package; None preserves backend selection.

    Device bindings can be explicit device resources or a named device
    parameter. A weight with no usable device binding is ambiguous, so it is
    not treated as either a CPU or GPU model. Explicit group placement always
    takes precedence over this default.
    """
    resources = group.get("resources", [])
    device_resources = [item for item in resources if item.get("kind") == "device" or
                        (item.get("kind") == "parameter" and item.get("id", "").lower() == "device")]
    values = []
    for item in device_resources:
        binding = group.get("bindings", {}).get(item["id"], {})
        value = binding.get("value") if binding.get("form") == "value" else None
        if not isinstance(value, str):
            return None
        device = value.strip().lower()
        if device == "cpu" or device.startswith("cpu:"):
            values.append("cpu")
        elif device in {"cuda", "gpu"} or device.startswith(("cuda:", "gpu:")):
            values.append("gpu")
        else:
            return None
    if values:
        return [] if all(value == "cpu" for value in values) else None
    return None if any(item.get("kind") == "weight" for item in resources) else []


def instance_contract(group_id, manifest, package, bindings):
    """The lifecycle operations one instance of this package supports.

    Derived from the package's own declarations and from whether every
    required resource is bound; an incomplete binding withholds start.
    """
    missing = check_bindings(manifest, bindings)
    instance = manifest["instance"]
    members = [capability["id"] for capability in manifest["capabilities"]]
    bound = not missing
    gap = "缺少必需资源绑定：" + ", ".join(missing)
    writable = any(binding.get("form") == "project" and not binding["read_only"] for binding in bindings.values())
    replicable = instance["replicable"] and not writable
    replica = ("可写项目索引由单个实例串行持有" if writable else
               "适配实现声明可独立复制" if replicable else "适配实现未声明可独立复制，保持单实例")

    def operation(supported, basis):
        return dict(status="SUPPORTED" if supported else "UNSUPPORTED", basis=basis)

    value = dict(schema=CONTRACT, group_id=group_id, package_digest=package, host_profile=WORKER_PROFILE,
        members=members, root_entries=members, missing_resources=missing, bindings_digest=digest(bindings),
        concurrency=1 if writable else instance["concurrency"], cancel=instance["cancel"],
        prepare={member: dict(status="SUPPORTED", scope="INITIALIZE",
                              basis="initialize(bindings) 创建实例状态，不执行能力") for member in members},
        lifecycle=dict(
            reclaim=dict(status="SUPPORTED", basis="实例状态由 initialize 创建、close 释放；在途调用结束后可结束并重建"),
            replicate=operation(replicable, replica)),
        operations=dict(
            start=operation(bound, "按能力包初始化新实例" if bound else gap),
            prewarm=operation(bound, "提前执行 initialize" if bound else gap),
            pause_intake=operation(True, "停止接受新调用，在途调用继续收尾"),
            drain_stop=operation(True, "在途调用结束后 close 并结束实例，由后端确认释放"),
            cold_restart=operation(bound, "释放后创建新实例，计入重新初始化成本" if bound else gap),
            add_replica=operation(bound and replicable, replica if bound else gap),
            remove_replica=operation(replicable, replica),
            live_restore=operation(False, "不保留进程现场")),
        replicas=dict(default=1, replicable=replicable))
    value["digest"] = digest(value)
    return value


class PackageSource:
    """A package's frozen source, in the form environment discovery and image building read."""

    def __init__(self, root, manifest):
        self.root = Path(root)
        self.files = {record["path"][len(SOURCE_PREFIX):]: dict(path=record["path"][len(SOURCE_PREFIX):],
                                                                 sha256=record["sha256"])
                      for record in manifest["files"] if record["path"].startswith(SOURCE_PREFIX)}
        self.request = dict(source_version=manifest["source"].get("version"))

    def data(self, path):
        data = package_path(self.root, SOURCE_PREFIX + path).read_bytes()
        if digest(data) != self.files[path]["sha256"]:
            raise ValueError("冻结源码内容已改变：" + path)
        return data


def worker_spec(group):
    """Everything an instance verifies and reports; its digest is the binding fingerprint."""
    return dict(schema=SPEC_SCHEMA, profile=WORKER_PROFILE, group_id=group["id"],
                installation_id=group["installation_id"], contract_digest=group["contract"]["digest"],
                package_digest=group["package_digest"], source_id=group.get("source_id"),
                source_version=group.get("source_version"),
                units=dict(zip(group["units"], group["entries"])), bindings=copy.deepcopy(group["bindings"]),
                platform_core=core())


def export_artifact(deployments, group, body):
    """Freeze package, bindings, environment and worker code into one build context."""
    platform = deployments.platform
    root, manifest = platform.capabilities.package(group["package_digest"])
    source = PackageSource(root, manifest)
    environment = normalize_environment((body or {}).get("environment") or manifest["environment"], source)
    destination = deployments.folder / (group["id"] + "-" + uuid.uuid4().hex[:8])
    (destination / "package").mkdir(parents=True)
    (destination / "source").mkdir()
    package_root, source_root = destination / "package", destination / "source"
    parents = {package_root, source_root}
    for record in manifest["files"]:
        data = package_path(root, record["path"]).read_bytes()
        if digest(data) != record["sha256"]:
            raise ValueError("已安装能力包的内容已改变：" + record["path"])
        # The source is always a separate, freshly created tree here. Resolve
        # that layout once rather than resolving both roots for every file.
        relative = record["path"]
        target = (source_root.joinpath(*PurePosixPath(relative[len(SOURCE_PREFIX):]).parts)
                  if relative.startswith(SOURCE_PREFIX) else package_root.joinpath(*PurePosixPath(relative).parts))
        if target.parent not in parents:
            target.parent.mkdir(parents=True, exist_ok=True)
            parents.add(target.parent)
        target.write_bytes(data)
    shutil.copyfile(root / MANIFEST, destination / "package" / MANIFEST)
    spec = worker_spec(group)
    copy_core(destination / "core" / "seampact", spec["platform_core"])
    fingerprint = digest(spec)
    image = "seampact-capability-" + group["id"][4:16] + ":" + digest([fingerprint, environment])[:16]
    (destination / "worker.json").write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
    (destination / "environment.json").write_text(json.dumps(environment, ensure_ascii=False, indent=2), encoding="utf-8")
    check_python = "import sys; assert '.'.join(map(str, sys.version_info[:2])) == " + repr(environment["python"])
    reuse = worker_dockerfile(destination, source, environment, check_python, runtime=IMAGE_RUNTIME)
    archive = shutil.make_archive(str(destination), "zip", root_dir=destination)
    record = dict(id=group["id"], kind=GROUP_KIND, status="ARTIFACT_READY", folder=str(destination), archive=archive,
        image=image, profile=WORKER_PROFILE, contract_digest=group["contract"]["digest"],
        binding_fingerprint=fingerprint, package_digest=group["package_digest"],
        installation_id=group["installation_id"], environment=environment,
        source_runtime_environment=environment_selection(environment), platform_core=spec["platform_core"],
        build_reuse=reuse, created_at=now(), source_version=group.get("source_version"),
        build=dict(status="NOT_RUN"), publish=dict(status="NOT_RUN"), deployment="NOT_APPLIED",
        copies_original_code=True, resource_downloads=0)
    with platform.source_lock, deployments.lock:
        deployments._artifact_guard(group["id"])
        with platform.store.transaction():
            current = platform.store.get("runtime_group", group["id"])
            if (not current or current.get("detached") or current.get("kind") != GROUP_KIND
                    or current.get("contract", {}).get("digest") != group["contract"]["digest"]
                    or any(current.get(name) != group.get(name) for name in
                           ("installation_id", "package_digest", "source_id", "source_version"))):
                raise ValueError("生成工件期间能力包安装已改变，请重新生成")
            current["runtime_environment"] = environment_selection(environment)
            platform.store.save("runtime_group", group["id"], current)
            platform.store.save("deployment", group["id"], record)
    return record


def credentials(platform, group):
    """Private values for the environment variables the bindings name; resolved at each start."""
    values = {}
    for binding in group["bindings"].values():
        name = binding.get("api_key_env") if binding["form"] == "service" else binding.get("env")
        if not name:
            continue
        connection = binding.get("connection_id")
        value = platform.profiles.connection_key(connection) if connection else os.environ.get(name, "")
        if not value:
            raise ValueError("实例所需凭据不可用：" + name)
        values[name] = value
    if sorted(values) != secret_names(group["bindings"]):
        raise ValueError("实例凭据与绑定声明不一致")
    return values


def resource_backend(platform, group, config):
    """Derive worker mounts from registered weights and managed project identities."""
    config = copy.deepcopy(config)
    mappings = copy.deepcopy(config.get("mounts", []))
    project_bound = bool(group.get("project_owner")) or any(
        binding.get("form") == "project" for binding in group.get("bindings", {}).values())
    # Backend mappings may name the platform's entire state directory. They
    # help translate container paths; project workers receive declared resources
    # only, never that broader state/workspace mount.
    mounts = [] if project_bound else config.setdefault("mounts", [])
    config["mounts"] = mounts

    def host_path(source, identity):
        source = Path(source).resolve()
        if Path("/.dockerenv").exists():
            candidates = [item for item in mappings if source.is_relative_to(Path(item["target"]))]
            mapping = max(candidates, key=lambda item: len(Path(item["target"]).parts), default=None)
            if mapping is None:
                raise ValueError("平台容器内的资源路径缺少 Docker 宿主映射：" + identity)
            return str(Path(mapping["source"]) / source.relative_to(mapping["target"]))
        return str(source)

    def add_mount(source, target, read_only):
        proposed = dict(source=source, target=target, read_only=read_only)
        for mount in mounts:
            if mount["target"] == target:
                if mount != proposed:
                    raise ValueError("能力资源挂载目标冲突：" + target)
                return
            if project_bound:
                left, right = PurePosixPath(target), PurePosixPath(mount["target"])
                if left.is_relative_to(right) or right.is_relative_to(left):
                    raise ValueError("能力资源挂载范围重叠：" + target)
        mounts.append(proposed)

    for binding in group.get("bindings", {}).values():
        if binding.get("form") == "project":
            target = project_binding_path(binding)
            if config["kind"] != "docker":
                raise ValueError("自动项目资料挂载需要 Docker 后端")
            source = platform.projects.project_mount(binding["project_id"], binding["area"], binding.get("namespace"))
            add_mount(host_path(source, binding["project_id"]), target, binding["read_only"])
            continue
        if binding.get("form") != "path" or not binding.get("resource_id"):
            if project_bound and binding.get("form") == "path":
                # An explicitly mapped weight keeps its existing read-only
                # mount; unrelated backend mounts are not propagated.
                mapping = next((item for item in mappings if item["target"] == binding["path"]), None)
                if mapping is None or mapping.get("read_only") is not True:
                    raise ValueError("项目能力的权重需要只读资源挂载：" + binding["path"])
                add_mount(mapping["source"], binding["path"], True)
            continue
        identity = binding["resource_id"]
        record = platform.store.get("weight_resource", identity)
        if record is None:
            raise ValueError("绑定的权重已不在资源登记中：" + identity)
        target = binding["path"]
        if any(mount["target"] == target for mount in mounts):
            continue
        if target != "/work/resources/" + identity or ".." in PurePosixPath(target).parts:
            raise ValueError("自动权重挂载须使用 /work/resources/<resource_id>；自定义路径需要明确映射")
        if config["kind"] != "docker":
            raise ValueError("自动本地权重挂载需要 Docker 后端；其他后端需提供对应卷")
        check = platform.resources.check_weight(identity)["check"]
        if check.get("status") != "present":
            raise ValueError("权重当前不可读取：" + identity + "（" + str(check.get("status")) + "）")
        if binding.get("sha256") and not any(item.get("sha256") == binding["sha256"]
                                             for item in check.get("files", [])):
            raise ValueError("绑定权重的内容身份已变化：" + identity)
        add_mount(host_path(record["path"], identity), target, True)
    return config


def _fetch_files(platform, record, call_id, files):
    delivered = []
    for item in files:
        url = record["endpoint"] + "/files/" + call_id + "/" + quote(item["path"])
        with urlopen(Request(url), timeout=600) as response:
            stored = platform.capabilities.artifacts.save(response, item["sha256"])
        delivered.append(dict(name=item["path"], sha256=item["sha256"], size=item["size"],
                              media_type=item["media_type"], artifact=stored))
    return delivered


def _post(url, body, timeout):
    request = Request(url, headers={"Content-Type": "application/json"},
                      data=json.dumps(body, ensure_ascii=False, allow_nan=False).encode())
    with urlopen(request, timeout=timeout) as response:
        return json.load(response)


def cancel(deployments, call_id):
    """Ask the instance to stop one in-flight call.

    The answer says whether the request was recorded. Whether the work really
    stopped is known only from that call's own terminal record.
    """
    with deployments.lock:
        record = deployments.instances.get(deployments.capability_calls.get(call_id))
    if record is None:
        return dict(requested=False, reason="调用不在途")
    try:
        answer = _post(record["endpoint"] + "/cancel", dict(call_id=call_id, **deployments._identity(record)), 30)
        deployments._check_identity(record, answer)
    except (OSError, ValueError, ProtocolError) as error:
        return dict(requested=False, reason="取消请求未送达：" + str(error))
    return dict(requested=answer.get("requested") is True, mode=answer.get("mode"), reason=answer.get("reason"))


def call(deployments, record, unit, arguments, on_event=None, *, preclaimed=False):
    """One call on one instance. A lost response is UNKNOWN and is never replayed."""
    platform = deployments.platform
    group = platform.store.get("runtime_group", record["group_id"])
    if group is None:
        raise ProtocolError("调用所属能力组已不存在")
    check_project_scope(platform, group)
    call_id = uuid.uuid4().hex
    identity = deployments._identity(record)
    final, dropped_events = None, 0

    def notify(kind, data):
        nonlocal dropped_events
        if on_event is None:
            return
        try:
            on_event(dict(call_id=call_id, unit_id=unit["id"], instance_id=record["id"], kind=kind, data=data))
        except Exception:
            # A listener fault must not abandon a running call.
            dropped_events += 1

    with deployments.lock:
        accepted = (record["state"] == "READY" and record.get("accepting") or
                    preclaimed and record["state"] == "DRAINING" and record.get("capability_reservations", 0) > 0)
        if not accepted or record.get("source_detached"):
            raise ProtocolError("实例已停止接单或来源已摘除，不能开始新的调用")
        if not preclaimed:
            record["in_flight"] += 1
        record["last_activity_wall"] = time.time()
        deployments.capability_calls[call_id] = record["id"]
    started = time.perf_counter()
    try:
        # The caller learns the call identity first, so it can request cancellation.
        notify("call_started", None)
        body = dict(unit=unit["id"], input=arguments, call_id=call_id, task_id=_task_context.get(), **identity)
        request = Request(record["endpoint"] + "/invoke", headers={"Content-Type": "application/json"},
                          data=json.dumps(body, ensure_ascii=False, allow_nan=False).encode())
        with urlopen(request) as response:
            for raw in response:
                if not raw.strip():
                    continue
                line = json.loads(raw)
                if line.get("type") == "event":
                    notify(line.get("kind"), line.get("data"))
                else:
                    final = line
        if final is None:
            raise ProtocolError("调用流在终态记录前结束")
        deployments._check_identity(record, final)
    except (OSError, ValueError, ProtocolError) as error:
        with deployments.lock:
            record.update(state="UNKNOWN", accepting=False, reason="调用响应未取得：" + str(error))
            deployments._event(record, "unknown", unit_id=unit["id"], reason=record["reason"])
            deployments._environment_change(record, "response_lost")
        platform.runtime.call("remote_unknown", record["group_id"], record["id"])
        raise ProtocolError("调用响应未取得；不重放，执行结果保留为 UNKNOWN：" + str(error)) from error
    finally:
        with deployments.lock:
            if not preclaimed:
                record["in_flight"] -= 1
            record["last_activity_wall"] = time.time()
            deployments.capability_calls.pop(call_id, None)
    elapsed = (time.perf_counter() - started) * 1000
    if final["type"] == "rejected":
        raise ProtocolError("实例拒绝该调用（未执行）：" + str(final.get("reason")))
    timing = final.get("timing") or {}
    worker_ms = timing.get("duration_ms")
    facts = dict(execution="REMOTE", call_id=call_id, group_id=record["group_id"], instance_id=record["id"],
        generation=record["generation"], session=record["session"], process_nonce=record["process_nonce"],
        outcome=final["type"], round_trip_ms=elapsed, worker_ms=worker_ms,
        transport_ms=elapsed - worker_ms if type(worker_ms) in (int, float) else None,
        execute_ms=timing.get("execute_ms"), initialization_ms=timing.get("initialization_ms"),
        initialization_before=timing.get("initialization_before"),
        initialization_after=timing.get("initialization_after"),
        startup_kind="WARM_REUSE" if timing.get("initialization_before") == "BOUND" else "COLD_INITIALIZE",
        cancel=final.get("cancel"), dropped_events=dropped_events)
    with deployments.lock:
        if facts["initialization_after"]:
            for member in record.get("units", []):
                record["initialization"][member] = facts["initialization_after"]
        record["observed_wall"] = time.time()
        deployments.unit_state[unit["id"]] = facts
        deployments._event(record, "call", unit_id=unit["id"], call_id=call_id,
                           status={"result": "RETURN", "error": "ERROR", "cancelled": "CANCELLED"}.get(final["type"], "UNKNOWN"),
                           round_trip_ms=elapsed, worker_ms=worker_ms,
                           initialization_before=facts["initialization_before"])
    if final["type"] == "cancelled":
        raise CapabilityError("调用已按请求取消，适配实现确认停止", kind="Cancelled", facts=facts)
    if final["type"] != "result":
        raise CapabilityError(str(final.get("error")), kind=final.get("error_type", "Error"), facts=facts,
                              trace=final.get("traceback"))
    files = []
    if final.get("files"):
        try:
            files = _fetch_files(platform, record, call_id, final["files"])
        except (OSError, ValueError) as error:
            raise CapabilityError("能力已执行，交付文件未取得：" + str(error), kind="DeliveryFailed", facts=facts) from error
        finally:
            try:
                _post(record["endpoint"] + "/calls/release", dict(call_id=call_id, **identity), 30)
            except (OSError, ValueError):
                pass
    return dict(output=final.get("output"), files=files, cancel=final.get("cancel")), facts
