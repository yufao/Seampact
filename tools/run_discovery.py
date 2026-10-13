"""Four frozen-source discovery runs through the existing real AnalysisJobs.

This tool delegates credential handling and API requests to Platform. It has no
round/call/spend/context caps, automatic retry, source-specific partition answer,
native code execution, or admission step. Only sanitized result summaries leave
the private home; complete model/tool messages remain in its database.
"""

import argparse
import hashlib
import json
import os
import platform as python_platform
import re
import tempfile
import time
import uuid
from pathlib import Path

from seampact import __version__
from seampact.analysis import RULES
from seampact.service import Platform, load_sources
from seampact.store import now

SOURCE_IDS = ("mmct-image", "crag-text", "abc-policy", "disp-offline")
TERMINAL = {"COMPLETED", "FAILED", "CANCELLED", "INTERRUPTED"}
OBSERVATION = (
    "保持冻结入口的原观察语义：输入、配置、数据及模型身份；输出、拒绝、异常与取消；"
    "原控制器的激活、分支、顺序及汇合；原生状态、数据别名与原框架复制行为；"
    "工具/API、文件及进程的外部效果；初始化、关闭、资源占用及效果退休。"
    "从冻结源码和允许的源工具独立发现能力、边界及接线，不预设拆分答案；未知边界标为UNKNOWN。"
    "候选仅作为有限规则下可分析、可构建的草案。原序行为、组合、并行、远程及生产许可分别核验，"
    "静态检查和构建均不能代替原生运行验收。"
)
SECRET_FIELDS = {
    "credential", "credentials", "api_key", "apikey", "authorization", "secret",
    "password", "access_token", "refresh_token", "api_token", "key",
}
PROVIDER_FIELDS = ("provider", "base_url", "model", "docs", "capabilities", "declared_max_output_tokens")
ENGINE_FILES = ("analysis.py", "inspection.py", "verification.py", "lowering.py", "build.py",
                "providers.py", "service.py", "runtime.py", "store.py")


def reproducibility_metadata(source_registry=None):
    """Record code identity before the first metadata/API request; no paths or keys."""
    runner = Path(__file__).resolve()
    engine = runner.parent.parent / "src" / "seampact"
    identities = {name: hashlib.sha256((engine / name).read_bytes()).hexdigest() for name in ENGINE_FILES}
    registry_hash = hashlib.sha256(Path(source_registry).read_bytes()).hexdigest() if source_registry is not None else None
    return dict(schema="discovery-code-identity/1", recorded_at=now(), python_version=python_platform.python_version(),
                engine_source_sha256=identities, rules_sha256=hashlib.sha256(RULES.encode()).hexdigest(),
                runner_sha256=hashlib.sha256(runner.read_bytes()).hexdigest(), source_registry_sha256=registry_hash)


def canonical_hash(value):
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def sanitize(value, private_paths=()):
    """Conservative publication redaction; never looks up a credential value."""
    if isinstance(value, dict):
        return {k: "<redacted>" if k.lower().replace("-", "_") in SECRET_FIELDS else sanitize(v, private_paths)
                for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize(v, private_paths) for v in value]
    if not isinstance(value, str):
        return value
    for path in private_paths:
        for prefix in (str(path), Path(path).as_posix()):
            value = value.replace(prefix, "<private-path>")
    value = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+", "Bearer <redacted>", value)
    value = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "<redacted-key>", value)
    value = re.sub(r"(?i)\b(api[_-]?key|api[_-]?token|authorization|password|secret)\s*[:=]\s*([\"']?)[^\s,;\"']+\2",
                   r"\1=<redacted>", value)
    value = re.sub(r"[A-Za-z]:[/\\][^\s\"'\n]+", "<local-path>", value)
    value = re.sub(r"/(?:home|Users)/[^\s\"'\n]+", "<local-path>", value)
    return value


def write_public(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Only the explicit public output tree gets public-readable artifacts.
    os.chmod(path.parent, 0o755)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    os.chmod(temporary, 0o644)
    temporary.replace(path)


def emit(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


def usage_totals(records):
    totals = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        for name, value in record.items():
            if type(value) in (int, float):
                totals[name] = totals.get(name, 0) + value
    return totals


def wait_terminal(platform, identity, source_id, notify=emit, sleep=time.sleep):
    observed = None
    while True:
        job = platform.store.get("analysis", identity)
        if job is None:
            raise RuntimeError("started analysis is absent from the durable store")
        progress = (job["status"], job["rounds"])
        if progress != observed:
            notify(dict(event="analysis_progress", source_id=source_id, analysis_id=identity,
                        status=job["status"], rounds=job["rounds"]))
            observed = progress
        if job["status"] in TERMINAL:
            return job
        sleep(.25)  # Poll frequency, not a deadline or analysis budget.


def public_job(platform, request, job, private_paths):
    submitted = job.get("submitted") or {}
    candidate, report = submitted.get("candidate"), submitted.get("report")
    profile = job.get("provider_profile") or {}
    final_request = platform.store.get("request", request["id"]) or request
    result = dict(source_id=request["source_id"], source_revision=request.get("source_revision"),
        request_id=request["id"], analysis_id=job["id"], status=job["status"], rounds=job["rounds"],
        initial_source_version=request["source_version"], final_source_version=final_request["source_version"],
        source_files=final_request["files"], started_at=job.get("started_at"), finished_at=job.get("finished_at"),
        usage=job.get("usage", []), usage_totals=usage_totals(job.get("usage", [])),
        pending_response=bool(job.get("pending_response")), last_finish_reason=job.get("last_finish_reason"),
        error=job.get("error"), candidate=candidate, report=report,
        candidate_sha256=canonical_hash(candidate) if candidate is not None else None,
        report_sha256=canonical_hash(report) if report is not None else None,
        provider_profile={key: profile[key] for key in PROVIDER_FIELDS if key in profile},
        package=None, runtime_admitted=False, production_admitted=False)
    return sanitize(result, private_paths)


def run(platform, output, provider="deepseek", notify=emit, sleep=time.sleep,
        private_paths=(), discover_metadata=True, source_registry=None):
    missing = set(SOURCE_IDS) - set(platform.sources)
    if missing:
        raise ValueError("four source registrations required: " + ", ".join(sorted(missing)))
    output = Path(output).resolve()
    run_id = uuid.uuid4().hex
    folder = output / ("run-" + run_id)
    summary = dict(schema="autonomous-discovery-run/1", version=__version__, run_id=run_id,
        started_at=now(), finished_at=None, status="RUNNING", provider=provider,
        source_order=list(SOURCE_IDS), observation=OBSERVATION, observation_sha256=canonical_hash(OBSERVATION),
        partition_origin="API agent from frozen source and common rules; no manually selected boundaries",
        limits=dict(analysis_rounds=None, application_calls=None, spend=None, context_truncation=False),
        retry_policy="no automatic replay or retry", runtime_admitted=False, production_admitted=False,
        provider_capabilities=None, reproducibility=reproducibility_metadata(source_registry), sources=[])

    def checkpoint():
        public = sanitize(summary, private_paths)
        write_public(folder / "summary.json", public)
        write_public(output / "discovery-result.json", public)

    checkpoint()
    if discover_metadata and provider == "deepseek":
        try:
            # Profiles owns credential decryption and Transport owns headers.
            # This one metadata read is shared by all four analyses.
            summary["provider_capabilities"] = platform.profiles.discover(provider, platform.analysis.transport)
            checkpoint()
        except Exception as error:
            summary.update(status="PROVIDER_METADATA_FAILED", error_type=type(error).__name__,
                           error=sanitize(str(error), private_paths), finished_at=now())
            checkpoint()
            return summary
    interrupted = False
    for source_id in SOURCE_IDS:
        request = None
        analysis_id = None
        try:
            request = platform.freeze(source_id, OBSERVATION)
            initial_request = json.loads(json.dumps(request))
            analysis_id = platform.start_analysis(request["id"], provider)
            try:
                job = wait_terminal(platform, analysis_id, source_id, notify, sleep)
            except KeyboardInterrupt:
                interrupted = True
                platform.analysis.cancel(analysis_id)
                notify(dict(event="cancellation_requested", source_id=source_id, analysis_id=analysis_id,
                    note="the current request may finish; no following analysis will start"))
                job = wait_terminal(platform, analysis_id, source_id, notify, sleep)
            result = public_job(platform, initial_request, job, private_paths)
            report = (job.get("submitted") or {}).get("report") or {}
            rows = report.get("boundaries") or []
            buildable = job["status"] == "COMPLETED" and bool(rows) and all(row.get("buildable") is True for row in rows)
            result["engine_buildable"] = buildable
            if buildable:
                try:
                    package = platform.build(analysis_id)
                    artifact = platform.home / "packages" / package["archive"]
                    assert artifact.resolve().is_relative_to((platform.home / "packages").resolve())
                    actual_hash = hashlib.sha256(artifact.read_bytes()).hexdigest()
                    if actual_hash != package["sha256"]:
                        raise ValueError("generated package hash differs from its manifest")
                    result["package"] = dict(id=package["id"], archive=package["archive"], status=package["status"],
                        sha256=actual_hash, location="private home/packages", runtime_admitted=False, production_admitted=False)
                except Exception as error:
                    result["build_error_type"] = type(error).__name__
                    result["build_error"] = sanitize(str(error), private_paths)
            elif job["status"] == "COMPLETED":
                result["build_skipped"] = "submitted candidate contains absent or unsupported/overlapping boundaries"
        except KeyboardInterrupt:
            interrupted = True
            result = dict(source_id=source_id, status="INTERRUPTED", request_id=request["id"] if request else None,
                analysis_id=analysis_id, runtime_admitted=False, production_admitted=False)
        except Exception as error:
            result = dict(source_id=source_id, source_revision=platform.sources[source_id].get("revision"),
                status="SOURCE_START_FAILED", request_id=request["id"] if request else None,
                analysis_id=analysis_id, error_type=type(error).__name__, error=sanitize(str(error), private_paths),
                runtime_admitted=False, production_admitted=False)
        result = sanitize(result, private_paths)
        result["public_record_sha256"] = canonical_hash(result)
        write_public(folder / (source_id + ".json"), result)
        summary["sources"].append(result)
        checkpoint()
        notify(dict(event="source_finished", source_id=source_id, status=result["status"],
            rounds=result.get("rounds"), engine_buildable=result.get("engine_buildable"),
            package_status=(result.get("package") or {}).get("status")))
        if interrupted:
            break
    summary.update(finished_at=now(), status="INTERRUPTED" if interrupted else
        "COMPLETED" if all(r["status"] == "COMPLETED" and not r.get("build_error_type") for r in summary["sources"]) else "COMPLETED_WITH_FAILURES")
    summary["all_candidates_buildable"] = len(summary["sources"]) == len(SOURCE_IDS) and all(
        r.get("engine_buildable") is True and r.get("package") for r in summary["sources"])
    checkpoint()
    return sanitize(summary, private_paths)


def self_check():
    """Controlled runner contract checks; no Profiles or API transport involved."""
    class FakeStore:
        def __init__(self): self.docs = {}
        def get(self, kind, identity): return self.docs.get((kind, identity))

    class FakePlatform:
        def __init__(self, home):
            self.home, self.store = home, FakeStore()
            self.sources = {name: {"revision": "controlled-revision"} for name in SOURCE_IDS}
            self.frozen, self.built = [], []
        def freeze(self, name, observation):
            self.frozen.append((name, observation))
            request = dict(id=name, source_id=name, source_revision="controlled-revision", source_version="frozen-version", files=[])
            self.store.docs[("request", name)] = request
            return request
        def start_analysis(self, identity, provider):
            status = "FAILED" if identity == "crag-text" else "COMPLETED"
            buildable = identity != "abc-policy"
            self.store.docs[("analysis", identity)] = dict(id=identity, request_id=identity, status=status,
                rounds=73, usage=[{"prompt_tokens": 123, "completion_tokens": 45}],
                provider_profile=dict(provider=provider, model="controlled-model", credential="PRIVATE_CONFIG_MARKER",
                    capabilities={"selected": {"max_output_tokens": 393216}}),
                messages=[{"content": "PRIVATE_TRANSCRIPT_MARKER"}],
                submitted=None if status == "FAILED" else dict(candidate={"boundaries": [{"id": "automatic-result"}]},
                    report={"boundaries": [{"buildable": buildable}], "admitted": False}))
            return identity
        def build(self, identity):
            self.built.append(identity)
            path = self.home / "packages" / (identity + ".zip")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"controlled-package-integrity-fixture")
            return dict(id=identity, archive=path.name, sha256=hashlib.sha256(path.read_bytes()).hexdigest(), status="BUILT_DRAFT")

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        assert root.is_relative_to(Path(tempfile.gettempdir()).resolve()) and root != Path(root.anchor)
        fake = FakePlatform(root / "private")
        registry = root / "sources.json"
        registry.write_text('{"controlled_fixture": true}', encoding="utf-8")
        result = run(fake, root / "public", notify=lambda value: None, discover_metadata=False, source_registry=registry)
        assert [name for name, _ in fake.frozen] == list(SOURCE_IDS)
        assert len({observation for _, observation in fake.frozen}) == 1
        assert fake.built == ["mmct-image", "disp-offline"]
        assert result["sources"][-1]["status"] == "COMPLETED"
        assert all(r.get("rounds") == 73 for r in result["sources"])
        assert set(result["reproducibility"]["engine_source_sha256"]) == set(ENGINE_FILES)
        assert result["reproducibility"]["source_registry_sha256"] == hashlib.sha256(registry.read_bytes()).hexdigest()
        assert result["reproducibility"]["rules_sha256"] == hashlib.sha256(RULES.encode()).hexdigest()
        published = (root / "public/discovery-result.json").read_text(encoding="utf-8")
        assert "PRIVATE_TRANSCRIPT_MARKER" not in published and "PRIVATE_CONFIG_MARKER" not in published
        assert sanitize({"api_key": "fixture-secret", "text": "Bearer fixture-token sk-fixture123456789"}) == {
            "api_key": "<redacted>", "text": "Bearer <redacted> <redacted-key>"}
    return dict(status="PASS", scope="controlled runner contracts only", live_api="NOT_RUN")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", help="private Platform state, distinct from public output and active UI state")
    parser.add_argument("--sources", help="four-source registration JSON")
    parser.add_argument("--legacy-config", help="optional read-only current-user DeepSeek migration directory")
    parser.add_argument("--output", help="sanitized public summary output, outside the private home")
    parser.add_argument("--provider", default="deepseek", choices=("deepseek", "glm"))
    parser.add_argument("--self-check", action="store_true", help="controlled stdlib runner checks; never call an API")
    args = parser.parse_args()
    if args.self_check:
        emit(self_check())
        return
    if not all((args.home, args.sources, args.output)):
        parser.error("--home, --sources and --output are required for real discovery")
    home, output = Path(args.home).resolve(), Path(args.output).resolve()
    if home == output or output.is_relative_to(home) or home.is_relative_to(output):
        parser.error("private home and public output must be separate, non-nested directories")
    os.umask(0o077)
    private_paths = [home]
    if args.legacy_config:
        private_paths.append(Path(args.legacy_config).resolve())
    platform = Platform(home, load_sources(args.sources), args.legacy_config)
    try:
        result = run(platform, output, args.provider, private_paths=private_paths, source_registry=args.sources)
        emit(dict(event="discovery_finished", run_id=result["run_id"], status=result["status"],
                  sources=len(result["sources"]), all_candidates_buildable=result.get("all_candidates_buildable", False)))
    finally:
        platform.close()
    raise SystemExit(0 if result["status"] == "COMPLETED" else 1)


if __name__ == "__main__":
    main()
