"""Read-only, sanitized mechanism evidence; this is not an admission checker.

Only fixed probe report schemas and paths are accepted. Free-form prompts,
paths, exceptions, logs, source text and observation payloads never reach UI.
"""

import hashlib
import json
import re
from pathlib import Path


SPECS = (
    dict(id="mmct-sdk", file="mmct-sdk-result.json", directory="mmct",
         schema="mmct-sdk-probe/1", scope="original MMCT + real AutoGen SDK; controlled provider",
         origin="manually_selected_mechanism_probe",
         controlled=("model_provider", "unused_optional_tools", "package_initializers"),
         sdk=("autogen-agentchat", "autogen-core", "autogen-ext", "Pillow")),
    dict(id="crag-fragment", file="crag-fragment-result.json", directory="crag",
         schema="crag-fragment-probe/1", scope="original CRAG method fragment; controlled Context/LLM/events",
         origin="manually_selected_mechanism_probe",
         controlled=("Context", "LLM", "events", "iterator"), sdk=()),
    dict(id="crag-sdk", file="crag-sdk-result.json", directory="crag-sdk",
         schema="crag-sdk-probe/1", scope="original CRAG workflow + real SDK; controlled external endpoints",
         origin="manually_selected_mechanism_probe",
         controlled=("OpenAI", "TavilyToolSpec", "VectorStoreIndex", "SummaryIndex"),
         sdk=("llama-index-core", "llama-index-workflows")),
    dict(id="docker", file="docker-result.json", directory=None,
         schema="docker-verification/1", scope="actual container verification; per-check scope",
         origin="not_applicable", controlled=("local_container_environment",), sdk=()),
    dict(id="crag-api", file="crag-api-package-result.json", directory="crag-api",
         schema="api-package-profile/1", scope="unchanged API-generated CRAG package + real SDK; controlled endpoints",
         origin="API_SOURCE_AGENT",
         controlled=("OpenAI", "TavilyToolSpec", "VectorStoreIndex", "SummaryIndex"),
         sdk=("llama-index-core", "llama-index-workflows")),
)
STATUSES = {"PASS", "FAIL", "NOT_RUN"}
VERSION = re.compile(r"[0-9]+(?:\.[0-9]+){1,3}(?:(?:a|b|rc)[0-9]+)?(?:\.(?:post|dev)[0-9]+)?")
DOCKER_CHECKS = {"container_build", "control_image", "core_checks", "mmct_sdk", "crag_fragment",
                 "crag_sdk", "health", "persistence", "network_isolation", "read_only_sources"}


class InvalidEvidence(ValueError):
    pass


def _unique_object(items):
    value = {}
    for key, item in items:
        if key in value:
            raise InvalidEvidence("invalid_json")
        value[key] = item
    return value


def _reject_constant(value):
    raise InvalidEvidence("invalid_json")


def _blank(spec, status="NOT_RUN", reason="missing"):
    return dict(id=spec["id"], status=status, source_report=spec["file"], sha256=None,
                scope=spec["scope"], cases=0, sdk={}, candidate_origin=spec["origin"],
                controlled_dependencies=list(spec["controlled"]), checks={},
                production_admitted=False, full_source_execution="NOT_RUN",
                live_model_quality="NOT_RUN", parallel_admitted=False, boundary_count=0, reason=reason)


def _report_paths(root, spec):
    candidates = [root / spec["file"]]
    if spec["directory"]:
        candidates.append(root / spec["directory"] / spec["file"])
    found = []
    for candidate in candidates:
        resolved = candidate.resolve()
        if not resolved.is_relative_to(root):
            raise InvalidEvidence("path_escape")
        if not candidate.exists():
            if candidate.is_symlink():
                raise InvalidEvidence("invalid_report")
            continue
        if not resolved.is_file():
            raise InvalidEvidence("invalid_report")
        found.append(resolved)
    return found


def _extract(raw, spec):
    try:
        report = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique_object,
                            parse_constant=_reject_constant)
    except (ValueError, UnicodeError):
        raise InvalidEvidence("invalid_json") from None
    if not isinstance(report, dict) or report.get("schema") != spec["schema"]:
        raise InvalidEvidence("unknown_schema")
    status = report.get("status")
    if not isinstance(status, str) or status not in STATUSES:
        raise InvalidEvidence("unsupported_status")
    cases = report.get("cases", [])
    if not isinstance(cases, list) or any(not isinstance(case, dict) or
            not isinstance(case.get("status"), str) or case["status"] not in STATUSES for case in cases):
        raise InvalidEvidence("invalid_cases")
    if status == "PASS" and (any(case["status"] != "PASS" for case in cases) or
                              (not cases and spec["id"] != "docker")):
        raise InvalidEvidence("conflicting_cases")
    sdk = {}
    supplied_sdk = report.get("sdk", {})
    if not isinstance(supplied_sdk, dict):
        raise InvalidEvidence("invalid_sdk")
    for name in spec["sdk"]:
        if name not in supplied_sdk:
            if status == "PASS":
                raise InvalidEvidence("invalid_sdk")
            continue
        version = supplied_sdk[name]
        if not isinstance(version, str) or len(version) > 40 or not VERSION.fullmatch(version):
            raise InvalidEvidence("invalid_sdk")
        sdk[name] = version
    checks = {}
    boundary_count = 0
    if spec["id"] == "crag-api" and status == "PASS":
        provenance = report.get("provenance", {})
        package = report.get("package", {})
        negative = report.get("negative_control", {})
        coverage = report.get("coverage", {})
        if not all(isinstance(value, dict) for value in (provenance, package, negative, coverage)):
            raise InvalidEvidence("invalid_profile")
        boundary_count = package.get("boundaries")
        if (provenance.get("status") != "PASS" or provenance.get("origin") != "API_SOURCE_AGENT" or
            package.get("status") != "PASS" or type(boundary_count) is not int or boundary_count < 1 or
            len(coverage) != boundary_count or any(type(v) is not int or v < 1 for v in coverage.values()) or
            negative.get("status") != "BYPASS_DETECTED" or
            report.get("profile") != "crag-real-sdk-controlled-endpoints/2"):
            raise InvalidEvidence("unverified_profile_or_coverage")
    if spec["id"] == "docker":
        supplied_checks = report.get("checks", {})
        if not isinstance(supplied_checks, dict):
            raise InvalidEvidence("invalid_checks")
        for name in DOCKER_CHECKS.intersection(supplied_checks):
            check_status = supplied_checks[name]
            if isinstance(check_status, dict):
                check_status = check_status.get("status")
            if not isinstance(check_status, str) or check_status not in STATUSES:
                raise InvalidEvidence("invalid_checks")
            checks[name] = check_status
        if status == "PASS" and (not checks or any(value != "PASS" for value in checks.values())):
            raise InvalidEvidence("conflicting_checks")
    result = _blank(spec, status, "reported_mechanism_evidence")
    result.update(sha256=hashlib.sha256(raw).hexdigest(), cases=len(cases), sdk=sdk, checks=checks,
                  boundary_count=boundary_count)
    # Report assertions cannot upgrade this index to production admission,
    # full-source execution, autonomous discovery or real model quality.
    return result


def collect_evidence(root):
    """Return fixed mechanism records, safely summarized from an explicit local root.

    Root-level reports and the known mmct/crag/crag-sdk subdirectories are
    supported. Conflicting duplicate reports are INVALID rather than selecting
    a stale PASS. The current report bytes determine its displayed hash.
    """
    records = []
    root_error = None
    if root is None:
        resolved_root = None
    else:
        try:
            resolved_root = Path(root).resolve()
            if resolved_root.exists() and not resolved_root.is_dir():
                root_error = "invalid_evidence_root"
        except (OSError, TypeError, ValueError):
            resolved_root = None
            root_error = "invalid_evidence_root"
    for spec in SPECS:
        if root_error:
            result = _blank(spec, "INVALID", root_error)
        elif resolved_root is None:
            result = _blank(spec, reason="evidence_root_not_configured")
        else:
            try:
                paths = _report_paths(resolved_root, spec)
                if not paths:
                    result = _blank(spec)
                else:
                    contents = [path.read_bytes() for path in paths]
                    if any(content != contents[0] for content in contents[1:]):
                        raise InvalidEvidence("ambiguous_reports")
                    result = _extract(contents[0], spec)
            except InvalidEvidence as error:
                result = _blank(spec, "INVALID", str(error))
            except (OSError, ValueError, RuntimeError):
                result = _blank(spec, "INVALID", "unreadable_report")
        records.append(result)
    return dict(schema="mechanism-evidence-index/1", production_admitted=False, records=records,
                summary={status: sum(record["status"] == status for record in records)
                         for status in ("PASS", "FAIL", "NOT_RUN", "INVALID")})
