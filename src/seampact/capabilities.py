"""Frozen, source-bound capability cards and low-cost event representations.

These describe contracts, never predicted utility. IDs and hidden evaluation
labels are not features. Version 1 covers one Unit or continue per action.
Version 2 adds checked ordered multi-Unit plans, business action types, results
already obtained in the task, and the runtime state visible at the event.
"""

import copy
import hashlib
import json
import math

VERSION = "capability-features/1"
CATEGORIES = {
    "roles": ("retrieve", "check", "revise", "detect"),
    "modalities": ("text", "image", "tool_text"),
    "inputs": ("question", "answer", "image", "corpus", "paper_evidence", "claim", "tool_result"),
    "outputs": ("passages", "risk", "verdict", "answer", "evidence"),
    "effects": ("add_evidence", "add_information", "change_answer", "filter_context"),
    "scopes": ("visual_qa", "paper_qa", "tool_use", "general_text"),
    "events": ("visual_answer_returned", "paper_answer_returned", "evidence_requested", "tool_result"),
    "granularity": ("whole", "claim", "call"),
}
NUMBERS = ("question_chars", "answer_chars", "evidence_count", "answer_version",
           "prior_calls", "remaining_budget", "risk", "query_token_coverage")
OBSERVATION_SCHEMA = ([f"event:{v}" for v in CATEGORIES["events"]] + ["event:unknown"] +
    [f"modality:{v}" for v in CATEGORIES["modalities"]] + ["modality:unknown"] +
    [f"scope:{v}" for v in CATEGORIES["scopes"]] + ["scope:unknown"] +
    [f"available:{v}" for v in CATEGORIES["inputs"]] + ["available:unknown"] +
    [name + suffix for name in NUMBERS for suffix in ("", ":missing")])
CANDIDATE_SCHEMA = ([f"{key}:{v}" for key, values in CATEGORIES.items() for v in (*values, "unknown")] +
    ["continue", "modality_match", "event_match", "scope_match", "input_coverage",
     "matching:missing", "latency_ms", "latency_ms:missing", "coverage_chars", "coverage_chars:missing"])

VERSION_2 = "capability-features/2"
ACTION_TYPES = ("invoke", "continue", "finish")
RUNTIME = ("local", "ready_instance", "warm", "cold_startable", "accepting", "busy")
RUNTIME_NUMBERS = ("queue", "state_age_ms")
OBSERVATION_SCHEMA_2 = (OBSERVATION_SCHEMA +
    [f"obtained:{v}" for v in CATEGORIES["outputs"]] + ["obtained:unknown"])
CANDIDATE_SCHEMA_2 = ([f"{key}:{v}" for key, values in CATEGORIES.items() for v in (*values, "unknown")] +
    [f"action:{v}" for v in ACTION_TYPES] + ["plan_units"] +
    ["modality_match", "event_match", "scope_match", "input_coverage", "matching:missing",
     "latency_ms", "latency_ms:missing", "coverage_chars", "coverage_chars:missing"] +
    [f"runtime:{v}" for v in RUNTIME] + [f"runtime:{v}" for v in RUNTIME_NUMBERS] +
    ["runtime:missing", "runtime:not_applicable"])
SCALES_2 = set(NUMBERS) | {"latency_ms", "coverage_chars", "plan_units", *RUNTIME_NUMBERS}

# joint/1: one shared scorer over business, dispatch and resource events. A
# business event keeps its capability-features/2 vectors; workload facts are only
# what has arrived or is observable now, never future arrivals.
JOINT_VERSION = "joint-features/2"
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
    ["resource:not_applicable"])


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
    if config.get("version") not in {VERSION, VERSION_2}:
        raise ValueError("unsupported capability representation version")
    second = config["version"] == VERSION_2
    scales = config.get("scales")
    if not isinstance(scales, dict) or set(scales) != (SCALES_2 if second else set(NUMBERS) | {"latency_ms", "coverage_chars"}):
        raise ValueError("declare all common representation scales explicitly")
    if any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0 for v in scales.values()):
        raise ValueError("representation scales must be positive and finite")
    config["cards"] = {uid: capability_card(cards.get(uid)) for uid in scene.get("unit_ids", [])}
    config["digest"] = content_digest({k: v for k, v in config.items() if k != "digest"})
    observation = OBSERVATION_SCHEMA_2 if second else OBSERVATION_SCHEMA
    scene["observation_schema"] = list(observation)
    scene["candidate_schema"] = list(CANDIDATE_SCHEMA_2 if second else CANDIDATE_SCHEMA)
    scene["observation_scales"] = [1.0] * len(observation)


def represent(event, config, runtime_state=None):
    """Use only adapter-declared facts available at this actual event."""
    if config["version"] == VERSION_2:
        return _represent_2(event, config, runtime_state)
    facts = event.get("scene_observation")
    if not isinstance(facts, dict) or set(facts) - {"modalities", "scopes", "available_inputs", *NUMBERS}:
        raise ValueError("common representation requires explicit current scene_observation facts")
    def labels(key, allowed):
        values = facts.get(key, ["unknown"])
        if not isinstance(values, list) or any(v not in (*allowed, "unknown") for v in values):
            raise ValueError("invalid visible observation: " + key)
        return set(values or ["unknown"])
    mods = labels("modalities", CATEGORIES["modalities"])
    scopes = labels("scopes", CATEGORIES["scopes"])
    available = labels("available_inputs", CATEGORIES["inputs"])
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
    event["observation"] = observation
    for candidate in event["candidates"]:
        units = candidate.get("units", [])
        if len(units) > 1:
            raise ValueError("capability-features/1 covers a single Unit or continue, not unordered plans")
        card = config["cards"][units[0]] if units else None
        features = [int(bool(card) and v in card[key])
                    for key, values in CATEGORIES.items() for v in (*values, "unknown")]
        known = bool(card) and all("unknown" not in card[k] for k in ("modalities", "events", "scopes", "inputs"))
        known = known and all("unknown" not in v for v in (mods, scopes, available))
        inputs = set(card["inputs"]) if card else set()
        features.extend([int(not units), int(bool(card) and bool(mods.intersection(card["modalities"]))),
            int(bool(card) and kind in card["events"]), int(bool(card) and bool(scopes.intersection(card["scopes"]))),
            len(inputs & available) / len(inputs) if inputs else 0.0, int(bool(units) and not known)])
        latency = card["cost_profile"]["latency_ms"] if card else 0.0
        coverage = candidate.get("coverage_text")
        if coverage is not None and not isinstance(coverage, str):
            raise ValueError("candidate coverage_text must be current visible text")
        features.extend([0.0 if latency is None else latency / config["scales"]["latency_ms"], int(latency is None),
            0.0 if coverage is None else len(coverage) / config["scales"]["coverage_chars"], int(coverage is None)])
        candidate["features"] = features
    event["representation_digest"] = config["digest"]


def _labels(facts, key, allowed):
    values = facts.get(key, ["unknown"])
    if not isinstance(values, list) or any(v not in (*allowed, "unknown") for v in values):
        raise ValueError("invalid visible observation: " + key)
    return set(values or ["unknown"])


def _represent_2(event, config, runtime_state):
    facts = event.get("scene_observation")
    if not isinstance(facts, dict) or set(facts) - {"modalities", "scopes", "available_inputs", "obtained_outputs", *NUMBERS}:
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


def freeze_joint(scales):
    if not isinstance(scales, dict) or set(scales) != JOINT_SCALES:
        raise ValueError("joint representation scales must declare exactly: " + ", ".join(sorted(JOINT_SCALES)))
    if any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0 for v in scales.values()):
        raise ValueError("joint representation scales must be positive and finite")
    value = dict(version=JOINT_VERSION, base=VERSION_2, scales=dict(sorted(scales.items())),
                 observation_schema=list(JOINT_OBSERVATION_SCHEMA), candidate_schema=list(JOINT_CANDIDATE_SCHEMA))
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
        raise ValueError("business observation must be a capability-features/2 vector")
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


def joint_candidate(action, scales, business=None, cards=None, dispatch=None, resource=None):
    if action not in JOINT_ACTIONS:
        raise ValueError("unknown joint action label")
    values = {"action:" + action: 1}
    if business is None:
        values["business:not_applicable"] = 1
    elif len(business) != len(CANDIDATE_SCHEMA_2):
        raise ValueError("business candidate must be a capability-features/2 vector")
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
    return _joint_vector(JOINT_CANDIDATE_SCHEMA, values)
