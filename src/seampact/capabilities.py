"""Frozen, source-bound capability cards and low-cost event representations.

These describe contracts, never predicted utility. IDs and hidden evaluation
labels are not features. The current version includes ordered plans and only
observations actually returned at the current event.
"""

import copy
import hashlib
import json
import math

CATEGORIES = {
    "roles": ("retrieve", "check", "revise", "detect"),
    "modalities": ("text", "image", "tool_text"),
    "inputs": ("question", "answer", "image", "corpus", "paper_evidence", "claim", "tool_result"),
    "outputs": ("passages", "risk", "verdict", "answer", "evidence"),
    "effects": ("add_evidence", "add_information", "change_answer", "filter_context"),
    "scopes": ("visual_qa", "paper_qa", "tool_use", "general_text"),
    "events": ("visual_answer_returned", "paper_answer_returned", "evidence_requested", "tool_result",
               "untrusted_material_received", "business_draft_returned", "before_publication"),
    "granularity": ("whole", "claim", "call"),
}
NUMBERS = ("question_chars", "answer_chars", "material_chars", "image_count", "image_pixels", "evidence_count", "answer_version", "material_version",
           "prior_calls", "remaining_budget", "risk", "query_token_coverage")
OBSERVATION_SCHEMA = ([f"event:{v}" for v in CATEGORIES["events"]] + ["event:unknown"] +
    [f"modality:{v}" for v in CATEGORIES["modalities"]] + ["modality:unknown"] +
    [f"scope:{v}" for v in CATEGORIES["scopes"]] + ["scope:unknown"] +
    [f"available:{v}" for v in CATEGORIES["inputs"]] + ["available:unknown"] +
    [name + suffix for name in NUMBERS for suffix in ("", ":missing")])
CANDIDATE_SCHEMA = ([f"{key}:{v}" for key, values in CATEGORIES.items() for v in (*values, "unknown")] +
    ["continue", "modality_match", "event_match", "scope_match", "input_coverage",
     "matching:missing", "latency_ms", "latency_ms:missing", "coverage_chars", "coverage_chars:missing"])

VERSION_2 = "capability-features/3"
ACTION_TYPES = ("invoke", "continue", "finish")
ASSESSMENTS = {"text": ("supported", "contradicted", "uncertain"),
               "image": ("authentic", "tampered", "uncertain")}
OBSERVATION_STATUSES = ("available", "missing", "not_applicable", "failed")
RESULT_KINDS = ("nli", "image_score", "prompt_injection", "privacy", "minicheck_binary", "selfcheck_score")
OBSERVATION_RECORD_SCHEMA = "visible-observations/1"
RUNTIME = ("local", "ready_instance", "warm", "cold_startable", "accepting", "busy")
RUNTIME_NUMBERS = ("queue", "state_age_ms")
OBSERVATION_SCHEMA_2 = (OBSERVATION_SCHEMA +
    [f"obtained:{v}" for v in CATEGORIES["outputs"]] + ["obtained:unknown"] +
    [f"assessment:{modality}:{v}" for modality, values in ASSESSMENTS.items()
     for v in (*values, *OBSERVATION_STATUSES[1:], "stale")] +
    [f"result:{kind}:{status}" for kind in (*RESULT_KINDS, "other") for status in OBSERVATION_STATUSES] +
    ["result:nli:entailment", "result:nli:neutral", "result:nli:contradiction",
     "result:nli:input_answer_version", "result:image_score:value",
     "result:prompt_injection:benign", "result:prompt_injection:injection",
     "result:prompt_injection:unknown", "result:prompt_injection:score", "result:prompt_injection:score:missing",
     "result:privacy:entity_count", "result:privacy:anonymized_available",
     "result:minicheck_binary:supported", "result:minicheck_binary:unsupported", "result:selfcheck_score:value"] +
    [f"result:prompt_injection:any:{label}" for label in ("benign", "injection", "unknown")])
CANDIDATE_SCHEMA_2 = ([f"{key}:{v}" for key, values in CATEGORIES.items() for v in (*values, "unknown")] +
    [f"action:{v}" for v in ACTION_TYPES] + ["plan_units"] +
    ["modality_match", *[f"modality_match:{v}" for v in CATEGORIES["modalities"]],
     "event_match", "scope_match", "input_coverage", "matching:missing",
     "latency_ms", "latency_ms:missing", "coverage_chars", "coverage_chars:missing"] +
    ["result:source_match:available", "result:source_match:failed", "result:source_match:current_version",
     *[f"result:source_match:injection:{label}" for label in ("benign", "injection", "unknown")]] +
    [f"runtime:{v}" for v in RUNTIME] + [f"runtime:{v}" for v in RUNTIME_NUMBERS] +
    ["runtime:missing", "runtime:not_applicable"])
SCALES_2 = set(NUMBERS) | {"latency_ms", "coverage_chars", "plan_units", *RUNTIME_NUMBERS}

# joint/1: one shared scorer over business, dispatch and resource events. A
# business event keeps its capability-features/3 vectors; workload facts are only
# what has arrived or is observable now, never future arrivals.
JOINT_VERSION = "joint-features/4"
JOINT_DECISIONS = ("business", "dispatch", "resource")
JOINT_ACTIONS = ("invoke", "continue", "finish", "dispatch", "prepare", "scale_out", "scale_in",
                 "yield_until_next_event")
JOINT_SCALES = {"tasks", "calls", "instances", "wait_ms", "service_ms", "window_ms"}
WORKLOAD = (("tasks_active", "tasks"), ("tasks_at_decision", "tasks"), ("tasks_done", "tasks"),
            ("roots_waiting", "calls"), ("roots_dispatchable", "calls"), ("roots_active", "calls"),
            ("slots_free", "calls"), ("oldest_wait_ms", "wait_ms"), ("instances_held", "instances"),
            ("instances_ready", "instances"), ("instances_starting", "instances"),
            ("instances_draining", "instances"), ("instances_idle", "instances"),
            ("replicas_remaining", "instances"), ("resource_remaining", None), ("resource_ops_in_row", None),
            ("window_elapsed_ms", "window_ms"))
DISPATCH_NUMBERS = (("wait_ms", "wait_ms"), ("estimate_ms", "service_ms"), ("task_age_ms", "wait_ms"),
                    ("task_roots", "calls"))
DISPATCH_FLAGS = ("estimate_missing", "remote", "warm_target", "aged", "boundary")
RESOURCE_NUMBERS = (("held", "instances"), ("ready", "instances"), ("starting", "instances"),
                    ("draining", "instances"), ("cap_remaining", "instances"), ("waiting_roots", "calls"),
                    ("idle_ms", "wait_ms"), ("budget_after", None))
RESOURCE_FLAGS = ("cold", "warm", "replicable", "budget_after:missing")
DEPENDENCY_FEATURES = ("service_target", "shared_users", "dependency_waiters", "dependencies_ready",
                       "leases", "load_ms", "load_ms:missing", "load_samples", "load_uncertainty")
UNIT_CATEGORIES = [f"unit:{key}:{v}" for key, values in CATEGORIES.items() for v in (*values, "unknown")]
JOINT_OBSERVATION_SCHEMA = ([f"decision:{v}" for v in JOINT_DECISIONS] +
    ["business:" + name for name in OBSERVATION_SCHEMA_2] + ["business:not_applicable"] +
    ["workload:" + name for name, _ in WORKLOAD] + ["workload:resource_remaining:missing"])
JOINT_CANDIDATE_SCHEMA = ([f"action:{v}" for v in JOINT_ACTIONS] +
    ["business:" + name for name in CANDIDATE_SCHEMA_2] + ["business:not_applicable"] +
    UNIT_CATEGORIES + ["unit:latency_ms", "unit:latency_ms:missing", "unit:not_applicable"] +
    ["dispatch:" + name for name, _ in DISPATCH_NUMBERS] + ["dispatch:" + name for name in DISPATCH_FLAGS] +
    ["dispatch:not_applicable"] +
    ["resource:" + name for name, _ in RESOURCE_NUMBERS] + ["resource:" + name for name in RESOURCE_FLAGS] +
    ["resource:not_applicable"] + ["dependency:" + name for name in DEPENDENCY_FEATURES])


def content_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def capability_card(value=None):
    value = copy.deepcopy({} if value is None else value)
    if not isinstance(value, dict):
        raise ValueError("capability card must be an object")
    if value.get("schema", "capability-card/1") != "capability-card/1":
        raise ValueError("unsupported capability card schema")
    result = dict(schema="capability-card/1", goal=value.get("goal", "UNKNOWN"))
    if not isinstance(result["goal"], str):
        raise ValueError("capability goal must be text")
    for key, categories in CATEGORIES.items():
        items = value.get(key, ["unknown"])
        if not isinstance(items, list) or not items or any(v not in (*categories, "unknown") for v in items):
            raise ValueError("capability card: invalid " + key)
        result[key] = sorted(set(items))
    cost = value.get("cost_profile", {})
    if not isinstance(cost, dict):
        raise ValueError("capability cost_profile must be an object")
    latency = cost.get("latency_ms")
    if latency is not None and (type(latency) not in (int, float) or not math.isfinite(latency) or latency < 0):
        raise ValueError("capability latency must be measured nonnegative milliseconds or null")
    if latency is not None and not cost.get("provenance"):
        raise ValueError("capability cost profile requires development measurement provenance")
    result["cost_profile"] = dict(latency_ms=latency, provenance=cost.get("provenance"))
    return result


def freeze_representation(scene, cards):
    config = scene.get("representation")
    if config is None:
        return
    if config.get("version") != VERSION_2:
        raise ValueError("unsupported capability representation version")
    scales = config.get("scales")
    if not isinstance(scales, dict) or set(scales) != SCALES_2:
        raise ValueError("declare all common representation scales explicitly")
    if any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0 for v in scales.values()):
        raise ValueError("representation scales must be positive and finite")
    config["cards"] = {uid: capability_card(cards.get(uid)) for uid in scene.get("unit_ids", [])}
    config["observation_record_schema"] = OBSERVATION_RECORD_SCHEMA
    config["digest"] = content_digest({k: v for k, v in config.items() if k != "digest"})
    observation = OBSERVATION_SCHEMA_2
    scene["observation_schema"] = list(observation)
    scene["candidate_schema"] = list(CANDIDATE_SCHEMA_2)
    scene["observation_scales"] = [1.0] * len(observation)


def represent(event, config, runtime_state=None):
    """Use only adapter-declared facts available at this actual event."""
    if config["version"] == VERSION_2:
        return _represent_2(event, config, runtime_state)
    raise ValueError("unsupported capability representation version")

def _labels(facts, key, allowed):
    values = facts.get(key, ["unknown"])
    if not isinstance(values, list) or any(v not in (*allowed, "unknown") for v in values):
        raise ValueError("invalid visible observation: " + key)
    return set(values or ["unknown"])


def _represent_2(event, config, runtime_state):
    facts = event.get("scene_observation")
    if not isinstance(facts, dict) or set(facts) - {"modalities", "scopes", "available_inputs", "obtained_outputs",
                                                   "result_status", "observation_records", *NUMBERS}:
        raise ValueError("common representation requires explicit current scene_observation facts")
    mods = _labels(facts, "modalities", CATEGORIES["modalities"])
    scopes = _labels(facts, "scopes", CATEGORIES["scopes"])
    available = _labels(facts, "available_inputs", CATEGORIES["inputs"])
    obtained = _labels(facts, "obtained_outputs", CATEGORIES["outputs"])
    kind = event.get("event", "unknown")
    kind = kind if kind in CATEGORIES["events"] else "unknown"
    observation = [int(kind == v) for v in (*CATEGORIES["events"], "unknown")]
    for items, allowed in ((mods, CATEGORIES["modalities"]), (scopes, CATEGORIES["scopes"]),
                           (available, CATEGORIES["inputs"])):
        observation.extend(int(v in items) for v in (*allowed, "unknown"))
    for name in NUMBERS:
        value = facts.get(name)
        if value is not None and (type(value) not in (int, float) or not math.isfinite(value)):
            raise ValueError("invalid visible numeric observation: " + name)
        observation.extend([0.0 if value is None else value / config["scales"][name], int(value is None)])
    observation.extend(int(v in obtained) for v in (*CATEGORIES["outputs"], "unknown"))
    records = facts.get("observation_records", [])
    statuses = facts.get("result_status", {})
    if not isinstance(records, list) or not isinstance(statuses, dict) or set(statuses) - set(RESULT_KINDS):
        raise ValueError("invalid visible observation records")
    latest = {}
    for record in records:
        if not isinstance(record, dict) or set(record) != {"kind", "status", "source", "event", "input_version", "cost_refs", "value"}:
            raise ValueError("visible observation requires source, event, version and cost references")
        kind, status = record["kind"], record["status"]
        if not isinstance(kind, str) or not kind or status not in ("available", "failed"):
            raise ValueError("invalid returned observation kind or status")
        if (not isinstance(record["source"], str) or not record["source"] or
            not isinstance(record["event"], str) or not record["event"] or
            not isinstance(record["input_version"], str) or not record["input_version"] or
            not isinstance(record["cost_refs"], list) or not record["cost_refs"] or
            any(not isinstance(ref, str) or not ref for ref in record["cost_refs"])):
            raise ValueError("visible observation provenance is incomplete")
        latest[kind] = record
    business = latest.get("business_assessment")
    assessments = (business["value"] if business and business["status"] == "available" else {})
    if not isinstance(assessments, dict) or set(assessments) - set(ASSESSMENTS):
        raise ValueError("invalid modality assessments")
    for modality, allowed in ASSESSMENTS.items():
        value = assessments.get(modality, "failed" if business and business["status"] == "failed" else "missing")
        if business and business["status"] == "available" and business["input_version"] != "answer:" + str(facts.get("answer_version")):
            value = "stale"
        if value not in (*allowed, *OBSERVATION_STATUSES[1:], "stale"):
            raise ValueError("invalid modality assessment")
        observation.extend(int(value == v) for v in (*allowed, *OBSERVATION_STATUSES[1:], "stale"))
    for kind in RESULT_KINDS:
        status = latest[kind]["status"] if kind in latest else statuses.get(kind, "missing")
        if status not in OBSERVATION_STATUSES or (status in ("available", "failed") and kind not in latest):
            raise ValueError("visible result status requires an actual returned record")
        observation.extend(int(status == v) for v in OBSERVATION_STATUSES)
    other = [record for record in records if record["kind"] not in (*RESULT_KINDS, "business_assessment")]
    other_status = other[-1]["status"] if other else "missing"
    observation.extend(int(other_status == v) for v in OBSERVATION_STATUSES)
    nli = latest.get("nli")
    counts, input_version = [0, 0, 0], 0
    if nli and nli["status"] == "available":
        value = nli["value"]
        if not isinstance(value, dict) or set(value) != {"labels", "answer_version"} or not isinstance(value["labels"], list) or any(v not in {"Entailment", "Neutral", "Contradiction"} for v in value["labels"]) or type(value["answer_version"]) is not int or value["answer_version"] < 0:
            raise ValueError("invalid original NLI observation")
        counts = [value["labels"].count(label) / config["scales"]["evidence_count"] for label in ("Entailment", "Neutral", "Contradiction")]
        input_version = value["answer_version"] / config["scales"]["answer_version"]
    image = latest.get("image_score")
    score = 0
    if image and image["status"] == "available":
        score = image["value"]
        if type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= 1:
            raise ValueError("invalid original image score")
    observation.extend([*counts, input_version, score])
    injection = latest.get("prompt_injection")
    label, injection_score = None, None
    if injection and injection["status"] == "available":
        value = injection["value"]
        if (not isinstance(value, dict) or not {"raw_label", "label", "score"} <= set(value) or
                set(value) - {"raw_label", "label", "score", "raw"} or
                value["label"] not in ("benign", "injection", "unknown")):
            raise ValueError("invalid original injection observation")
        label, injection_score = value["label"], value["score"]
        if injection_score is not None and (type(injection_score) not in (int, float) or not math.isfinite(injection_score)):
            raise ValueError("invalid original injection score")
    observation.extend([*(int(label == v) for v in ("benign", "injection", "unknown")),
                        0 if injection_score is None else injection_score, int(injection_score is None)])
    privacy = latest.get("privacy")
    entity_count, anonymized = 0, False
    if privacy and privacy["status"] == "available":
        value = privacy["value"]
        if (not isinstance(value, dict) or not {"entity_count", "anonymized_available"} <= set(value) or
                set(value) - {"entity_count", "anonymized_available", "raw"} or
                type(value["entity_count"]) is not int or value["entity_count"] < 0 or
                type(value["anonymized_available"]) is not bool):
            raise ValueError("invalid original privacy observation")
        entity_count, anonymized = value["entity_count"], value["anonymized_available"]
    observation.extend([entity_count / config["scales"]["evidence_count"], int(anonymized)])
    mini, selfcheck = latest.get("minicheck_binary"), latest.get("selfcheck_score")
    mini_value, self_value = None, 0
    if mini and mini["status"] == "available":
        mini_value = mini["value"]
        if mini_value not in ("supported", "unsupported"):
            raise ValueError("MiniCheck observation must retain its binary meaning")
    if selfcheck and selfcheck["status"] == "available":
        self_value = selfcheck["value"]
        if type(self_value) not in (int, float) or not math.isfinite(self_value):
            raise ValueError("invalid original SelfCheck score")
    observation.extend([int(mini_value == "supported"), int(mini_value == "unsupported"), self_value])
    injection_labels = {r["value"]["label"] for r in records
                        if r["kind"] == "prompt_injection" and r["status"] == "available"
                        and isinstance(r["value"], dict) and r["value"].get("label") in ("benign", "injection", "unknown")}
    observation.extend(int(label in injection_labels) for label in ("benign", "injection", "unknown"))
    event["observation"] = observation
    scales = config["scales"]
    for candidate in event["candidates"]:
        units = candidate.get("units", [])
        if not isinstance(units, list) or len(set(units)) != len(units):
            raise ValueError("an ordered plan names each Unit once")
        action = candidate.get("action_type", "invoke" if units else "continue")
        if action not in ACTION_TYPES or (action == "invoke") != bool(units):
            raise ValueError("action_type is invoke for a Unit plan, or continue/finish without Units")
        candidate["action_type"] = action
        cards = [config["cards"][uid] for uid in units]
        features = [int(any(v in card[key] for card in cards))
                    for key, values in CATEGORIES.items() for v in (*values, "unknown")]
        features.extend(int(action == v) for v in ACTION_TYPES)
        features.append(len(units) / scales["plan_units"])
        known = bool(cards) and all("unknown" not in card[k] for card in cards
                                    for k in ("modalities", "events", "scopes", "inputs"))
        known = known and all("unknown" not in v for v in (mods, scopes, available))
        inputs = set().union(*(card["inputs"] for card in cards)) if cards else set()
        features.extend([int(any(mods.intersection(card["modalities"]) for card in cards)),
            *[int(v in mods and any(v in card["modalities"] for card in cards)) for v in CATEGORIES["modalities"]],
            int(any(kind in card["events"] for card in cards)),
            int(any(scopes.intersection(card["scopes"]) for card in cards)),
            len(inputs & available) / len(inputs) if inputs else 0.0, int(bool(units) and not known)])
        latencies = [card["cost_profile"]["latency_ms"] for card in cards]
        latency = None if any(v is None for v in latencies) else sum(latencies)
        coverage = candidate.get("coverage_text")
        if coverage is not None and not isinstance(coverage, str):
            raise ValueError("candidate coverage_text must be current visible text")
        features.extend([0.0 if latency is None else latency / scales["latency_ms"], int(latency is None),
            0.0 if coverage is None else len(coverage) / scales["coverage_chars"], int(coverage is None)])
        matched = [record for record in records if record["source"] in units]
        current_version = candidate.get("input_version", "answer:" + str(facts.get("answer_version")))
        if not isinstance(current_version, str) or not current_version:
            raise ValueError("candidate input_version must be nonempty text")
        features.extend([int(any(record["status"] == "available" for record in matched)),
            int(any(record["status"] == "failed" for record in matched)),
            int(any(record["input_version"] == current_version for record in matched)),
            *[int(any(record["kind"] == "prompt_injection" and record["status"] == "available"
                and isinstance(record["value"], dict) and record["value"].get("label") == label
                for record in matched)) for label in ("benign", "injection", "unknown")]])
        state = runtime_state(units) if units and runtime_state else None
        if not units:
            features.extend([0] * len(RUNTIME) + [0.0] * len(RUNTIME_NUMBERS) + [0, 1])
        elif state is None:
            features.extend([0] * len(RUNTIME) + [0.0] * len(RUNTIME_NUMBERS) + [1, 0])
        else:
            features.extend(int(bool(state.get(name))) for name in RUNTIME)
            features.extend(0.0 if state.get(name) is None else state[name] / scales[name] for name in RUNTIME_NUMBERS)
            features.extend([int(any(state.get(name) is None for name in (*RUNTIME, *RUNTIME_NUMBERS))), 0])
        candidate["runtime_state"] = state
        candidate["features"] = features
    event["representation_digest"] = config["digest"]


def freeze_joint(scales, resources=None):
    if not isinstance(scales, dict) or set(scales) != JOINT_SCALES:
        raise ValueError("joint representation scales must declare exactly: " + ", ".join(sorted(JOINT_SCALES)))
    if any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0 for v in scales.values()):
        raise ValueError("joint representation scales must be positive and finite")
    names = sorted(resources or {})
    value = dict(version=JOINT_VERSION, base=VERSION_2, scales=dict(sorted(scales.items())), resources=names,
                 observation_schema=list(JOINT_OBSERVATION_SCHEMA),
                 candidate_schema=list(JOINT_CANDIDATE_SCHEMA) + ["marginal:" + name for name in names])
    value["digest"] = content_digest(value)
    return value


def _joint_vector(schema, values):
    unknown = set(values) - set(schema)
    if unknown:
        raise ValueError("joint features outside the frozen schema: " + ", ".join(sorted(unknown)))
    vector = [float(values.get(name, 0.0)) for name in schema]
    if any(not math.isfinite(v) for v in vector):
        raise ValueError("joint features must be finite")
    return vector


def joint_observation(kind, scales, workload, business=None):
    if kind not in JOINT_DECISIONS:
        raise ValueError("unknown joint decision kind")
    values = {"decision:" + kind: 1}
    if business is None:
        values["business:not_applicable"] = 1
    elif len(business) != len(OBSERVATION_SCHEMA_2):
        raise ValueError("business observation must be a capability-features/3 vector")
    else:
        values.update(("business:" + name, value) for name, value in zip(OBSERVATION_SCHEMA_2, business))
    for name, scale in WORKLOAD:
        value = workload.get(name)
        if value is None:
            if name != "resource_remaining":
                raise ValueError("joint workload fact is unavailable: " + name)
            values["workload:resource_remaining:missing"] = 1
            continue
        values["workload:" + name] = value / scales[scale] if scale else value
    return _joint_vector(JOINT_OBSERVATION_SCHEMA, values)


def joint_candidate(action, scales, business=None, cards=None, dispatch=None, resource=None, resource_schema=None):
    if action not in JOINT_ACTIONS:
        raise ValueError("unknown joint action label")
    values = {"action:" + action: 1}
    if business is None:
        values["business:not_applicable"] = 1
    elif len(business) != len(CANDIDATE_SCHEMA_2):
        raise ValueError("business candidate must be a capability-features/3 vector")
    else:
        values.update(("business:" + name, value) for name, value in zip(CANDIDATE_SCHEMA_2, business))
    if not cards:
        values["unit:not_applicable"] = 1
    else:
        for key, categories in CATEGORIES.items():
            for v in (*categories, "unknown"):
                if any(v in card[key] for card in cards):
                    values[f"unit:{key}:{v}"] = 1
        latencies = [card["cost_profile"]["latency_ms"] for card in cards]
        if any(v is None for v in latencies):
            values["unit:latency_ms:missing"] = 1
        else:
            values["unit:latency_ms"] = sum(latencies) / scales["service_ms"]
    if dispatch is None:
        values["dispatch:not_applicable"] = 1
    else:
        for name, scale in DISPATCH_NUMBERS:
            value = dispatch.get(name)
            if value is None and name != "estimate_ms":
                raise ValueError("dispatch fact is unavailable: " + name)
            values["dispatch:" + name] = 0.0 if value is None else value / scales[scale]
        values["dispatch:estimate_missing"] = int(dispatch.get("estimate_ms") is None)
        for name in ("remote", "warm_target", "aged", "boundary"):
            values["dispatch:" + name] = int(bool(dispatch.get(name)))
    if resource is None:
        values["resource:not_applicable"] = 1
    else:
        for name, scale in RESOURCE_NUMBERS:
            value = resource.get(name)
            if value is None:
                if name not in {"idle_ms", "budget_after"}:
                    raise ValueError("resource fact is unavailable: " + name)
                continue
            values["resource:" + name] = value / scales[scale] if scale else value
        values["resource:budget_after:missing"] = int(resource.get("budget_after") is None)
        for name in ("cold", "warm", "replicable"):
            values["resource:" + name] = int(bool(resource.get(name)))
    facts = resource or dispatch or {}
    for name in DEPENDENCY_FEATURES:
        value = facts.get(name)
        if name == "load_ms:missing":
            value = int(facts.get("load_ms") is None)
        if value is not None:
            scale = scales["service_ms"] if name == "load_ms" else scales["tasks"] if name in {
                "shared_users", "dependency_waiters", "leases"} else 1
            values["dependency:" + name] = value / scale
    for name in resource_schema or []:
        value = (facts.get("marginal_resources") or {}).get(name)
        # An absent business resource action has no additional allocation.
        if value is None and resource is not None:
            raise ValueError("candidate marginal resource demand is unavailable: " + name)
        values["marginal:" + name] = value or 0.0
    return _joint_vector(JOINT_CANDIDATE_SCHEMA + ["marginal:" + name for name in resource_schema or []], values)
