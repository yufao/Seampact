"""Render the actual frozen pilot records; missing evidence stays unknown."""
import argparse
import csv
import json
import math
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager


ARMS = (("app_config", "常规配置", "#3B668B"),
        ("resource_rule", "资源规则", "#B6793C"),
        ("ppo", "RL", "#43816C"))
QUALITY = (("normal_success", "正常任务成功率"),
           ("attack_utility", "受攻击任务成功率"),
           ("native_asr", "原生攻击成功率 ASR"))
COST = (("ram_gib_s", "RAM / 到达任务", "GiB·s"),
        ("vram_gib_s", "NVML 显存 / 到达任务", "GiB·s"),
        ("latency_s", "平均任务耗时", "秒"),
        ("api_tokens", "API tokens / 到达任务", "tokens"))


def read(path):
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def boolean_rate(values, total):
    known = [v for v in values if type(v) is bool]
    return dict(value=(sum(known) / total if len(known) == total and total else None),
                numerator=sum(known), denominator=total, known=len(known), unknown=total - len(known))


def costs(workload):
    if not isinstance(workload, dict):
        return {name: None for name, _, _ in COST}, 0
    raw = workload.get("raw") or {}
    total = workload.get("denominator", 0)
    if type(total) is not int or total <= 0 or raw.get("finished") != total:
        return {name: None for name, _, _ in COST}, total if type(total) is int else 0
    measured = (workload.get("resources") or {}).get("measured") or {}
    def average(value, divisor=1):
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            return None
        return value / total / divisor
    return dict(ram_gib_s=average(measured.get("ram_byte_s"), 1024**3),
                vram_gib_s=average(measured.get("vram_byte_s"), 1024**3),
                latency_s=average(raw.get("latency_s")), api_tokens=average(raw.get("api_tokens"))), total


def data_for_arm(root, mode, case_ids, summary):
    start = read(root / (mode + "-0-start.json"))
    finished = read(root / (mode + "-0-finish.json"))
    if start and start.get("case_ids") != case_ids:
        raise ValueError("Case order differs from the frozen evaluation protocol: " + mode)
    rows = []
    for index, case_id in enumerate(case_ids):
        task = read(root / (mode + "-0-task-" + str(index) + ".json"))
        if task and (task.get("experiment") or {}).get("case") != case_id:
            raise ValueError("Task and frozen case disagree: " + mode + "/" + str(index))
        if task and (not start or task.get("id") != start["task_ids"][index]
                     or (task.get("experiment") or {}).get("arm") != mode):
            raise ValueError("Task is not from this arm's frozen workload: " + mode + "/" + str(index))
        evaluation = (task or {}).get("evaluation") or {}
        metrics = (evaluation.get("metrics") or {}) if evaluation.get("status") == "EVALUATED" else {}
        attacked = ":clean" not in case_id
        rows.append(dict(attacked=attacked, utility=metrics.get("utility"),
                         attack=metrics.get("attack_success"), quality=evaluation.get("quality")))
    normal = [row for row in rows if not row["attacked"]]
    attacked = [row for row in rows if row["attacked"]]
    if len(normal) != 6 or len(attacked) != 2:
        raise ValueError("Frozen evaluation must contain six clean and two attacked cases")
    safe = [bool(row["utility"] and not row["attack"]) if type(row["utility"]) is bool
            and type(row["attack"]) is bool else None for row in attacked]
    for row, value in zip(attacked, safe):
        if row["quality"] is not None and value is not None and row["quality"] != float(value):
            raise ValueError("Recorded quality conflicts with the native metrics: " + mode)
    rates = dict(normal_success=boolean_rate([row["utility"] for row in normal], len(normal)),
                 attacked_safe_completion=boolean_rate(safe, len(attacked)),
                 native_asr=boolean_rate([row["attack"] for row in attacked], len(attacked)),
                 attack_utility=boolean_rate([row["utility"] for row in attacked], len(attacked)))
    workload = (finished or {}).get("workload", {}).get("summary")
    reported = ((summary or {}).get("arms") or {}).get(mode)
    if reported and reported.get("workload") != workload:
        raise ValueError("Summary and workload record disagree: " + mode)
    averages, arrivals = costs(workload)
    return rates, averages, arrivals, start


def draw_panel(ax, title, values, labels, colors, suffix="", rate=False):
    ax.set_title(title, loc="left", fontweight="bold", fontsize=12, pad=12)
    available = [value for value in values if value is not None]
    upper = 1.18 if rate else max([1e-9, *available]) * 1.25 if available else 1
    ax.set_xlim(0, upper)
    for index, (item, label, color) in enumerate(zip(values, labels, colors)):
        y = len(values) - index - 1
        ax.text(-0.03, y, label, transform=ax.get_yaxis_transform(), ha="right", va="center", fontsize=10)
        if item is None:
            ax.text(upper * 0.025, y, "未知", color="#6B7280", va="center", fontsize=10)
        else:
            ax.barh(y, item, height=0.55, color=color)
            text = f"{item * 100:.0f}%" if rate else f"{item:,.2f}"
            ax.text(item + upper * 0.02, y, text + suffix, va="center", fontsize=10)
    ax.set_ylim(-0.7, len(values) - 0.3)
    ax.set_yticks([])
    ax.set_xticks([0, 0.5, 1] if rate else [])
    if rate:
        ax.set_xticklabels(["0%", "50%", "100%"], fontsize=9, color="#6B7280")
    ax.tick_params(axis="x", length=0)
    for spine in ax.spines.values():
        spine.set_visible(False)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("results", type=Path, help="runner output directory")
    parser.add_argument("--output", type=Path, help="figure directory; defaults to results/figures")
    args = parser.parse_args()
    root = args.results
    plan = read(root / "frozen-pilot-protocol.json")
    if not plan:
        raise ValueError("No frozen final pilot protocol; no data will be fabricated")
    if (plan.get("scheduler") or {}).get("vram_source") != "nvml_process":
        raise ValueError("The frozen protocol does not specify NVML process attribution for VRAM")
    case_ids = plan["cases"]["evaluation"]
    summary = read(root / "summary.json")
    all_data = {mode: data_for_arm(root, mode, case_ids, summary) for mode, _, _ in ARMS}
    pools = [row[3]["workload"]["pool"] for row in all_data.values() if row[3]]
    if pools and any(pool != pools[0] for pool in pools[1:]):
        raise ValueError("Arms do not share the same frozen capability pool")
    expected_pool = (plan.get("development_evidence") or {}).get("pool")
    if pools and (expected_pool is None or any(pool != expected_pool for pool in pools)):
        raise ValueError("An arm differs from the development capability pool")
    policies = [row[3]["workload"].get("policy_config_id") for row in all_data.values() if row[3]]
    if policies and (None in policies or len(set(policies)) != 1):
        raise ValueError("Arms do not share the same frozen objective configuration")
    ppo_start = all_data["ppo"][3]
    if ppo_start and ppo_start["workload"].get("policy") != "CandidatePPO":
        raise ValueError("RL arm does not use the frozen PPO policy")
    out = args.output or root / "figures"
    out.mkdir(parents=True, exist_ok=True)
    font_names = {font.name for font in font_manager.fontManager.ttflist}
    chinese = next((name for name in ("Microsoft YaHei", "Noto Sans CJK SC", "SimHei", "Arial Unicode MS")
                    if name in font_names), None)
    if not chinese:
        raise RuntimeError("A Chinese font is required for readable figure labels")
    plt.rcParams.update({"font.sans-serif": [chinese], "axes.unicode_minus": False,
                         "svg.fonttype": "none", "figure.facecolor": "white", "savefig.facecolor": "white"})
    colors, labels = [row[2] for row in ARMS], [row[1] for row in ARMS]
    fig, axes = plt.subplots(1, 3, figsize=(13.4, 3.7), constrained_layout=False)
    for ax, (name, title) in zip(axes, QUALITY):
        values = [all_data[mode][0][name]["value"] for mode, _, _ in ARMS]
        draw_panel(ax, title, values, labels, colors, rate=True)
        for i, (mode, _, _) in enumerate(ARMS):
            stat = all_data[mode][0][name]
            if stat["unknown"]:
                ax.text(0.06, 2-i-0.27, f"未知 {stat['unknown']}/{stat['denominator']}", fontsize=8, color="#6B7280")
            elif stat["value"] is not None:
                ax.text(min(stat["value"] + 0.03, 1.02), 2-i-0.27,
                        f"{stat['numerator']}/{stat['denominator']}", fontsize=8, color="#6B7280")
    fig.suptitle("办公任务质量与攻击结果", x=0.08, ha="left", fontsize=15, fontweight="bold")
    fig.text(0.08, 0.02, "每臂8例：6正常、2攻击，覆盖7个任务族；小样本描述性比较。任务成功率采用原生 utility。", fontsize=9, color="#4B5563")
    fig.subplots_adjust(left=0.11, right=0.99, top=0.79, bottom=0.20, wspace=0.60)
    for ext in ("png", "svg"):
        fig.savefig(out / ("office-quality." + ext), dpi=220)
    plt.close(fig)
    fig, axes = plt.subplots(2, 2, figsize=(10.8, 6.4))
    for ax, (name, title, unit) in zip(axes.flat, COST):
        values = [all_data[mode][1][name] for mode, _, _ in ARMS]
        draw_panel(ax, title + "（" + unit + "）", values, labels, colors)
    fig.suptitle("每个到达任务的资源与时间", x=0.11, ha="left", fontsize=15, fontweight="bold")
    fig.text(0.11, 0.02, "资源为受管实例实测积分，显存采用完整 NVML 归属；计量覆盖不完整时显示未知。", fontsize=9, color="#4B5563")
    fig.subplots_adjust(left=0.19, right=0.98, top=0.88, bottom=0.10, wspace=0.72, hspace=0.55)
    for ext in ("png", "svg"):
        fig.savefig(out / ("office-cost." + ext), dpi=220)
    plt.close(fig)
    with (out / "office-pilot-metrics.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["arm", "metric", "value", "unit", "numerator", "denominator", "known", "unknown"])
        writer.writeheader()
        for mode, label, _ in ARMS:
            rates, averages, arrivals, _ = all_data[mode]
            for metric, stat in rates.items():
                writer.writerow(dict(arm=label, metric=metric, unit="fraction", **stat))
            for metric, _, unit in COST:
                value = averages[metric]
                writer.writerow(dict(arm=label, metric=metric, value=value, unit=unit, denominator=arrivals,
                                     known=arrivals if value is not None else 0, unknown=0 if value is not None else (arrivals or 8)))
    print("Saved two figures (PNG/SVG) and an evidence-preserving metrics CSV to", out)


if __name__ == "__main__":
    main()
