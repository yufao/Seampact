"""Experiment-owned image references and optional, operator-selected archives.

References never enter the runtime-instance registry. Docker calls run outside
the record lock; a persisted launch ticket keeps close from racing submission.
"""

import copy
import hashlib
import json
import os
import re
import shutil
import tarfile
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from seampact.hosting import backends
from seampact.hosting.image_build import BASE_LABEL, RECIPE_LABEL, dependency_image_reference
from seampact.store import now

IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")
ACTIVE = {"PREPARING", "READY_TO_RUN", "RUNNING", "CLOSING", "INTERRUPTED"}
ROLE = "io.seampact.role"
SCOPE = "io.seampact.retention-scope"
OWNER = "io.seampact.deployment"
IMAGE = "io.seampact.image"
IMAGE_FORMAT = ('{{$labels := index .Config "Labels"}}{"Id":{{json .Id}},"Size":{{json .Size}},"Os":{{json .Os}},'
    '"Architecture":{{json .Architecture}},"RepoTags":{{json .RepoTags}},'
    '"RepoDigests":{{json .RepoDigests}},"Config":{"Volumes":{{json (index .Config "Volumes")}},'
    '"Labels":{' + ','.join('"' + label + '":{{if $labels}}{{json (index $labels "' + label +
        '")}}{{else}}null{{end}}' for label in (RECIPE_LABEL, BASE_LABEL)) + '}}}')
# Do not inspect Config.Env (source images may contain private values).
CONTAINER_FORMAT = ('{"Id":{{json .Id}},"Image":{{json .Image}},"State":{{json .State}},'
    '"Labels":{{json (index .Config "Labels")}},"Healthcheck":{{json (index .Config "Healthcheck")}},'
    '"Entrypoint":{{json (index .Config "Entrypoint")}},"HostConfig":{{json .HostConfig}},"Mounts":{{json .Mounts}}}')


class PreparationFailed(ValueError):
    def __init__(self, reason, scope_id=None):
        super().__init__(reason)
        self.scope_id = scope_id


def file_digest(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


class Images:
    def __init__(self, platform, runner=None):
        self.platform, self.store = platform, platform.store
        self.lock = threading.RLock()
        self.runner = runner or backends.run
        self.operation = None
        self.session = uuid.uuid4().hex
        owner = self.store.get("image_owner", "current")
        if owner is None:
            owner = dict(id=uuid.uuid4().hex)
            self.store.save("image_owner", "current", owner)
        self.owner = owner["id"]
        for scope in self.store.list("image_scope"):
            if scope["status"] in ACTIVE:
                scope.update(status="INTERRUPTED", measuring=False, accepting=False,
                    reason="平台已重启；核对在途操作及真实消费者后结束旧范围")
                self._save(scope)

    def _save(self, scope):
        scope["updated_at"] = now()
        self.store.save("image_scope", scope["id"], scope)

    def get(self, identity):
        scope = self.store.get("image_scope", identity)
        if not scope:
            raise ValueError("镜像保留范围不存在")
        return scope

    def _json(self, argv, absent=None):
        result = self.runner(argv, 120)
        if result.get("returncode") != 0:
            message = result.get("stderr", "") + result.get("stdout", "")
            if absent and re.search(r"no such (?:image|container|object)\b", message, re.I):
                return None
            raise PreparationFailed("DOCKER_UNKNOWN：无法确认 Docker 对象（连接、权限或命令失败）")
        try:
            return json.loads(result["stdout"])
        except (ValueError, KeyError):
            raise PreparationFailed("DOCKER_UNKNOWN：Docker 回执格式无法核对") from None

    def _command(self, argv, timeout=7200):
        result = self.runner(argv, timeout)
        if result.get("returncode") != 0:
            raise PreparationFailed("Docker 操作失败或超时；对象仍须核对")
        return result.get("stdout", "").strip()

    def engine(self, expected=None):
        info = self._json(["docker", "info", "--format",
            '{"id":{{json .ID}},"os":{{json .OSType}},"root":{{json .DockerRootDir}},'
            '"runtimes":{{if index .Runtimes "runc"}}{"runc":true}{{else}}{}{{end}}}'])
        context = self._command(["docker", "context", "show"], 120)
        locator = hashlib.sha256(json.dumps([context, *[os.environ.get(key) for key in
            ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH")]]).encode()).hexdigest()
        info["locator"] = locator
        if not info.get("id") or info.get("os") != "linux":
            raise PreparationFailed("引用保留目前要求可核对身份的 Linux Docker daemon")
        if expected and any(info.get(key) != expected.get(key) for key in ("id", "locator")):
            raise PreparationFailed("DAEMON_CHANGED：原 Docker 位置或身份已变化；保留原引用待核对")
        return info

    def inspect(self, image):
        if not IMAGE_ID.fullmatch(image or ""):
            raise ValueError("运行镜像必须使用完整 sha256 ID；请先正常准备并发布")
        value = self._json(["docker", "image", "inspect", "--format", IMAGE_FORMAT, image], absent=True)
        if value and value.get("Id") != image:
            raise PreparationFailed("IMAGE_CHANGED：实际镜像身份不符")
        return value

    def _container(self, identity):
        return self._json(["docker", "container", "inspect", "--format", CONTAINER_FORMAT, identity], absent=True)

    def _reference(self, scope, image, create=False):
        name = "seampact-image-ref-" + scope["id"] + "-" + image[7:19]
        labels = {ROLE: "image-reference", SCOPE: scope["id"], OWNER: self.owner, IMAGE: image}
        tmpfs = (scope.get("references", {}).get(image) or {}).get("tmpfs") or {}
        created_here = False
        info = self._container(scope.get("references", {}).get(image, {}).get("id") or name)
        if info is None and create:
            facts = self.inspect(image)
            if facts is None:
                raise PreparationFailed("IMAGE_MISSING：本机不存在冻结镜像 " + image)
            volumes = (facts.get("Config") or {}).get("Volumes") or {}
            if any(not p.startswith("/") or any(c in p for c in (":", ",", "\x00")) for p in volumes):
                raise PreparationFailed("VOLUME_UNSUPPORTED：无法无副作用地覆盖镜像卷目标")
            # Verified against real Linux Docker: tmpfs overrides declared
            # VOLUME without initializing anonymous volumes or copying data.
            tmpfs = {path: "ro,size=65536,mode=000" for path in volumes}
            engine = scope["engine"]
            if "runc" not in (engine.get("runtimes") or {}):
                raise PreparationFailed("NON_GPU_RUNTIME_UNKNOWN：未确认普通 runc 运行时")
            argv = ["docker", "create", "--pull=never", "--name", name, "--runtime", "runc",
                "--restart=no", "--no-healthcheck", "--network=none", "--entrypoint", "/__image_reference_never_start__"]
            for key, value in labels.items():
                argv += ["--label", key + "=" + value]
            for target, options in tmpfs.items():
                argv += ["--tmpfs", target + ":" + options]
            # Persist intent before create, including the name used to reconcile
            # a successful create whose response was lost.
            with self.lock:
                current = self.get(scope["id"])
                current.setdefault("references", {})[image] = dict(name=name, id=None, tmpfs=tmpfs)
                self._save(current)
            self.runner(argv + [image], 120)
            created_here = True
            info = self._container(name)
            if info is None:
                raise PreparationFailed("REFERENCE_MISSING：引用创建未确认；未进入计量")
        if info is None:
            raise PreparationFailed("REFERENCE_MISSING：本轮引用已丢失")
        state, host = info.get("State") or {}, info.get("HostConfig") or {}
        if (info.get("Image") != image or any((info.get("Labels") or {}).get(k) != v for k, v in labels.items())
                or state.get("Status") != "created" or state.get("Running") is not False or state.get("Pid") != 0
                or (info.get("Healthcheck") or {}).get("Test") != ["NONE"]
                or info.get("Entrypoint") != ["/__image_reference_never_start__"]
                or host.get("Runtime") != "runc" or host.get("NetworkMode") != "none"
                or (host.get("RestartPolicy") or {}).get("Name") != "no"
                or any(host.get(k) for k in ("DeviceRequests", "Devices", "Binds", "PortBindings"))
                or (host.get("Tmpfs") or {}) != tmpfs
                or any(m.get("Type") != "tmpfs" or m.get("Destination") not in tmpfs for m in info.get("Mounts") or [])):
            if (created_here and info.get("Image") == image and state.get("Status") == "created"
                    and all((info.get("Labels") or {}).get(k) == v for k, v in labels.items())):
                # No source mounts were supplied; any anonymous volume belongs
                # solely to this just-created reference, never a real worker.
                self._command(["docker", "rm", "-v", info["Id"]], 120)
            raise PreparationFailed("REFERENCE_INVALID：引用归属、镜像或无进程／无挂载条件不符")
        return dict(id=info["Id"], name=name, image_id=image, status="created", tmpfs=tmpfs)

    def _fail(self, identity, error):
        with self.lock:
            scope = self.get(identity)
            scope.setdefault("failure", dict(category="PREPARATION_FAILED", phase="RUNTIME" if scope.get("started_at") else "BEFORE_RUN",
                reason=str(error), at=now()))
            scope["accepting"] = False
            self._save(scope)
        return PreparationFailed(str(error), identity)

    @contextmanager
    def image_operation(self, kind):
        with self.lock:
            if self.operation or any(s.get("measuring") for s in self.store.list("image_scope")):
                raise ValueError("正式计量或镜像操作正在进行；结束后重试")
            self.operation = kind
        try:
            yield
        finally:
            with self.lock:
                self.operation = None

    def guard_artifact(self, group):
        with self.lock:
            for scope in self.store.list("image_scope"):
                if scope["status"] in ACTIVE and group in scope["groups"]:
                    raise ValueError("整轮实验仍保护该工件；请先结束镜像范围 " + scope["id"])

    def guard_source(self, source_id):
        for group in self.store.list("runtime_group"):
            if group.get("source_id") == source_id:
                self.guard_artifact(group["id"])

    def scope_for(self, group, run_id=None, scope_id=None):
        with self.lock:
            all_scopes = self.store.list("image_scope")
            scopes = [s for s in all_scopes if s["status"] in ACTIVE and group in s["groups"]]
            selected = next((s for s in scopes if (scope_id and s["id"] == scope_id) or
                (run_id and run_id in s["run_ids"])), None)
            if selected is None and (run_id or scope_id):
                selected = next((s for s in all_scopes if group in s["groups"] and
                    ((scope_id and s["id"] == scope_id) or (run_id and run_id in s["run_ids"]))), None)
            if scopes and selected is None:
                raise PreparationFailed("运行组属于镜像保留范围；启动须声明该范围或已登记子运行")
            return selected

    def prepare(self, body):
        groups = copy.deepcopy(body.get("groups") or {})
        selected_groups = set(body.get("group_ids") or [])
        if selected_groups and getattr(self.platform, "catalog", None):
            from seampact.scenes.scenarios import FrozenCatalog
            units = [u["id"] for u in self.platform.catalog.public() if u.get("runtime_group") in selected_groups]
            if units:
                catalog = FrozenCatalog(self.platform.catalog, units, {uid: "REMOTE" for uid in units})
                groups.update(catalog.remote_groups())
        for group in body.get("group_ids") or []:
            if group in groups:
                continue
            if group.startswith("service:"):
                groups[group] = self.platform.managed_services.placement(group)
            else:
                artifact = self.store.get("deployment", group) or {}
                current = self.store.get("runtime_group", group) or {}
                groups[group] = dict(group_id=group, image_id=(artifact.get("build") or {}).get("image_id"),
                    image=(artifact.get("publish") or {}).get("reference"),
                    contract_digest=(current.get("contract") or {}).get("digest"),
                    binding_fingerprint=artifact.get("binding_fingerprint"))
        def visit_service(group):
            spec = self.platform.managed_services._spec(group)
            for dependency in spec.get("depends_on", []):
                identity = "service:" + dependency.removeprefix("service:")
                if identity not in groups:
                    groups[identity] = self.platform.managed_services.placement(identity)
                    visit_service(identity)
        for group in list(groups):
            if group.startswith("service:"):
                visit_service(group)
        if not isinstance(groups, dict):
            raise ValueError("groups 须为冻结运行组到 placement 的映射")
        requested = [p.get("image_id") or p.get("image") for p in groups.values()] + body.get("images", [])
        if not requested or any(not isinstance(i, str) or not IMAGE_ID.fullmatch(i) for i in requested):
            raise ValueError("请先准备全部最终镜像，声明完整 sha256 ID")
        images = sorted(set(requested))
        comparisons = copy.deepcopy(body.get("comparison_units") or [])
        if (not isinstance(comparisons, list) or not comparisons or any(not isinstance(u, dict) or not u.get("id") or
                not isinstance(u.get("run_ids"), list) or not u["run_ids"] for u in comparisons)
                or len({u["id"] for u in comparisons}) != len(comparisons)):
            raise ValueError("须事先声明 comparison_units 比较单元及其子运行")
        if any("status" in unit or "completed_at" in unit for unit in comparisons):
            raise ValueError("比较单元不能预填执行结果")
        with self.image_operation("prepare"):
            scope = dict(id=uuid.uuid4().hex, status="PREPARING", created_at=now(), engine=self.engine(),
                images=images, groups=groups, references={}, run_ids=list(body.get("run_ids") or []),
                comparison_units=comparisons, tickets={}, accepting=False, measuring=False,
                session=self.session, owner=body.get("owner", "manual"))
            # Bind current actual artifacts, contract/core and mount metadata.
            self.store.create("image_scope", scope["id"], scope)
            try:
                settings = self.store.get("image_settings", "current") or {}
                scope["bindings"] = self._bindings(groups)
                scope["storage"] = self.storage(scope["engine"], body.get("storage_path") or settings.get("storage_path"),
                    body.get("reserve_bytes", settings.get("reserve_bytes", 1024 ** 3)))
                self._save(scope)
                for image in images:
                    reference = self._reference(scope, image, create=True)
                    with self.lock:
                        scope = self.get(scope["id"])
                        scope["references"][image] = reference
                        self._save(scope)
                self.verify(scope["id"])
                with self.lock:
                    scope = self.get(scope["id"])
                    scope.update(status="READY_TO_RUN", accepting=True)
                    self._save(scope)
            except Exception as error:
                self._fail(scope["id"], error)
                self.close(scope["id"])
            return self.get(scope["id"])

    def configure(self, body):
        with self.image_operation("configure"):
            value = dict(storage_path=body.get("storage_path") or None, reserve_bytes=body.get("reserve_bytes", 1024 ** 3))
            if type(value["reserve_bytes"]) is not int or value["reserve_bytes"] < 0:
                raise ValueError("reserve_bytes 须为非负整数")
            self.storage(self.engine(), value["storage_path"], value["reserve_bytes"])
            self.store.save("image_settings", "current", value)
            return value

    def register_runs(self, identity, run_ids, groups, comparison_units):
        with self.lock:
            scope = self.get(identity)
            if scope["status"] != "READY_TO_RUN" or scope.get("failure"):
                raise ValueError("须在整轮正式开始前登记所有子运行")
            def frozen(placement):
                return (placement.get("image_id") or placement.get("image"), placement.get("contract_digest"),
                        placement.get("binding_fingerprint"))
            if any(group not in scope["groups"] or frozen(scope["groups"][group]) != frozen(placement)
                    for group, placement in groups.items()):
                raise ValueError("子运行的冻结闭包与原范围不符")
            scope["run_ids"] = sorted(set(scope["run_ids"]) | set(run_ids))
            existing = {u["id"] for u in scope["comparison_units"]}
            if existing.intersection(u["id"] for u in comparison_units):
                raise ValueError("比较单元身份重复")
            scope["comparison_units"].extend(comparison_units)
            self._save(scope)

    def _bindings(self, groups):
        if groups and self.platform.deployments.backend().get("kind") != "docker":
            raise PreparationFailed("本轮镜像保留仅适用于同一 Linux Docker 后端；未签发 K8S 节点镜像保证")
        facts = {}
        for group, placement in groups.items():
            if group.startswith("service:"):
                spec = self.platform.managed_services._spec(group)
                if spec["image_id"] != (placement.get("image_id") or placement.get("image")):
                    raise PreparationFailed("服务镜像与清单不符")
                facts[group] = dict(spec_digest=spec["spec_digest"], mounts=self._mount_signals(spec["docker"].get("mounts", [])))
            else:
                current = self.store.get("runtime_group", group)
                artifact = self.store.get("deployment", group)
                if not current or not artifact or self.platform.deployments._start_gap(current, artifact):
                    raise PreparationFailed("运行组尚未满足当前平台协议和工件运行条件：" + group)
                if (artifact.get("build") or {}).get("image_id") != (placement.get("image_id") or placement.get("image")):
                    raise PreparationFailed("worker 镜像与冻结清单不符")
                facts[group] = dict(contract_digest=current["contract"]["digest"],
                    binding_fingerprint=artifact.get("binding_fingerprint"), platform_core=artifact.get("platform_core"),
                    mounts=self._mount_signals(self.platform.deployments.backend().get("mounts", [])))
        return facts

    @staticmethod
    def _mount_signals(mounts):
        facts = []
        for mount in mounts:
            source = Path(mount["source"])
            # These are explicitly lightweight signals, not a claim of content
            # immutability. Existing binding digests remain in the group facts.
            stat = source.stat() if source.exists() else None
            facts.append(dict(source=str(source), target=mount["target"], read_only=mount.get("read_only", True),
                size=stat.st_size if stat and mount.get("read_only", True) else None,
                mtime_ns=stat.st_mtime_ns if stat and mount.get("read_only", True) else None))
        return facts

    def storage(self, engine, path=None, reserve=1024 ** 3):
        if type(reserve) is not int or reserve < 0:
            raise ValueError("reserve_bytes 须为非负整数")
        root = Path(engine["root"])
        candidate = Path(path) if path else root
        if Path("/.dockerenv").exists():
            mounts = self._json(["docker", "container", "inspect", "--format", "{{json .Mounts}}", os.environ.get("HOSTNAME", "")])
            mapped = any(m.get("Type") == "bind" and Path(m["Source"]) == root and
                Path(m["Destination"]).resolve() == candidate.resolve() for m in mounts)
        else:
            # A remote daemon's directory must never be confused with this
            # process's filesystem, even when the same pathname exists locally.
            remote = os.environ.get("DOCKER_HOST", "").startswith(("tcp:", "ssh:"))
            context = self._command(["docker", "context", "show"], 120)
            endpoint = self._json(["docker", "context", "inspect", context, "--format", "{{json .Endpoints.docker.Host}}"])
            mapped = not remote and str(endpoint).startswith("unix:") and candidate.resolve() == root.resolve()
        if not mapped or not candidate.is_dir():
            raise PreparationFailed("DOCKER_STORAGE_UNKNOWN：须配置可核对的 Docker 存储文件系统挂载，不能用平台根目录空间代替")
        free = shutil.disk_usage(candidate).free
        if free < reserve:
            raise PreparationFailed("DOCKER_STORAGE_FULL：Docker 存储空间不足，需保留运行与临时空间")
        return dict(path=str(candidate), free_bytes=free, reserve_bytes=reserve, checked_at=now())

    def verify(self, identity, image=None):
        scope = self.get(identity)
        try:
            self.engine(scope["engine"])
            if self._bindings(scope["groups"]) != scope["bindings"]:
                raise PreparationFailed("BINDING_CHANGED：源码、配置或挂载的轻量信号已变化")
            for value in [image] if image else scope["images"]:
                if value not in scope["images"]:
                    raise PreparationFailed("IMAGE_NOT_FROZEN：镜像不在本轮冻结闭包中")
                self._reference(scope, value)
                if self.inspect(value) is None:
                    raise PreparationFailed("IMAGE_MISSING：冻结镜像已不在本机")
        except Exception as error:
            raise self._fail(identity, error) from error
        return dict(status="VERIFIED", checked_at=now(), retention_scope_id=identity)

    def begin(self, identity, run_id=None):
        self.verify(identity)
        with self.lock:
            scope = self.get(identity)
            if scope["status"] not in {"READY_TO_RUN", "RUNNING"} or scope.get("failure") or not scope["accepting"]:
                raise PreparationFailed("范围已关闭或准备条件失效", identity)
            if run_id and run_id not in scope["run_ids"]:
                raise ValueError("子运行未在准备清单登记")
            if self.operation or any(s.get("measuring") and s["id"] != identity for s in self.store.list("image_scope")):
                raise ValueError("镜像操作或另一实验正在计量")
            for artifact in self.store.list("deployment"):
                if any((artifact.get(k) or {}).get("status") == "RUNNING" for k in ("build", "publish")):
                    raise ValueError("镜像构建／发布尚未结束")
            if any((h.get("target_runtime") or {}).get("status") == "PREPARING" for h in self.store.list("harness")):
                raise ValueError("目标解释器准备尚未结束")
            scope.update(status="RUNNING", measuring=True, started_at=scope.get("started_at") or now())
            self._save(scope)
        return scope

    def confirm_start(self, frozen, handle, applied, instance_id=None):
        identity, image = frozen["retention_scope_id"], frozen["image_id"]
        try:
            if not applied:
                # Reinspect only failed submissions to distinguish a create/GC
                # race from an actual model/runtime failure.
                self.verify(identity, image)
                return
            info = self._container((handle or {}).get("container_id") or (handle or {}).get("container", ""))
            if (not info or info.get("Image") != image or
                    (instance_id and (info.get("Labels") or {}).get("seampact.instance") != instance_id)):
                raise PreparationFailed("START_IMAGE_UNKNOWN：新容器实际镜像身份未确认")
            handle["container_id"] = info["Id"]
        except PreparationFailed as error:
            raise self._fail(identity, error) from error

    def record_run_end(self, identity, run_id, status):
        with self.lock:
            scope = self.get(identity)
            scope.setdefault("completed_runs", {})[run_id] = dict(status=status, at=now(), conditions_valid=not scope.get("failure"))
            for unit in scope["comparison_units"]:
                members = unit["run_ids"]
                if unit.get("status") == "COMPLETED":
                    continue
                records = [scope["completed_runs"].get(r) for r in members]
                if all(r and r["status"] == "COMPLETED" and r["conditions_valid"] for r in records):
                    unit.update(status="COMPLETED", completed_at=now())
            self._save(scope)

    def result(self, body):
        scope = self.get(body["retention_scope_id"])
        run_id, status = body["run_id"], body["status"]
        declared = {r for unit in scope["comparison_units"] for r in unit["run_ids"]}
        if scope.get("owner") != "manual" or run_id not in declared or status not in {"COMPLETED", "FAILED", "INTERRUPTED"}:
            raise ValueError("只允许记录手动范围中事先声明的子运行结果")
        if status == "COMPLETED":
            self.verify(scope["id"])
        self.record_run_end(scope["id"], run_id, status)
        return self.get(scope["id"])

    def usage(self, body):
        """Explicit tickets for native/scoring scripts sharing a manual scope."""
        identity = body["retention_scope_id"]
        if body.get("action") == "release":
            self.engine(self.get(identity)["engine"])
            with self.lock:
                scope = self.get(identity)
                ticket = scope["tickets"].get(body["ticket_id"])
                if ticket and not ticket.get("external"):
                    raise ValueError("平台在途启动票据不能由外部脚本解除")
                scope["tickets"].pop(body["ticket_id"], None)
                self._save(scope)
            return dict(status="RELEASED")
        self.verify(identity)
        with self.lock:
            scope = self.get(identity)
            if not scope["accepting"] or scope.get("failure"):
                raise PreparationFailed("范围不再接收执行")
            ticket = uuid.uuid4().hex
            scope["tickets"][ticket] = dict(external=True, purpose=body.get("purpose", "native/scoring"), at=now())
            self._save(scope)
        return dict(ticket_id=ticket, retention_scope_id=identity, images=scope["images"], engine=scope["engine"]["id"])

    def enter_task(self, identity, task_id, groups):
        with self.lock:
            scope = self.get(identity)
            if not scope["accepting"] or scope.get("failure") or scope["status"] not in {"READY_TO_RUN", "RUNNING"}:
                raise PreparationFailed("本轮已关闭或准备条件失效", identity)
            if set(groups) - set(scope["groups"]):
                raise ValueError("任务运行组不在本轮冻结闭包中")
            scope["tickets"][task_id] = dict(task_id=task_id, session=self.session, at=now(), external=False)
            self._save(scope)

    def dispatch_check(self, group, run_id=None, scope_id=None):
        scope = self.scope_for(group, run_id, scope_id)
        if scope and scope.get("failure"):
            raise PreparationFailed(scope["failure"]["reason"], scope["id"])

    def finish_task(self, identity, task_id):
        with self.lock:
            scope = self.get(identity)
            scope["tickets"].pop(task_id, None)
            self._save(scope)

    def reconcile(self, body):
        identity = body["retention_scope_id"]
        scope = self.get(identity)
        self.engine(scope["engine"])
        if not body.get("confirm_launches_settled"):
            raise ValueError("须先确认旧平台提交的启动操作已结束并完成对象核对")
        if any(self.platform.deployments.live_instances(g) for g in scope["groups"]):
            raise ValueError("仍有未确认释放的真实实例")
        for group in scope["groups"]:
            key = "seampact.service=" + group[8:] if group.startswith("service:") else "seampact.group=" + group
            if self._command(["docker", "ps", "-a", "--filter", "label=" + key, "--format", "{{.ID}}"], 120):
                raise ValueError("Docker 仍有真实消费者；请先按原流程核对释放")
        with self.lock:
            scope = self.get(identity)
            scope["tickets"] = {k: v for k, v in scope["tickets"].items() if v.get("external") or v.get("session") == self.session}
            self._save(scope)
        return self.close(identity)

    def inventory(self):
        try:
            engine = self.engine()
        except PreparationFailed:
            engine = None
        purposes, mappings = {}, {}
        def add(image, purpose, mapping=None):
            if IMAGE_ID.fullmatch(image or ""):
                purposes.setdefault(image, []).append(purpose)
                if mapping:
                    mappings.setdefault(image, []).append(mapping)
        for artifact in self.store.list("deployment"):
            build = artifact.get("build") or {}
            reuse = build.get("dependency_reuse") or {}
            add(build.get("image_id"), "worker:" + artifact["id"])
            add(reuse.get("dependency_image_id"), "dependency:" + artifact["id"], reuse.get("dependency_image"))
            add(build.get("resolved_base_id"), "base:" + artifact["id"], build.get("base_build_reference"))
        for spec in self.store.list("managed_service"):
            add(spec.get("image_id"), "service:" + spec["id"])
        for source in self.store.list("harness"):
            add((source.get("target_runtime") or {}).get("image_id"), "checker:" + source["id"])
        scopes = self.store.list("image_scope")
        for scope in scopes:
            if scope["status"] in ACTIVE | {"CLEANUP_PENDING"}:
                for image in scope["images"]:
                    add(image, "scope:" + scope["id"])
        rows = []
        for image, uses in purposes.items():
            row = dict(image_id=image, purposes=sorted(set(uses)), mappings=sorted(set(mappings.get(image, []))), checked_at=now())
            try:
                info = self.inspect(image)
                row.update(local_status="PRESENT" if info else "MISSING", facts=info)
            except PreparationFailed as error:
                row.update(local_status="UNKNOWN", reason=str(error))
            row["scopes"] = [s["id"] for s in scopes if s["status"] in ACTIVE and image in s["images"]]
            row["backups"] = [b["id"] for b in self.store.list("image_backup") if image in b["images"] and b["status"] == "VERIFIED_ARCHIVE"]
            rows.append(row)
        return dict(images=rows, checked_at=now(), engine=engine, size_basis="镜像 Size 含共享层；逐项之和为保守上界")

    def backup(self, body):
        with self.image_operation("save"):
            images = sorted(set(body.get("images") or []))
            if not images:
                raise ValueError("请选择导出镜像")
            # This path is deliberately not created: the operator must mount an
            # existing directory on a filesystem with sufficient space.
            directory = Path(body["directory"]).resolve(strict=True)
            if not directory.is_dir():
                raise ValueError("归档目录不是已有目录")
            engine = self.engine()
            mapping = self.archive_directory(directory)
            facts = {i: self.inspect(i) for i in images}
            if any(v is None for v in facts.values()):
                raise ValueError("选中镜像缺失；未导出")
            size = sum(i.get("Size", 0) for i in facts.values())
            reserve = body.get("reserve_bytes", 1024 ** 3)
            if type(reserve) is not int or reserve < 0:
                raise ValueError("reserve_bytes 须为非负整数")
            if shutil.disk_usage(directory).free < size + reserve:
                raise ValueError("备份目录空间不足（按镜像大小上界加运行余量）")
            for backup in self.store.list("image_backup"):
                if backup["status"] == "VERIFIED_ARCHIVE" and set(images) <= set(backup["images"]):
                    if Path(backup["path"]).is_file() and file_digest(backup["path"]) == backup["sha256"]:
                        return backup
            identity = uuid.uuid4().hex
            temporary, target = directory / (identity + ".tar.partial"), directory / (identity + ".tar")
            metadata, metadata_temporary = directory / (identity + ".json"), directory / (identity + ".json.partial")
            published = False
            scope = dict(id=uuid.uuid4().hex, engine=engine, references={})
            # Temporary references use the same ownership checks and remain
            # recoverable after a failed save or controller restart.
            scope.update(status="PREPARING", images=images, groups={}, bindings={}, run_ids=[], comparison_units=[],
                         tickets={}, accepting=False, measuring=False, created_at=now(), owner="backup")
            self.store.create("image_scope", scope["id"], scope)
            try:
                for image in images:
                    reference = self._reference(scope, image, create=True)
                    scope = self.get(scope["id"])
                    scope["references"][image] = reference
                    self._save(scope)
                self._command(["docker", "image", "save", "-o", str(temporary), *images])
                digest = file_digest(temporary)
                self._archive_check(temporary, images)
                rows = self.inventory()["images"]
                backup = dict(id=identity, images=images, facts=facts, path=str(target), sha256=digest,
                    bytes=temporary.stat().st_size, created_at=now(), status="VERIFIED_ARCHIVE", restored_at=None,
                    mappings={i: next((r["mappings"] for r in rows if r["image_id"] == i), []) for i in images},
                    purposes={i: next((r["purposes"] for r in rows if r["image_id"] == i), []) for i in images},
                    engine_id=engine["id"], archive_filesystem=dict(**mapping, free_bytes=shutil.disk_usage(directory).free))
                backup["metadata_path"] = str(metadata)
                metadata_temporary.write_text(json.dumps(backup, ensure_ascii=False, indent=2), encoding="utf-8")
                temporary.replace(target)
                metadata_temporary.replace(metadata)
                self.store.create("image_backup", identity, backup)
                published = True
                return backup
            finally:
                for partial in [temporary, metadata_temporary] + ([] if published else [target, metadata]):
                    if partial.exists():
                        partial.unlink()
                self.close(scope["id"])

    def archive_directory(self, directory):
        if not Path("/.dockerenv").exists():
            return dict(platform_path=str(directory), host_path=str(directory), mapping="HOST_PROCESS")
        mounts = self._json(["docker", "container", "inspect", "--format", "{{json .Mounts}}", os.environ.get("HOSTNAME", "")])
        for mount in sorted(mounts, key=lambda m: len(m.get("Destination", "")), reverse=True):
            target = Path(mount["Destination"]).resolve()
            if mount.get("Type") == "bind" and mount.get("RW") and directory.is_relative_to(target):
                return dict(platform_path=str(directory), host_path=str(Path(mount["Source"]) / directory.relative_to(target)), mapping="VERIFIED_BIND")
        raise ValueError("归档目录须为平台可见的可写宿主挂载；请先在部署配置中挂载有空间的目录")

    @staticmethod
    def _archive_check(path, images):
        # Read, never extract, Docker's manifest and config members.
        with tarfile.open(path) as archive:
            manifest = json.load(archive.extractfile("manifest.json"))
            found = set()
            for row in manifest:
                config = archive.extractfile(row["Config"])
                if config is None:
                    raise ValueError("归档缺少镜像配置")
                found.add("sha256:" + hashlib.sha256(config.read()).hexdigest())
                if any(not archive.getmember(layer).isfile() for layer in row.get("Layers", [])):
                    raise ValueError("归档缺少实际镜像层")
            # Containerd-backed engines can report an OCI index/manifest as
            # their local .Id. Keep that actual identity distinct from a config
            # digest and from RepoDigests; verify its referenced metadata bytes.
            if "index.json" in archive.getnames():
                pending = json.load(archive.extractfile("index.json")).get("manifests", [])
                seen = set()
                while pending:
                    descriptor = pending.pop()
                    identity = descriptor.get("digest", "")
                    media = descriptor.get("mediaType", "")
                    if identity in seen or not IMAGE_ID.fullmatch(identity) or not any(k in media for k in ("image.index", "image.manifest", "manifest.list", "manifest.v2")):
                        continue
                    seen.add(identity)
                    try:
                        member = archive.getmember("blobs/sha256/" + identity[7:])
                    except KeyError:
                        # Docker save may preserve an index referring to other
                        # platforms that were never present in this local store.
                        continue
                    if member.size > 8 * 1024 ** 2:
                        raise ValueError("镜像归档的索引元数据过大")
                    data = archive.extractfile(member).read()
                    if "sha256:" + hashlib.sha256(data).hexdigest() != identity:
                        raise ValueError("OCI 归档索引或 manifest 摘要不符")
                    found.add(identity)
                    pending.extend(json.loads(data).get("manifests", []))
            if not set(images) <= found:
                raise ValueError("归档没有完整覆盖选中镜像 ID")

    def restore(self, body):
        with self.image_operation("load"):
            backup = self.store.get("image_backup", body["backup_id"])
            if not backup or backup["status"] != "VERIFIED_ARCHIVE":
                raise ValueError("归档未完成校验")
            if not Path(backup["path"]).is_file() or file_digest(backup["path"]) != backup["sha256"]:
                raise ValueError("归档缺失或摘要不符")
            engine = self.engine()
            present = {i: self.inspect(i) for i in backup["images"]}
            if any(v is None for v in present.values()):
                required = sum(v.get("Size", 0) for i, v in backup["facts"].items() if present[i] is None)
                settings = self.store.get("image_settings", "current") or {}
                self.storage(engine, body.get("storage_path") or settings.get("storage_path"),
                    2 * required + body.get("reserve_bytes", settings.get("reserve_bytes", 1024 ** 3)))
                self._archive_check(backup["path"], backup["images"])
                self._command(["docker", "image", "load", "-i", backup["path"]])
            for image in backup["images"]:
                facts, old = self.inspect(image), backup["facts"][image]
                if not facts or any(facts.get(k) != old.get(k) for k in ("Id", "Os", "Architecture", "Config")):
                    raise ValueError("恢复后实际镜像 ID、架构或依赖配方不符")
            # Restore only verified builder names. Never overwrite a mutable
            # shared tag, including a user-selected newer base version.
            conflicts = []
            for image, names in backup.get("mappings", {}).items():
                for name in names:
                    if not name or name.startswith("-"):
                        raise ValueError("归档构建名称不正确")
                    current = self._json(["docker", "image", "inspect", "--format", '{{json .Id}}', name], absent=True)
                    if current and current != image:
                        conflicts.append(name)
                    elif current is None:
                        labels = backup["facts"][image].get("Config", {}).get("Labels") or {}
                        if name.startswith("seampact-environment:") and name != dependency_image_reference(labels.get(RECIPE_LABEL), labels.get(BASE_LABEL)):
                            raise ValueError("依赖名称与 recipe/base ID 不符")
                        if not (name.startswith("seampact-environment:") or name == "seampact-build-input:sha256-" + image[7:]):
                            # Restoring an old archive must not silently repoint
                            # a missing shared base tag chosen by another run.
                            conflicts.append(name)
                            continue
                        self._command(["docker", "tag", image, name], 120)
            backup.update(restored_at=now(), restore_status="NAME_CONFLICT" if conflicts else "VERIFIED", name_conflicts=conflicts)
            self.store.save("image_backup", backup["id"], backup)
            return backup

    def import_backup(self, body):
        with self.image_operation("import_metadata"):
            metadata = Path(body["metadata_path"]).resolve(strict=True)
            backup = json.loads(metadata.read_text(encoding="utf-8"))
            if (not re.fullmatch(r"[0-9a-f]{32}", backup.get("id", "")) or not backup.get("images")
                    or any(not IMAGE_ID.fullmatch(i) for i in backup["images"])):
                raise ValueError("归档元数据身份不正确")
            archive = metadata.parent / (backup["id"] + ".tar")
            if not archive.is_file() or file_digest(archive) != backup.get("sha256"):
                raise ValueError("归档缺失或摘要不符")
            self._archive_check(archive, backup["images"])
            backup.update(path=str(archive), metadata_path=str(metadata), status="VERIFIED_ARCHIVE", restored_at=None,
                archive_filesystem=dict(**self.archive_directory(metadata.parent), free_bytes=shutil.disk_usage(metadata.parent).free))
            previous = self.store.get("image_backup", backup["id"])
            if previous and previous["sha256"] != backup["sha256"]:
                raise ValueError("归档身份已用于其他内容")
            self.store.save("image_backup", backup["id"], backup)
            return backup

    def delete(self, body):
        with self.image_operation("delete"):
            if body.get("backup_id"):
                backup = self.store.get("image_backup", body["backup_id"])
                if not backup:
                    raise ValueError("归档记录不存在")
                if sorted(body.get("images") or []) != sorted(backup["images"]):
                    raise ValueError("须确认此归档覆盖的全部镜像 ID")
                path = Path(backup["path"])
                if path.exists():
                    if file_digest(path) != backup["sha256"]:
                        raise ValueError("文件身份已改变；未删除")
                    path.unlink()
                metadata = Path(backup.get("metadata_path") or path.with_suffix(".json"))
                if metadata.exists():
                    meta = json.loads(metadata.read_text(encoding="utf-8"))
                    if meta.get("id") == backup["id"] and meta.get("sha256") == backup["sha256"]:
                        metadata.unlink()
                backup["status"] = "DELETED"
                self.store.save("image_backup", backup["id"], backup)
                return backup
            image = body["image_id"]
            self.engine()
            if any(image in s["images"] and s["status"] in ACTIVE | {"CLEANUP_PENDING"} for s in self.store.list("image_scope")):
                raise ValueError("镜像仍由实验引用保护")
            if any((a.get(k) or {}).get("status") == "RUNNING" for a in self.store.list("deployment") for k in ("build", "publish")):
                raise ValueError("构建或发布仍在使用镜像")
            facts = self.inspect(image)
            if facts is None:
                return dict(status="MISSING")
            tags = facts.get("RepoTags") or []
            if sorted(tags) != sorted(body.get("tags") or []) or len(tags) > 1 or not body.get("confirm_unused"):
                raise ValueError("须确认实际名称、归属和不用状态；多标签冲突保留")
            refs = self._command(["docker", "ps", "-a", "--filter", "ancestor=" + image, "--format", "{{.ID}}"], 120)
            if refs:
                raise ValueError("镜像仍有容器引用")
            self._command(["docker", "image", "rm", "--no-prune", image], 120)
            return dict(status="DELETED" if self.inspect(image) is None else "PRESENT")

    @contextmanager
    def launch(self, group, run_id=None, scope_id=None):
        scope = self.scope_for(group, run_id, scope_id)
        if scope is None:
            yield None
            return
        identity, ticket = scope["id"], uuid.uuid4().hex
        with self.lock:
            scope = self.get(identity)
            if not scope.get("accepting") or scope.get("failure") or scope["status"] not in {"READY_TO_RUN", "RUNNING"}:
                raise PreparationFailed("范围已关闭或准备失败", identity)
            scope["tickets"][ticket] = dict(group_id=group, run_id=run_id, at=now(), session=self.session)
            self._save(scope)
        started = time.perf_counter()
        try:
            image = scope["groups"][group].get("image_id") or scope["groups"][group]["image"]
            self.verify(identity, image)
            yield dict(retention_scope_id=identity, image_id=image, check_ms=(time.perf_counter() - started) * 1000)
        except PreparationFailed as error:
            raise self._fail(identity, error) from error
        finally:
            with self.lock:
                current = self.get(identity)
                current["tickets"].pop(ticket, None)
                self._save(current)

    def close(self, identity):
        with self.lock:
            scope = self.get(identity)
            if scope["status"] == "CLOSED":
                return scope
            scope.update(accepting=False, status="CLOSING")
            self._save(scope)
            if scope["tickets"]:
                return scope
        deployments = self.platform.deployments
        consumers = [i for g in scope["groups"] for i in deployments.live_instances(g)]
        leases = getattr(self.platform.managed_services, "leases", {})
        if consumers or any(leases.get(g) for g in scope["groups"]):
            return self.get(identity)
        with self.lock:
            scope = self.get(identity)
            scope["measuring"] = False
            self._save(scope)
        try:
            self.engine(scope["engine"])
            # Final full check before releasing the references. Preserve earlier
            # complete comparison units; this marks only the scope's condition.
            if not scope.get("failure") and scope["status"] != "PREPARING":
                try:
                    self.verify(identity)
                except PreparationFailed:
                    pass
            for image in scope["images"]:
                ref = scope.get("references", {}).get(image)
                if not ref:
                    continue
                actual = self._container(ref.get("id") or ref["name"])
                if actual is None:
                    continue
                checked = self._reference(scope, image)
                self._command(["docker", "rm", checked["id"]], 120)
                if self._container(checked["id"]) is not None:
                    raise PreparationFailed("引用删除尚未确认")
            with self.lock:
                current = self.get(identity)
                current.update(status="CLOSED", ended_at=now(), cleanup_status="COMPLETED")
                self._save(current)
        except Exception as error:
            with self.lock:
                current = self.get(identity)
                current.update(status="CLEANUP_PENDING", cleanup_status="CLEANUP_PENDING", cleanup_reason=str(error))
                self._save(current)
        return self.get(identity)

    def snapshot(self):
        return dict(scopes=self.store.list("image_scope"), backups=self.store.list("image_backup"), operation=self.operation,
                    settings=self.store.get("image_settings", "current") or {})
