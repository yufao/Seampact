"""Prepare frozen reward settings for development-only comparison."""
import argparse
import copy
import hashlib
import json
from pathlib import Path


GROUPS = {
    "balanced": (0.1, 0.1, 0.1, 0.1),
    "ram_priority": (0.25, 0.05, 0.05, 0.05),
    "vram_priority": (0.05, 0.25, 0.05, 0.05),
    "latency_priority": (0.05, 0.05, 0.25, 0.05),
    "api_priority": (0.05, 0.05, 0.05, 0.25),
    "quality_priority": (0.05, 0.05, 0.05, 0.05),
    "no_api_cost": (0.1, 0.1, 0.1, 0.0),
}
METRICS = ("ram", "vram", "latency", "api")
ONE_FACTOR = {"balanced": GROUPS["balanced"]}
for level, value in (("high", 0.20), ("low", 0.05)):
    for metric in METRICS:
        ONE_FACTOR[f"{metric}_{level}"] = tuple(value if name == metric else 0.10 for name in METRICS)
DESIGNS = {"one-factor": ONE_FACTOR, "joint": GROUPS}


def training_calibration(plan, metadata):
    """Freeze sixteen training-only arrivals covering the four office training sources."""
    ids = plan["cases"]["training"]
    population = set(ids)
    by_id = {row["id"]: row for row in metadata["case_metadata"] if row["id"] in population}
    if set(by_id) != population or any(r["split"] != "train_pool" for r in by_id.values()):
        raise ValueError("The normalization population must match the training split")
    def take(values, count):
        ordered = sorted(values, key=lambda key: digest(f"{plan['seed']}|train-scale|{key}".encode()))
        if len(ordered) < count:
            raise ValueError("Insufficient training cases for the office cost reference")
        return ordered[:count]
    sheet_blocks = {}
    start = 0
    for block in plan["arrival_schedule"]["training"]:
        stop = start + len(block["offsets_ms"])
        sheet_blocks.setdefault(block["profile"], []).extend(i for i in ids[start:stop] if by_id[i]["source"] == "spreadsheetbench")
        start = stop
    light, heavy = (take(sheet_blocks[name], 4) for name in ("small_workbook_burst", "large_workbook_burst"))
    middle = take(sheet_blocks["sustained_mixed"], 4)
    def one(source, variant=None):
        return take([i for i, row in by_id.items() if row["source"] == source and
                     (variant is None or row["variant"] == variant) and not (source == "agentdojo" and row["category"] == "rag")], 1)[0]
    office = [one("agentdojo", "normal"), one("agentdojo", "risk_attempt"), one("privacy", "risk_attempt"), middle[0]]
    ordered = [*light, *office, *heavy, *middle[1:], one("office_rag")]
    if len(set(ordered)) != 16 or set(ordered) & set(plan["cases"]["development"] + plan["cases"]["evaluation"]):
        raise ValueError("Training cost calibration overlaps another split or contains duplicates")
    blocks = [("small_workbook_burst", [0]*4), ("sustained_mixed", [0,0,3000,6000]),
              ("large_workbook_burst", [0]*4), ("mixed_tail", [0,0,0]), ("exclusive_corpus", [0])]
    return dict(source_split="training", case_ids=ordered,
                arrival_schedule=[dict(profile=name, offsets_ms=offsets) for name, offsets in blocks])


def digest(data):
    return hashlib.sha256(data).hexdigest()


def write(path, value):
    raw = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    with path.open("xb") as stream:
        stream.write(raw)
    identity = digest(raw)
    path.with_suffix(path.suffix + ".sha256").write_text(identity + "  " + path.name + "\n", encoding="ascii")
    return identity


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base", type=Path)
    parser.add_argument("output", type=Path, help="new private sweep directory")
    parser.add_argument("--training-metadata", type=Path, required=True, help="frozen source/family metadata for training-only cost calibration")
    parser.add_argument("--design", choices=DESIGNS, default="one-factor",
                        help="one-factor varies one coefficient around 0.10; joint reproduces the earlier preferences")
    args = parser.parse_args()
    raw = args.base.read_bytes()
    base_sha = digest(raw)
    if args.base.with_suffix(args.base.suffix + ".sha256").read_text(encoding="ascii").split()[0] != base_sha:
        raise ValueError("Base protocol checksum mismatch")
    base = json.loads(raw)
    dataset = args.training_metadata.read_bytes()
    if digest(dataset) != base["workload_trace"]["dataset_sha256"]:
        raise ValueError("Training source metadata differs from the frozen dispatch")
    base["training_calibration"] = training_calibration(base, json.loads(dataset))
    if (base.get("schema") != "seampact.office-comparison/1" or not base.get("continuous_training") or
            base.get("update_after_tasks") != 16 or base.get("scheduler", {}).get("objective") is not None or
            base.get("workload_trace", {}).get("order") not in {"shuffle", "timed_mixed"} or
            any(not base["cases"].get(s) for s in ("development", "training", "evaluation"))):
        raise ValueError("Expected a prepared continuous office comparison protocol")
    if "reward_sweep" in base:
        raise ValueError("Base protocol already belongs to a reward sweep")
    extension_sources = base.get("workload_trace", {}).get("development_extensions", {})
    if extension_sources:
        if not set(extension_sources) <= {"office_ci", "office_summedits", "office_routing", "office_access", "office_links"}:
            raise ValueError("Unsupported development extension denominators")
        if any(row.get("distinct_cases") != row.get("original_tasks") or
               row.get("distinct_cases") != row.get("families")
               for row in extension_sources.values()):
            raise ValueError("Development extension denominators are inconsistent")
        if sum(row["distinct_cases"] for row in extension_sources.values()) > len(base["cases"]["development"]):
            raise ValueError("Development extension counts exceed the development dispatch")
    args.output.mkdir(parents=True, exist_ok=False)
    rows = []
    for name, costs in DESIGNS[args.design].items():
        plan = copy.deepcopy(base)
        weights = dict(quality=1, delivery=1, **dict(zip(METRICS, costs)))
        plan["objective_weights"] = weights
        plan["reward_sweep"] = dict(group=name, base_protocol_sha256=base_sha, phase="development_tuning",
            interpretation="fixed preference chosen before training; weights are not learned automatically",
            scales="common cost scales measured on sixteen training cases before PPO; frozen across groups",
            comparison="compare source quality, delivery and resource metrics; do not compare totals across different reward functions",
            final_evaluation="select once from development, then evaluate the same frozen protocol and checkpoint without retraining")
        # No task, seed, binding, lifecycle, budget, or other setting may change.
        stripped = {k: v for k, v in plan.items() if k not in {"objective_weights", "reward_sweep"}}
        original = {k: v for k, v in base.items() if k != "objective_weights"}
        if stripped != original:
            raise ValueError("A non-weight protocol field changed")
        path = args.output / f"{name}.json"
        rows.append(dict(group=name, protocol=path.name, sha256=write(path, plan), weights=weights))
    lines = ["# 办公调度开发侧权重比较", "", f"共 {len(rows)} 组。各组使用同一题单、顺序、种子和训练量。", "",
             "| 组别 | 质量 | 交付 | RAM | VRAM | 时延 | API |",
             "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for row in rows:
        w = row["weights"]
        lines.append(f"| {row['group']} | {w['quality']} | {w['delivery']} | {w['ram']} | {w['vram']} | {w['latency']} | {w['api']} |")
    lines.extend(["", "从训练集抽取的 16 题提供共同成本尺度。每个候选独立训练完整训练集，"
                  "以验证集的原生质量、防护、交付和各项资源实测选择参数，选定后用冻结检查点跑测试集。", ""])
    (args.output / "README.md").write_text("\n".join(lines), encoding="utf-8")
    counts = {name: len(ids) for name, ids in base["cases"].items()}
    manifest = dict(schema="seampact.office-reward-sweep/1", status="PREPARED_NOT_RUN",
        base_protocol=str(args.base.resolve()), base_protocol_sha256=base_sha, design=args.design, groups=rows,
        readable_summary="README.md",
        seed=base["seed"], workload_trace=base["workload_trace"],
        cost_order=list(METRICS), fixed_weights=dict(quality=1, delivery=1),
        normalization=base["training_calibration"],
        planned_arrivals=counts,
        development_source_denominators=extension_sources,
        development=f"16 training arrivals for cost calibration; {len(rows)} training runs of {counts['training']} tasks; {len(rows)} frozen PPOs and two fixed baselines on {counts['development']} validation tasks",
        selection="select on development evidence, preserve unknowns, compare native quality, delivery and resource metrics rather than scalar totals across reward functions",
        final_evaluation=f"after selection only: three {counts['evaluation']}-task arms, using the selected frozen protocol and trained checkpoint without retraining",
        note="Fixed cost preferences; no automatic weight learning or non-linear utility is implied.")
    manifest_sha = write(args.output / "manifest.json", manifest)
    print(json.dumps(dict(groups=len(rows), manifest_sha256=manifest_sha, status="PREPARED_NOT_RUN",
                          development_source_denominators=extension_sources)))


if __name__ == "__main__":
    main()
