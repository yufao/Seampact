"""Platform state and job orchestration; source packages are never auto-executed."""

import asyncio
import json
import os
import threading
import uuid
from pathlib import Path
from . import __version__

from .analysis import AnalysisJobs
from .build import build_package
from .evidence import collect_evidence
from .inspection import Inspector
from .providers import Profiles
from .runtime import Coordinator, OwnerRuntime
from .store import Store, now
from .tasks import TaskRuns


class Platform:
    def __init__(self, home, sources=None, legacy=None, evidence_root=None, artifact_root=None):
        self.home = Path(home).resolve()
        self.home.mkdir(parents=True, exist_ok=True)
        self.store = Store(self.home / "platform.sqlite3")
        self.profiles = Profiles(self.home / "config", legacy)
        self.analysis = AnalysisJobs(self.store, self.profiles)
        self.tasks = TaskRuns(self.store, self.profiles, self.home, artifact_root)
        self.sources = sources or {}
        self.evidence_root = Path(evidence_root).resolve() if evidence_root is not None else None
        self.inspectors = {}
        self.lock = threading.RLock()
        self.runtime = OwnerRuntime(Coordinator(self.store))
        for request in self.store.list("request"):
            source = self.sources.get(request["source_id"])
            if source:
                self.inspectors[request["id"]] = Inspector(source["root"], self.home / "snapshots", request)

    def state(self):
        jobs = [{k:v for k,v in j.items() if k != "messages"} for j in self.store.list("analysis")]
        return dict(schema="platform-state/1", version=__version__, sources=[
            dict(id=i, **{k:v for k,v in s.items() if k != "root"},
                 native="NOT_RUN", adapted="NOT_RUN", parallel="NOT_RUN", deployment="NOT_RUN")
            for i,s in self.sources.items()], requests=self.store.list("request"), analyses=jobs,
            packages=self.store.list("package"), demos=self.store.list("demo"),
            runs=self.tasks.list(), execution=self.tasks.availability(),
            runtime=self.runtime.snapshot(), evidence=collect_evidence(self.evidence_root),
            profiles=[dict(profile,credential_mode="windows_dpapi" if os.name=="nt" else "private_environment")
                      for profile in self.profiles.public()])

    def freeze(self, source_id, observation):
        source = self.sources[source_id]
        identity = uuid.uuid4().hex
        inspector = Inspector.freeze(source["root"], self.home / "snapshots", identity,
                                     source["files"], source["entry"], observation)
        inspector.request["source_id"] = source_id
        inspector.request["source_revision"] = source.get("revision")
        self.store.save("request", identity, inspector.request, "scope_frozen")
        with self.lock:
            self.inspectors[identity] = inspector
        return inspector.request

    def start_analysis(self, request_id, provider):
        with self.lock:
            # The source version belongs to one analysis loop until it submits.
            if any(j["request_id"] == request_id and j["status"] in {"RUNNING", "CANCELLING"}
                   for j in self.store.list("analysis")):
                raise ValueError("this frozen request already has an analysis loop")
            return self.analysis.start(self.inspectors[request_id], provider)

    def build(self, analysis_id):
        job = self.store.get("analysis", analysis_id)
        if not job or job["status"] != "COMPLETED" or not job["submitted"]:
            raise ValueError("completed structured analysis required")
        inspector = self.inspectors[job["request_id"]]
        identity = uuid.uuid4().hex
        package = build_package(inspector, job["submitted"]["candidate"], self.home / "packages", identity)
        package["analysis_id"] = analysis_id
        self.store.save("package", identity, package, "package_built")
        return package

    def demo(self):
        identity = uuid.uuid4().hex
        status = dict(id=identity, status="RUNNING", kind="TEACHING_PROTOCOL_ONLY", started_at=now())
        self.store.save("demo", identity, status, "demo_started")
        threading.Thread(target=self._demo, args=(status,), daemon=True).start()
        return identity

    def _demo(self, status):
        """Original controller awaits its two tools; gate keeps actual start back."""
        prefix = status["id"][:12]
        gate, owner = "demo-"+prefix, "teaching-native-owner-"+prefix
        native = []
        async def original_tool(value):
            native.append(["tool_enter", value])
            await asyncio.sleep(0.05)
            native.append(["tool_exit", value])
            return {"value": value * 2}
        async def controller(tool):
            first = await tool(3)
            native.append(["controller_continue", first["value"]])
            second = await tool(first["value"])
            native.append(["controller_return", second["value"]])
            return second
        try:
            reference_events = []
            reference = asyncio.run(controller(original_tool))
            reference_events.extend(native)
            native.clear()
            self.runtime.call("register", gate, owner)
            adapted = asyncio.run(controller(self.runtime.wrap(original_tool, gate, owner)))
            calls = [c for c in self.runtime.snapshot()["calls"].values() if c["gate"] == gate]
            status.update(status="PASS" if reference == adapted and reference_events == native
                          and len(calls) == 2 and all(c["starts"] == 1 and c["physical"] == "RETIRED" for c in calls)
                          else "FAIL", reference=reference, adapted=adapted,
                          native_trace=reference_events, adapted_trace=native, calls=calls,
                          scope="two controlled local callable occurrences; not a four-source result")
        except Exception as error:
            status.update(status="FAIL", error=type(error).__name__)
        status["finished_at"] = now()
        self.store.save("demo", status["id"], status, "demo_finished")

    def close(self):
        self.tasks.close()
        self.runtime.close()
        self.store.close()


def load_sources(path):
    if path is None:
        return {}
    path = Path(path).resolve()
    sources = json.loads(path.read_text(encoding="utf-8"))
    for value in sources.values():
        value["root"] = str((path.parent / value["root"]).resolve())
    return sources
