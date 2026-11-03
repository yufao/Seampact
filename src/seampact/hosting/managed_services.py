"""Owned, single-model HTTP services. These are dependencies, never worker Units.

Docker argv execution reuses the existing backend runner. Only selected Docker
identity facts and probe outcomes are retained: engine output and credentials
are deliberately absent from public receipts.
"""

import copy
import hashlib
import json
import math
import os
import re
import threading
import time
import uuid
from contextlib import contextmanager
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from seampact.hosting import backends
from seampact.store import now
from seampact.runtime import ProtocolError

SCHEMA = "managed-model-service/1"
PROTOCOL = "openai-single-model/1"
IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")
NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
ENV = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def service_id(identity):
    return str(identity).removeprefix("service:")


def group_id(identity):
    return "service:" + service_id(identity)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False).encode()).hexdigest()


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        # Probe identity is tied to the frozen endpoint. A redirect must neither
        # move that identity nor forward its Authorization header elsewhere.
        return None


def _url_get(url, headers, timeout):
    with build_opener(_NoRedirect()).open(Request(url, headers=headers), timeout=timeout) as response:
        data = response.read(1024 * 1024)
        return response.status, json.loads(data) if data.strip() else None


class ManagedServices:
    def __init__(self, platform, runner=None, http_get=None):
        self.platform = platform
        self.deployments = platform.deployments
        self.lock = self.deployments.lock
        self.instances = self.deployments.instances
        self.run_locks = self.deployments.run_locks
        self.event_log = self.deployments.event_log
        self.listeners = self.deployments.listeners
        self.environment_changes = self.deployments.environment_changes
        self.runner = runner or backends.run
        self.http_get = http_get or _url_get
        self.leases = {}
        self.changed = threading.Condition(self.lock)
        self.waiters = {}
        # A controller restart cannot prove that direct HTTP callers finished.
        for item in self.instances.values():
            if item.get("target_kind") == "model-service" and not item.get("released"):
                item.update(state="UNKNOWN", accepting=False, reason="controller_restarted", busy_scope_unknown=True)

    def _command(self, argv, timeout=30):
        try:
            result = self.runner(argv, timeout)
            return result.get("returncode"), result.get("stdout", "")
        except Exception:
            return None, ""

    def _image(self, reference):
        status, output = self._command(["docker", "image", "inspect", "--format",
            '{"Id":{{json .Id}},"RepoDigests":{{json .RepoDigests}}}', reference])
        try:
            info = json.loads(output) if status == 0 else {}
            if IMAGE_ID.fullmatch(info.get("Id", "")):
                return info
        except (ValueError, TypeError):
            pass
        raise ValueError("无法核对本地 Docker 镜像内容身份；未下载镜像")

    def configure(self, body):
        """Freeze a locally inspectable Docker image and explicit service contract."""
        if not isinstance(body, dict):
            raise ValueError("服务规格须为 JSON 对象")
        value = copy.deepcopy(body)
        identity = service_id(value.get("id", ""))
        if getattr(self.platform, "images", None):
            self.platform.images.guard_artifact(group_id(identity))
        if not NAME.fullmatch(identity):
            raise ValueError("服务 id 须为小写字母数字及短横线，最多 40 字符")
        value["id"] = identity
        allowed = {"id", "schema", "protocol", "model", "model_identity", "weight_identity", "image",
            "image_id", "argv", "endpoint", "health_path", "models_path", "docker", "contract",
            "resource_envelope", "depends_on", "key_env", "start_timeout_s", "probe_timeout_s", "spec_digest"}
        if set(value) - allowed:
            raise ValueError("服务规格含未支持字段")
        if value.get("schema", SCHEMA) != SCHEMA:
            raise ValueError("未支持的服务规格 schema")
        if value.get("protocol", PROTOCOL) != PROTOCOL:
            raise ValueError("首版只支持 openai-single-model/1")
        for key in ("model", "model_identity", "weight_identity", "image"):
            if not isinstance(value.get(key), str) or not value[key].strip():
                raise ValueError("服务规格缺少 " + key)
        argv = value.get("argv")
        if not isinstance(argv, list) or not argv or any(not isinstance(arg, str) or "\x00" in arg for arg in argv):
            raise ValueError("argv 须为非空字符串列表")
        if any(re.search(r"(?:api[-_]?key|token|password|secret|credential)", arg, re.I) for arg in argv):
            raise ValueError("凭据须经 pass_env 变量名传入，不写入 argv")
        contract = value.get("contract", {})
        if not isinstance(contract, dict) or set(contract) - {"exclusive_control", "request_isolation", "rebuildable", "background_work", "control_owner", "max_concurrency"}:
            raise ValueError("contract 须为明确支持的服务契约对象")
        required = {"exclusive_control": True, "request_isolation": True, "rebuildable": True, "background_work": False}
        if (any(contract.get(key) is not expected for key, expected in required.items())
                or not isinstance(contract.get("control_owner"), str) or not contract["control_owner"].strip()):
            raise ValueError("服务须明确独占控制、请求隔离、可重建、无后台工作及控制所有者")
        if type(contract.get("max_concurrency")) is not int or contract["max_concurrency"] <= 0:
            raise ValueError("max_concurrency 须为正整数")
        docker = value.get("docker", {})
        if not isinstance(docker, dict) or set(docker) - {"network", "network_alias", "container_port", "publish_host", "publish_port", "gpus", "mounts", "pass_env"}:
            raise ValueError("docker 含未支持字段")
        if not isinstance(value.get("endpoint"), str):
            raise ValueError("endpoint 须为稳定 HTTP 地址")
        parsed = urlsplit(value["endpoint"])
        if parsed.scheme != "http" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("受管 Docker 服务须使用无凭据的稳定 HTTP endpoint")
        port = docker.get("container_port")
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("container_port 须为固定端口")
        if docker.get("network"):
            if "publish_host" in docker or "publish_port" in docker:
                raise ValueError("专用网络别名与固定端口映射须只选一种")
            for key in ("network", "network_alias"):
                if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", str(docker.get(key, ""))):
                    raise ValueError("network 与稳定 network_alias 必须明确")
            if parsed.hostname != docker["network_alias"] or (parsed.port or 80) != port:
                raise ValueError("endpoint 须匹配稳定网络别名及容器端口")
        else:
            host = docker.get("publish_host", "127.0.0.1")
            published = docker.get("publish_port")
            if host not in {"127.0.0.1", "localhost"} or type(published) is not int or not 1 <= published <= 65535:
                raise ValueError("无专用网络时须固定 loopback publish_host/publish_port")
            if parsed.hostname not in {host, "localhost", "127.0.0.1"} or (parsed.port or 80) != published:
                raise ValueError("endpoint 须匹配固定映射端口")
            docker["publish_host"] = host
        if (not isinstance(docker.get("pass_env", []), list)
                or any(not isinstance(name, str) or not ENV.fullmatch(name) for name in docker.get("pass_env", []))):
            raise ValueError("pass_env 只接受环境变量名")
        if value.get("key_env") is not None and (not isinstance(value["key_env"], str) or not ENV.fullmatch(value["key_env"])):
            raise ValueError("key_env 只接受环境变量名")
        if not isinstance(docker.get("mounts", []), list):
            raise ValueError("mounts 须为挂载列表")
        for mount in docker.get("mounts", []):
            if (not isinstance(mount, dict) or set(mount) - {"source", "target", "read_only"}
                    or type(mount.get("read_only", True)) is not bool or not isinstance(mount.get("source"), str)
                    or not isinstance(mount.get("target"), str) or not mount["target"].startswith("/")
                    or any("," in mount[key] or "\x00" in mount[key] for key in ("source", "target"))):
                raise ValueError("mounts 须有明确 source 和绝对容器 target")
            if not (mount["source"].startswith("/") or re.match(r"^[A-Za-z]:[/\\]", mount["source"])):
                raise ValueError("mount source 须为后端主机上的固定绝对路径")
        for key, default in (("health_path", "/health"), ("models_path", "/v1/models")):
            value[key] = value.get(key, default)
            if not isinstance(value[key], str) or not value[key].startswith("/") or "?" in value[key] or "#" in value[key]:
                raise ValueError("探针须为固定绝对 HTTP 路径")
        for key, default in (("start_timeout_s", 900), ("probe_timeout_s", 5)):
            value[key] = value.get(key, default)
            if type(value[key]) not in (int, float) or not math.isfinite(value[key]) or value[key] <= 0:
                raise ValueError(key + " 须为正数")
        envelope = value.get("resource_envelope", {})
        if (not isinstance(envelope, dict) or set(envelope) - {"host_memory_mb", "devices", "basis"}
                or not isinstance(envelope.get("basis"), str) or not envelope["basis"].strip()
                or type(envelope.get("host_memory_mb")) not in (int, float)
                or not math.isfinite(envelope["host_memory_mb"]) or envelope["host_memory_mb"] <= 0):
            raise ValueError("须提供有依据的 host_memory_mb 资源包络")
        devices = envelope.get("devices")
        if not isinstance(devices, dict):
            raise ValueError("resource_envelope.devices 须明确列设备，CPU 服务可为空")
        for device, facts in devices.items():
            memory = facts.get("memory_mb") if isinstance(facts, dict) else None
            if (not isinstance(device, str) or not device or not isinstance(facts, dict) or set(facts) != {"memory_mb"}
                    or type(memory) not in (int, float) or not math.isfinite(memory) or memory <= 0):
                raise ValueError("每个设备须有正数 memory_mb 上界")
        if not isinstance(docker.get("gpus", ""), str):
            raise ValueError("gpus 须为显式 device=<设备列表> 字符串")
        selected = docker.get("gpus", "").strip('"')
        if selected:
            if not selected.startswith("device="):
                raise ValueError("gpus 须为明确 device=<设备列表>，不能使用 all 或数量推断设备")
            device_ids = selected.removeprefix("device=").split(",")
            if (any(not re.fullmatch(r"[A-Za-z0-9_.:-]+", device) for device in device_ids)
                    or len(set(device_ids)) != len(device_ids) or set(device_ids) != set(devices)):
                raise ValueError("Docker 设备列表须与资源包络设备集合一致")
            # Docker's --gpus argument is CSV; multiple device names need one
            # quoted device field even though subprocess itself uses no shell.
            docker["gpus"] = '"' + selected + '"' if len(device_ids) > 1 else selected
        elif devices:
            raise ValueError("GPU 包络需要对应的显式 Docker 设备选择")
        dependencies = value.get("depends_on", [])
        if not isinstance(dependencies, list) or any(not isinstance(item, str) or not NAME.fullmatch(service_id(item)) for item in dependencies):
            raise ValueError("depends_on 须为服务 id 列表")
        value["depends_on"] = list(dict.fromkeys(service_id(item) for item in dependencies))
        with self.lock:
            if self._held(identity) or group_id(identity) in self.run_locks:
                raise ValueError("服务仍被运行使用或未确认释放，不能修改规格")
            def network_target(spec):
                settings = spec["docker"]
                if settings.get("network"):
                    return ("network", settings["network"], settings["network_alias"], settings["container_port"])
                # localhost and 127.0.0.1 denote the same supported binding.
                return ("publish", "127.0.0.1", settings["publish_port"])
            def endpoint_origin(endpoint):
                address = urlsplit(endpoint)
                host = "127.0.0.1" if address.hostname == "localhost" else address.hostname
                return address.scheme, host, address.port or 80
            for existing in self.platform.store.list("managed_service"):
                if existing["id"] != identity and (endpoint_origin(existing["endpoint"]) == endpoint_origin(value["endpoint"])
                        or network_target(existing) == network_target(value)):
                    raise ValueError("不同服务不能复用同一网络目标；共用同一实例请让连接引用同一个 service id")
            def visit(current, trail):
                if current in trail:
                    raise ValueError("服务依赖含环")
                spec = value if current == identity else self.get(current)
                if spec is None:
                    raise ValueError("服务依赖尚未登记")
                for dependency in spec.get("depends_on", []):
                    visit(dependency, trail | {current})
            visit(identity, set())
            image = self._image(value["image"])
            requested = value["image"]
            if "@sha256:" in requested and requested not in (image.get("RepoDigests") or []):
                raise ValueError("镜像仓库摘要与实际 inspect 不匹配")
            if IMAGE_ID.fullmatch(requested) and requested != image["Id"]:
                raise ValueError("镜像内容摘要不匹配")
            value.update(schema=SCHEMA, protocol=PROTOCOL, image_id=image["Id"], docker=docker)
            value.pop("spec_digest", None)
            value["spec_digest"] = _digest(value)
            self.platform.store.save("managed_service", identity, value)
            self.platform.store.save("runtime_group", group_id(identity), self._group(value))
            return copy.deepcopy(value)

    def get(self, identity):
        value = self.platform.store.get("managed_service", service_id(identity))
        if value is None:
            raise ValueError("没有这个受管模型服务")
        return value

    def _spec(self, identity):
        value = self.get(identity)
        if value is None:
            raise ValueError("没有这个受管模型服务")
        return value

    def _group(self, spec):
        supported = {name: dict(status="SUPPORTED", basis="explicit managed single-model service contract")
                     for name in ("start", "prewarm", "drain_stop")}
        supported.update({name: dict(status="UNSUPPORTED", basis="single frozen instance; replication not implemented")
                          for name in ("add_replica", "remove_replica", "pause_intake")})
        return dict(id=group_id(spec["id"]), target_kind="model-service", managed_service_id=spec["id"],
            units=[], entries=[], source_id=None, source_version=spec["spec_digest"], resource_envelope=spec["resource_envelope"],
            contract=dict(schema="managed-service-contract/1", host_profile=PROTOCOL,
                          digest=spec["spec_digest"], operations=supported, prepare={}, replicas=dict(replicable=False),
                          lifecycle=dict(reclaim=dict(status="SUPPORTED"))), service_spec=copy.deepcopy(spec))

    def groups(self):
        return [self._group(spec) for spec in self.platform.store.list("managed_service")]

    def placement(self, identity):
        spec = self._spec(identity)
        return dict(kind="MODEL_SERVICE", target_kind="model-service", group_id=group_id(identity), image=spec["image_id"],
            image_id=spec["image_id"], binding_fingerprint=spec["spec_digest"], contract_digest=spec["spec_digest"],
            service_spec=spec, start_timeout_s=spec["start_timeout_s"], resource_envelope=spec["resource_envelope"])

    def validate_connection(self, connection):
        spec = self._spec(connection.get("managed_service_id"))
        if connection.get("base_url", "").rstrip("/") != spec["endpoint"].rstrip("/") or connection.get("model") != spec["model"]:
            raise ValueError("连接端点或模型与受管服务规格不符")
        if connection.get("managed_service_digest") != spec["spec_digest"]:
            raise ValueError("连接冻结的受管服务规格已变化")
        return spec

    def _held(self, identity):
        return [item for item in self.instances.values() if item.get("target_kind") == "model-service"
                and item["group_id"] == group_id(identity) and item.get("released") is not True]

    def live_instances(self, identity):
        with self.lock:
            return copy.deepcopy(self._held(identity))

    def _guard(self, identity, run_id):
        self.deployments._guard(group_id(identity), run_id)

    def _event(self, record, kind, **facts):
        if record["state"] == "UNKNOWN":
            record["busy_scope_unknown"] = True
        result = self.deployments._event(record, kind, **facts)
        if kind == "unknown" or facts.get("unexpected"):
            changed = getattr(self.deployments, "_environment_change", None)
            if changed:
                changed(record, "managed_service_" + kind)
        return result

    def start_gap(self, identity, expected_image=None):
        spec = self._spec(identity)
        if self.deployments.backend().get("kind") != "docker":
            return "受管单模型服务首版仅支持 Docker 后端"
        return "冻结镜像与当前服务规格不符" if expected_image and expected_image != spec["image_id"] else None

    def start(self, identity, body=None, run_id=None, replica=False):
        from contextlib import nullcontext
        images = getattr(self.platform, "images", None)
        scope_id = (body or {}).get("retention_scope_id")
        with images.launch(group_id(service_id(identity)), run_id, scope_id) if images else nullcontext(None) as frozen:
            return self._start(identity, body, run_id, replica, frozen)

    def _start(self, identity, body=None, run_id=None, replica=False, frozen=None):
        body = body or {}
        with self.lock:
            self._guard(identity, run_id)
            spec = self._spec(identity)
            if replica:
                raise ValueError("首版单模型服务不支持复制")
            gap = self.start_gap(identity, body.get("expected_image"))
            if gap:
                raise ValueError(gap)
            held = self._held(identity)
            if held:
                item = held[0]
                if item.get("run_id") == run_id and item["state"] in {"STARTING", "READY"}:
                    return copy.deepcopy(item)
                raise ValueError("服务已有实例或未确认释放对象")
            if any(lease["unknown"] for lease in self.leases.get(group_id(identity), {}).values()):
                raise ValueError("服务仍有未解决的使用者结果，不能重建")
            image = self._image(spec["image_id"])
            if image["Id"] != spec["image_id"]:
                raise ValueError("冻结镜像内容已不可用")
            generation = 1 + max((item["generation"] for item in self.instances.values()
                                   if item["group_id"] == group_id(identity)), default=0)
            instance = uuid.uuid4().hex
            name = "seampact-service-" + spec["id"]
            item = dict(id=instance, group_id=group_id(identity), managed_service_id=spec["id"], target_kind="model-service",
                generation=generation, run_id=run_id, session=self.deployments.session, state="STARTING", model_state="STARTING",
                backend="docker", desired="READY", accepting=False, released=False, in_flight=0, unknown_requests=0,
                busy_scope_started_wall=None, busy_scope_intervals=[], busy_scope_unknown=False,
                busy_scope_basis="OUTER_REQUEST_SCOPE_WALL_NOT_GPU_TIME",
                units=[], initialization={}, endpoint=spec["endpoint"], start_wall=time.time(), start_at=now(),
                last_activity_wall=time.time(), spec_digest=spec["spec_digest"], image_id=spec["image_id"],
                handle=dict(kind="docker", container=name, instance_id=instance), service_spec=copy.deepcopy(spec), pending_stop=None)
            self.instances[instance] = item
            if frozen:
                item.update(**frozen, startup_kind="COLD_START", model_load_ms=None)
            self._event(item, "start_requested")
            docker = spec["docker"]
            argv = ["docker", "run", "-d", "--pull=never", "--name", name, "--label", "seampact.instance=" + instance,
                    "--label", "seampact.service=" + spec["id"], "--label", "seampact.spec=" + spec["spec_digest"]]
            if docker.get("network"):
                argv += ["--network", docker["network"], "--network-alias", docker["network_alias"]]
            else:
                argv += ["-p", docker["publish_host"] + ":" + str(docker["publish_port"]) + ":" + str(docker["container_port"])]
            if docker.get("gpus"):
                argv += ["--gpus", str(docker["gpus"])]
            for name_ in docker.get("pass_env", []):
                argv += ["-e", name_]
            for mount in docker.get("mounts", []):
                argv += ["--mount", "type=bind,source=" + mount["source"] + ",target=" + mount["target"] +
                         (",readonly" if mount.get("read_only", True) else "")]
            argv += [spec["image_id"]] + spec["argv"]
            status, output = self._command(argv)
            # The image and engine argv are frozen but process output is private.
            # The actual image must precede the engine args, never worker args.
            if status == 0:
                item["handle"]["container_id"] = output.strip() if re.fullmatch(r"[0-9a-f]{12,64}", output.strip()) else None
            else:
                item.update(state="UNKNOWN", model_state="UNKNOWN", reason="docker_start_unconfirmed")
            self._event(item, "start_submitted", command_succeeded=status == 0)
        if frozen:
            self.platform.images.confirm_start(frozen, item["handle"], status == 0, item["id"])
        self.refresh(identity)
        return copy.deepcopy(item)

    def _observe(self, record, timeout=30):
        handle = record["handle"]
        status, output = self._command(["docker", "inspect", "--format",
            '{"Id":{{json .Id}},"Image":{{json .Image}},"State":{"Running":{{json .State.Running}},"Pid":{{json .State.Pid}},"StartedAt":{{json .State.StartedAt}}},'
            '"instance":{{json (index .Config.Labels "seampact.instance")}},"spec":{{json (index .Config.Labels "seampact.spec")}}}',
            handle.get("container_id") or handle["container"]], timeout)
        try:
            info = json.loads(output) if status == 0 else None
        except (ValueError, TypeError):
            info = None
        if info is None:
            # An inspect error alone may mean a broken daemon or permission error.
            code, names = self._command(["docker", "ps", "-a", "--filter", "name=^/" + handle["container"] + "$", "--format", "{{.ID}}"], timeout)
            return dict(status="ABSENT" if code == 0 and not names.strip() else "UNKNOWN")
        if (info.get("instance") != record["id"] or info.get("spec") != record["spec_digest"]
                or info.get("Image") != record["image_id"]
                or (handle.get("container_id") and info.get("Id") != handle["container_id"])):
            return dict(status="UNKNOWN", reason="backend_identity_mismatch")
        handle["container_id"] = info["Id"]
        state = info.get("State", {})
        started_at = state.get("StartedAt")
        if state.get("Running"):
            if (not isinstance(started_at, str) or not started_at
                    or handle.get("process_started_at") not in (None, started_at)):
                return dict(status="UNKNOWN", reason="backend_process_restarted_or_unconfirmed")
            handle["process_started_at"] = started_at
        return dict(status="RUNNING" if state.get("Running") else "EXITED", pid=state.get("Pid"), process_started_at=started_at)

    def _probe(self, spec):
        parsed = urlsplit(spec["endpoint"])
        origin = parsed.scheme + "://" + parsed.netloc
        headers = {}
        if spec.get("key_env"):
            key = os.environ.get(spec["key_env"])
            if not key:
                return False, "probe_credential_unavailable"
            headers["Authorization"] = "Bearer " + key
        try:
            health, _ = self.http_get(origin + spec["health_path"], headers, spec["probe_timeout_s"])
            if health != 200:
                return False, "health_not_ready"
            status, models = self.http_get(origin + spec["models_path"], headers, spec["probe_timeout_s"])
            ids = [item.get("id") for item in models.get("data", [])] if isinstance(models, dict) else []
            if status != 200 or ids != [spec["model"]]:
                return False, "single_model_identity_mismatch"
            return True, None
        except Exception:
            return False, "http_probe_unconfirmed"

    def refresh(self, identity=None):
        with self.lock:
            targets = [item for item in self.instances.values() if item.get("target_kind") == "model-service"
                       and not item.get("released") and (identity is None or item["group_id"] == group_id(identity))]
            for item in targets:
                previous = item["state"]
                observed = self._observe(item)
                item["observed"] = observed
                item["observed_wall"] = time.time()
                if observed["status"] == "ABSENT":
                    unexpected = not item.get("pending_stop")
                    item.update(state="STOPPED", model_state="STOPPED", released=True, accepting=False, desired="STOPPED",
                                stopped_wall=time.time(), pending_stop=None)
                    self._event(item, "stopped", reason="backend_absence_confirmed", unexpected=unexpected)
                    continue
                if item.get("pending_stop") and observed["status"] in {"RUNNING", "EXITED"}:
                    self._stop_if_drained(item)
                elif observed["status"] != "RUNNING":
                    item.update(state="UNKNOWN", model_state="UNKNOWN", accepting=False,
                                reason=observed.get("reason", "backend_not_running_or_unconfirmed"))
                    if previous != "UNKNOWN":
                        self._event(item, "unknown", reason=item["reason"])
                elif item["state"] != "UNKNOWN":
                    ready, reason = self._probe(item["service_spec"])
                    if ready:
                        was_ready = item["state"] == "READY"
                        item.update(state="READY", model_state="MODEL_READY", accepting=True, reason=None)
                        if not was_ready:
                            item["ready_wall"] = time.time()
                            self._event(item, "ready", start_to_ready_ms=(item["ready_wall"] - item["start_wall"]) * 1000)
                    elif item["state"] == "READY" or time.time() - item["start_wall"] >= item["service_spec"]["start_timeout_s"]:
                        item.update(state="UNKNOWN", model_state="UNKNOWN", accepting=False, reason=reason)
                        self._event(item, "unknown", reason=reason)
                if item["state"] == "UNKNOWN":
                    item["busy_scope_unknown"] = True
                self.platform.store.save("runtime_instance", item["id"], item)
            return copy.deepcopy(targets)

    def acquire(self, identity, owner, run_id=None):
        with self.lock:
            self._guard(identity, run_id)
            self._spec(identity)
            if any(item.get("run_id") != run_id for item in self._held(identity)):
                raise ValueError("实例控制属于另一运行，不能借用或接管")
            if not isinstance(owner, str) or not owner:
                raise ValueError("服务租约需要使用者身份")
            if any(item.get("pending_stop") for item in self._held(identity)):
                raise ValueError("服务正在排空，不能增加使用者")
            leases = self.leases.setdefault(group_id(identity), {})
            previous = leases.get(owner)
            if previous and previous["run_id"] != run_id:
                raise ValueError("使用者租约属于另一运行")
            leases.setdefault(owner, dict(run_id=run_id, unknown=False))
            return dict(group_id=group_id(identity), owner=owner)

    def release_lease(self, owner, unknown=False):
        with self.lock:
            for leases in self.leases.values():
                if owner in leases:
                    if unknown or leases[owner]["unknown"]:
                        leases[owner]["unknown"] = True
                    else:
                        del leases[owner]

    def resolve_lease(self, owner):
        """Explicit caller evidence that a previously unknown request ended."""
        with self.lock:
            for leases in self.leases.values():
                leases.pop(owner, None)

    def begin_request(self, identity, owner, run_id=None):
        with self.lock:
            self._guard(identity, run_id)
            if owner not in self.leases.get(group_id(identity), {}):
                raise ValueError("原客户端直连前须持有使用者租约")
            ready = [item for item in self._held(identity) if item["state"] == "READY" and item["accepting"]]
            if not ready or ready[0]["in_flight"] >= self._spec(identity)["contract"]["max_concurrency"]:
                raise ValueError("模型未就绪或请求容量已满")
            item = ready[0]
            if item["in_flight"] == 0:
                item["busy_scope_started_wall"] = time.time()
            item["in_flight"] += 1
            self._event(item, "request_started")
            return item["id"]

    def end_request(self, instance_id, owner, unknown=False):
        with self.lock:
            item = self.instances[instance_id]
            if item["in_flight"] <= 0:
                raise ValueError("没有对应在途请求")
            item["in_flight"] -= 1
            item["last_activity_wall"] = time.time()
            if item["in_flight"] == 0:
                started = item.get("busy_scope_started_wall")
                if started is not None:
                    item.setdefault("busy_scope_intervals", []).append([started, item["last_activity_wall"]])
                else:
                    item["busy_scope_unknown"] = True
                item["busy_scope_started_wall"] = None
            if unknown:
                item["busy_scope_unknown"] = True
                item["unknown_requests"] += 1
                item.update(state="UNKNOWN", model_state="UNKNOWN", accepting=False, reason="request_outcome_unknown")
                leases = self.leases.setdefault(item["group_id"], {})
                leases.setdefault(owner, dict(run_id=item.get("run_id"), unknown=True))["unknown"] = True
                self._event(item, "unknown", reason="request_outcome_unknown")
            self._event(item, "request_finished", outcome="UNKNOWN" if unknown else "RETURN")
            self.changed.notify_all()

    @contextmanager
    def request_scope(self, groups, owner, run_id=None):
        """Cover original client retries/streams; never call a model through /invoke."""
        started, temporary, invoked = [], [], False
        unknown = False
        identities = sorted(set(group_id(identity) for identity in groups))
        try:
            for identity in identities:
                with self.lock:
                    exists = owner in self.leases.get(group_id(identity), {})
                if not exists:
                    self.acquire(identity, owner, run_id=run_id)
                    temporary.append(group_id(identity))
            # Acquire the full dependency request-slot set atomically. Waiting
            # releases the shared lock, so end_request/controller can progress.
            with self.changed:
                for identity in identities:
                    self.waiters[identity] = self.waiters.get(identity, 0) + 1
                try:
                    while True:
                        available = True
                        for identity in identities:
                            self._guard(identity, run_id)
                            ready = [item for item in self._held(identity)
                                     if item["state"] == "READY" and item["accepting"] and not item.get("pending_stop")]
                            if not ready:
                                raise ValueError("服务尚未就绪或已有未知请求，不能开始原客户端调用")
                            available &= ready[0]["in_flight"] < self._spec(identity)["contract"]["max_concurrency"]
                        if available:
                            started = [self.begin_request(identity, owner, run_id=run_id) for identity in identities]
                            break
                        closed = getattr(self.deployments, "closed", None)
                        if closed is not None and closed.is_set():
                            raise ValueError("服务控制器已关闭")
                        self.changed.wait(0.2)
                finally:
                    for identity in identities:
                        self.waiters[identity] -= 1
            invoked = True
            yield
        except BaseException as error:
            # SDKs often wrap a network failure in their own exception class.
            # Do not persist or echo exception text, which can contain keys/URLs.
            unknown = invoked and (isinstance(error, (URLError, OSError)) and not isinstance(error, HTTPError)
                or getattr(error, "kind", None) in {"transport", "response_incomplete", "invalid_response"}
                or any(marker in type(error).__name__.lower() for marker in ("timeout", "connection"))
                or isinstance(error, ProtocolError))
            raise
        finally:
            for instance in reversed(started):
                self.end_request(instance, owner, unknown=unknown)
            with self.lock:
                for identity in temporary:
                    leases = self.leases.get(identity, {})
                    if owner in leases and not leases[owner]["unknown"]:
                        del leases[owner]

    def resolve_requests(self, instance_id):
        with self.lock:
            item = self.instances[instance_id]
            item["unknown_requests"] = 0
            self._event(item, "unknown_requests_resolved")

    def _stop_if_drained(self, item):
        if self.leases.get(item["group_id"]) or item["in_flight"] or item.get("unknown_requests"):
            item["drain_blocked"] = "active_or_unknown_service_users"
            return
        if item.get("retention_scope_id"):
            try:
                images = self.platform.images
                images.engine(images.get(item["retention_scope_id"])["engine"])
            except ValueError as error:
                item.update(state="UNKNOWN", accepting=False, reason=str(error), drain_blocked="daemon_unconfirmed")
                return
        # Inspect identity before stop; never stop a replacement at the same name.
        observed = self._observe(item)
        if observed["status"] not in {"RUNNING", "EXITED"}:
            item.update(state="UNKNOWN", accepting=False, reason="stop_identity_unconfirmed")
            return
        handle = item["handle"]
        target = handle["container_id"]
        if observed["status"] == "RUNNING":
            status, _ = self._command(["docker", "stop", "-t", "30", target], 60)
            if status != 0:
                item.update(state="UNKNOWN", model_state="UNKNOWN", reason="stop_unconfirmed")
                return
        self._command(["docker", "rm", target])
        if self._observe(item)["status"] == "ABSENT":
            item.update(state="STOPPED", model_state="STOPPED", desired="STOPPED", accepting=False, released=True,
                        stopped_wall=time.time(), pending_stop=None, drain_blocked=None)
            self._event(item, "stopped", reason="backend_deletion_confirmed")
        else:
            item.update(state="UNKNOWN", model_state="UNKNOWN", accepting=False, reason="deletion_unconfirmed")

    def drain_stop(self, identity, body=None, run_id=None, reason="operator"):
        body = body or {}
        with self.lock:
            self._guard(identity, run_id)
            targets = [item for item in self._held(identity) if body.get("instance_id") in (None, item["id"])]
            for item in targets:
                if item["state"] == "UNKNOWN" and not body.get("release_unknown"):
                    raise ValueError("UNKNOWN 请求／实例须先核对；未确认结束不回收")
                item.update(accepting=False, desired="STOPPED", pending_stop=dict(reason=reason, requested_at=now()))
                if item["state"] != "UNKNOWN":
                    item["state"] = "DRAINING"
                self._event(item, "drain_requested", reason=reason)
                self._stop_if_drained(item)
                if item["state"] == "UNKNOWN":
                    item["busy_scope_unknown"] = True
                self.platform.store.save("runtime_instance", item["id"], item)
            return dict(group_id=group_id(identity), instances=copy.deepcopy(targets))

    def prepare(self, identity, run_id=None, task_id=None, prewarm=False):
        record = self.start(identity, {}, run_id=run_id)
        deadline = time.monotonic() + self._spec(identity)["start_timeout_s"]
        while record["state"] == "STARTING" and time.monotonic() < deadline:
            time.sleep(min(0.1, max(0, deadline - time.monotonic())))
            self.refresh(identity)
            record = copy.deepcopy(self.instances[record["id"]])
        if record["state"] != "READY":
            raise ValueError("受管模型未通过模型就绪及身份核对")
        return record

    def ensure_ready(self, placement, unit_id=None):
        if placement.get("contract_digest") != self._spec(placement["group_id"])["spec_digest"]:
            raise ValueError("冻结服务规格已变化")
        return self.prepare(placement["group_id"], run_id=placement.get("run_id"))

    def prewarm(self, identity, body=None, run_id=None):
        record = self.prepare(identity, run_id=run_id)
        return dict(group_id=group_id(identity), instances=[record], model_state="MODEL_READY")

    def release_run(self, identity, run_id):
        with self.lock:
            owned = [item for item in self._held(identity) if item.get("run_id") == run_id]
        results = [self.drain_stop(identity, dict(instance_id=item["id"]), run_id=run_id, reason="run_window_end") for item in owned]
        return dict(group_id=group_id(identity), instances=[item for result in results for item in result["instances"]])

    def group_view(self, identity, expected_image=None):
        spec = self._spec(identity)
        group = self._group(spec)
        with self.lock:
            instances = self.live_instances(identity)
            return dict(group_id=group_id(identity), units=[], target_kind="model-service", contract=group["contract"],
                operations={key: value["status"] for key, value in group["contract"]["operations"].items()}, prepare={},
                replicable=False, reclaim=True, start_gap=self.start_gap(identity, expected_image), instances=instances,
                resource_envelope=spec["resource_envelope"], spec_digest=spec["spec_digest"],
                leases={item["id"]: sorted(self.leases.get(group_id(identity), {})) for item in instances})

    def scheduler_view(self, service_ids, run_id=None):
        return {group_id(identity): self.group_view(identity) for identity in service_ids}

    def instance_fact(self, instance_id):
        with self.lock:
            item = self.instances.get(instance_id)
            return dict(copy.deepcopy(item), held=item.get("released") is not True) if item else None

    def runtime_fact(self, identity, unit_id=None, expected_image=None):
        view = self.group_view(identity, expected_image)
        ready = [item for item in view["instances"] if item["state"] == "READY" and item["accepting"]]
        return dict(local=False, ready_instance=bool(ready), warm=bool(ready), cold_startable=not view["instances"] and not view["start_gap"],
            accepting=bool(ready), busy=any(item["in_flight"] for item in view["instances"]),
            queue=self.waiters.get(group_id(identity), 0), state_age_ms=0.0,
            reason=view["start_gap"])

    def inflight(self, identity):
        with self.lock:
            return sum(item["in_flight"] + item.get("unknown_requests", 0) for item in self._held(identity))

    def sample_resources(self, group_ids):
        """Read this container's v2 counters, never aggregate host PID/GPU use.

        The read-only docker exec probe itself contributes a small amount of
        CPU/memory to these counters. Counter identity includes each generation.
        """
        selected = {group_id(identity) for identity in group_ids}
        rows = []
        with self.lock:
            targets = [copy.deepcopy(item) for item in self.instances.values()
                       if item.get("target_kind") == "model-service" and item["group_id"] in selected
                       and item.get("released") is not True]
        for item in targets:
            row = dict(scope="managed_service_cgroup", instance_id=item["id"], group_id=item["group_id"],
                generation=item["generation"], container_id=item["handle"].get("container_id"), pid=None,
                identity=None, monotonic_s=time.monotonic(), status="UNKNOWN", reason="COUNTERS_UNAVAILABLE",
                cpu_seconds=None, memory_current_bytes=None, gpu_memory_bytes=None,
                note="container cgroup v2; docker exec sampling overhead included; GPU attribution unavailable")
            before = self._observe(item, timeout=10)
            row["container_id"], row["pid"] = item["handle"].get("container_id"), before.get("pid")
            if before["status"] != "RUNNING" or not row["container_id"]:
                row["reason"] = "BACKEND_IDENTITY_OR_RUNNING_STATE_UNCONFIRMED"
                rows.append(row)
                continue
            code, output = self._command(["docker", "exec", row["container_id"], "cat", "/proc/self/cgroup",
                                         "/sys/fs/cgroup/cpu.stat", "/sys/fs/cgroup/memory.current"], 10)
            sampled = time.monotonic()
            after = self._observe(item, timeout=10)
            if after["status"] != "RUNNING" or after.get("pid") != before.get("pid"):
                row["reason"] = "IDENTITY_CHANGED_DURING_SAMPLE"
            elif code == 0:
                try:
                    lines = output.strip().splitlines()
                    if not lines or lines[0] != "0::/":
                        raise ValueError("private cgroup v2 namespace required")
                    counters = dict(line.split() for line in lines[1:-1])
                    usage, memory = int(counters["usage_usec"]), int(lines[-1])
                    if usage < 0 or memory < 0:
                        raise ValueError("negative counters")
                    row.update(status="OBSERVED", reason=None, cpu_seconds=usage / 1_000_000,
                               memory_current_bytes=memory, monotonic_s=sampled,
                               identity=row["container_id"] + ":" + str(item["generation"]))
                except (ValueError, KeyError):
                    row["reason"] = "PRIVATE_CGROUP_V2_COUNTERS_UNCONFIRMED"
            rows.append(row)
        return rows

    def public(self):
        return [dict(group, instances=self.live_instances(group["id"])) for group in self.groups()]

    def lock_groups(self, run_id, placements, control):
        with self.lock:
            for identity in placements:
                self._spec(identity)
                self._guard(identity, run_id)
            for identity, placement in placements.items():
                self.run_locks[group_id(identity)] = dict(run_id=run_id, placement=copy.deepcopy(placement), control=copy.deepcopy(control))

    def reserve_run(self, run_id, placements, control=None):
        return self.lock_groups(run_id, placements, control or {})

    def unlock(self, run_id):
        with self.lock:
            for identity in [key for key, value in self.run_locks.items() if key.startswith("service:") and value["run_id"] == run_id]:
                del self.run_locks[identity]

    unlock_run = unlock

    def close(self):
        pass
