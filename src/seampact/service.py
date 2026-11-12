"""Repository generation, capability execution and the common Agent host."""
import copy
import json
import os
import threading
import uuid
from pathlib import Path

from seampact import __version__
from seampact.capability_catalog import CapabilityCatalog
from seampact.capability_contract import digest
from seampact.capability_generation import GenerationJobs
from seampact.chat import ChatJobs
from seampact.intake import RepositoryIntake, managed_checkout
from seampact.providers import Profiles
from seampact.resources import ResourceRegistry
from seampact.runtime import Coordinator, OwnerRuntime
from seampact.store import Store, now
from seampact.hosting.deployment import Deployments
from seampact.hosting.managed_services import ManagedServices
from seampact.hosting.images import Images
from seampact.observability.monitoring import ResourceMonitor
from seampact.scheduling.capability_scheduler import CapabilityScheduler
from seampact.run_history import RunHistory
from seampact.project_spaces import ProjectSpaces
from seampact.project_applications import ProjectApplications
from seampact.isolated_execution import DockerPythonExecutor

TASK_PAGE = 30
PROTOCOL = "capability-agent/1"

class Platform:
    def __init__(self, home, legacy=None):
        self.home = Path(home).resolve()
        self.home.mkdir(parents=True, exist_ok=True)
        self.protocol = PROTOCOL
        self.source_lock = threading.RLock()
        self.store = Store(self.home / "platform.sqlite3")
        self.profiles = Profiles(self.home / "config", legacy)
        self.resources = ResourceRegistry(self.home, self.store, self.profiles)
        # The controller still supplies instance identity and UNKNOWN handling;
        # new task/call records belong to the common Agent host.
        self.runtime = OwnerRuntime(Coordinator(self.store))
        self.intake = RepositoryIntake(self.home / "sources")
        self.capabilities = CapabilityCatalog(self.home, self.store)
        self.projects = ProjectSpaces(self.home, self.store)
        self.projects.default()
        execution_config = self.home / "config" / "execution.json"
        execution_options = json.loads(execution_config.read_text(encoding="utf-8")) if execution_config.exists() else {}
        python_image = os.environ.get("SEAMPACT_PYTHON_IMAGE") or execution_options.get("python_image")
        self.code_execution = DockerPythonExecutor(python_image) if python_image else None
        self.catalog = self.capabilities
        self.deployments = Deployments(self)
        self.capabilities.router = self.deployments
        self.managed_services = ManagedServices(self)
        self.images = Images(self)
        self.profiles.managed_services = self.managed_services
        self.execution = CapabilityScheduler(self.capabilities, self.deployments, store=self.store)
        self.generation = GenerationJobs(self)
        self.project_applications = ProjectApplications(self)
        self.chat = ChatJobs(self.store, self.profiles, self.capabilities, self.runtime,
                             self.execution, dispatch=self.execution.invoke, projects=self.projects,
                             code_execution=self.code_execution, applications=self.project_applications)
        # Historical runs are readable, but no old trainer or source checker starts.
        self.training = RunHistory(self.home, self.store)
        self.monitor = ResourceMonitor(self)

    def state(self):
        running = self.store.tasks_with_status("RUNNING", "CANCELLING", visible=True)
        recent, older = self.store.task_summaries(TASK_PAGE)
        active = {task["id"] for task in running}
        tasks = running + [task for task in recent if task["id"] not in active]
        harnesses = []
        for source in self.store.list("harness"):
            item = {key: source.get(key) for key in
                    ("id", "name", "address", "root", "revision", "status", "error", "generation_id", "detached_at")}
            item["files"] = len(source.get("files", []))
            harnesses.append(item)
        capabilities = self.capabilities.public()
        return dict(version=__version__, protocol=PROTOCOL, models=self.profiles.public_roles(),
            agent_host=dict(runtime="pi-agent-core", protocol="stdio-jsonl/1"),
            projects=self.projects.list(), default_project_id="default",
            code_execution=dict(configured=self.code_execution is not None, runtime="docker-python"),
            harnesses=harnesses, generations=self.generation.snapshot(), analyses=[], analysis_revisions=[],
            resources=dict(connections=self.public_connections(), weights=self.resources.public_weights(),
                           managed_services=self.managed_services.public(), dependencies=[], bindings=[]),
            units=capabilities, capabilities=capabilities, capability_packages=self.capabilities.installations(),
            tasks=tasks, tasks_older=older, runtime_groups=self.deployments.public(),
            deployments=self.store.list("deployment"),
            gpu_devices=copy.deepcopy(self.monitor.gpu_settings["devices"]),
            deployment_backend=self.deployments.backend(), image_retention=self.images.snapshot(),
            runtime_controller=self.deployments.controller_state(),
            scheduler=dict(status="RUNNING", policy="capability/1", execution=self.execution.status(),
                           research=self.training.snapshot(), active=[], waiting=[]))

    def _with_calls(self, tasks):
        # Completed tasks are cold records; do not rebuild them through a live coordinator.
        return tasks

    def tasks(self, before=None, limit=TASK_PAGE):
        """One older page of task summaries, continuing from a cursor state() or this returned."""
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("limit 须为 1–200 的整数")
        if before is not None and (type(before) is not int or before < 1):
            raise ValueError("before 须为上一页返回的游标")
        tasks, older = self.store.task_summaries(limit, before)
        return dict(tasks=self._with_calls(tasks), older=older)


    def task(self, identity):
        """A task's complete record, including the detail fields polling omits."""
        task = self.store.get("task", identity)
        if task is None or self.store.get("task_deleted", identity):
            raise ValueError("没有这个任务")
        return self._with_calls([task])[0]


    def public_connections(self):
        return self.resources.public_connections()

    def task_activity(self, identity):
        task = self.task(identity)
        session = (task.get("objective_result") or task.get("execution") or {}).get("session")
        groups, units = {}, {}
        for item in task.get("capability_pool", []):
            if not isinstance(item, dict):
                continue
            package = item.get("package")
            if isinstance(package, dict) and package.get("name"):
                groups[item.get("runtime_group")] = package["name"]
            elif isinstance(package, str):
                groups[item.get("runtime_group")] = package
            units[item.get("id")] = item.get("name")
        calls, instances = [], {}
        for call in task.get("calls", []):
            if not isinstance(call, dict):
                continue
            group = call.get("group_id")
            calls.append(dict(name=units.get(call.get("unit_id")) or groups.get(group, group), **{key: call.get(key) for key in (
                "instance_id", "outcome", "round_trip_ms", "start_wait_ms", "execute_ms", "cold_instance")}))
            key = (call.get("instance_id"), call.get("generation"))
            if key in instances or key[0] is None or key[1] is None:
                continue
            frozen = self.store.get("capability_resource_final", digest([session, *key])) if session else None
            measured = ((frozen or {}).get("totals") or {}).get("measured") or {}
            instances[key] = dict(name=groups.get(group, group), instance_id=key[0], generation=key[1],
                resident_s=((frozen or {}).get("instance") or {}).get("resident_s"),
                ram_byte_s=measured.get("ram_byte_s"), vram_byte_s=measured.get("vram_byte_s"))
        checks = []
        for event in task.get("events", []):
            if event.get("kind") not in {"hook_result", "hook_skipped"}:
                continue
            result = event.get("result")
            if result is None and event["kind"] == "hook_result":
                continue  # Internal context bookkeeping has no protective finding.
            action = (result.get("action") if isinstance(result, dict) else "block" if result is False else "allow")
            checks.append(dict(at=event.get("at"), phase=event.get("event"), hook=event.get("hook"),
                action="skip" if event["kind"] == "hook_skipped" else action,
                fact_support=result.get("fact_support") if isinstance(result, dict) else None,
                reason=result.get("reason") if isinstance(result, dict) and action == "block" else None))
        return dict(task_id=identity, project_id=task.get("project_id"), status=task["status"],
                    calls=calls, instances=list(instances.values()), checks=checks)


    def delete_tasks(self, body):
        """Remove ended tasks from the panel, retaining execution references."""
        identities = body.get("task_ids") if isinstance(body, dict) else None
        if (not isinstance(identities, list) or not identities
                or any(not isinstance(identity, str) or not identity.strip() for identity in identities)):
            raise ValueError("task_ids 需要非空的任务标识数组")
        identities = list(dict.fromkeys(identities))
        selected = set(identities)
        # Observe before the store transaction: the coordinator may persist
        # while answering. Ended task identities are never reused for new work.
        runtime = self.runtime.call("active_view")
        if any(call.get("task_id") in selected and call.get("physical") != "RETIRED"
               for call in runtime["calls"].values()):
            raise ValueError("所选任务仍有未结束或状态未确认的调用，请等待执行收尾后再删除记录")
        if any(root.get("task_id") in selected and root.get("state") not in {"RETIRED", "CANCELLED"}
               for root in runtime.get("roots", {}).values()):
            raise ValueError("所选任务仍有未结束或状态未确认的根请求，请等待执行收尾后再删除记录")
        with self.deployments.lock, self.managed_services.lock, self.store.transaction():
            if any(task_id in selected for task_id, _ in self.deployments.affinity):
                raise ValueError("所选任务仍占用运行实例，请等待任务租约释放后再删除记录")
            if any(selected.intersection(leases) for leases in self.managed_services.leases.values()):
                raise ValueError("所选任务仍占用模型服务，请等待服务租约释放后再删除记录")
            finished = {"COMPLETED", "FAILED", "INTERRUPTED", "CANCELLED", "CANCELED", "TIMEOUT", "TIMED_OUT", "STOPPED"}
            for identity in identities:
                task = self.store.get("task", identity)
                if task is None:
                    raise ValueError("没有这个任务：" + identity)
                if task.get("status") not in finished:
                    raise ValueError("只能删除已结束的任务记录，请等待所选任务结束")
            self.store.hide_tasks(identities)
        return dict(deleted=identities)


    def resource_catalog(self):
        return dict(connections=self.public_connections(), weights=self.resources.public_weights(),
                    managed_services=self.managed_services.public(), execution_environment="worker")

    def cleanup_project(self, body):
        project_id = body.get("project_id", "default")
        self.generation.reconcile_project_leases(project_id)
        self.chat.reconcile_project_leases(project_id)
        return self.projects.cleanup(project_id, body)

    def add_harness(self, body):
        source = self.intake.prepare(body.get("address", ""), body.get("revision"),
                                    body.get("entry") or None, body.get("resources") or None,
                                    discover_entries=False)
        configuration = body.get("configuration") or {}
        if not isinstance(configuration, dict):
            raise ValueError("已有配置须为 JSON 对象")
        def sensitive(value):
            if isinstance(value, dict):
                return any(str(k).lower().replace("_", "").replace("-", "") in
                    {"apikey", "password", "accesstoken", "secret"} or sensitive(v) for k, v in value.items())
            return isinstance(value, list) and any(sensitive(v) for v in value)
        if sensitive(configuration):
            raise ValueError("凭据请保存到资源连接，不放入仓库配置")
        source.update(configuration=configuration, user_resources=body.get("resources") or [], status="CONNECTED")
        clone = managed_checkout(self.intake.home, source.get("root"))
        if clone is not None:
            source["managed_clones"] = [str(clone)]
        self.store.save("harness", source["id"], source)
        result = dict(id=source["id"], files=len(source["files"]))
        if body.get("generate", True):
            try:
                result["generation"] = self.generation.start(source["id"])
            except ValueError as error:
                # The repository remains connected when the analysis model is missing.
                result["needs_input"] = str(error)
        return result

    def analyze(self, identity, **unused):
        return self.generation.start(identity)

    def detach_harness(self, identity):
        with self.source_lock:
            source = self.store.get("harness", identity)
            if not source:
                raise ValueError("没有这个来源")
            jobs = self.generation.snapshot(identity)
            if any(job["status"] in {"RUNNING", "CANCELLING"} for job in jobs):
                raise ValueError("来源正在生成，请先等待或取消")
            packages = [p for p in self.capabilities.installations() if p["source_id"] == identity]
            # Check every package first so a refused detach changes nothing.
            with self.capabilities.lock:
                for package in packages:
                    self.capabilities._require_released(package["group_id"])
                for package in packages:
                    self.capabilities.detach(package["group_id"])
            source.update(status="DETACHED", detached_at=now())
            self.store.save("harness", identity, source)
        return dict(harness_id=identity, status="DETACHED")

    def restore_harness(self, identity):
        source = self.store.get("harness", identity)
        if not source:
            raise ValueError("没有这个来源")
        source.update(status="CONNECTED", detached_at=None)
        self.store.save("harness", identity, source)
        return self.generation.start(identity)

    def clear_pool(self):
        return dict(detached=[self.detach_harness(s["id"]) for s in self.store.list("harness")
                              if s.get("status") != "DETACHED"], history="RETAINED")

    def analysis_snapshot(self, identity):
        result = self.store.get("analysis", identity)
        if result is None:
            raise ValueError("没有这个历史分析")
        return dict(analysis=result, read_only=True)

    def export_analysis(self, identity):
        return self.analysis_snapshot(identity)

    def preprocessing_summary(self, identity):
        value = self.store.get("preprocessing", identity)
        if value is None:
            raise ValueError("没有这个历史预处理")
        return value

    def revision_detail(self, identity):
        value = self.store.get("analysis_revision", identity)
        if value is None:
            raise ValueError("没有这个历史修订")
        return value

    def close(self):
        self.generation.close()
        self.chat.close()
        self.execution.close()
        self.monitor.close()
        self.deployments.close()
        self.runtime.close()
        self.store.close()
