"""Report business-model output throughput and successful office work from saved arrivals."""
import argparse
import csv
import json
import math
from pathlib import Path


def read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def summarize(directory, finish_path):
    finish = read(finish_path)
    workload = finish["workload"]
    if finish.get("status") != "COMPLETE" or workload["status"] != "COMPLETE":
        raise ValueError(f"Unfinished workload: {finish_path.name}")
    ids = finish["task_ids"]
    if not ids or len(set(ids)) != len(ids) or set(ids) != set(workload["finished"]):
        raise ValueError(f"Incomplete or duplicate task identities: {finish_path.name}")
    prefix = finish_path.name.removesuffix("-finish.json")
    starts, ends = [], []
    tokens = known_calls = model_calls = known_scores = successful = 0
    for i, identity in enumerate(ids):
        task = read(directory / f"{prefix}-task-{i}.json")
        terminal = task["business_terminal"]
        result = task["objective_result"]
        if (task["id"] != identity or terminal["task_id"] != identity
                or result["session"] != workload["session"]):
            raise ValueError(f"Task identity or measurement clock changed at {prefix}/{i}")
        start, end = terminal["arrived_s"], terminal["business_finished_s"]
        if not (math.isfinite(start) and math.isfinite(end) and end >= start):
            raise ValueError(f"Invalid clock at {prefix}/{i}")
        starts.append(start)
        ends.append(end)
        model_calls += task["model_calls"]
        for receipt in task["usage"]:
            if isinstance(receipt, dict):
                n = receipt.get("completion_tokens", receipt.get("output_tokens"))
                if type(n) is int and n >= 0:
                    tokens += n
                    known_calls += 1
        if len(task["usage"]) > task["model_calls"]:
            raise ValueError(f"Too many business-model usage records at {prefix}/{i}")
        score = task.get("evaluation") or {}
        grade_path = directory / f"{prefix}-spreadsheet-{i}" / "native-score.json"
        if score.get("status") == "PENDING_NATIVE_SCORING" and grade_path.is_file():
            grade = read(grade_path)
            if grade.get("task_id") != identity or grade.get("case_id") != task.get("experiment", {}).get("case"):
                raise ValueError(f"Native workbook score belongs to another task at {prefix}/{i}")
            if (type(grade.get("hard")) is bool and isinstance(grade.get("cases"), list)
                    and len(grade["cases"]) == 3 and all(type(value) is bool for value in grade["cases"])):
                score = dict(status="EVALUATED", quality=int(grade["hard"]))
        if (score.get("status") in {"EVALUATED", "NATIVE_SCORED"}
                and type(score.get("quality")) in (bool, int, float) and score["quality"] in (0, 1)):
            known_scores += 1
            successful += int(score["quality"] == 1 and result["delivered"])
    seconds = max(ends) - min(starts)
    if seconds <= 0:
        raise ValueError(f"Zero-length workload: {finish_path.name}")
    return dict(arm=finish["mode"], arrivals=len(ids), window_s=seconds,
                business_model_requests=model_calls, requests_with_output_usage=known_calls,
                measured_output_tokens=tokens, business_output_tokens_per_s=(
                    tokens / seconds if known_calls == model_calls else None),
                terminal_tasks_per_min=60 * len(ids) / seconds,
                safe_completed=successful, known_evaluations=known_scores,
                safe_completed_per_min=60 * successful / seconds if known_scores == len(ids) else None)


def summarize_arm(directory, arm):
    paths = sorted(directory.glob(arm + "-*-finish.json"), key=lambda p: int(p.name.split("-")[1]))
    if not paths or [int(p.name.split("-")[1]) for p in paths] != list(range(len(paths))):
        raise ValueError("Arm has missing or out-of-order workload records: " + arm)
    task_ids = [identity for path in paths for identity in read(path)["task_ids"]]
    if len(set(task_ids)) != len(task_ids):
        raise ValueError("A task cannot contribute to multiple workload windows")
    rows = [summarize(directory, path) for path in paths]
    result = dict(arm=arm, **{key: sum(row[key] for row in rows) for key in (
        "arrivals", "window_s", "business_model_requests", "requests_with_output_usage",
        "measured_output_tokens", "safe_completed", "known_evaluations")})
    seconds = result["window_s"]
    result.update(business_output_tokens_per_s=(result["measured_output_tokens"] / seconds
                      if result["requests_with_output_usage"] == result["business_model_requests"] else None),
                  terminal_tasks_per_min=60 * result["arrivals"] / seconds,
                  safe_completed_per_min=(60 * result["safe_completed"] / seconds
                      if result["known_evaluations"] == result["arrivals"] else None))
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", type=Path)
    args = parser.parse_args()
    rows = [summarize_arm(args.results, arm) for arm in ("app_config", "resource_rule", "ppo")]
    path = args.results / "office-throughput.csv"
    with path.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(path)
