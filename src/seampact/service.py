"""The four-layer research prototype: intake, analysis, Units, RL and chat."""

import uuid
import copy
import json
import math
import re
import threading
import time
from pathlib import Path, PurePosixPath, PureWindowsPath

from . import __version__
from .analysis import AnalysisJobs
from .chat import ChatJobs
from .inspection import Inspector
from .intake import RepositoryIntake
from .providers import Profiles
from .resources import ResourceRegistry
from .dependencies import Dependencies
from .runtime import Coordinator, OwnerRuntime
from .scheduler import TwoLayerScheduler
from .store import Store, now
from .units import UnitCatalog
from .deployment import Deployments, REMOTE_PROFILE, GROUP_PROFILE
from .training import TrainingJobs
from .environments import discover_environment, normalize_environment
from .target_runtime import attach_target, checker_identity, environment_selection, prepare_checker

NATIVE_PROFILE = dict(id="python311-local-explicit-lifetime/1", confirmed=True,
    interpreter="Python 3.11", host="local-native-process",
    frame_observation_excluded=True, explicit_lifetime=True,
    source="用户2026-10-14确认的有限宿主范围")

RULESET = dict(id="native-entry-construction-inplace-runtime-group/3", confirmed=True,
    source="用户确认的首版执行判断",
    rules=["真实源码入口、参数、初始化和资源绑定", "缺失依赖或参数明确保留为MISSING",
           "内部W/H/R保留原controller与owner", "B-CALL原位置构造与原对象引用",
           "有限R提取及原位调用规则", "真实结果数据边及原顺序控制边",
           "粗细方案互斥", "原对象/共享状态/控制依赖保留同进程及原生命周期",
           "远程条件独立核对，JSON不是合法拆分依据", "重启不默认恢复原会话"],
    parallel=False, remote="PER_ENTRY_CHECKED", remote_profile=REMOTE_PROFILE, group_profile=GROUP_PROFILE)


class Platform:
    def __init__(self, home, legacy=None):
        self.home = Path(home).resolve()
        self.home.mkdir(parents=True, exist_ok=True)
        self.source_lock = threading.RLock()
        self.store = Store(self.home / "platform.sqlite3")
        self.profiles = Profiles(self.home / "config", legacy)
        self.resources = ResourceRegistry(self.home, self.store, self.profiles)
        self.dependencies = Dependencies(self.store, self.profiles, self.resources)
        self.scheduler = TwoLayerScheduler(self.home / "policy")
        self.runtime = OwnerRuntime(Coordinator(self.store, policy=self.scheduler))
        self.intake = RepositoryIntake(self.home / "sources")
        self.catalog = UnitCatalog(self.home, self.store, self.runtime)
        self.catalog.resource_registry = self.dependencies
        self.deployments = Deployments(self)
        self.analysis = AnalysisJobs(self.store, self.profiles, on_complete=self._connect_units,
                                     check_entries=self.catalog.inspect_candidate)
        self.chat = ChatJobs(self.store, self.profiles, self.catalog, self.runtime, self.scheduler)
        self.inspectors = {}
        for source in self.store.list("harness"):
            if source.get("status") == "DETACHED":
                self.catalog.detach(source["id"])
            elif source.get("runtime_environment"):
                self._checker_record(source, restarted=True)
                if source.get("environment_pending_recheck"):
                    self._invalidate_environment_units(source, "运行环境尚未重新核对；原能力记录保留为历史")
                    self.store.save("harness", source["id"], source)
        for job in self.store.list("analysis"):
            if job.get("status") != "COMPLETED" or not job.get("submitted"):
                continue
            request = self.store.get("request", job["request_id"])
            if not request:
                continue
            source = self.store.get("harness", request.get("source_id"))
            if source and source.get("status") != "DETACHED" and source.get("analysis_id") == job["id"]:
                if source.get("environment_pending_recheck"):
                    continue
                try:
                    inspector = self._attach_inspector(Inspector(source["root"], self.home / "snapshots", request), source)
                    self.inspectors[request["id"]] = inspector
                    self.dependencies.record(inspector, job["submitted"]["candidate"])
                    self.catalog.install(inspector, job["submitted"]["candidate"], job["id"], source.get("installation_id"))
                except ValueError as error:
                    if not request.get("runtime_environment") and not source.get("runtime_environment"):
                        raise
                    record = source.get("target_runtime") or {}
                    source.update(environment_pending_recheck=True, error=str(error),
                        target_runtime=dict(record, status="NOT_PREPARED", reason=str(error)))
                    self._invalidate_environment_units(source, str(error))
                    self.store.save("harness", source["id"], source)
        self.training = TrainingJobs(self)

    def state(self):
        snapshot = self.runtime.snapshot()
        active = [call for call in snapshot["calls"].values() if call["physical"] in {"AUTHORIZED", "STARTED"}]
        waiting = [call for call in snapshot["calls"].values() if call["physical"] in {"ENABLED", "RESERVED"}]
        tasks = self.store.list("task")
        for task in tasks:
            calls = [call for call in snapshot["calls"].values() if call.get("task_id") == task["id"]]
            if calls or task.get("status") == "RUNNING":
                task["calls"] = calls
        harnesses = []
        for source in self.store.list("harness"):
            harnesses.append({name: source.get(name) for name in
                ("id", "name", "address", "revision", "status", "error", "analysis_id", "entry", "resources", "detached_at",
                 "runtime_environment", "environment_discovery", "target_runtime", "environment_pending_recheck")})
            harnesses[-1]["files"] = len(source.get("files", []))
            harnesses[-1]["inventory_count"] = len(source.get("inventory", []))
            job = self.store.get("analysis", source.get("analysis_id"))
            if source.get("status") != "DETACHED" and job and job["status"] in {"FAILED", "INTERRUPTED", "CANCELLED"}:
                harnesses[-1].update(status=job["status"], error=job.get("error"))
        jobs = [dict({name: value for name, value in job.items()
                      if name not in {"messages", "provider_profile"}},
                     resumable=self.analysis.can_resume(job))
                for job in self.store.list("analysis")]
        for job in jobs:
            if not job.get("harness_id"):
                request = self.store.get("request", job.get("request_id"))
                job["harness_id"] = (request or {}).get("source_id")
        requirements, selections = self.dependencies.public()
        return dict(version=__version__, models=self.profiles.public_roles(), harnesses=harnesses,
            resources=dict(connections=self.public_connections(), weights=self.resources.public_weights(),
                           dependencies=requirements, bindings=selections),
            analyses=jobs, units=self.catalog.public(), tasks=tasks, rules=RULESET, entry_profile=NATIVE_PROFILE,
            runtime_groups=self.deployments.public(), deployments=self.store.list("deployment"),
            deployment_backend=self.deployments.backend(),
            runtime_controller=self.deployments.controller_state(),
            scheduler=dict(status="RUNNING", policy="candidate-RL / FIFO-dispatch", active=active, waiting=waiting,
                used=snapshot["used"], quota=snapshot["quota"], theory_pending=False,
                learning=self.scheduler.snapshot(), research=self.training.snapshot(),
                reward="研究运行使用冻结分项判据；普通方案保留历史完成/时延反馈"))

    def public_connections(self):
        return self.resources.public_connections()

    def resource_catalog(self):
        return dict(connections=self.profiles.public_connections(), weights=self.resources.public_weights(),
                    execution_environment="platform_process", platform_home=str(self.home))

    def add_harness(self, body):
        source = self.intake.prepare(body.get("address", ""), body.get("revision"),
                                    body.get("entry") or None, body.get("resources") or None)
        source["entry_profile"] = dict(NATIVE_PROFILE)
        configuration = body.get("configuration") or {}
        if not isinstance(configuration, dict):
            raise ValueError("初始化配置需要JSON对象")
        def contains_api_key(value):
            if isinstance(value, dict):
                return any(str(key).lower().replace("-", "").replace("_", "") == "apikey"
                           or contains_api_key(item) for key, item in value.items())
            return isinstance(value, list) and any(contains_api_key(item) for item in value)
        if contains_api_key(configuration):
            raise ValueError("初始化配置不接收API密钥，请使用模型配置或原生环境变量")
        source["configuration"] = configuration
        source["user_resources"] = body.get("resources") or []
        source["status"] = "CONNECTED"
        inspector = self._environment_inspector(source)
        source["environment_discovery"] = discover_environment(inspector)
        if body.get("environment"):
            source["runtime_environment"] = normalize_environment(body["environment"], inspector)
        self.store.save("harness", source["id"], source)
        print("[SeamPact] source_connected " + source["id"], flush=True)
        return dict(id=source["id"], files=len(source["files"]))

    def _environment_inspector(self, source):
        request = self.store.get("request", source.get("request_id"))
        if request:
            return Inspector(source["root"], self.home / "snapshots", copy.deepcopy(request))
        return Inspector.freeze_repository(source, self.home / "snapshots", uuid.uuid4().hex,
                                            "读取原库运行环境声明，不执行原库或分析模型。")

    def _attach_inspector(self, inspector, source, selected=False):
        if selected and source.get("runtime_environment"):
            # A new analysis can freeze newer manifest bytes. Revalidate the
            # choice against those bytes before any paid model request.
            environment = normalize_environment(source["runtime_environment"], inspector)
            source.update(runtime_environment=environment, environment_discovery=environment["discovery"])
        current = environment_selection(source.get("runtime_environment") or {})
        if selected:
            if current:
                inspector.request["runtime_environment"] = current
                inspector.request["target_runtime"] = {key: value for key, value in source.get("target_runtime", {}).items()
                    if key in {"status", "identity", "image", "image_id", "python", "base_image"}}
            else:
                inspector.request.pop("runtime_environment", None)
                inspector.request.pop("target_runtime", None)
        elif environment_selection(inspector.request.get("runtime_environment") or {}) != current:
            raise ValueError("当前运行环境与原分析冻结环境不同，请先零模型重新核对候选")
        if inspector.request.get("runtime_environment"):
            record = self._checker_record(source)
            attach_target(inspector, record, self.deployments.backend()["operation_timeout_s"])
        return inspector

    def _invalidate_environment_units(self, source, reason):
        """Keep reports as history while removing obsolete callable bindings."""
        with self.catalog.lock:
            for unit in self.store.list("unit"):
                if unit.get("source_id") != source["id"] or unit.get("status") in {"SUPERSEDED", "DETACHED"}:
                    continue
                binding = self.catalog.bindings.pop(unit["id"], None)
                deferred = self.catalog.deferred.pop(unit["id"], None)
                if binding or deferred:
                    (binding or deferred[0]).detached = True
                self.catalog.entry_specs.pop(unit["id"], None)
                unit.update(status="MISSING", reason=reason)
                self.store.save("unit", unit["id"], unit)
        # Current groups use this installation identity.  An old contract or
        # artifact must not stay actionable after its environment was changed.
        source["installation_id"] = None

    def _checker_record(self, source, restarted=False):
        environment = source.get("runtime_environment") or {}
        record = source.get("target_runtime") or {}
        if not environment:
            return record
        stale = record.get("identity") != checker_identity(environment)
        interrupted = restarted and record.get("status") == "PREPARING"
        incomplete = record.get("status") == "READY" and not record.get("image_id")
        if stale or interrupted or incomplete:
            reason = ("平台代码或所选环境已变化，请重新准备目标解释器" if stale else
                      "上次环境准备未完成，请重新准备目标解释器" if interrupted else
                      "目标解释器缺少实际镜像记录，请重新准备")
            record = dict(record, status="NOT_PREPARED", python=environment["python"],
                          base_image=environment["base_image"], reason=reason)
            source.update(target_runtime=record, environment_pending_recheck=bool(source.get("analysis_id")))
            self._invalidate_environment_units(source, reason)
            self.store.save("harness", source["id"], source)
        return record

    def _environment_guard(self, source):
        if source.get("status") in {"DETACHED", "ANALYZING"} or (source.get("target_runtime") or {}).get("status") == "PREPARING":
            raise ValueError("请先结束当前分析或环境准备，再调整运行环境")
        if self.catalog.source_running(source["id"]):
            raise ValueError("请等原调用结束后再调整运行环境")
        for group in self.store.list("runtime_group"):
            if group["source_id"] != source["id"]:
                continue
            self.deployments._guard(group["id"], None)
            if any(self.deployments._held(item) for item in self.deployments._instances(group["id"])):
                raise ValueError("该来源仍有运行实例；请先排空并停止，再调整运行环境")
            artifact = self.store.get("deployment", group["id"]) or {}
            if any(artifact.get(field, {}).get("status") == "RUNNING" for field in ("build", "publish")):
                raise ValueError("该来源正在构建或发布镜像，请结束后再调整环境")

    def configure_environment(self, body):
        with self.source_lock:
            source = self.store.get("harness", body.get("harness_id"))
            if not source:
                raise ValueError("Harness 尚未接入")
            self._environment_guard(source)
            inspector = self._environment_inspector(source)
            environment = normalize_environment(body.get("environment", source.get("runtime_environment") or {}), inspector)
            changed = environment_selection(environment) != environment_selection(source.get("runtime_environment") or {})
            record = source.get("target_runtime") or {}
            if record.get("identity") != checker_identity(environment):
                record = dict(status="NOT_PREPARED", python=environment["python"], base_image=environment["base_image"])
            source.update(runtime_environment=environment, environment_discovery=environment["discovery"], target_runtime=record)
            record = self._checker_record(source)
            if changed:
                source["environment_pending_recheck"] = bool(source.get("analysis_id"))
                # Keep history, but an environment edit cannot leave old local
                # bindings callable under a newly displayed configuration.
                self._invalidate_environment_units(source, "运行环境已修改，请零模型重新核对已有候选")
            if body.get("prepare"):
                # An explicit preparation can restore a removed image even if
                # the last persisted receipt still says READY.
                source["target_runtime"] = dict(record, status="PREPARING", started_at=now())
                config = self.deployments.backend()
                def prepare():
                    try:
                        answer = prepare_checker(self.home, environment, config)
                    except Exception as error:
                        answer = dict(status="FAILED", reason=str(error))
                    with self.source_lock:
                        current = self.store.get("harness", source["id"])
                        current["target_runtime"] = dict(answer, finished_at=now())
                        if record.get("image_id") and (answer.get("status") != "READY" or
                                answer.get("image_id") != record["image_id"]):
                            current["environment_pending_recheck"] = bool(current.get("analysis_id"))
                            self._invalidate_environment_units(current, "目标解释器镜像已变化或不可用，请重新准备并核对已有候选")
                        self.store.save("harness", current["id"], current)
                self.store.save("harness", source["id"], source)
                threading.Thread(target=prepare, daemon=True).start()
                return dict(harness_id=source["id"], runtime_environment=environment, target_runtime=source["target_runtime"])
            self.store.save("harness", source["id"], source)
            if body.get("recheck"):
                return self._recheck_environment(source)
            return dict(harness_id=source["id"], runtime_environment=environment, target_runtime=record)

    def _recheck_environment(self, source):
        previous = self.store.get("analysis", source.get("analysis_id"))
        if not previous or previous.get("status") != "COMPLETED" or not previous.get("submitted"):
            raise ValueError("尚无完整分析候选；准备目标解释器后再开始分析")
        request = copy.deepcopy(self.store.get("request", previous["request_id"]))
        if not request:
            raise ValueError("原分析的冻结源码请求缺失，请重新分析")
        request["id"] = uuid.uuid4().hex
        request.update(resource_catalog=self.resource_catalog(),
                       dependency_bindings=self.dependencies.selections(source["id"]))
        inspector = self._attach_inspector(Inspector(source["root"], self.home / "snapshots", request), source, selected=True)
        candidate = copy.deepcopy(previous["submitted"]["candidate"])
        checked = self.catalog.inspect_candidate(inspector, candidate)
        implementation = self.analysis.freeze_implementation(inspector)
        jid = uuid.uuid4().hex
        job = dict(id=jid, request_id=request["id"], harness_id=source["id"], status="RUNNING",
            kind="ENVIRONMENT_RECHECK", prior_analysis_id=previous["id"], rounds=0, usage=[], messages=[],
            started_at=now(), finished_at=None, error=None, model_calls=0, implementation=implementation,
            phase="environment_installation",
            submitted=dict(candidate=candidate), binding_report=checked)
        self.store.create("request", request["id"], request)
        self.store.create("analysis", jid, job)
        source.update(request_id=request["id"], analysis_id=jid, error=None)
        self.store.save("harness", source["id"], source)
        self.inspectors[request["id"]] = inspector
        try:
            job["units"] = self._connect_units(inspector, job)
            if job["status"] == "RUNNING":
                job.update(status="COMPLETED", phase="installation")
        except Exception as error:
            job.update(status="FAILED", error=str(error))
            source.update(environment_pending_recheck=True, error=str(error),
                          analysis_id=previous["id"], request_id=previous["request_id"])
            self._invalidate_environment_units(source, str(error))
            self.store.save("harness", source["id"], source)
            raise
        finally:
            job["finished_at"] = now()
            self.store.save("analysis", jid, job)
        return dict(analysis_id=jid, model_calls=0, units=job["units"])

    def analyze(self, identity, preprocessing="api", preprocessing_id=None):
        with self.source_lock:
            return self._analyze(identity, preprocessing, preprocessing_id)

    def _analyze(self, identity, preprocessing="api", preprocessing_id=None):
        source = self.store.get("harness", identity)
        if not source:
            raise ValueError("Harness地址尚未接入")
        if source.get("status") == "DETACHED":
            raise ValueError("来源已摘除，请先重新接入")
        if any(job["status"] == "RUNNING" and job.get("request_id") == source.get("request_id")
               for job in self.store.list("analysis")):
            raise ValueError("这个来源正在分析")
        self._environment_guard(source)
        request_id = uuid.uuid4().hex
        previous = self.store.get("analysis", source.get("analysis_id"))
        source["entry_profile"] = dict(NATIVE_PROFILE)
        inspector = Inspector.freeze_repository(source, self.home / "snapshots", request_id,
            "保留原算法、原生状态、初始化及控制/返回关系，按已确认RuleSet寻找真实可接入入口和有限W/H/R边界。")
        inspector.request["rules"] = RULESET
        inspector.request.update(preprocessing_mode=preprocessing, preprocessing_id=preprocessing_id,
            resource_catalog=self.resource_catalog(), dependency_bindings=self.dependencies.selections(identity))
        self._attach_inspector(inspector, source, selected=True)
        if getattr(inspector, "target_runtime", None):
            inspector.target_runtime.call("entry_candidates", path="")
        else:
            # Fail before paid analysis when the local interpreter cannot meet
            # the original project's declared Python requirements.
            import sys
            normalize_environment(dict(python=".".join(map(str, sys.version_info[:2]))), inspector)
        self.store.save("request", request_id, inspector.request)
        self.inspectors[request_id] = inspector
        analysis_id = uuid.uuid4().hex
        source.update(status="ANALYZING", analysis_id=analysis_id, request_id=request_id, error=None)
        self.store.save("harness", identity, source)
        try:
            self.analysis.start(inspector, self.profiles.role_identity("analysis"), identity=analysis_id,
                                previous=previous, preprocessing=preprocessing, preprocessing_id=preprocessing_id)
        except Exception as error:
            source.update(status="FAILED", error=str(error))
            self.store.save("harness", identity, source)
            raise
        return dict(analysis_id=analysis_id)

    def _connect_units(self, inspector, job):
        with self.source_lock:
            source = self.store.get("harness", inspector.request["source_id"])
            if not source or source.get("status") == "DETACHED" or source.get("analysis_id") != job["id"]:
                job["status"] = "CANCELLED"
                return []
            self.dependencies.record(inspector, job["submitted"]["candidate"])
            units = self.catalog.install(inspector, job["submitted"]["candidate"], job["id"])
            source.update(status="ANALYZED", analysis_id=job["id"], installation_id=job["id"], error=None,
                          environment_pending_recheck=False)
            self.store.save("harness", source["id"], source)
        print("[SeamPact] units_connected " + str(len(units)), flush=True)
        return [unit["id"] for unit in units]

    def resume_analysis(self, identity):
        with self.source_lock:
            previous = self.store.get("analysis", identity)
            if not previous or not self.analysis.can_resume(previous):
                raise ValueError("这次分析没有可直接续接的完整模型请求边界，请重新分析")
            request = self.store.get("request", previous["request_id"])
            source = self.store.get("harness", (request or {}).get("source_id"))
            if not request or not source:
                raise ValueError("原分析的源码快照或来源记录缺失")
            if source.get("status") == "DETACHED":
                raise ValueError("来源已摘除，请先重新接入，再继续这次分析")
            for job in self.store.list("analysis"):
                if job.get("status") == "RUNNING":
                    current = self.store.get("request", job["request_id"]) or {}
                    if current.get("source_id") == source["id"]:
                        raise ValueError("这个来源正在分析")
            if request.get("rules") != RULESET or request.get("entry_profile") != NATIVE_PROFILE:
                raise ValueError("分析规则或宿主范围已变化，请重新分析")
            bindings = {"source_revision": "revision", "entry": "entry",
                        "configuration": "configuration", "user_resources": "user_resources"}
            if any(request.get(old) != source.get(current) for old, current in bindings.items()):
                raise ValueError("来源版本、接入目标或资源配置已变化，请重新分析")
            inspector = self._attach_inspector(Inspector(source["root"], self.home / "snapshots", request), source)
            for path in inspector.files:
                inspector.data(path)
            if getattr(inspector, "target_runtime", None):
                inspector.target_runtime.call("entry_candidates", path="")
            analysis_id = uuid.uuid4().hex
            source.update(status="ANALYZING", analysis_id=analysis_id,
                          request_id=request["id"], error=None)
            self.store.save("harness", source["id"], source)
            self.inspectors[request["id"]] = inspector
            try:
                self.analysis.start(inspector, self.profiles.role_identity("analysis"),
                                    identity=analysis_id, resume=previous)
            except Exception as error:
                source.update(status="FAILED", error=str(error))
                self.store.save("harness", source["id"], source)
                raise
            return dict(analysis_id=analysis_id, resumed_from=identity)

    def configure_harness(self, body):
        """Bind explicit resources to an existing analysis, without another model call."""
        with self.source_lock:
            source = self.store.get("harness", body["harness_id"])
            if not source or source.get("status") in {"DETACHED", "ANALYZING"}:
                raise ValueError("需要已接入且未在分析的来源")
            if self.catalog.source_running(source["id"]):
                raise ValueError("请等原调用结束后再修改初始化绑定")
            previous = self.store.get("analysis", source.get("analysis_id"))
            if not previous or previous.get("status") != "COMPLETED":
                raise ValueError("需要已有完整分析")
            supplied = body["configuration"]
            if not isinstance(supplied, dict) or set(supplied) - {"entries"}:
                raise ValueError("重新绑定仅接收 entries 初始化参数")
            configuration = copy.deepcopy(source.get("configuration") or {})
            for eid, values in supplied.get("entries", {}).items():
                configuration.setdefault("entries", {}).setdefault(eid, {}).update(values)
            encoded = str(configuration).lower().replace("_", "").replace("-", "")
            if "apikey" in encoded:
                raise ValueError("密钥请使用模型配置或原生环境变量")
            candidate = copy.deepcopy(previous["submitted"]["candidate"])
            entries = {entry["id"]: entry for entry in candidate["entries"]}
            for eid, values in configuration.get("entries", {}).items():
                if eid not in entries or not isinstance(values, dict) or set(values) - {
                        "constructor_arguments", "fixed_arguments", "initializers", "binding_arguments"}:
                    raise ValueError("需按已有入口 ID 填写显式绑定参数")
                entry = entries[eid]
                if "constructor_arguments" in values:
                    if not entry.get("constructor"):
                        raise ValueError("原分析没有这个入口的构造器")
                    entry["constructor"]["arguments"] = values["constructor_arguments"]
                if "fixed_arguments" in values:
                    entry["fixed_arguments"] = values["fixed_arguments"]
                if "initializers" in values:
                    parameters = values["initializers"]
                    known = {item["symbol"]: item for item in entry.get("initializers", [])}
                    if not isinstance(parameters, dict) or set(parameters) - set(known):
                        raise ValueError("仅可填写原初始化方法的参数，不改变方法和顺序")
                    for symbol, arguments in parameters.items():
                        known[symbol]["arguments"] = arguments
                if "binding_arguments" in values:
                    raise ValueError("构造程序绑定请沿原分析入口配置后重新分析")
            request = copy.deepcopy(self.store.get("request", previous["request_id"]))
            request.update(id=uuid.uuid4().hex, configuration=configuration)
            if "resources" in body:
                request["user_resources"] = body["resources"]
            inspector = self._attach_inspector(Inspector(source["root"], self.home / "snapshots", request), source, selected=True)
            # Original finite source/binding checks still decide READY/MISSING.
            self.catalog.inspect_candidate(inspector, candidate)
            jid = uuid.uuid4().hex
            job = dict(id=jid, request_id=request["id"], harness_id=source["id"], status="COMPLETED",
                kind="EXPLICIT_RESOURCE_BINDING", prior_analysis_id=previous["id"], rounds=0, usage=[],
                messages=[], started_at=now(), finished_at=now(), error=None,
                submitted=dict(candidate=candidate), model_calls=0)
            self.store.create("request", request["id"], request)
            self.store.create("analysis", jid, job)
            source.update(configuration=configuration, user_resources=request.get("user_resources", []),
                          request_id=request["id"], analysis_id=jid)
            self.store.save("harness", source["id"], source)
            job["units"] = self._connect_units(inspector, job)
            self.store.save("analysis", jid, job)
            return dict(analysis_id=jid, model_calls=0, units=job["units"])

    def analysis_snapshot(self, analysis_id):
        """Export observation metadata without source messages or machine configuration."""
        job = self.store.get("analysis", analysis_id)
        if not job:
            raise ValueError("没有这个分析记录")
        request = self.store.get("request", job.get("request_id")) or {}

        def status(value):
            return value if isinstance(value, str) and re.fullmatch(r"[A-Z][A-Z0-9_/-]{0,95}", value) else None

        def relative_path(value):
            if not isinstance(value, str) or not value or "\\" in value:
                return None
            if (PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute()
                    or ".." in PurePosixPath(value).parts or ":" in value):
                return None
            return value

        token_fields = {"prompt_tokens", "completion_tokens", "total_tokens", "input_tokens", "output_tokens",
                        "prompt_cache_hit_tokens", "prompt_cache_miss_tokens", "cached_tokens", "reasoning_tokens",
                        "audio_tokens", "image_tokens", "text_tokens", "accepted_prediction_tokens",
                        "rejected_prediction_tokens"}
        detail_fields = {"prompt_tokens_details", "completion_tokens_details", "input_tokens_details", "output_tokens_details"}

        def usage(value):
            if not isinstance(value, dict):
                return None
            return {key: (usage(item) if key in detail_fields else item) for key, item in value.items()
                    if (key in token_fields and type(item) is int and item >= 0)
                    or (key in detail_fields and isinstance(item, dict))}

        def report_summary(report):
            if not isinstance(report, dict):
                return None
            counts = {}

            def visit(value):
                if isinstance(value, dict):
                    label = status(value.get("status"))
                    if label:
                        counts[label] = counts.get(label, 0) + 1
                    for child in value.values():
                        visit(child)
                elif isinstance(value, list):
                    for child in value:
                        visit(child)

            visit(report)
            summary = dict(status=status(report.get("status")), status_counts=counts)
            for key in ("admitted", "has_bindable_entries", "admission_granted", "split_permission", "export_permission"):
                if type(report.get(key)) is bool:
                    summary[key] = report[key]
            for key in ("boundaries", "entries", "targets", "issues", "missing", "dependencies"):
                if isinstance(report.get(key), list):
                    summary[key + "_count"] = len(report[key])
            if isinstance(report.get("entry_bindings"), dict):
                summary["entry_bindings"] = report_summary(report["entry_bindings"])
            return summary

        messages = job.get("messages") or []
        replies = {message.get("tool_call_id"): message for message in messages
                   if isinstance(message, dict) and message.get("role") == "tool"}
        tool_names = {"list_source", "read_file", "inspect_symbols", "inspect_construction", "list_structure",
                      "inspect_imports", "entry_candidates", "list_resources", "add_source", "check_candidate",
                      "submit_candidate", "check_preprocessing", "submit_preprocessing"}
        tools = []
        for message in messages:
            if not isinstance(message, dict):
                continue
            for call in message.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                function = call.get("function") or {}
                if not isinstance(function, dict):
                    continue
                name = function.get("name")
                if name not in tool_names:
                    continue
                item = dict(index=len(tools) + 1, name=name, status="PENDING")
                try:
                    arguments = json.loads(function.get("arguments", "{}"))
                except (TypeError, ValueError):
                    arguments = {}
                if isinstance(arguments, dict):
                    path = relative_path(arguments.get("path"))
                    if path:
                        item["path"] = path
                    for key in ("start", "count", "offset"):
                        if type(arguments.get(key)) is int and arguments[key] >= 0:
                            item[key] = arguments[key]
                reply = replies.get(call.get("id"))
                if reply:
                    item["status"] = "RETURNED"
                    try:
                        result = json.loads(reply.get("content", "null"))
                    except (TypeError, ValueError):
                        result = None
                    if isinstance(result, dict):
                        item["result_status"] = status(result.get("status"))
                        if result.get("error"):
                            item["status"] = "ERROR"
                        for key in ("total_lines", "next_line", "total", "next_offset"):
                            if type(result.get(key)) is int and result[key] >= 0:
                                item[key] = result[key]
                        if isinstance(result.get("lines"), list):
                            item["returned_lines"] = len(result["lines"])
                tools.append(item)

        submitted = job.get("submitted") or {}
        candidate = submitted.get("candidate") or {}
        preparation = job.get("preprocessing") or {}
        binding = job.get("model_binding") or {}
        output = binding.get("output_configuration") or {}
        output_configuration = {}
        if isinstance(output, dict):
            if type(output.get("stream")) is bool:
                output_configuration["stream"] = output["stream"]
            for key in ("max_tokens", "max_completion_tokens", "seed"):
                if type(output.get(key)) is int:
                    output_configuration[key] = output[key]
            for key in ("temperature", "top_p"):
                if type(output.get(key)) is int or (type(output.get(key)) is float and math.isfinite(output[key])):
                    output_configuration[key] = output[key]
            if (isinstance(output.get("reasoning_effort"), str)
                    and output["reasoning_effort"] in {"none", "minimal", "low", "medium", "high", "xhigh", "max"}):
                output_configuration["reasoning_effort"] = output["reasoning_effort"]
            thinking = output.get("thinking")
            if (isinstance(thinking, dict) and isinstance(thinking.get("type"), str)
                    and thinking["type"] in {"enabled", "disabled"}):
                output_configuration["thinking"] = dict(type=thinking["type"])
        terminal = job.get("status") in {"COMPLETED", "FAILED", "INTERRUPTED", "CANCELLED"}
        revision = request.get("source_revision")
        if not (isinstance(revision, str) and re.fullmatch(r"[A-Za-z0-9_.@+/-]{1,256}", revision)
                and not PurePosixPath(revision).is_absolute() and not PureWindowsPath(revision).is_absolute()
                and ".." not in PurePosixPath(revision).parts):
            revision = None
        return dict(schema="seampact-analysis-progress/1", analysis_id=job["id"], exported_at=now(),
            status=status(job.get("status")), complete=job.get("status") == "COMPLETED", partial=job.get("status") != "COMPLETED",
            terminal=terminal, importable=False, resumable_artifact=False,
            scope="Observation snapshot only; not an importable candidate or a resumable checkpoint.",
            excluded=["messages", "source text", "credential values and references", "service addresses",
                      "machine paths", "raw candidate arguments", "raw errors", "non-whitelisted generation parameters"],
            request_id=job.get("request_id"),
            source=dict(source_id=request.get("source_id"), source_version=request.get("source_version"),
                        source_revision=revision),
            pipeline=dict(stage=job.get("pipeline_stage"), phase=job.get("phase"),
                preprocessing=dict(mode=preparation.get("mode"), status=status(preparation.get("status")),
                    artifact_id=preparation.get("artifact_id"), report=report_summary(preparation.get("report")))),
            model_binding=dict({key: binding[key] for key in ("provider", "model")
                                if isinstance(binding.get(key), str) and relative_path(binding[key])},
                               output_configuration=output_configuration),
            rounds=job.get("rounds", 0), prior_rounds=job.get("prior_rounds", 0),
            refinement_of=job.get("refinement_of"), resumed_from=job.get("resumed_from"),
            started_at=job.get("started_at"), finished_at=job.get("finished_at"),
            usage=[usage(item) for item in job.get("usage", [])],
            usage_by_stage={stage: [usage(item) for item in job.get("usage_by_stage", {}).get(stage, [])]
                            for stage in ("preprocessing", "analysis")},
            unreported_usage=bool(job.get("unreported_usage")), error_present=bool(job.get("error")),
            tools=tools, tools_scope="current pipeline stage saved messages",
            candidate=dict(submitted=bool(submitted), analysis_completed=job.get("status") == "COMPLETED",
                **{key + "_count": len(candidate[key]) if isinstance(candidate.get(key), list) else 0
                   for key in ("entries", "boundaries", "runtime_groups")}, report=report_summary(submitted.get("report"))))

    def export_analysis(self, analysis_id):
        """A shareable analysis artifact: candidate plus frozen source identity; no configuration, paths or state."""
        job = self.store.get("analysis", analysis_id)
        if not job or job.get("status") != "COMPLETED" or not (job.get("submitted") or {}).get("candidate"):
            raise ValueError("需要已完成且有候选的分析")
        request = self.store.get("request", job["request_id"]) or {}
        value = dict(schema="seampact-analysis-export/1", analysis_id=analysis_id, kind=job.get("kind", "API_ANALYSIS"),
            source_version=request.get("source_version"), source_revision=request.get("source_revision"),
            rules=RULESET["id"], entry_profile=NATIVE_PROFILE["id"], candidate=job["submitted"]["candidate"],
            exported_at=now(), excluded="configuration values, resource selections, credentials, platform paths and state; "
                                        "rebind them on the target platform")
        value["absolute_path_locations"] = self._absolute_paths(value["candidate"])
        folder = self.home / "exports"
        folder.mkdir(exist_ok=True)
        path = folder / ("analysis-" + analysis_id + ".json")
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        return dict(value, path=str(path))

    @staticmethod
    def _absolute_paths(value, location="candidate"):
        """Find machine-bound file values without treating HTTP routes or prose as files."""
        network_fields = {"url", "uri", "base_url", "endpoint", "route", "api_path", "api_route",
                          "api_endpoint", "endpoint_path", "route_path", "request_path", "url_path"}
        text_fields = {"description", "analysis", "rationale", "basis", "reason", "goal", "title",
                       "label", "name", "id", "symbol", "scope", "receiver", "unit", "schema",
                       "question", "prompt", "message", "content", "text", "pattern", "regex"}
        file_fields = {"path", "paths", "file", "files", "filename", "filenames", "dir", "dirs",
                       "directory", "directories", "folder", "folders", "root", "cwd", "resources",
                       "working_directory", "model", "weights", "checkpoint", "pretrained"}
        argument_fields = {"arguments", "fixed_arguments", "constructor_arguments", "binding_arguments",
                           "parameters", "configuration", "environment"}

        def role(key, inherited):
            key = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(key)).lower().replace("-", "_")
            if key in network_fields or key.endswith(("_url", "_uri", "_endpoint", "_route")):
                return "network"
            if key in text_fields:
                return "text"
            if key == "command":
                return "command"
            if key in argument_fields:
                return "arguments"
            if inherited == "network":
                return "network"
            if key in file_fields or key.endswith(("_path", "_paths", "_file", "_files", "_dir", "_dirs",
                                                   "_directory", "_directories", "_folder", "_root")):
                return "file"
            return inherited

        def visit(item, where, field=None):
            if isinstance(item, dict):
                return [found for key, child in item.items()
                        for found in visit(child, where + "." + str(key), role(key, field))]
            if isinstance(item, list):
                found = []
                option = None
                for index, child in enumerate(item):
                    child_role, checked = field, child
                    if field == "command" and isinstance(child, str):
                        if child.startswith("-"):
                            flag, separator, checked = child.partition("=")
                            option = flag.lstrip("-")
                            child_role = role(option, "arguments")
                            if not separator:
                                continue
                            option = None
                        else:
                            child_role = role(option, "arguments") if option else "file"
                            option = None
                    found.extend(visit(checked, where + "[" + str(index) + "]", child_role))
                return found
            if (not isinstance(item, str) or field not in {"file", "arguments"}
                    or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", item)):
                return []
            return [where] if PureWindowsPath(item).is_absolute() or PurePosixPath(item).is_absolute() else []

        return visit(value, location)

    def import_analysis(self, body):
        """Re-check an exported analysis against a source connected on this platform; zero model calls."""
        value = body.get("artifact") or {}
        if value.get("schema") != "seampact-analysis-export/1" or not isinstance(value.get("candidate"), dict):
            raise ValueError("需要 seampact-analysis-export/1 分析工件")
        if value.get("rules") != RULESET["id"] or value.get("entry_profile") != NATIVE_PROFILE["id"]:
            raise ValueError("分析规则或宿主范围与本平台不同，请重新分析")
        located = self._absolute_paths(value["candidate"])
        if located:
            raise ValueError("分析工件含原机器绝对路径（位置：" + ", ".join(located[:8]) +
                             "）；请导出未绑定配置的原分析，在本平台重新配置")
        with self.source_lock:
            source = self.store.get("harness", body.get("harness_id"))
            if not source or source.get("status") in {"DETACHED", "ANALYZING"}:
                raise ValueError("需要已在本平台接入且未在分析的来源")
            if self.catalog.source_running(source["id"]):
                raise ValueError("请等当前原生调用结束后再导入")
            request_id = uuid.uuid4().hex
            source["entry_profile"] = dict(NATIVE_PROFILE)
            inspector = Inspector.freeze_repository(source, self.home / "snapshots", request_id,
                "导入外部分析工件：只按已确认 RuleSet 在本平台重新核对，不调用模型。")
            if inspector.request["source_version"] != value.get("source_version"):
                raise ValueError("本平台冻结的源码内容与分析工件不一致（包括换行符差异）；请重新分析")
            inspector.request.update(rules=RULESET, preprocessing_mode="imported", resource_catalog=self.resource_catalog(),
                                     dependency_bindings=self.dependencies.selections(source["id"]))
            self._attach_inspector(inspector, source, selected=True)
            candidate = copy.deepcopy(value["candidate"])
            checked = self.catalog.inspect_candidate(inspector, candidate)
            jid = uuid.uuid4().hex
            job = dict(id=jid, request_id=request_id, harness_id=source["id"], status="COMPLETED", kind="IMPORTED_ANALYSIS",
                imported_from=dict(analysis_id=value.get("analysis_id"), kind=value.get("kind"), exported_at=value.get("exported_at"),
                                   source_version=value["source_version"]),
                prior_analysis_id=None, rounds=0, usage=[], messages=[], started_at=now(), finished_at=now(), error=None,
                submitted=dict(candidate=candidate), model_calls=0, binding_report=checked)
            self.store.create("request", request_id, inspector.request)
            self.store.create("analysis", jid, job)
            source.update(request_id=request_id, analysis_id=jid)
            self.store.save("harness", source["id"], source)
            self.inspectors[request_id] = inspector
            job["units"] = self._connect_units(inspector, job)
            self.store.save("analysis", jid, job)
        return dict(analysis_id=jid, model_calls=0, units=job["units"])

    def bind_dependency(self, body):
        """Apply only the analyzer's checked field mappings; never invent source wiring."""
        started_at, started = now(), time.perf_counter()
        with self.source_lock:
            source = self.store.get("harness", body["harness_id"])
            if not source or source.get("status") in {"DETACHED", "ANALYZING"}:
                raise ValueError("需要已分析且未摘除的来源")
            if self.catalog.source_running(source["id"]):
                raise ValueError("请等当前原生调用结束后再创建资源绑定")
            previous = self.store.get("analysis", source.get("analysis_id"))
            if not previous or previous.get("status") != "COMPLETED" or not previous.get("submitted"):
                raise ValueError("需要已有完整分析及其原依赖映射")
            selection = self.dependencies.select(body)
            candidate = copy.deepcopy(previous["submitted"]["candidate"])
            # Keep the API-produced candidate, original destinations and
            # references unchanged. This action supplies resource values only.
            request = copy.deepcopy(self.store.get("request", previous["request_id"]))
            request.update(id=uuid.uuid4().hex, resource_catalog=self.resource_catalog(),
                           dependency_bindings=self.dependencies.selections(source["id"]))
            inspector = self._attach_inspector(Inspector(source["root"], self.home / "snapshots", request), source, selected=True)
            implementation = self.analysis.freeze_implementation(inspector)
            checked = self.catalog.inspect_candidate(inspector, candidate)
            jid = uuid.uuid4().hex
            job = dict(id=jid, request_id=request["id"], harness_id=source["id"], status="RUNNING",
                kind="ANALYZER_RESOURCE_BINDING", prior_analysis_id=previous["id"], rounds=0, usage=[],
                messages=[], started_at=started_at, finished_at=None, error=None, model_calls=0,
                dependency_binding=selection["dependency_id"], submitted=dict(candidate=candidate),
                binding_report=checked, implementation=implementation,
                phase="binding_installation", binding_cost_scope="operation wall time including source-lock wait",
                implementation_scope="current binding check and installation; prior API generation retains its own implementation")
            self.store.create("request", request["id"], request)
            self.store.create("analysis", jid, job)
            source.update(request_id=request["id"], analysis_id=jid)
            self.store.save("harness", source["id"], source)
            self.inspectors[request["id"]] = inspector
            try:
                job["units"] = self._connect_units(inspector, job)
                if job["status"] == "RUNNING":
                    job.update(status="COMPLETED", phase="installation")
            except Exception as error:
                job.update(status="FAILED", phase="binding_installation", error=str(error))
                source.update(status="FAILED", error=str(error))
                self.store.save("harness", source["id"], source)
                raise
            finally:
                job.update(finished_at=now(), binding_duration_ms=(time.perf_counter() - started) * 1000)
                self.store.save("analysis", jid, job)
            return dict(analysis_id=jid, model_calls=0, units=job["units"], binding=selection)

    def detach_harness(self, identity):
        with self.source_lock:
            source = self.store.get("harness", identity)
            if not source:
                raise ValueError("没有这个Harness来源")
            source.update(status="DETACHED", detached_at=now(), error=None)
            self.store.save("harness", identity, source)
            self.analysis.cancel(source.get("analysis_id"))
            count = self.catalog.detach(identity)
            self.deployments.mark_source_detached(identity)
        print("[SeamPact] source_detached " + identity, flush=True)
        return dict(harness_id=identity, status="DETACHED", detached_units=count,
                    history="RETAINED", started_calls="ALLOW_FINISH")

    def restore_harness(self, identity):
        with self.source_lock:
            source = self.store.get("harness", identity)
            if not source:
                raise ValueError("没有这个Harness来源")
            if source.get("status") != "DETACHED":
                return dict(harness_id=identity, status=source["status"])
            if self.catalog.source_running(identity):
                raise ValueError("已开始的原调用仍在收尾，结束后可重新接入")
            job = next((item for item in self.store.list("analysis")
                        if item.get("status") == "COMPLETED" and item.get("submitted")
                        and (self.store.get("request", item["request_id"]) or {}).get("source_id") == identity), None)
            self._checker_record(source)
            self.runtime.call("restore_source", identity)
            try:
                if job and not source.get("environment_pending_recheck"):
                    request = self.store.get("request", job["request_id"])
                    inspector = self._attach_inspector(Inspector(source["root"], self.home / "snapshots", request), source)
                    installation_id = uuid.uuid4().hex
                    self.catalog.install(inspector, job["submitted"]["candidate"], job["id"], installation_id)
                    source.update(status="ANALYZED", analysis_id=job["id"], installation_id=installation_id,
                                  request_id=request["id"])
                    self.inspectors[request["id"]] = inspector
                elif job:
                    source.update(status="ANALYZED", analysis_id=job["id"], installation_id=None,
                                  request_id=job["request_id"])
                else:
                    source.update(status="CONNECTED", analysis_id=None)
                source.update(detached_at=None)
                if not source.get("environment_pending_recheck"):
                    source["error"] = None
                self.store.save("harness", identity, source)
            except Exception:
                self.catalog.detach(identity)
                raise
        print("[SeamPact] source_restored " + identity, flush=True)
        return dict(harness_id=identity, status=source["status"], model_api_calls=0)

    def clear_pool(self):
        removed = [self.detach_harness(source["id"]) for source in self.store.list("harness")
                   if source.get("status") != "DETACHED"]
        self.training.active = None
        self.store.save("scheduler_active", "current", None)
        return dict(detached=removed, history="RETAINED", ready_units=len(self.catalog.public()))

    def close(self):
        self.deployments.close()
        self.training.close()
        self.analysis.close()
        self.chat.close()
        self.runtime.close()
        self.store.close()
