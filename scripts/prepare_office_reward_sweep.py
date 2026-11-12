"""Prepare seven fixed, development-only reward preferences without running them."""
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
    args = parser.parse_args()
    raw = args.base.read_bytes()
    base_sha = digest(raw)
    if args.base.with_suffix(args.base.suffix + ".sha256").read_text(encoding="ascii").split()[0] != base_sha:
        raise ValueError("Base protocol checksum mismatch")
    base = json.loads(raw)
    if (base.get("schema") != "seampact.office-comparison/1" or not base.get("continuous_training") or
            base.get("update_after_tasks") != 16 or base.get("scheduler", {}).get("objective") is not None or
            [len(base["cases"][s]) for s in ("development", "training", "evaluation")] != [32, 256, 72] or
            base.get("workload_trace", {}).get("order") != "shuffle"):
        raise ValueError("Expected the unfrozen continuous shuffle comparison protocol")
    if "reward_sweep" in base:
        raise ValueError("Base protocol already belongs to a reward sweep")
    args.output.mkdir(parents=True, exist_ok=False)
    rows = []
    for name, costs in GROUPS.items():
        plan = copy.deepcopy(base)
        weights = dict(quality=1, delivery=1, **dict(zip(METRICS, costs)))
        plan["objective_weights"] = weights
        plan["reward_sweep"] = dict(group=name, base_protocol_sha256=base_sha, phase="development_tuning",
            interpretation="fixed preference chosen before training; weights are not learned automatically",
            scales="balanced development calibration establishes common normalization; all other groups reuse that frozen scale",
            comparison="compare source quality, delivery and resource metrics; do not compare totals across different reward functions",
            final_evaluation="select once from development, then evaluate the same frozen protocol and checkpoint without retraining")
        # No task, seed, binding, lifecycle, budget, or other setting may change.
        stripped = {k: v for k, v in plan.items() if k not in {"objective_weights", "reward_sweep"}}
        original = {k: v for k, v in base.items() if k != "objective_weights"}
        if stripped != original:
            raise ValueError("A non-weight protocol field changed")
        path = args.output / f"{name}.json"
        rows.append(dict(group=name, protocol=path.name, sha256=write(path, plan), weights=weights))
    lines = ["# 固定奖励偏好：开发侧准备", "", "状态：未运行。七组使用相同任务、顺序、种子和训练量。", "",
             "| 组别 | 质量 | 交付 | RAM | VRAM | 时延 | API |",
             "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for row in rows:
        w = row["weights"]
        lines.append(f"| {row['group']} | {w['quality']} | {w['delivery']} | {w['ram']} | {w['vram']} | {w['latency']} | {w['api']} |")
    lines.extend(["", "先由 balanced 开发校准确定共同归一化尺度，其余六组复用。no_api_cost 将 token 惩罚设为零，"
                  "但仍记录 token 消耗。比较原生质量、交付和各资源指标，"
                  "不跨不同奖励函数比较奖励总分。依据开发结果选定一组后，使用同一冻结协议和检查点进行正式评测，"
                  "不重新训练。每组的这些权重是预先固定的偏好，不由模型学习。", ""])
    (args.output / "README.md").write_text("\n".join(lines), encoding="utf-8")
    manifest = dict(schema="seampact.office-reward-sweep/1", status="PREPARED_NOT_RUN",
        base_protocol=str(args.base.resolve()), base_protocol_sha256=base_sha, groups=rows,
        readable_summary="README.md",
        seed=base["seed"], workload_trace=base["workload_trace"],
        cost_order=list(METRICS), fixed_weights=dict(quality=1, delivery=1),
        normalization="one shared set of development scales established by balanced, reused by six remaining groups",
        development="seven independent 256-task training runs and seven 32-task PPO development evaluations; one 32-task app calibration and one 32-task rule baseline",
        selection="select on development evidence, preserve unknowns, compare native quality, delivery and resource metrics rather than scalar totals across reward functions",
        final_evaluation="after selection only: three 72-task arms, using the selected frozen protocol and trained checkpoint without retraining",
        note="Fixed cost preferences; no automatic weight learning or non-linear utility is implied.")
    manifest_sha = write(args.output / "manifest.json", manifest)
    print(json.dumps(dict(groups=len(rows), manifest_sha256=manifest_sha, status="PREPARED_NOT_RUN")))


if __name__ == "__main__":
    main()
