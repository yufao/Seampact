"""Independent package intake and finite transformation replay.

Intake never imports package code. A validated adapter may subsequently be used
by an explicitly selected native profile; integrity alone grants no operation.
"""
import hashlib
import json
import re
import zipfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .build import ADAPTER, VERIFY
from .inspection import digest, symbols
from .lowering import lower_loop
from .verification import check_candidate, source_unit


class PackageRejected(ValueError):
    pass


def canonical_hash(value):
    return digest(json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False).encode())


def _object(items):
    value = {}
    for key, item in items:
        if key in value:
            raise PackageRejected("duplicate JSON key")
        value[key] = item
    return value


def _constant(value):
    raise PackageRejected("non-finite JSON value")


def _json(data):
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=_object,
                          parse_constant=_constant)
    except (ValueError, UnicodeError) as error:
        raise PackageRejected("invalid package JSON") from error


def _path(name):
    if not isinstance(name, str) or not name or "\\" in name or ":" in name or "\x00" in name:
        raise PackageRejected("unsafe package path")
    path = PurePosixPath(name)
    if path.is_absolute() or any(p in {".", ".."} for p in name.split("/")):
        raise PackageRejected("unsafe package path")
    if path.as_posix() != name:
        raise PackageRejected("noncanonical package path")
    return name


class _Source:
    def __init__(self, request, payloads):
        self.request = request
        self.files = {item["path"]: item for item in request["files"]}
        self.payloads = payloads

    def data(self, path):
        return self.payloads["source/" + path]

    def inspect_symbols(self, path):
        return dict(path=path, sha256=self.files[path]["sha256"],
                    **symbols(path, self.data(path)))


@dataclass
class CheckedPackage:
    manifest: dict
    payloads: dict
    sha256: str
    request: dict
    report: dict

    def receipt(self):
        return dict(schema="package-intake/1", status="PASS", sha256=self.sha256,
                    package_id=self.manifest["id"], source_version=self.request["source_version"],
                    boundaries=len(self.manifest["candidate"]["boundaries"]),
                    modes=dict(Counter(b["mode"] for b in self.manifest["candidate"]["boundaries"])),
                    adapter_identity="PASS", independent_source_check="PASS",
                    transformation_replay="PASS", runtime_conformance="NOT_RUN",
                    production_admitted=False, parallel_admitted=False)


def inspect_package(archive, expected_sha256):
    """Check exact inventory, frozen bytes, engine-owned code and replayed R.

    A caller supplies the expected digest from its experiment record, rather
    than trusting an archive's own assertion. No file is extracted or executed.
    """
    if not isinstance(expected_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise PackageRejected("expected archive digest required")
    archive = Path(archive)
    raw = archive.read_bytes()
    actual_sha256 = hashlib.sha256(raw).hexdigest()
    if actual_sha256 != expected_sha256:
        raise PackageRejected("archive digest mismatch")
    try:
        with zipfile.ZipFile(archive) as bundle:
            names = bundle.namelist()
            if len(names) != len(set(names)):
                raise PackageRejected("duplicate archive member")
            for info in bundle.infolist():
                _path(info.filename)
                if info.is_dir() or (info.external_attr >> 16) & 0o170000 == 0o120000:
                    raise PackageRejected("directory or symlink member rejected")
            manifest = _json(bundle.read("manifest.json"))
            if not isinstance(manifest, dict) or manifest.get("schema") != "module-package/2" or manifest.get("status") != "BUILT_DRAFT" or manifest.get("admitted") is not False:
                raise PackageRejected("unrecognized draft manifest")
            hashes = manifest["artifact_hashes"]
            if not isinstance(hashes, dict) or "manifest.json" in hashes:
                raise PackageRejected("invalid artifact inventory")
            if set(names) != set(hashes) | {"manifest.json"}:
                raise PackageRejected("undeclared or missing archive member")
            payloads = {}
            for name, expected in hashes.items():
                _path(name)
                data = bundle.read(name)
                if not isinstance(expected, str) or digest(data) != expected:
                    raise PackageRejected("artifact digest mismatch")
                payloads[name] = data
    except (KeyError, TypeError, zipfile.BadZipFile) as error:
        raise PackageRejected("invalid package envelope") from error
    if payloads.get("adapter.py") != ADAPTER.encode() or payloads.get("verify.py") != VERIFY.encode():
        raise PackageRejected("adapter or verifier is not engine-owned code")
    try:
        request, stored_report = _json(payloads["request.json"]), _json(payloads["verification.json"])
        records = request["files"]
        if not isinstance(records, list) or len({r["path"] for r in records}) != len(records):
            raise PackageRejected("invalid frozen source inventory")
        for record in records:
            path = _path(record["path"])
            data = payloads["source/" + path]
            if type(record["bytes"]) is not int or record["bytes"] < 0:
                raise PackageRejected("invalid frozen byte count")
            if digest(data) != record["sha256"] or len(data) != record["bytes"]:
                raise PackageRejected("frozen source mismatch")
        if {name for name in payloads if name.startswith("source/")} != {"source/" + r["path"] for r in records}:
            raise PackageRejected("source inventory mismatch")
        version = digest(json.dumps(records, sort_keys=True).encode())
        if request["source_version"] != version or manifest["source_version"] != version:
            raise PackageRejected("source version mismatch")
        source = _Source(request, payloads)
        fresh_report = check_candidate(source, manifest["candidate"])
        if fresh_report != stored_report or not fresh_report["boundaries"] or not all(b["buildable"] for b in fresh_report["boundaries"]):
            raise PackageRejected("independent candidate check mismatch")
        changed, transforms = {}, []
        for boundary in manifest["candidate"]["boundaries"]:
            if boundary["mode"] != "R":
                continue
            path, unit = boundary["path"], boundary.get("unit", "file")
            key = (path, unit)
            original = changed.get(key, source_unit(source, path, unit))
            lowered = lower_loop(original, boundary["symbol"], boundary.get("loop_index", 0))
            changed[key] = lowered["source"]
            transforms.append(dict(id=boundary["id"], path=path, unit=unit, recipe=lowered["recipe"],
                input_unit_sha256=digest(original.encode()), output_unit_sha256=digest(lowered["source"].encode()),
                verification=lowered["verification"]))
        if manifest["transformations"] != transforms:
            raise PackageRejected("transformation recipe mismatch")
        expected_patches = {}
        for path in dict.fromkeys(p for p, unit in changed):
            if path.endswith(".ipynb"):
                book = json.loads(source.data(path).decode("utf-8-sig"))
                for (p, unit), code in changed.items():
                    if p == path:
                        book["cells"][int(unit.split(":")[1])]["source"] = code.splitlines(keepends=True)
                expected_patches["patched/" + path] = json.dumps(book, ensure_ascii=False, indent=2).encode()
            else:
                expected_patches["patched/" + path] = changed[(path, "file")].encode()
        if {name for name in payloads if name.startswith("patched/")} != set(expected_patches):
            raise PackageRejected("patched inventory mismatch")
        if any(payloads[name] != data for name, data in expected_patches.items()):
            raise PackageRejected("patched source differs from independent replay")
    except (KeyError, TypeError, SyntaxError, UnicodeError) as error:
        raise PackageRejected("invalid source or transformation data") from error
    return CheckedPackage(manifest, payloads, actual_sha256, request, fresh_report)


def match_discovery_record(package, record):
    """Link an intact package to the preserved real API experiment record.

    This is experiment provenance matching, not cryptographic proof of an API
    call or a production admission certificate.
    """
    saved = dict(record)
    expected = saved.pop("public_record_sha256", None)
    if canonical_hash(saved) != expected or record.get("status") != "COMPLETED":
        raise PackageRejected("invalid discovery record")
    artifact = record.get("package") or {}
    if artifact.get("sha256") != package.sha256 or artifact.get("id") != package.manifest["id"]:
        raise PackageRejected("discovery package identity mismatch")
    if canonical_hash(package.manifest["candidate"]) != record.get("candidate_sha256") or canonical_hash(package.report) != record.get("report_sha256"):
        raise PackageRejected("discovery candidate or report mismatch")
    if record.get("final_source_version") != package.request["source_version"]:
        raise PackageRejected("discovery source version mismatch")
    return dict(status="PASS", analysis_id=record["analysis_id"], request_id=record["request_id"],
                source_id=record["source_id"], source_revision=record.get("source_revision"),
                record_sha256=canonical_hash(record), origin="API_SOURCE_AGENT",
                production_admitted=False)
