"""Measured cost scopes with explicit gaps; nested timings are never added."""

import copy
import math


def _valid(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def validate_costs(config):
    value = copy.deepcopy(config or {})
    prices = value.setdefault("prices", {})
    if not isinstance(prices, dict):
        raise ValueError("costs.prices must be an object")
    for role, price in prices.items():
        if (not isinstance(price, dict) or not price.get("currency") or not price.get("provenance")
                or not all(_valid(price.get(k)) for k in ("prompt_per_million", "completion_per_million"))):
            raise ValueError("cost price requires currency, provenance and explicit token rates: " + role)
        if "prompt_cached_per_million" in price and not _valid(price["prompt_cached_per_million"]):
            raise ValueError("cached prompt price must be an explicit nonnegative rate")
    return value


def usage_record(role, usage, requests=1, provenance="provider_response", prices=None):
    usage = usage or {}
    tokens = {key: usage.get(key) if type(usage.get(key)) is int and usage[key] >= 0 else None
              for key in ("prompt_tokens", "completion_tokens", "total_tokens")}
    price = (prices or {}).get(role)
    amount = None
    cached = usage.get("prompt_cache_hit_tokens", (usage.get("prompt_tokens_details") or {}).get("cached_tokens"))
    cached = cached if type(cached) is int and cached >= 0 else None
    if price and tokens["prompt_tokens"] is not None and tokens["completion_tokens"] is not None:
        if "prompt_cached_per_million" not in price:
            amount = (tokens["prompt_tokens"] * price["prompt_per_million"] +
                      tokens["completion_tokens"] * price["completion_per_million"]) / 1_000_000
        elif cached is not None and cached <= tokens["prompt_tokens"]:
            amount = ((tokens["prompt_tokens"] - cached) * price["prompt_per_million"] +
                      cached * price["prompt_cached_per_million"] +
                      tokens["completion_tokens"] * price["completion_per_million"]) / 1_000_000
    return dict(role=role, requests=requests, **tokens, provenance=provenance,
                cached_prompt_tokens=cached, amount_kind="token_rate_estimate",
                amount=amount, currency=price["currency"] if price else None,
                price_provenance=price["provenance"] if price else None,
                money_status="KNOWN" if amount is not None else "UNKNOWN")


def summarize_usage(records):
    totals = {}
    for key in ("requests", "prompt_tokens", "completion_tokens", "total_tokens"):
        known = [r[key] for r in records if r.get(key) is not None]
        totals[key] = dict(observed=sum(known), known_records=len(known), records=len(records),
                           total=sum(known) if len(known) == len(records) else None)
    currencies = {r.get("currency") for r in records if r.get("currency")}
    money_known = all(r.get("amount") is not None for r in records) and len(currencies) <= 1
    totals["money"] = dict(total=sum(r["amount"] for r in records) if money_known else None,
                           currency=next(iter(currencies), None),
                           known_records=sum(r.get("amount") is not None for r in records), records=len(records))
    return totals


def runtime_costs(calls, events=None):
    """Instance-level facts of the calls this task actually made."""
    remote = [call for call in calls if call.get("execution") == "REMOTE"]
    def measured(key):
        values = [call[key] for call in remote if _valid(call.get(key))]
        return dict(observed=sum(values), known_calls=len(values), calls=len(remote),
                    total=sum(values) if len(values) == len(remote) else None)
    return dict(remote_calls=len(remote), local_calls=len(calls) - len(remote),
                start_wait=measured("start_wait_ms"), cold_instance_calls=sum(bool(call.get("cold_instance")) for call in remote),
                round_trip=measured("round_trip_ms"), worker=measured("worker_ms"), transport=measured("transport_ms"),
                instances=sorted({call["instance_id"] for call in remote if call.get("instance_id")}),
                instance_events=copy.deepcopy(events),
                semantics="start_wait/round_trip are nested in unit_wall_ms; start waits are charged to the task that waited; "
                          "cross-task residency and idle time are reported only as the run-window total")


def episode_costs(context, manifest, online_ms, transitions, feedback, independent, runtime_events=None):
    prices = manifest.get("costs", {}).get("prices", {})
    online = [usage_record(call["role"], call.get("usage"), prices=prices) for call in context.model_calls]
    for call in context.calls:
        usage = call.get("usage")
        record = usage_record("native:" + call["unit_id"], usage,
            requests=call.get("model_requests"), provenance=call.get("usage_provenance", "native_usage_unavailable"),
            prices=prices)
        record["missing"] = copy.deepcopy(call.get("usage_missing", {}))
        online.append(record)
    evaluations = []
    for scope, result, spec in (("training_feedback", feedback, manifest["criteria"]),
                                ("independent_evaluation", independent, manifest.get("evaluation_criteria"))):
        if result and result["usage"]["request_count"]:
            role = spec["teacher"]["role"]
            record = usage_record(role, result["usage"], result["usage"]["request_count"], prices=prices)
            record.update(scope=scope, duration_ms=result["cost_duration"] * 1000)
            evaluations.append(record)
    bindings = [c["binding_duration_ms"] for c in context.calls if c.get("binding_duration_ms") is not None]
    return dict(schema="cost-ledger/1", online=dict(wall_ms=online_ms,
        unit_wall_ms=sum(c["duration_ms"] for c in context.calls),
        unit_binding_ms=sum(bindings) if len(bindings) == len(context.calls) else None,
        observed_unit_binding_ms=sum(bindings), binding_timing_coverage=len(bindings),
        cold_calls=sum(c.get("cold_start", False) for c in context.calls),
        model_wall_ms=sum(c["duration_ms"] for c in context.model_calls),
        representation_ms=sum(t["event"].get("representation_duration_ms", 0) for t in transitions),
        policy_ms=sum(t["decision"].get("duration_ms", 0) for t in transitions),
        runtime_groups=runtime_costs(context.calls, runtime_events),
        records=online, usage=summarize_usage(online)),
        evaluation=dict(wall_ms=sum((r or {}).get("cost_duration", 0) * 1000 for r in (feedback, independent)),
                        records=evaluations, usage=summarize_usage(evaluations)),
        upstream=copy.deepcopy(manifest.get("upstream_costs", {})),
        task_upstream=copy.deepcopy(getattr(context, "upstream_costs", {})),
        gpu_time=dict(status="UNKNOWN", reason="wall time is not GPU kernel time"),
        timing_semantics="online.wall_ms includes nested Unit/model/representation/policy timings; do not add them")
