"""Apply checked service selections to an isolated original CLI configuration.

Only source-proved scalar leaves are replaced. The original file, loader and
remaining configuration bytes are retained; credentials exist only in the
private temporary file while the original subprocess is running.
"""

import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

from seampact.configuration.dependencies import reference


def _leaves(value, path=()):
    if reference(value) is not None:
        if not path:
            raise ValueError("configuration resources need an original scalar key path")
        yield path, value
        return
    if not isinstance(value, dict) or not value or any(not isinstance(k, str) for k in value):
        raise ValueError("configuration overrides contain only checked resource references")
    for key, child in value.items():
        yield from _leaves(child, path + (key,))


def configuration_plans(bindings, entry, runtime=False):
    configuration = entry.get("configuration", {})
    if not configuration:
        return {}
    if entry.get("kind") != "cli" or not isinstance(configuration, dict):
        raise ValueError("configuration files require an original CLI entry")
    plans = {}
    for parameter, overrides in configuration.items():
        original = entry.get("fixed_arguments", {}).get(parameter)
        if not isinstance(parameter, str) or not parameter.isidentifier() or not isinstance(original, str):
            raise ValueError("configuration injection needs its fixed original CLI path")
        for keys, value in _leaves(overrides):
            ref = reference(value)
            row = bindings.rows.get(ref["id"], {})
            check = row.get("entry_checks", {}).get(entry["id"], {})
            proofs = [p["proof"] for p in check.get("mapping_proofs", [])
                      if p.get("location") == "configuration" and p.get("parameter") == parameter
                      and p.get("field") == ref["field"] and tuple(p.get("parameter_path", [])) == keys]
            if check.get("status") != "MAPPING_CHECKED" or len(proofs) != 1 or not proofs[0].get("proved"):
                raise ValueError("configuration leaf has no unique checked original consumer")
            proof = proofs[0]
            resolved = bindings.value(value, entry["id"], "configuration", parameter,
                                      runtime=runtime, parameter_path=keys)
            suffix = proof.get("original_url_suffix", "")
            if suffix and ref["field"] == "base_url" and isinstance(resolved, str) and resolved.endswith(suffix):
                resolved = resolved[:-len(suffix)]
            filename = proof["configuration_file"]
            if parameter not in plans:
                data = bindings.inspector.data(filename)
                source = data.decode("utf-8") if isinstance(data, bytes) else data
                plans[parameter] = dict(original=original, source=source, filename=filename, changes=[])
            plan = plans[parameter]
            if plan["filename"] != filename:
                raise ValueError("one CLI parameter cannot identify multiple original configurations")
            span = proof["value_span"]
            if (type(span.get("start")) is not int or span["start"] != span.get("end")
                    or not 1 <= span["start"] <= len(plan["source"].splitlines())
                    or span["key"] != keys[-1]):
                raise ValueError("configuration scalar source span is invalid")
            if any(old[0]["start"] == span["start"] for old in plan["changes"]):
                raise ValueError("configuration scalar cannot receive conflicting selections")
            plan["changes"].append((span, resolved))
    return plans


@contextmanager
def configuration_arguments(bindings, entry, arguments):
    """Yield CLI arguments pointing at private copies; remove copies on exit."""
    plans = configuration_plans(bindings, entry, runtime=True)
    if not plans:
        yield arguments
        return
    with tempfile.TemporaryDirectory(prefix="seampact-config-") as directory:
        selected = dict(arguments)
        for index, (parameter, plan) in enumerate(plans.items()):
            if selected.get(parameter) != plan["original"]:
                raise ValueError("the original checked configuration path cannot be overridden by a task")
            lines = plan["source"].splitlines(keepends=True)
            for span, value in plan["changes"]:
                old = lines[span["start"] - 1]
                end = "\r\n" if old.endswith("\r\n") else "\n" if old.endswith("\n") else ""
                lines[span["start"] - 1] = (" " * span["indent"] + span["key"] + ": "
                    + json.dumps(value, ensure_ascii=False, allow_nan=False) + end)
            path = Path(directory) / ("config-" + str(index) + ".yaml")
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
                stream.write("".join(lines))
            selected[parameter] = str(path)
        yield selected


def redact_process_result(bindings, result):
    """Keep subprocess results useful without returning resolved credentials."""
    for field in ("stdout", "stderr"):
        value = getattr(result, field, None)
        if isinstance(value, bytes):
            for credential in bindings.keys.values():
                if credential:
                    value = value.replace(credential.encode("utf-8"), b"[private credential]")
        elif isinstance(value, str):
            value = bindings.redact(value)
        setattr(result, field, value)
    return result
