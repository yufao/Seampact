"""The four-layer research prototype: intake, analysis, Units, RL and chat."""

import uuid
import threading
from pathlib import Path

from . import __version__
from .analysis import AnalysisJobs
from .chat import ChatJobs
from .inspection import Inspector
from .intake import RepositoryIntake
from .providers import Profiles
from .runtime import Coordinator, OwnerRuntime
from .scheduler import TwoLayerScheduler
from .store import Store, now
from .units import UnitCatalog
from .deployment import Deployments, REMOTE_PROFILE

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
    parallel=False, remote="PER_ENTRY_CHECKED", remote_profile=REMOTE_PROFILE)


class Platform:
    def __init__(self, home, legacy=None):
        self.home = Path(home).resolve()
        self.home.mkdir(parents=True, exist_ok=True)
        self.source_lock = threading.RLock()
        self.store = Store(self.home / "platform.sqlite3")
        self.profiles = Profiles(self.home / "config", legacy)
        self.scheduler = TwoLayerScheduler(self.home / "policy")
        self.runtime = OwnerRuntime(Coordinator(self.store, policy=self.scheduler))
        self.intake = RepositoryIntake(self.home / "sources")
        self.catalog = UnitCatalog(self.home, self.store, self.runtime)
        self.deployments = Deployments(self)
        self.analysis = AnalysisJobs(self.store, self.profiles, on_complete=self._connect_units,
                                     check_entries=self.catalog.inspect_candidate)
        self.chat = ChatJobs(self.store, self.profiles, self.catalog, self.runtime, self.scheduler)
        self.inspectors = {}
        for source in self.store.list("harness"):
            if source.get("status") == "DETACHED":
                self.catalog.detach(source["id"])
        for job in self.store.list("analysis"):
            if job.get("status") != "COMPLETED" or not job.get("submitted"):
                continue
            request = self.store.get("request", job["request_id"])
            if not request:
                continue
            source = self.store.get("harness", request.get("source_id"))
            if source and source.get("status") != "DETACHED" and source.get("analysis_id") == job["id"]:
                inspector = Inspector(source["root"], self.home / "snapshots", request)
                self.inspectors[request["id"]] = inspector
                self.catalog.install(inspector, job["submitted"]["candidate"], job["id"], source.get("installation_id"))

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
                ("id", "name", "address", "revision", "status", "error", "analysis_id", "entry", "resources", "detached_at")})
            harnesses[-1]["files"] = len(source.get("files", []))
            harnesses[-1]["inventory_count"] = len(source.get("inventory", []))
            job = self.store.get("analysis", source.get("analysis_id"))
            if source.get("status") != "DETACHED" and job and job["status"] in {"FAILED", "INTERRUPTED", "CANCELLED"}:
                harnesses[-1].update(status=job["status"], error=job.get("error"))
        jobs = [{name: value for name, value in job.items() if name not in {"messages", "provider_profile"}}
                for job in self.store.list("analysis")]
        for job in jobs:
            if not job.get("harness_id"):
                request = self.store.get("request", job.get("request_id"))
                job["harness_id"] = (request or {}).get("source_id")
        return dict(version=__version__, models=self.profiles.public_roles(), harnesses=harnesses,
            analyses=jobs, units=self.catalog.public(), tasks=tasks, rules=RULESET, entry_profile=NATIVE_PROFILE,
            runtime_groups=self.deployments.public(), deployments=self.store.list("deployment"),
            scheduler=dict(status="RUNNING", policy="two-layer-tabular-q-learning", active=active, waiting=waiting,
                used=snapshot["used"], quota=snapshot["quota"], theory_pending=False,
                learning=self.scheduler.snapshot(), reward="success(+1)/failure(-1) - seconds/(1+seconds)"))

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
        self.store.save("harness", source["id"], source)
        print("[SeamPact] source_connected " + source["id"], flush=True)
        return dict(id=source["id"], files=len(source["files"]))

    def analyze(self, identity):
        with self.source_lock:
            return self._analyze(identity)

    def _analyze(self, identity):
        source = self.store.get("harness", identity)
        if not source:
            raise ValueError("Harness地址尚未接入")
        if source.get("status") == "DETACHED":
            raise ValueError("来源已摘除，请先重新接入")
        if any(job["status"] == "RUNNING" and job.get("request_id") == source.get("request_id")
               for job in self.store.list("analysis")):
            raise ValueError("这个来源正在分析")
        request_id = uuid.uuid4().hex
        previous = self.store.get("analysis", source.get("analysis_id"))
        source["entry_profile"] = dict(NATIVE_PROFILE)
        inspector = Inspector.freeze_repository(source, self.home / "snapshots", request_id,
            "保留原算法、原生状态、初始化及控制/返回关系，按已确认RuleSet寻找真实可接入入口和有限W/H/R边界。")
        inspector.request["rules"] = RULESET
        self.store.save("request", request_id, inspector.request)
        self.inspectors[request_id] = inspector
        analysis_id = uuid.uuid4().hex
        source.update(status="ANALYZING", analysis_id=analysis_id, request_id=request_id, error=None)
        self.store.save("harness", identity, source)
        try:
            self.analysis.start(inspector, self.profiles.role_identity("analysis"), identity=analysis_id, previous=previous)
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
            units = self.catalog.install(inspector, job["submitted"]["candidate"], job["id"])
            source.update(status="ANALYZED", analysis_id=job["id"], installation_id=job["id"], error=None)
            self.store.save("harness", source["id"], source)
        print("[SeamPact] units_connected " + str(len(units)), flush=True)
        return [unit["id"] for unit in units]

    def detach_harness(self, identity):
        with self.source_lock:
            source = self.store.get("harness", identity)
            if not source:
                raise ValueError("没有这个Harness来源")
            source.update(status="DETACHED", detached_at=now(), error=None)
            self.store.save("harness", identity, source)
            self.analysis.cancel(source.get("analysis_id"))
            count = self.catalog.detach(identity)
            for group in list(self.deployments.routes):
                record = self.store.get("runtime_group", group)
                if record and record["source_id"] == identity:
                    # Keep the route for started physical receipts. Catalog/source
                    # masks stop new calls, rather than killing the original owner.
                    self.deployments.routes[group]["source_detached"] = True
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
            self.runtime.call("restore_source", identity)
            try:
                if job:
                    request = self.store.get("request", job["request_id"])
                    inspector = Inspector(source["root"], self.home / "snapshots", request)
                    installation_id = uuid.uuid4().hex
                    self.catalog.install(inspector, job["submitted"]["candidate"], job["id"], installation_id)
                    source.update(status="ANALYZED", analysis_id=job["id"], installation_id=installation_id)
                else:
                    source.update(status="CONNECTED", analysis_id=None)
                source.update(detached_at=None, error=None)
                self.store.save("harness", identity, source)
            except Exception:
                self.catalog.detach(identity)
                raise
        print("[SeamPact] source_restored " + identity, flush=True)
        return dict(harness_id=identity, status=source["status"], model_api_calls=0)

    def close(self):
        self.analysis.close()
        self.chat.close()
        self.runtime.close()
        self.store.close()
