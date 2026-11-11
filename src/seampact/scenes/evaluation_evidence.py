"""Detach finite terminal evidence before business owners are closed.

Only passive values cross this boundary. No deepcopy, serializer, property or
owner callback is invoked to make an unsupported value appear detachable.
Explicit teacher image selectors preserve their actual bytes in the run's
evidence directory; paths alone do not establish independence from an owner.
"""

import base64
import hashlib
import math
import subprocess
import sys
import time
from pathlib import Path

from seampact.source.record_ports import RecordSnapshot


class PassiveEvidenceGap(ValueError):
    pass


def _passive_numpy(value, active, location):
    numpy, kind = sys.modules.get("numpy"), type(value)
    # The same exact official type boundary as group-port/3: never consult a
    # source __class__/__array__, ndarray subclass, serializer or copy hook.
    scalar = numpy is not None and any(kind is candidate for candidate in numpy.sctypeDict.values())
    if numpy is None or not (kind is numpy.ndarray or scalar):
        raise PassiveEvidenceGap("live or unsupported evaluation evidence at " + location)
    array = numpy.asarray(value)  # Only after the exact trusted-type check.
    dtype = array.dtype
    official = any(dtype.type is candidate for candidate in numpy.sctypeDict.values())
    if (not official or dtype.hasobject or dtype.fields is not None or dtype.subdtype is not None
            or dtype.kind not in {"b", "i", "u", "f", "U"}):
        raise PassiveEvidenceGap("NumPy evidence has no supported passive value dtype at " + location)
    if dtype.kind == "f" and dtype.char not in {"e", "f", "d"}:
        # Python float represents float16/32/64 losslessly. Long double may
        # contain additional precision/range; never narrow it silently.
        raise PassiveEvidenceGap("NumPy floating evidence has no covered lossless Python projection at " + location)
    # The official method returns fresh builtin containers/scalars. Reuse the
    # ordinary recursive boundary to reject NaN/Inf and ensure finite values.
    return _passive(numpy.ndarray.tolist(array), active, location + ".numpy")


def _passive(value, active, location):
    kind = type(value)
    if value is None or any(kind is k for k in (str, bool, int)):
        return value
    if kind is float:
        if not math.isfinite(value):
            raise PassiveEvidenceGap("non-finite evaluation evidence at " + location)
        return value
    if kind is bytes:
        return {"$bytes": base64.b64encode(value).decode("ascii")}
    if kind is subprocess.CompletedProcess:
        fields = vars(value)
        names = {"args", "returncode", "stdout", "stderr"}
        if (set(fields) - names - {"seampact_completion"} or not names <= set(fields)
                or type(fields["returncode"]) is not int
                or type(fields["args"]) not in (str, list, tuple)
                or type(fields["args"]) in (list, tuple) and any(type(v) is not str for v in fields["args"])
                or any(fields[key] is not None and type(fields[key]) not in (str, bytes)
                       for key in ("stdout", "stderr"))
                or "seampact_completion" in fields and type(fields["seampact_completion"]) is not dict):
            raise PassiveEvidenceGap("unsupported original CLI result fields at " + location)
        if id(value) in active:
            raise PassiveEvidenceGap("cyclic CLI evidence at " + location)
        active.add(id(value))
        try:
            result = {key: _passive(fields[key], active, location + "." + key) for key in sorted(names)}
            result["result_type"] = "subprocess.CompletedProcess"
            if "seampact_completion" in fields:
                result["completion"] = _passive(fields["seampact_completion"], active, location + ".completion")
            return result
        finally:
            active.remove(id(value))
    if not any(kind is k for k in (dict, list, tuple, RecordSnapshot)):
        return _passive_numpy(value, active, location)
    identity = id(value)
    if identity in active:
        raise PassiveEvidenceGap("cyclic evaluation evidence at " + location)
    active.add(identity)
    try:
        if kind is RecordSnapshot:
            return _passive(dict(module=value.module, symbol=value.symbol,
                source_sha256=value.source_sha256, fields=value.fields), active, location + ".record")
        if kind is dict:
            if any(type(key) is not str for key in value):
                raise PassiveEvidenceGap("evaluation evidence requires plain string keys at " + location)
            return {key: _passive(item, active, location + "." + key) for key, item in value.items()}
        return [_passive(item, active, location + "[]") for item in value]
    finally:
        active.remove(identity)


def _select(evidence, selector):
    value = evidence[selector["field"]]
    for part in selector.get("path", []):
        value = value[part]
    return value


def passive_value(value):
    """Project already returned finite values without invoking source methods."""
    return _passive(value, set(), "value")


def passive_numpy_value(value):
    """Project only supported exact NumPy values; other native leaves reject."""
    return _passive_numpy(value, set(), "value")


def _replace(evidence, selector, replacement):
    owner, key = evidence, selector["field"]
    for part in selector.get("path", []):
        owner, key = owner[key], part
    owner[key] = replacement


def passive_evaluation_snapshot(terminal, calls, model_calls=None, *, criteria=(), destination=None):
    """Capture an owner-free terminal and the explicit local teacher images.

    Failure means that this evidence has not been detached; callers must not
    infer that its live owner or resource lease can now be discarded.
    """
    started = time.perf_counter()
    result = _passive(dict(terminal=terminal, calls=calls,
        model_calls=[] if model_calls is None else model_calls), set(), "evaluation")
    selectors = [selector for criterion in criteria if criterion
                 for selector in (criterion.get("teacher") or {}).get("images", [])]
    artifacts = []
    output_files = result["terminal"].get("output_artifacts", [])
    if output_files:
        if destination is None:
            raise PassiveEvidenceGap("original CLI artifacts require a retained evidence directory")
        folder = Path(destination).resolve()
        folder.mkdir(parents=True, exist_ok=True)
        for original in output_files:
            if (type(original) is not dict or original.get("format") != "npz"
                    or type(original.get("path")) is not str or type(original.get("bytes")) is not int
                    or original["bytes"] <= 0 or type(original.get("sha256")) is not str):
                raise PassiveEvidenceGap("invalid declared CLI artifact evidence")
            try:
                data = Path(original["path"]).read_bytes()
                digest = hashlib.sha256(data).hexdigest()
                if len(data) != original["bytes"] or digest != original["sha256"]:
                    raise PassiveEvidenceGap("original CLI artifact changed before owner release")
                target = folder / (digest + ".npz")
                if target.is_symlink() or target.resolve().parent != folder:
                    raise PassiveEvidenceGap("retained CLI artifact must be an independent file in its evidence directory")
                try:
                    with target.open("xb") as stream:
                        stream.write(data)
                except FileExistsError:
                    if target.read_bytes() != data:
                        raise PassiveEvidenceGap("retained CLI artifact differs from its content identity") from None
                artifacts.append(dict(path=str(target), sha256=digest, bytes=len(data),
                    original_path=original["path"], source="original_cli_output", format="npz"))
                original["original_path"], original["path"] = original["path"], str(target)
            except OSError as error:
                raise PassiveEvidenceGap("original CLI artifact could not be retained") from error
        # Scoring consumes terminal.evidence, rather than the top-level event.
        # Remap only the exact file bindings produced by this scene; process
        # argv and original completion receipts remain unchanged provenance.
        replacements = {(item["original_path"], item["sha256"], item["bytes"]): item["path"]
                        for item in output_files}
        evidence = result["terminal"].setdefault("evidence", {})
        for calls_ in evidence.get("original_results", {}).values():
            for returned in calls_:
                if type(returned) is not dict:
                    continue
                for artifact in returned.get("artifacts", {}).values():
                    key = (artifact.get("path"), artifact.get("sha256"), artifact.get("bytes"))
                    if key in replacements:
                        artifact["original_path"], artifact["path"] = artifact["path"], replacements[key]
        evidence["output_artifacts"] = output_files
    if selectors:
        if destination is None:
            raise PassiveEvidenceGap("teacher images require a retained evidence directory")
        folder = Path(destination).resolve()
        folder.mkdir(parents=True, exist_ok=True)
        evidence = result["terminal"].get("evidence", {})
        # Resolve before replacement so overlapping selectors cannot read a
        # path introduced by an earlier selector instead of original evidence.
        try:
            selected = [(selector, _select(evidence, selector)) for selector in selectors]
            from seampact.providers import image_evidence
            for selector, value in selected:
                wire, identity = image_evidence(value, selector.get("detail", "auto"))
                data = base64.b64decode(wire["image_url"]["url"].split(",", 1)[1], validate=True)
                if hashlib.sha256(data).hexdigest() != identity["sha256"]:
                    raise PassiveEvidenceGap("teacher image changed during evidence capture")
                suffix = {"image/png": ".png", "image/jpeg": ".jpg",
                          "image/gif": ".gif", "image/webp": ".webp"}[identity["mime_type"]]
                target = folder / (identity["sha256"] + suffix)
                if target.resolve().parent != folder:
                    raise PassiveEvidenceGap("retained evidence path leaves its directory")
                try:
                    with target.open("xb") as stream:
                        stream.write(data)
                except FileExistsError:
                    if hashlib.sha256(target.read_bytes()).hexdigest() != identity["sha256"]:
                        raise PassiveEvidenceGap("retained image differs from its content identity") from None
                _replace(evidence, selector, dict(source="path", value=str(target)))
                artifacts.append(dict(path=str(target), sha256=identity["sha256"],
                    bytes=len(data), original_path=identity["path"], field=selector["field"],
                    selector_path=list(selector.get("path", []))))
        except PassiveEvidenceGap:
            raise
        except (KeyError, IndexError, TypeError, ValueError, OSError) as error:
            raise PassiveEvidenceGap("teacher image evidence could not be retained") from error
    result.update(schema="passive-evaluation/1", artifacts=artifacts,
                  capture_duration_ms=(time.perf_counter() - started) * 1000)
    return result


def verify_passive_evaluation(snapshot):
    """Recheck retained bytes immediately before scoring, without an owner."""
    for artifact in snapshot.get("artifacts", []):
        try:
            data = Path(artifact["path"]).read_bytes()
        except OSError as error:
            raise PassiveEvidenceGap("retained teacher image is unavailable") from error
        if len(data) != artifact["bytes"] or hashlib.sha256(data).hexdigest() != artifact["sha256"]:
            raise PassiveEvidenceGap("retained teacher image changed before scoring")
    return snapshot
