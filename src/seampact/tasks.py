"""Browser task jobs: explicit artifact intake and owned native-host subprocess."""

import importlib.util
import json
import os
import subprocess
import sys
import threading
import uuid
from pathlib import Path

from .evaluation import canonical_hash, inspect_package, match_discovery_record
from .store import now


DEFAULT_QUESTION = "What does SeamPact preserve when decomposing a native harness?"
DEFAULT_DOCUMENTS = [
    "SeamPact keeps the original harness controller, native state and algorithm. "
    "Its adapter exposes supported callable boundaries and finite local loop transformations.",
    "The dispatcher releases enabled native occurrences. Package integrity does not prove "
    "live model quality, parallel correctness or production admission.",
]


class TaskRuns:
    def __init__(self, store, profiles, home, artifacts=None):
        self.store, self.profiles, self.home = store, profiles, Path(home)
        self.artifacts = Path(artifacts).resolve() if artifacts is not None else None
        self.tools = Path(__file__).resolve().parents[2] / "tools"
        self.lock, self.processes = threading.RLock(), {}
        self.closed = False
        for old in self.store.list("run"):
            if old["status"] == "RUNNING":
                old.update(status="INTERRUPTED", error="platform stopped; paid outcome may be unknown; no automatic replay")
                self.store.save("run", old["id"], old, "run_interrupted")

    def _preset(self):
        if self.artifacts is None or not self.artifacts.is_dir():
            raise ValueError("configure --artifacts with the CRAG API package and crag-text.json")
        record_path = (self.artifacts / "crag-text.json").resolve()
        if not record_path.is_relative_to(self.artifacts):
            raise ValueError("task record escapes its artifact directory")
        record = json.loads(record_path.read_text(encoding="utf-8"))
        identity = record["package"]["id"]
        if not isinstance(identity, str) or not identity.isascii() or not identity.replace("-", "").replace("_", "").isalnum():
            raise ValueError("invalid task package identity")
        archive = (self.artifacts / (identity + ".zip")).resolve()
        if not archive.is_relative_to(self.artifacts):
            raise ValueError("task archive escapes its artifact directory")
        return archive, record

    def _built(self, identity):
        package = self.store.get("package", identity)
        if not package:
            raise ValueError("package not found")
        analysis = self.store.get("analysis", package.get("analysis_id", ""))
        if not analysis or analysis.get("status") != "COMPLETED":
            raise ValueError("a completed source analysis is required for this package")
        request = self.store.get("request", analysis["request_id"])
        if not request or request.get("source_id") != "crag-text":
            raise ValueError("this native task host currently supports CRAG packages only")
        archive = (self.home / "packages" / package["archive"]).resolve()
        if not archive.is_relative_to((self.home / "packages").resolve()):
            raise ValueError("package archive escapes the platform package directory")
        checked = inspect_package(archive, package["sha256"])
        submitted = analysis.get("submitted") or {}
        if canonical_hash(submitted.get("candidate")) != canonical_hash(checked.manifest["candidate"]):
            raise ValueError("package does not match its completed analysis candidate")
        if checked.request["source_version"] != request["source_version"]:
            raise ValueError("package does not match its frozen source request")
        record = dict(source_id="crag-text", source_revision=request.get("source_revision"),
            request_id=request["id"], analysis_id=analysis["id"], status="COMPLETED",
            final_source_version=request["source_version"], package=dict(id=identity, sha256=package["sha256"]),
            candidate_sha256=canonical_hash(checked.manifest["candidate"]),
            report_sha256=canonical_hash(checked.report), provenance_origin="PLATFORM_COMPLETED_ANALYSIS")
        record["public_record_sha256"] = canonical_hash(record)
        return archive, record

    def availability(self):
        try:
            sdk = importlib.util.find_spec("llama_index.core") is not None
        except (ImportError, ModuleNotFoundError, ValueError):
            sdk = False
        ready, reason, digest = False, None, None
        try:
            archive, record = self._preset()
            ready = archive.is_file() and self.tools.joinpath("run_crag_task.py").is_file()
            digest = record["package"]["sha256"]
            if not ready:
                reason = "CRAG API task artifact is missing"
        except (ValueError, KeyError, OSError, json.JSONDecodeError) as error:
            reason = str(error)
        if ready and not sdk:
            ready, reason = False, "install the optional CRAG SDK requirements in the application environment"
        return dict(profiles=[dict(id="crag-api", source_id="crag-text", ready=ready, reason=reason,
            package_sha256=digest, modes=["live", "preview"], search_scope="LOCAL_DOCUMENTS_ONLY",
            input_defaults=dict(question=DEFAULT_QUESTION, documents=DEFAULT_DOCUMENTS))],
            supported_sources=["crag-text"], sdk_available=sdk, scheduler="FIFO_ENABLED_OCCURRENCES",
            rl="NOT_IMPLEMENTED")

    def get(self, identity):
        run = self.store.get("run", identity)
        if run and run["status"] == "RUNNING":
            path = self.home / "runs" / run["id"] / "output" / "progress.json"
            try:
                progress = json.loads(path.read_text(encoding="utf-8"))
                if progress.get("schema") == "crag-task-progress/1":
                    run["progress"] = progress
            except (OSError, ValueError):
                pass
        return run

    def list(self):
        return [self.get(run["id"]) for run in self.store.list("run")]

    def start(self, body):
        if not isinstance(body, dict):
            raise ValueError("task configuration must be a JSON object")
        question = body.get("question", "")
        documents = body.get("documents")
        mode, provider = body.get("mode", "live"), body.get("provider", "deepseek")
        if not isinstance(question, str) or not question.strip():
            raise ValueError("provide a question")
        if not isinstance(documents, list) or not documents or any(not isinstance(item, str) or not item.strip() for item in documents):
            raise ValueError("provide one or more nonempty text documents")
        if mode not in {"live", "preview"}:
            raise ValueError("task mode must be live or preview")
        if provider not in {"deepseek", "glm"}:
            raise ValueError("supported providers: deepseek, glm")
        if mode == "live":
            profile = self.profiles.profile(provider)
            if not profile["model"] or not self.profiles.key(provider):
                raise ValueError("configure the selected provider model and credential first")
        if not self.availability()["sdk_available"]:
            raise ValueError("install the optional CRAG SDK requirements in the application environment")
        if body.get("package_id"):
            if not isinstance(body["package_id"], str):
                raise ValueError("package_id must be a string")
            if body.get("preset"):
                raise ValueError("choose a built package or a preset, not both")
            archive, record = self._built(body["package_id"])
            origin = "PLATFORM_BUILT_PACKAGE"
        else:
            if body.get("preset") != "crag-api":
                raise ValueError("select a supported preset or built package")
            archive, record = self._preset()
            origin = "EXISTING_API_PACKAGE"
        checked = inspect_package(archive, record["package"]["sha256"])
        match_discovery_record(checked, record)
        # Reject unsupported symbols/revisions before a subprocess or paid call.
        allowed = {"ingest", "prepare_for_retrieval", "retrieve", "eval_relevance", "extract_relevant_texts", "transform_query", "query_result"}
        path = "docs/examples/workflow/corrective_rag_pack.ipynb"
        from .inspection import digest
        if digest(checked.payloads.get("source/" + path, b"")) != "287398bf70bd31513627a8ea83a3ffb37e25e60b882e690efa88b51e21361dfc":
            raise ValueError("unsupported CRAG source revision for this task host")
        for boundary in checked.manifest["candidate"]["boundaries"]:
            symbol = boundary.get("symbol", "")
            if boundary["path"] != path or boundary.get("unit", "file") != "cell:10" or not symbol.startswith("CorrectiveRAGWorkflow."):
                raise ValueError("this task host cannot bind every submitted boundary")
            if boundary["mode"] == "R":
                if symbol != "CorrectiveRAGWorkflow.eval_relevance" or boundary.get("loop_index", 0) != 0:
                    raise ValueError("unsupported CRAG loop binding for this task host")
            elif boundary["mode"] not in {"W", "H"} or isinstance(boundary.get("native_wiring"), dict) or symbol.split(".")[-1] not in allowed:
                raise ValueError("unsupported CRAG callable binding for this task host")
        with self.lock:
            if self.closed:
                raise ValueError("platform is closing")
            identity = uuid.uuid4().hex
            folder = self.home / "runs" / identity
            folder.mkdir(parents=True, mode=0o700)
            # Snapshot this exact selected artifact; never silently substitute a preset.
            (folder / "package.zip").write_bytes(archive.read_bytes())
            (folder / "record.json").write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
            options = dict(mode=mode, provider=provider, question=question, documents=documents)
            configuration = dict(archive=str(folder / "package.zip"), record=str(folder / "record.json"),
                options=options, config=str(self.profiles.directory.resolve()), output=str(folder / "output"))
            (folder / "job.json").write_text(json.dumps(configuration, ensure_ascii=False), encoding="utf-8")
            run = dict(id=identity, source_id="crag-text", status="RUNNING", started_at=now(), mode=mode,
                provider=provider if mode == "live" else None, question=question, document_count=len(documents),
                package_id=checked.manifest["id"], package_sha256=checked.sha256, artifact_origin=origin,
                analysis_id=record["analysis_id"], report=None, automatic_retry=False)
            self.store.save("run", identity, run, "run_started")
            thread = threading.Thread(target=self._work, args=(run, folder), daemon=True)
            thread.start()
            return identity

    def _work(self, run, folder):
        process = None
        try:
            environment = dict(os.environ, PYTHONPATH=str(self.tools.parent / "src"), PYTHONDONTWRITEBYTECODE="1")
            with (folder / "runner.log").open("wb") as log:
                with self.lock:
                    if self.closed:
                        raise RuntimeError("platform stopped before task execution")
                    process = subprocess.Popen([sys.executable, "-B", str(self.tools / "run_crag_task.py"),
                        str(folder / "job.json")], cwd=self.tools.parent, env=environment, stdout=log, stderr=subprocess.STDOUT)
                    self.processes[run["id"]] = process
                process.wait()
            report_file = folder / "output" / "task-result.json"
            if not report_file.is_file():
                raise RuntimeError("task host stopped without a result; inspect its local runner.log")
            report = json.loads(report_file.read_text(encoding="utf-8"))
            run.update(status=report["status"], report=report)
            if process.returncode != 0 or report["status"] != "COMPLETED":
                run["status"] = "FAILED"
                run["error"] = report.get("error", "native task host failed")
        except Exception as error:
            run.update(status="FAILED", error=str(error), error_type=type(error).__name__)
        finally:
            with self.lock:
                self.processes.pop(run["id"], None)
                if not self.closed:
                    run["finished_at"] = now()
                    self.store.save("run", run["id"], run, "run_finished")

    def close(self):
        with self.lock:
            self.closed = True
            for identity, process in self.processes.items():
                if process.poll() is None:
                    process.terminate()
                run = self.store.get("run", identity)
                if run:
                    run.update(status="INTERRUPTED", finished_at=now(),
                        error="platform stopped; paid outcome may be unknown; no automatic replay")
                    self.store.save("run", identity, run, "run_interrupted")
