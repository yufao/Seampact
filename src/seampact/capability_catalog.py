"""Installed capability packages and their callable capabilities.

A package folder is installed as immutable content under the platform home.
Each of its capabilities becomes one record carrying the same description the
generator wrote: the model sees its input interface as a tool, the instance
manager reads its lifecycle operations, and a call validates against it.
Execution always happens in an instance of the package's worker.
"""

import hashlib
import os
import shutil
import threading
import uuid
from pathlib import Path

from seampact.capability_contract import digest, load_package, package_digest, tool_definition, validate_value
from seampact.hosting.capability_hosting import GROUP_KIND, cancel as cancel_call, group_identity, instance_contract
from seampact.store import now

UNIT_SCHEMA = "capability-unit/1"
RETIRED = {"SUPERSEDED", "DETACHED"}


class ArtifactStore:
    """Files delivered by capability calls, kept by content identity."""

    def __init__(self, root):
        self.root = Path(root)

    def path(self, sha256):
        return self.root / sha256[:2] / sha256

    def save(self, stream, sha256):
        target = self.path(sha256)
        if target.is_file():
            return str(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(target.name + "." + uuid.uuid4().hex + ".partial")
        actual = hashlib.sha256()
        try:
            with partial.open("wb") as output:
                for block in iter(lambda: stream.read(1 << 20), b""):
                    actual.update(block)
                    output.write(block)
            if actual.hexdigest() != sha256:
                raise ValueError("交付文件内容与实例报告的身份不一致")
            os.replace(partial, target)
        finally:
            partial.unlink(missing_ok=True)
        return str(target)


class CapabilityCatalog:
    def __init__(self, home, store):
        self.home, self.store = Path(home), store
        self.packages = self.home / "packages"
        self.artifacts = ArtifactStore(self.home / "artifacts")
        self.router = None  # the instance manager; set once it exists
        self.session = uuid.uuid4().hex
        self.lock = threading.RLock()
        self._image_work = threading.Lock()
        self._slots, self._preparing = {}, {}

    # Installation.

    def install(self, folder, bindings=None):
        """Install a generated package folder and register its capabilities."""
        manifest = load_package(folder)
        package = package_digest(manifest)
        group_id = group_identity(manifest)
        with self.lock:
            self._require_released(group_id)
            target = self.packages / package
            if target.exists():
                if package_digest(load_package(target)) != package:
                    raise ValueError("已安装的能力包内容与其身份不一致")
            else:
                self.packages.mkdir(parents=True, exist_ok=True)
                staging = self.packages / (package + "." + uuid.uuid4().hex + ".partial")
                try:
                    shutil.copytree(folder, staging, ignore=shutil.ignore_patterns("__pycache__"))
                    if package_digest(load_package(staging)) != package:
                        raise ValueError("能力包在安装过程中发生变化")
                    os.replace(staging, target)
                finally:
                    shutil.rmtree(staging, ignore_errors=True)
            if bindings is None:
                # A regenerated package keeps the bindings of resources it still declares.
                previous = self.store.get("runtime_group", group_id) or {}
                kinds = {resource["id"]: resource["kind"] for resource in manifest["resources"]}
                bindings = {identity: binding for identity, binding in previous.get("bindings", {}).items()
                            if identity in kinds and kinds[identity] == previous.get("resource_kinds", {}).get(identity)}
            return self._register(manifest, package, group_id, bindings)

    def bind(self, group_id, bindings):
        """Set or clear (None) resource bindings of an installed package."""
        if not isinstance(bindings, dict):
            raise ValueError("资源绑定须为对象")
        with self.lock:
            group = self._group(group_id)
            self._require_released(group_id)
            merged = dict(group["bindings"])
            for identity, binding in bindings.items():
                if binding is None:
                    merged.pop(identity, None)
                else:
                    merged[identity] = binding
            _, manifest = self.package(group["package_digest"])
            return self._register(manifest, group["package_digest"], group_id, merged)

    def _register(self, manifest, package, group_id, bindings):
        contract = instance_contract(group_id, manifest, package, bindings)
        installation = digest([package, contract["bindings_digest"]])[:32]
        missing = contract["missing_resources"]
        status = "NEEDS_INPUT" if missing else "READY"
        reason = "缺少必需资源绑定：" + ", ".join(missing) if missing else None
        source = manifest["source"]
        units = [dict(schema=UNIT_SCHEMA, id=installation + ":" + capability["id"], entry_id=capability["id"],
                      name=capability["name"], tool_name=capability["id"][:50] + "_" + group_id[4:10],
                      description=capability["purpose"], parameters=capability["input"], output=capability["output"],
                      capability=capability, package=manifest["name"], package_digest=package,
                      source_id=source.get("id"), source_version=source.get("version"),
                      installation_id=installation, runtime_group=group_id, status=status, reason=reason,
                      installed_at=now())
                 for capability in manifest["capabilities"]]
        if manifest["generator"].get("template") == "repository-python-adapter/1":
            for unit in units:
                previous = self.store.get("capability", unit["id"]) or {}
                unit["generation_verification"] = previous.get("generation_verification") or dict(
                    status="PENDING", reason="生成能力尚未完成真实调用验证")
        group = dict(id=group_id, kind=GROUP_KIND, name=manifest["name"], status=status, gaps=[reason] if reason else [],
                     source_id=source.get("id"), source_version=source.get("version"), installation_id=installation,
                     package_digest=package, entries=[unit["entry_id"] for unit in units],
                     units=[unit["id"] for unit in units], bindings=bindings, resources=manifest["resources"],
                     resource_kinds={resource["id"]: resource["kind"] for resource in manifest["resources"]},
                     contract=contract, detached=False, installed_at=now())
        with self.store.transaction():
            for previous in self.store.list("capability"):
                if (previous["runtime_group"] == group_id and previous["installation_id"] != installation
                        and previous["status"] not in RETIRED):
                    previous.update(status="SUPERSEDED", superseded_by=installation)
                    self.store.save("capability", previous["id"], previous)
            for unit in units:
                self.store.save("capability", unit["id"], unit)
            self.store.save("runtime_group", group_id, group)
        print("[SeamPact] capability_package_installed " + group_id, flush=True)
        return self.installation(group_id)

    def detach(self, group_id):
        with self.lock:
            group = self._group(group_id)
            self._require_released(group_id)
            with self.store.transaction():
                for unit_id in group["units"]:
                    unit = self.store.get("capability", unit_id)
                    unit.update(status="DETACHED", detached_at=now())
                    self.store.save("capability", unit_id, unit)
                group.update(detached=True, detached_at=now())
                self.store.save("runtime_group", group_id, group)
        return dict(group_id=group_id, detached=True)

    def _require_released(self, group_id):
        if self.router is None:
            return
        self.router._guard(group_id, None)
        if self.router.live_instances(group_id):
            raise ValueError("该能力包仍有实例或未确认释放的对象；请先排空并停止，再变更安装或绑定")
        thread = self._preparing.get(group_id)
        if thread is not None and thread.is_alive():
            raise ValueError("该能力包正在准备运行环境，请等待结束")

    def _group(self, group_id):
        group = self.store.get("runtime_group", group_id)
        if not group or group.get("kind") != GROUP_KIND or group.get("detached"):
            raise ValueError("没有这个已安装的能力包")
        return group

    def package(self, package):
        """The installed folder and verified manifest of a package identity."""
        root = self.packages / package
        manifest = load_package(root)
        if package_digest(manifest) != package:
            raise ValueError("已安装的能力包内容与其身份不一致")
        return root, manifest

    # Preparation of the run environment.

    def prepare(self, group_id, body=None, wait=False):
        """Build the worker image for the current installation in the background."""
        with self.lock:
            group = self._group(group_id)
            if group["contract"]["missing_resources"]:
                raise ValueError(group["gaps"][0])
            if self.router is None:
                raise ValueError("实例管理尚未接通")
            self._require_released(group_id)
            record = dict(id=group_id, status="PREPARING", contract_digest=group["contract"]["digest"],
                          session=self.session, started_at=now(), finished_at=None, reason=None, steps=[])
            self.store.save("capability_preparation", group_id, record)
            thread = threading.Thread(target=self._prepare, args=(group_id, record, body or {}), daemon=True,
                                      name="seampact-capability-prepare")
            self._preparing[group_id] = thread
            thread.start()
        if wait:
            thread.join()
        return self.installation(group_id)

    def _prepare(self, group_id, record, body):
        def step(name, **facts):
            record["steps"].append(dict(name=name, at=now(), **facts))
            self.store.save("capability_preparation", group_id, record)

        def finished(field):
            # Image work runs in the instance manager's own background thread.
            while True:
                attempt = (self.store.get("deployment", group_id) or {}).get(field) or {}
                if attempt.get("status") != "RUNNING":
                    return attempt
                if self.router.closed.wait(1.0):
                    raise ValueError("平台已停止")

        def tail(attempt):
            steps = attempt.get("steps") or [{}]
            return (steps[-1].get("stderr_tail") or steps[-1].get("stdout_tail") or "")[-2000:]

        try:
            # The instance manager admits one image operation at a time.
            with self._image_work:
                artifact = self.router.export(group_id, body)
                step("export", status="DONE", image=artifact["image"])
                self.router.build(group_id, {})
                build = finished("build")
                step("build", status=build.get("status"), log_tail=None if build.get("status") == "BUILT" else tail(build))
                if build.get("status") != "BUILT":
                    raise ValueError("运行镜像构建未完成：" + str(build.get("reason") or build.get("status")))
                self.router.publish(group_id, {})
                publish = finished("publish")
                step("publish", status=publish.get("status"))
                if publish.get("status") not in {"PUBLISHED", "NOT_REQUIRED", "OPERATOR_REPORTED"}:
                    raise ValueError("运行镜像尚未对部署后端可达：" + str(publish.get("reason") or publish.get("status")))
            record.update(status="READY")
        except Exception as error:
            record.update(status="FAILED", reason=str(error))
        record["finished_at"] = now()
        self.store.save("capability_preparation", group_id, record)
        print("[SeamPact] capability_package_prepared " + group_id + " " + record["status"], flush=True)

    def availability(self, group):
        """Preparing, available, needs input or failed, each with its actual reason."""
        if group["contract"]["missing_resources"]:
            return dict(status="NEEDS_INPUT", reason=group["gaps"][0])
        preparation = self.store.get("capability_preparation", group["id"]) or {}
        current = preparation.get("contract_digest") == group["contract"]["digest"]
        if current and preparation["status"] == "PREPARING":
            if preparation.get("session") == self.session:
                return dict(status="PREPARING", reason=None)
            return dict(status="FAILED", reason="平台重启中断了运行环境准备，请重新准备")
        gap = self.router.start_gap(group["id"]) if self.router is not None else "实例管理尚未接通"
        if gap is None:
            return dict(status="AVAILABLE", reason=None)
        if current and preparation["status"] == "FAILED":
            return dict(status="FAILED", reason=preparation["reason"])
        return dict(status="NOT_PREPARED", reason=gap)

    # Views.

    def installation(self, group_id):
        group = self._group(group_id)
        resources = [dict(resource, bound=resource["id"] in group["bindings"],
                          binding=group["bindings"].get(resource["id"])) for resource in group["resources"]]
        return dict(group_id=group_id, name=group["name"], package_digest=group["package_digest"],
                    installation_id=group["installation_id"], source_id=group["source_id"],
                    source_version=group["source_version"], installed_at=group["installed_at"],
                    capabilities=list(group["units"]), resources=resources, contract_digest=group["contract"]["digest"],
                    availability=self.availability(group),
                    preparation=self.store.get("capability_preparation", group_id))

    def installations(self):
        return [self.installation(group["id"]) for group in self.store.list("runtime_group")
                if group.get("kind") == GROUP_KIND and not group.get("detached")]

    def unit(self, unit_id):
        unit = self.store.get("capability", unit_id)
        if not unit or unit["status"] in RETIRED:
            raise ValueError("没有这个已安装的能力")
        return unit

    def public(self):
        """Current capabilities with whether a call could be dispatched now."""
        result, availability = [], {}
        for unit in self.store.list("capability"):
            if unit["status"] in RETIRED:
                continue
            group_id = unit["runtime_group"]
            if group_id not in availability:
                availability[group_id] = self.availability(self._group(group_id))
            unit["availability"] = availability[group_id]
            verification = unit.get("generation_verification")
            if verification and verification["status"] != "SUCCEEDED" and unit["availability"]["status"] == "AVAILABLE":
                unit["availability"] = dict(status="NEEDS_INPUT", reason=verification.get("reason") or "生成能力尚未完成真实调用验证")
            if unit["availability"]["status"] != "AVAILABLE":
                unit["dispatch"] = dict(status="UNAVAILABLE", reason=unit["availability"]["reason"])
            else:
                fact = self.router.runtime_fact(group_id, unit["id"])
                unit["dispatch"] = dict(status="READY_INSTANCE" if fact["ready_instance"] else
                                        "COLD_STARTABLE" if fact["cold_startable"] else "UNAVAILABLE",
                                        warm=fact["warm"], busy=fact["busy"], reason=fact.get("reason"))
            result.append(unit)
        return result

    def callable_units(self):
        return [unit for unit in self.public() if unit["dispatch"]["status"] != "UNAVAILABLE"]

    def tools(self):
        return [tool_definition(unit["tool_name"], unit["capability"]) for unit in self.callable_units()]

    def tools_for_project(self, project_id):
        """Offer only global capabilities and those bound to this project."""
        offered = []
        for unit in self.callable_units():
            group = self._group(unit["runtime_group"])
            owners = {binding["project_id"] for binding in group.get("bindings", {}).values()
                      if binding.get("form") == "project"}
            if not owners or owners == {project_id}:
                offered.append(tool_definition(unit["tool_name"], unit["capability"]))
        return offered

    def name_to_id(self, name):
        found = [unit["id"] for unit in self.callable_units() if unit["tool_name"] == name]
        if len(found) != 1:
            raise ValueError("没有这个可调用的能力：" + str(name))
        return found[0]

    # Calls.

    def validate_input(self, unit_id, arguments):
        validate_value(arguments, self.unit(unit_id)["parameters"], "input")

    def _slot(self, group):
        key = (group["id"], group["contract"]["digest"])
        with self.lock:
            if key not in self._slots:
                self._slots[key] = threading.BoundedSemaphore(group["contract"]["concurrency"])
            return self._slots[key]

    def invoke(self, unit_id, arguments, on_event=None, run_id=None):
        """Run one capability on an instance, starting one on demand.

        Returns the output, delivered files and the call's execution facts.
        Calls beyond the instance's declared concurrency wait here.
        """
        unit = self.unit(unit_id)
        if unit["status"] != "READY":
            raise ValueError("能力尚不可用：" + str(unit["reason"]))
        validate_value(arguments, unit["parameters"], "input")
        group = self._group(unit["runtime_group"])
        placement = dict(group_id=group["id"], run_id=run_id, contract_digest=group["contract"]["digest"],
                         start_timeout_s=self.router.backend()["start_timeout_s"])
        with self._slot(group):
            try:
                result, facts = self.router.invoke_placed(placement, unit, arguments, on_event=on_event)
            except Exception as error:
                self.record_verification(unit_id, "FAILED", str(error))
                raise
        self.record_verification(unit_id, "SUCCEEDED", call=facts)
        return dict(result, call=facts)

    def record_verification(self, unit_id, status, reason=None, call=None):
        """Expose a generated tool after an actual successful worker invocation.

        Explicit user calls can supply a missing example later. An ordinary
        later business failure does not erase an earlier successful validation.
        """
        with self.lock:
            unit = self.unit(unit_id)
            previous = unit.get("generation_verification")
            if previous is None or (previous["status"] == "SUCCEEDED" and status != "SUCCEEDED"):
                return
            unit["generation_verification"] = dict(status=status, reason=reason, at=now(),
                call={name: call.get(name) for name in ("call_id", "instance_id", "generation")} if call else None)
            self.store.save("capability", unit_id, unit)

    def cancel(self, call_id):
        """Request cancellation of an in-flight call named by its call_started event."""
        return cancel_call(self.router, call_id)
