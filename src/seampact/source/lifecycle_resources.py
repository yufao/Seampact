"""Consume checked original weight bindings without granting loader effects.

Resolved values stay local to the checker. Evidence contains digests, original
mapping proofs and declared-file identities, never credentials or path values.
The normal worker check repeats the resolution against its actual assets.
"""

import copy
import ast
import base64
import hashlib
from importlib import metadata
import json

from seampact.configuration.dependencies import reference


RULE = "bound-resource-value/1"
EFFECT_RULE = "bound-sdk-call-facts/1"


# These are conditional facts from the accepted, exact-byte SDK audit, not
# permissions. They deliberately have no SUPPORTED lifecycle conclusion. A
# source/configuration/domain closure must still be proved by its own rule.
# Keeping the catalog in the executable closure also freezes its trust origin;
# candidate fields and files in an imported repository cannot add an entry.
_TQDM_FILES = (
    ("tqdm", "4.64.0", "tqdm/std.py", "dc3f2a7d1fc2751235d80774e459bd9dd99f1650f933a1efb4ada09fe2900c01"),
    ("tqdm", "4.64.0", "tqdm/_monitor.py", "524bbe0cf5a0cc9edd3b908ad3cc4a24af84fc5eaa43e241de4b2e5dccd2611d"),
)
_CREATOR_GUARDS = (
    "ADMISSION_STOPPED", "OWNER_LEASE_ENDED", "ROOTS_AND_PERMITS_DRAINED",
    "PREPARATION_AND_EVALUATION_USERS_DRAINED", "NO_UNKNOWN_CALLS",
    "ALL_MONITOR_CREATORS_ACCOUNTED", "EXACT_LOADED_OBJECT_AND_METHOD",
    "NO_ACTIVE_INSTANCES", "NO_OTHER_UNACCOUNTED_PYTHON_WORK",
    "ORIGINAL_METHOD_JOIN_CONFIRMED", "PYTHON_THREADS_QUIESCENT",
    "PROCESS_AND_BACKEND_RELEASE_CONFIRMED",
)


def _fact(identity, roots, path, sha256, line, call, sdk, effects, remaining, cleanup=None):
    return dict(id=identity, roots=roots, original=dict(path=path, sha256=sha256, line=line, call=call),
        sdk=[dict(distribution=d, version=v, path=p, sha256=h) for d, v, p, h in sdk],
        effects=effects, remaining=remaining, cleanup=cleanup)


_EXTRACT = ("refchecker/extractor/llm_extractor.py", "LLMExtractor.extract_claim_triplets")
_NLI = ("refchecker/checker/nli_checker.py", "NLIChecker.check")
_MONITOR_CLEANUP = dict(rule="exact-object-original-method-join/1", scope="ENDING_WORKER_ONLY",
    object=dict(module="tqdm.std", owner="tqdm", attribute="monitor"),
    exact_type=dict(module="tqdm._monitor", symbol="TMonitor"), method="exit",
    owner_identity_attribute="tqdm_cls", empty_owner_collection="_instances",
    guards=list(_CREATOR_GUARDS))
_FACTS = (
    _fact("progress-monitor/extraction", [_EXTRACT], _EXTRACT[0],
        "48ae42a2be141ad279fb3e139aef6829a539c3e7dfd97a2be6b181b4a40591a8", 143,
        "tqdm(range(0, len(prompt_list), self.batch_size))", _TQDM_FILES,
        dict(python_background="MAY_SURVIVE_RETURN", idle_monitor_is_business_work=False),
        ["reachable original creation and all subsequent creators are not closed"], _MONITOR_CLEANUP),
    _fact("progress-monitor/nli", [_NLI], _NLI[0],
        "d3d9ed0318f04a1e68c0149b4ad413a38dfbad05a6e0b96ea72a3e15a236883e", 75,
        "tqdm(range(0, len(claims), self.batch_size))", _TQDM_FILES,
        dict(python_background="MAY_SURVIVE_RETURN", idle_monitor_is_business_work=False),
        ["public joint/segmented branches and all subsequent creators are not closed"], _MONITOR_CLEANUP),
    _fact("batch-completion/logging", [_EXTRACT], "refchecker/utils.py",
        "6052475b3c1944d811c63bcccd2e7586a06e2d7748ac7c9b14d881c91a8187ef", 146,
        "batch_completion(model=model, messages=message_list, temperature=temperature, n=n_choices, max_tokens=max_new_tokens, api_base=api_base, max_workers=None, **kwargs)",
        (("litellm", "1.54.1", "litellm/batch_completion/main.py", "74ee52a88f18fe66e9418d0ffd4bffeedc4aa69eaf213e228a7e5ac58ee450e9"),
         ("litellm", "1.54.1", "litellm/utils.py", "1e14e4c20f19924ab7d9b2b224ca8f1f7a0e20876ad7cb83b7fce2d2720e3c71")),
        dict(batch_executor="WAITS_SUBMITTED_FUTURES", python_background="SEPARATE_SUCCESS_LOGGING_THREADS",
             whole_owner_read_only=False, remote_cancellation="UNKNOWN"),
        ["effective provider/stream/client/callback/cache/global configuration is not closed",
         "logging thread completion and retry lifetime require runtime guards",
         "HTTP return does not establish arbitrary remote job cancellation"]),
    _fact("fast-tokenizer/configuration", [_NLI], _NLI[0],
        "d3d9ed0318f04a1e68c0149b4ad413a38dfbad05a6e0b96ea72a3e15a236883e", 79,
        "self.tokenizer(batch_references, batch_claims, max_length=512, truncation=True, return_tensors='pt', padding=True, return_token_type_ids=True)",
        (("transformers", "4.41.2", "transformers/tokenization_utils_fast.py", "5d50164474fd6d2eb4a2636fe9ac3680b20b1afa421d71f3ee39c7ff53eecb2c"),),
        dict(owner_state="MUTATES_TRUNCATION_AND_PADDING_CONFIGURATION", whole_owner_read_only=False),
        ["exact original constructor/resource/configuration effects remain unbound",
         "serial original owner and deterministic reconstruction need a source rule",
         "public joint/segmented branches and callback values are not closed"]),
    *(_fact("dataloader/single-process/" + str(index), [(path, symbol)], path, sha, line,
        "torch.utils.data.DataLoader(test_dataset, batch_size=1)",
        (("torch", "2.7.1+cu128", "torch/utils/data/dataloader.py", "80d0a8cfe36e1a252880e86d2b3324f137ff5f6d7abc3dbbacfeec9837839c96"),),
        dict(loader_num_workers=0, pin_memory=False, persistent_workers=False,
             whole_process_effects="UNKNOWN"),
        ["CLI input/output/scratch/cache and child/native/CUDA ownership effects are not closed",
         "model/configuration/resource effects and the complete installed environment remain required"])
      for index, (path, symbol, sha, line) in enumerate((
        ("test_docker/src/trufor_test.py", "trufor_test.py", "eb20f3ad039567ca47ed4b922c4cee014ab336427100b39bbd18929962aeda1e", 74),
        ("TruFor_train_test/test.py", "test.py", "403c554f96fe7923572543cc62cf9d50792fe1ef57e2b6eb8418baaa8577b18e", 72)))),
)


def sdk_identity(requirements):
    """Read distribution/RECORD bytes without importing or executing an SDK.

    This is an actual-host observation. It never enters a frozen contract digest
    and cannot turn an incomplete effect fact into a lifecycle conclusion.
    """
    files = []
    for wanted in requirements:
        result = dict(**wanted, status="UNKNOWN")
        try:
            dist = metadata.distribution(wanted["distribution"])
            matches = [p for p in dist.files or () if str(p).replace("\\", "/") == wanted["path"]]
            if len(matches) != 1 or dist.version != wanted["version"]:
                raise ValueError("distribution version/file differs")
            selected = matches[0]
            data = selected.locate().read_bytes()
            actual = hashlib.sha256(data).hexdigest()
            record = selected.hash
            encoded = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip("=")
            checked = (actual == wanted["sha256"] and record is not None
                and record.mode == "sha256" and record.value == encoded)
            result.update(status="CHECKED" if checked else "UNKNOWN", actual_sha256=actual,
                          record_matches=record is not None and record.mode == "sha256" and record.value == encoded)
        except (metadata.PackageNotFoundError, OSError, ValueError, TypeError):
            result["reason"] = "exact installed distribution/version/file/RECORD identity is unavailable"
        files.append(result)
    return dict(schema="sdk-file-observation/1", basis="ACTUAL_HOST_METADATA_AND_BYTES",
                status="CHECKED" if files and all(f["status"] == "CHECKED" for f in files) else "UNKNOWN", files=files)


def compile_effects(inspector, entry, request_domain=None, resource_values=()):
    """Consume exact original-call facts while retaining each missing premise.

    Source reachability, effective values, configuration and resource effects
    have independent obligations. The catalog describes partial effects only;
    matching all its hashes does not discharge any complete owner condition.
    """
    facts, requirements = [], {}
    for definition in _FACTS:
        if (entry.get("path"), entry.get("symbol")) not in definition["roots"]:
            continue
        definition = copy.deepcopy(definition)
        original = definition["original"]
        source_status = "UNKNOWN"
        try:
            data = inspector.data(original["path"])
            if (hashlib.sha256(data).hexdigest() == original["sha256"]
                    and inspector.files[original["path"]]["sha256"] == original["sha256"]):
                calls = [node for node in ast.walk(ast.parse(data)) if isinstance(node, ast.Call)
                         and node.lineno == original["line"] and ast.unparse(node) == original["call"]]
                if len(calls) == 1:
                    source_status = "CHECKED"
        except (OSError, ValueError, KeyError, TypeError, SyntaxError):
            pass
        premises = dict(original_call=dict(status=source_status, **original),
            request_domain=dict(status="CHECKED" if (request_domain or {}).get("status") == "CHECKED" else "UNKNOWN",
                                digest=(request_domain or {}).get("digest")),
            reachable_source_and_module_effects=dict(status="UNKNOWN"),
            effective_options_and_global_configuration=dict(status="UNKNOWN"),
            resource_effects=dict(status="UNKNOWN" if any(v.get("values") for v in resource_values) else "NOT_APPLICABLE"),
            sdk_identity=dict(status="REQUIRED_AT_ACTUAL_HOST", files=definition["sdk"]))
        cleanup = definition["cleanup"]
        if cleanup is not None:
            cleanup.update(status="UNKNOWN", executable=False,
                reason="exact SDK objects plus closed creator scope and all runtime guards are required")
        fact = dict(id=definition["id"], status="PARTIAL" if source_status == "CHECKED" else "UNKNOWN",
            permission="NOT_GRANTED", premises=premises, conditional_effects=definition["effects"],
            remaining_obligations=definition["remaining"], cleanup=cleanup)
        facts.append(fact)
        for wanted in definition["sdk"]:
            requirements[(wanted["distribution"], wanted["path"])] = wanted
    evidence = dict(schema="bound-sdk-call-evidence/1", rule=EFFECT_RULE,
        status="PARTIAL" if facts and all(f["status"] == "PARTIAL" for f in facts) else "UNKNOWN" if facts else "NOT_APPLICABLE",
        permission="NOT_GRANTED", facts=facts,
        scope="conditional original-call facts only; no whole-owner lifecycle conclusion or executable cleanup")
    evidence["digest"] = _digest(evidence)
    return evidence, sdk_identity([requirements[key] for key in sorted(requirements)])


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        allow_nan=False, separators=(",", ":")).encode()).hexdigest()


def _assets(resource, observed):
    """Content evidence covers declared files only, not loader/cache effects."""
    root = str(resource.get("path", "")).replace("\\", "/").rstrip("/")
    observed_root = str(observed.get("resolved_path", "")).replace("\\", "/").rstrip("/")
    path_matches = root == observed_root
    expected = ([dict(path=".", **{k: resource[k] for k in ("sha256", "bytes") if k in resource})]
                if resource.get("kind") == "file" else resource.get("required_files", []))
    expected = {item["path"]: item for item in expected if type(item) is dict and type(item.get("path")) is str}
    files = []
    for item in observed.get("files", []):
        path = str(item.get("path", "")).replace("\\", "/")
        relative = "." if resource.get("kind") == "file" and path == root else (
            path[len(root) + 1:] if root and path.startswith(root + "/") else None)
        wanted = expected.get(relative, {})
        checked = (path_matches and type(item.get("sha256")) is str and item.get("sha256") == wanted.get("sha256")
                   and item.get("content_identity") == "checked_sha256"
                   and item.get("checksum_matches") is True
                   and ("bytes" not in wanted or wanted["bytes"] == item.get("bytes")))
        files.append(dict(path=relative, bytes=item.get("bytes"), sha256=item.get("sha256"),
                          status="CHECKED" if checked else "UNKNOWN"))
    covered = {item["path"] for item in files if item["status"] == "CHECKED"}
    return dict(status="CHECKED" if expected and set(expected) <= covered else "UNKNOWN",
                scope="DECLARED_FILES_ONLY", completeness="UNKNOWN",
                path_matches=path_matches,
                manifest_digest=_digest(list(expected.values())),
                files=sorted(files, key=lambda item: item["path"] or ""),
                effects=dict(status="UNKNOWN", reason="original loader, configuration, SDK and cache effects are not bound"))


def resolve_values(values, entry_id, location, bindings):
    """Use the native resolver's exact entry/location/field mapping.

    The returned resolved values are ephemeral. Service/credential references
    deliberately remain unresolved: runtime=False's empty credential is not an
    original value or an authentication-effect proof.
    """
    reports, observations = [], []

    def resolve(value, parameter, keys=()):
        ref = reference(value)
        if ref is not None:
            report = dict(parameter=parameter, parameter_path=list(keys), dependency=copy.deepcopy(ref),
                          status="UNKNOWN", reason=None)
            reports.append(report)
            observed, resource = {}, None
            try:
                if bindings is None:
                    raise ValueError("checked native dependency resolver is unavailable")
                row, selection = bindings._selection(ref["id"], entry_id)
                mappings = [item for item in row["mappings"] if item.get("entry_id") == entry_id
                    and item.get("location") == location and item.get("parameter") == parameter
                    and item.get("field") == ref["field"] and tuple(item.get("parameter_path", [])) == keys]
                if len(mappings) != 1:
                    raise ValueError("resource reference has no unique checked original parameter mapping")
                report.update(definition_digest=row["definition_digest"], mapping=copy.deepcopy(mappings[0]),
                    original_mapping_proofs=copy.deepcopy(row.get("entry_checks", {}).get(entry_id, {}).get("mapping_proofs", [])))
                if row["kind"] != "weight" or ref["field"] != "path":
                    raise ValueError("service and credential values require their own original effect summary; no secret is resolved")
                resource = selection["resource"]
                if resource.get("kind") == "directory" and not resource.get("required_files"):
                    # A missing declaration is the same frozen gap whether the
                    # directory is present, absent, or has an undeclared index.
                    report["gap_code"] = "ASSET_REQUIREMENTS_UNDECLARED"
                    raise ValueError("directory asset requirements are undeclared")
                previous_gaps = bindings.environment_gaps
                bindings.environment_gaps = []
                try:
                    result = bindings.value(value, entry_id, location, parameter, runtime=False,
                                            parameter_path=keys, observation=observed)
                finally:
                    bindings.environment_gaps = previous_gaps
                if type(result) is not str:
                    raise ValueError("original weight path is not a plain string")
                report.update(status="CHECKED", value_type="string", value_digest=_digest(result),
                    resource_id=resource.get("id"), resource_configuration_digest=_digest({k: resource.get(k)
                        for k in ("kind", "path", "model_revision", "required_files", "sha256", "bytes", "execution_environment")}),
                    asset_requirements=dict(scope="DECLARED_FILES_ONLY", completeness="UNKNOWN",
                        kind=resource.get("kind"), required_files=copy.deepcopy(resource.get("required_files", [])),
                        sha256=resource.get("sha256"), bytes=resource.get("bytes")))
                return result
            except (ValueError, KeyError, TypeError, OSError):
                # Values/errors from a service or resource record are not echoed.
                # Source/parameter evidence remains useful even when unavailable.
                report["reason"] = ("service and credential values require their own original effect summary; no secret is resolved"
                    if bindings is not None and (bindings.rows.get(ref["id"], {}).get("kind") == "service")
                    else "original weight value or its checked mapping/selection/environment is unavailable")
                return None
            finally:
                if resource is not None and observed:
                    observations.append(dict(parameter=parameter, parameter_path=list(keys), dependency=copy.deepcopy(ref),
                        assets=_assets(resource, observed), asset_check=dict(status=observed.get("status"),
                            necessary_files=observed.get("necessary_files"),
                            missing_count=len(observed.get("missing_files", [])),
                            mismatch_count=len(observed.get("mismatches", [])),
                            unreadable_count=len(observed.get("unreadable_files", [])))))
        if type(value) is dict:
            return {key: resolve(child, parameter, keys + (key,)) for key, child in value.items()}
        if type(value) is list:
            return [resolve(child, parameter, keys + (index,)) for index, child in enumerate(value)]
        return value

    resolved = {parameter: resolve(value, parameter) for parameter, value in values.items()}
    proof = dict(schema="resource-value-evidence/1", rule=RULE, location=location,
        status="CHECKED" if all(item["status"] == "CHECKED" for item in reports) else "UNKNOWN",
        values=reports, effects=dict(status="UNKNOWN" if reports else "NOT_APPLICABLE",
            scope="value origins and listed assets only; no source/SDK or lifecycle permission"))
    proof["digest"] = _digest(proof)
    return resolved, proof, dict(schema="resource-value-observation/1", location=location, values=observations,
        basis="ACTUAL_HOST_RECHECK" if bindings is not None and bindings.host_check else "FROZEN_SELECTION_CHECK",
        scope="actual host check when configured, otherwise frozen selection observation; separate from contract requirements")
