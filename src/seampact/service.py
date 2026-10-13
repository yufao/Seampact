"""The four-layer research prototype: intake, analysis, Units, RL and chat."""

import uuid
from pathlib import Path

from . import __version__
from .analysis import AnalysisJobs
from .chat import ChatJobs
from .inspection import Inspector
from .intake import RepositoryIntake
from .providers import Profiles
from .runtime import Coordinator, OwnerRuntime
from .scheduler import TwoLayerScheduler
from .store import Store
from .units import UnitCatalog

RULESET = dict(id="native-entry-whR-prototype/1", confirmed=True,
    source="用户确认的首版执行判断",
    rules=["真实源码入口、参数、初始化和资源绑定", "缺失依赖或参数明确保留为MISSING",
           "内部W/H/R保留原controller与owner", "有限R沿用现有循环规则",
           "粗细方案互斥", "不依据模型自述开放并行或远程"],
    parallel=False, remote=False)


class Platform:
    def __init__(self, home, legacy=None):
        self.home = Path(home).resolve()
        self.home.mkdir(parents=True, exist_ok=True)
        self.store = Store(self.home / "platform.sqlite3")
        self.profiles = Profiles(self.home / "config", legacy)
        self.scheduler = TwoLayerScheduler(self.home / "policy")
        self.runtime = OwnerRuntime(Coordinator(self.store, policy=self.scheduler))
        self.intake = RepositoryIntake(self.home / "sources")
        self.catalog = UnitCatalog(self.home, self.store, self.runtime)
        self.analysis = AnalysisJobs(self.store, self.profiles, on_complete=self._connect_units,
                                     check_entries=self.catalog.inspect_candidate)
        self.chat = ChatJobs(self.store, self.profiles, self.catalog, self.runtime, self.scheduler)
        self.inspectors = {}
        for job in self.store.list("analysis"):
            if job.get("status") != "COMPLETED" or not job.get("submitted"):
                continue
            request = self.store.get("request", job["request_id"])
            if not request:
                continue
            source = self.store.get("harness", request.get("source_id"))
            if source:
                inspector = Inspector(source["root"], self.home / "snapshots", request)
                self.inspectors[request["id"]] = inspector
                self.catalog.install(inspector, job["submitted"]["candidate"], job["id"])

    def state(self):
        snapshot = self.runtime.snapshot()
        active = [call for call in snapshot["calls"].values() if call["physical"] in {"AUTHORIZED", "STARTED"}]
        waiting = [call for call in snapshot["calls"].values() if call["physical"] in {"ENABLED", "RESERVED"}]
        tasks = self.store.list("task")
        for task in tasks:
            task["calls"] = [call for call in snapshot["calls"].values() if call.get("task_id") == task["id"]]
        harnesses = []
        for source in self.store.list("harness"):
            harnesses.append({name: source.get(name) for name in
                ("id", "name", "address", "revision", "status", "error", "analysis_id", "entry", "resources")})
            harnesses[-1]["files"] = len(source.get("files", []))
            harnesses[-1]["inventory_count"] = len(source.get("inventory", []))
            job = self.store.get("analysis", source.get("analysis_id"))
            if job and job["status"] in {"FAILED", "INTERRUPTED", "CANCELLED"}:
                harnesses[-1].update(status=job["status"], error=job.get("error"))
        jobs = [{name: value for name, value in job.items() if name not in {"messages", "provider_profile"}}
                for job in self.store.list("analysis")]
        return dict(version=__version__, models=self.profiles.public_roles(), harnesses=harnesses,
            analyses=jobs, units=self.catalog.public(), tasks=tasks, rules=RULESET,
            scheduler=dict(status="RUNNING", policy="two-layer-tabular-q-learning", active=active, waiting=waiting,
                used=snapshot["used"], quota=snapshot["quota"], theory_pending=False,
                learning=self.scheduler.snapshot(), reward="success(+1)/failure(-1) - seconds/(1+seconds)"))

    def add_harness(self, body):
        source = self.intake.prepare(body.get("address", ""), body.get("revision"),
                                    body.get("entry") or None, body.get("resources") or None)
        source["status"] = "CONNECTED"
        self.store.save("harness", source["id"], source)
        print("[SeamPact] source_connected " + source["id"], flush=True)
        return dict(id=source["id"], files=len(source["files"]))

    def analyze(self, identity):
        source = self.store.get("harness", identity)
        if not source:
            raise ValueError("Harness地址尚未接入")
        if any(job["status"] == "RUNNING" and job.get("request_id") == source.get("request_id")
               for job in self.store.list("analysis")):
            raise ValueError("这个来源正在分析")
        request_id = uuid.uuid4().hex
        inspector = Inspector.freeze_repository(source, self.home / "snapshots", request_id,
            "保留原算法、原生状态、初始化及控制/返回关系，按已确认RuleSet寻找真实可接入入口和有限W/H/R边界。")
        inspector.request["rules"] = RULESET
        self.store.save("request", request_id, inspector.request)
        self.inspectors[request_id] = inspector
        analysis_id = uuid.uuid4().hex
        source.update(status="ANALYZING", analysis_id=analysis_id, request_id=request_id, error=None)
        self.store.save("harness", identity, source)
        try:
            self.analysis.start(inspector, self.profiles.role_identity("analysis"), identity=analysis_id)
        except Exception as error:
            source.update(status="FAILED", error=str(error))
            self.store.save("harness", identity, source)
            raise
        return dict(analysis_id=analysis_id)

    def _connect_units(self, inspector, job):
        units = self.catalog.install(inspector, job["submitted"]["candidate"], job["id"])
        source = self.store.get("harness", inspector.request["source_id"])
        source.update(status="ANALYZED", analysis_id=job["id"], error=None)
        self.store.save("harness", source["id"], source)
        print("[SeamPact] units_connected " + str(len(units)), flush=True)
        return [unit["id"] for unit in units]

    def close(self):
        self.analysis.close()
        self.chat.close()
        self.runtime.close()
        self.store.close()
