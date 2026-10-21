"""Frozen, source-bound capability cards and low-cost event representations.

These describe contracts, never predicted utility. IDs and hidden evaluation
labels are not features. Version 1 covers one Unit or continue per action;
ordered multi-Unit plans still use an explicitly configured scene schema.
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
    if config.get("version") != VERSION:
        raise ValueError("unsupported capability representation version")
    scales = config.get("scales")
    if not isinstance(scales, dict) or set(scales) != set(NUMBERS) | {"latency_ms", "coverage_chars"}:
        raise ValueError("declare all common representation scales explicitly")
    if any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0 for v in scales.values()):
        raise ValueError("representation scales must be positive and finite")
    config["cards"] = {uid: capability_card(cards.get(uid)) for uid in scene.get("unit_ids", [])}
    config["digest"] = content_digest({k: v for k, v in config.items() if k != "digest"})
    scene["observation_schema"] = list(OBSERVATION_SCHEMA)
    scene["candidate_schema"] = list(CANDIDATE_SCHEMA)
    scene["observation_scales"] = [1.0] * len(OBSERVATION_SCHEMA)


def represent(event, config):
    """Use only adapter-declared facts available at this actual event."""
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
