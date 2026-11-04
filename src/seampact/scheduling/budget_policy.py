"""Window budgets for the fixed-work, legal-candidate joint scheduler."""

import math
import random


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def validate(spec, algorithm, mode, episodes):
    if spec is None:
        return None
    keys = {"latency_budgets", "task_seconds_scale", "instance_seconds_scale",
            "max_unfinished_fraction", "dual_step"}
    if mode == "freeze":
        keys.add("freeze_budget")
    if not isinstance(spec, dict) or set(spec) != keys or algorithm != "ppo" or mode not in {"train", "freeze"}:
        raise ValueError("budget_policy requires joint PPO train/freeze and its declared budget fields")
    for name in ("task_seconds_scale", "instance_seconds_scale", "dual_step"):
        if not _number(spec[name]) or spec[name] <= 0:
            raise ValueError(name + " must be finite and positive")
    grid = spec["latency_budgets"]
    if (not isinstance(grid, list) or not grid or any(not _number(b) or b <= 0 for b in grid)
            or len(set(grid)) != len(grid)):
        raise ValueError("latency_budgets must contain distinct positive normalized window budgets")
    if not _number(spec["max_unfinished_fraction"]) or not 0 <= spec["max_unfinished_fraction"] <= 1:
        raise ValueError("max_unfinished_fraction must lie in [0,1]")
    if mode == "freeze":
        if not _number(spec["freeze_budget"]) or spec["freeze_budget"] not in grid:
            raise ValueError("freeze_budget must name a trained grid point")
    elif episodes < len(grid):
        raise ValueError("training needs at least one window for each budget")
    return dict(spec, latency_budgets=list(grid))


def training_semantics(spec):
    return {key: value for key, value in (spec or {}).items() if key != "freeze_budget"}


def budget_index(spec, seed, number):
    grid = spec["latency_budgets"]
    if "freeze_budget" in spec:
        return grid.index(spec["freeze_budget"])
    block, offset = divmod(number, len(grid))
    order = list(range(len(grid)))
    random.Random(f"{seed}:{block}:window-budgets").shuffle(order)
    return order[offset]


def settle(item, spec, index, dual, training):
    """Costs before the first choice affect feasibility but are action-independent."""
    budget = spec["latency_budgets"][index]
    limits = [budget, spec["max_unfinished_fraction"]]
    service = item["service"]
    arrived = service["arrived"]
    missed = arrived - service["completed"]
    ledger = item["parallelism"]
    totals = [ledger["instance_seconds"], ledger["charged_task_seconds"], missed]
    scales = [spec["instance_seconds_scale"], spec["task_seconds_scale"], arrived]
    record = dict(latency_budget=budget, constraint_limits=limits, multipliers=list(dual),
                  components=["instance_seconds", "charged_task_seconds", "missed_arrivals"],
                  scales=scales, raw=totals, normalized=None, eligible=item["eligible"],
                  coverage_complete=arrived == len(item["task_ids"]))
    item["budget_policy"] = record
    item["scalar_ledger_return"] = item["window_return"]
    item["reward_components"]["semantics"] = "legacy scalar ledger; budget_policy specifies the learner return"
    if not arrived or not item["eligible"] or not record["coverage_complete"]:
        record.update(status="INELIGIBLE", eligible=False)
        item["eligible"] = False
        if not item.get("exclusion_reason"):
            item["exclusion_reason"] = "budget_no_arrivals" if not arrived else "budget_arrival_coverage"
        item["reward"] = None
        return False
    scaled = [value / scale for value, scale in zip(totals, scales)]
    record.update(status="EVALUATED", normalized=scaled)
    history = item["transitions"]
    before = [row["transition"]["cost_at_decision"] for row in history]
    record["before_first_choice"] = before[0] if before else totals
    for row, start, end in zip(history, before, before[1:] + [totals]):
        transition = row["transition"]
        raw = [v - u for u, v in zip(start, end)]
        cost = [value / scale for value, scale in zip(raw, scales)]
        transition.update(latency_budget=budget, raw_cost_delta=raw, normalized_cost_delta=cost,
                          multipliers=list(dual), scalar_ledger_reward=transition["reward"],
                          reward=-cost[0] - sum(w * c for w, c in zip(dual, cost[1:])))
    record["lagrangian_return"] = -scaled[0] - sum(w * c for w, c in zip(dual, scaled[1:]))
    item["reward"] = record["lagrangian_return"]
    if training:
        dual[:] = [max(0., weight + spec["dual_step"] * (value - limit))
                   for weight, value, limit in zip(dual, scaled[1:], limits)]
    record["multipliers_after"] = list(dual)
    return True
