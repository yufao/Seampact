"""Run a frozen, development-only calibration followed by the selected reward sweep.

All inputs must already be rebound and frozen for this deployment. This driver
does not deploy, create bindings, run a formal evaluation, or resume a failed run.
"""
import argparse
import copy
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys
import time
from datetime import datetime, timezone

from prepare_office_reward_sweep import DESIGNS, METRICS
CODE = Path(__file__).resolve().parents[1]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def checked_json(path):
    raw = path.read_bytes()
    sidecar = path.with_suffix(path.suffix + ".sha256")
    if hashlib.sha256(raw).hexdigest() != sidecar.read_text(encoding="ascii").split()[0]:
        raise ValueError(f"Frozen input digest mismatch: {path.name}")
    return json.loads(raw)


def comparable(plan):
    plan = copy.deepcopy(plan)
    for key in ("objective_weights", "reward_sweep", "development_evidence"):
        plan.pop(key, None)
    plan["scheduler"]["objective"] = None
    return plan


def calibration_gate(folder, plan, data_root):
    """Require actual, identifiable per-source scores without requiring success."""
    def local_path(path):
        value = PurePosixPath(path)
        if not value.is_absolute() or not value.is_relative_to("/data"):
            raise ValueError("Protocol manifest must use the platform /data mapping")
        result = (data_root / Path(*value.relative_to("/data").parts)).resolve(strict=True)
        if not result.is_relative_to(data_root.resolve()):
            raise ValueError("Protocol manifest escapes the platform data root")
        return result

    sources = {}
    locations = {"agentdojo": {"manifest": plan["manifest"]}, **plan["source_manifests"]}
    for source, location in locations.items():
        path = local_path(location["manifest"])
        expected = plan["manifest_sha256"] if source == "agentdojo" else location["manifest_sha256"]
        if sha(path) != expected:
            raise ValueError(f"Calibration source identity changed: {source}")
        for case in json.loads(path.read_text(encoding="utf-8"))["cases"]:
            if case["id"] in sources:
                raise ValueError("Overlapping calibration source identities")
            sources[case["id"]] = source
    mode = "training_calibration" if plan.get("training_calibration") else "development"
    case_ids = plan["training_calibration"]["case_ids"] if mode == "training_calibration" else plan["cases"]["development"]
    finish = json.loads((folder / f"{mode}-0-finish.json").read_text(encoding="utf-8"))
    task_ids = finish.get("task_ids", [])
    if (finish.get("status") != "COMPLETE" or finish.get("case_ids") != case_ids
            or len(task_ids) != len(case_ids) or len(set(task_ids)) != len(task_ids)):
        raise ValueError("Calibration finish does not cover the exact frozen arrivals")
    expected_evaluator = {
        "agentdojo": "agentdojo/", "spreadsheet": "separate pinned SpreadsheetBench scorer",
        "privacy": "office-synthetic-delivered-markers/1", "office_rag": "seampact-synthetic-office-rag/1",
        "office_ci": "seampact.ci-simulated-keywords/1", "office_summedits": "summedits-office-final-decision/1",
        "office_links": "phiusiil-office-link-review-final-decision/1",
        "office_routing": "seampact.office-recipient-material-binding/1",
        "office_access": "office-simulated-authorization/1",
    }
    counts = {}
    for index, (case_id, task_id) in enumerate(zip(case_ids, task_ids)):
        source = sources.get(case_id)
        if source not in expected_evaluator:
            raise ValueError("Calibration case has an unknown source")
        task = json.loads((folder / f"{mode}-0-task-{index}.json").read_text(encoding="utf-8"))
        if task.get("id") != task_id or (task.get("experiment") or {}).get("case") != case_id:
            raise ValueError("Calibration task identity differs from the finish record")
        terminal = task.get("status")
        if terminal not in {"COMPLETED", "BLOCKED", "FAILED"} or task.get("workspace_status") != "FINISHED":
            raise ValueError("Calibration task has unresolved terminal or workspace state")
        evaluation = task.get("evaluation") or {}
        metrics = evaluation.get("metrics")
        quality = evaluation.get("quality")
        valid_quality = type(quality) in (float, int, bool) and math.isfinite(quality) and 0 <= quality <= 1
        expected_status = "NATIVE_SCORED" if source == "spreadsheet" else "EVALUATED"
        if (evaluation.get("status") != expected_status or not valid_quality
                or not isinstance(metrics, dict) or not metrics
                or not str(evaluation.get("evaluator", "")).startswith(expected_evaluator[source])):
            raise ValueError(f"Calibration lacks observed {source} scoring evidence")
        if source in {"agentdojo", "office_rag"} and type(metrics.get("utility")) is not bool:
            raise ValueError(f"Calibration has no observed {source} utility")
        if source in {"privacy", "office_ci", "office_routing", "office_access"} and type(metrics.get("legitimate_task_success")) is not bool:
            raise ValueError(f"Calibration has no observed {source} task outcome")
        if source == "spreadsheet" and (len(metrics.get("native_cases", [])) != 3
                                        or any(type(v) is not bool for v in metrics["native_cases"])):
            raise ValueError("Calibration has incomplete native spreadsheet scores")
        row = counts.setdefault(source, {"observed": 0, "COMPLETED": 0, "BLOCKED": 0, "FAILED": 0,
                                         "decision_unknown": 0})
        row["observed"] += 1
        row[terminal] += 1
        row["decision_unknown"] += int(metrics.get("decision_unknown") == 1)
    return {"status": "PASSED", "arrivals": len(case_ids), "sources": counts,
            "semantics": "Observed scoring, not a task-success or safety threshold"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sweep", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--grader-root", type=Path, required=True)
    parser.add_argument("--grader-manifest", type=Path, required=True)
    parser.add_argument("--grader-receipt", type=Path, required=True)
    parser.add_argument("--business-container", required=True)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--group-limit", type=int, help="run only the first N groups of the frozen design")
    parser.add_argument("--budget-hours", type=float, help="finish the current arrival block and its grades at the time limit")
    parser.add_argument("--url", default="http://127.0.0.1:8882")
    args = parser.parse_args()
    if args.budget_hours is not None and (not math.isfinite(args.budget_hours) or args.budget_hours <= 0):
        parser.error("--budget-hours must be a positive finite number")
    root = args.data_root.resolve(strict=True)
    output = args.output.resolve()
    if not output.is_relative_to(root) or output == root:
        raise ValueError("Output must be a new directory inside the platform data root")
    grading = args.grader_root.resolve(strict=True)
    if grading.is_relative_to(root) or root.is_relative_to(grading):
        raise ValueError("Grading answers must remain outside the platform data root")
    manifest_path = args.grader_manifest.resolve(strict=True)
    if not manifest_path.is_relative_to(grading):
        raise ValueError("Grading manifest must be under the grading root")
    receipt = json.loads(args.grader_receipt.resolve(strict=True).read_text())
    if receipt.get("status") != "READY_SYNTHETIC_VALIDATED" or receipt.get("grading_only", {}).get("manifest_sha256") != sha(manifest_path):
        raise ValueError("A matching, synthetic-validated grading receipt is required")
    sweep = args.sweep.resolve(strict=True)
    if not sweep.is_relative_to(root):
        raise ValueError("Frozen server protocols must be under the platform data root")
    manifest = checked_json(sweep / "manifest.json")
    entries = manifest["groups"]
    groups = DESIGNS[manifest.get("design", "joint")]
    if [row["group"] for row in entries] != list(groups):
        raise ValueError("Unexpected development group list")
    plans = []
    for row in entries:
        path = (sweep / row["protocol"]).resolve(strict=True)
        if not path.is_relative_to(sweep) or sha(path) != row["sha256"]:
            raise ValueError("Group protocol identity mismatch")
        plan = checked_json(path)
        expected = dict(quality=1, delivery=1, **dict(zip(METRICS, groups[row["group"]])))
        if plan["objective_weights"] != expected or row["weights"] != expected:
            raise ValueError("Reward weights differ from the selected sweep design")
        if len(plan["cases"]["development"]) != 225:
            raise ValueError("Expected the frozen 225-item development set")
        plans.append((path, plan))
    if any(comparable(plan) != comparable(plans[0][1]) for _, plan in plans[1:]):
        raise ValueError("Non-weight fields differ between groups")
    if args.group_limit is not None:
        if not 1 <= args.group_limit <= len(entries):
            parser.error("--group-limit must be within the number of frozen groups")
        entries, plans = entries[:args.group_limit], plans[:args.group_limit]

    def assert_code():
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=CODE, text=True).strip()
        dirty = subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=no"], cwd=CODE, text=True).strip()
        if commit != args.expected_commit or dirty:
            raise RuntimeError("Committed platform code changed or differs from the requested commit")
        image_id = subprocess.check_output(["docker", "inspect", args.business_container,
                                            "--format", "{{.Image}}"], text=True).strip()
        labels = json.loads(subprocess.check_output(["docker", "image", "inspect", image_id,
                                                      "--format", "{{json .Config.Labels}}"], text=True)) or {}
        if labels.get("org.opencontainers.image.revision") != commit:
            raise RuntimeError("Running platform image revision differs from committed code")

    assert_code()
    output.mkdir(parents=True, exist_ok=False)
    (output / "logs").mkdir()
    state = dict(status="STARTING", commit=args.expected_commit, current=None, completed=[],
                 scope=f"calibration and {len(entries)} development groups; no formal evaluation")

    def save():
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        temporary = output / "progress.json.partial"
        temporary.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        temporary.replace(output / "progress.json")

    grader = [sys.executable, str(CODE / "scripts/run_office_spreadsheet_grader.py"), "{collection}",
              "--allowed-root", str(output), "--grading-root", str(grading),
              "--grading-manifest", str(manifest_path), "--receipt", str(args.grader_receipt.resolve()),
              "--business-container", args.business_container]
    common = ["--url", args.url, "--local-data-root", str(root), "--server-data-root", "/data",
              "--grader-command", json.dumps(grader)]
    if args.budget_hours is not None:
        state["admission_deadline"] = time.time() + args.budget_hours * 3600
        common += ["--admission-deadline", str(state["admission_deadline"])]
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    save()
    try:
        for index, (name, protocol) in enumerate([("calibration", plans[0][0])] +
                                                [(entry["group"], plan[0]) for entry, plan in zip(entries, plans)]):
            assert_code()
            state.update(status="RUNNING", current=name, current_started_at=datetime.now(timezone.utc).isoformat())
            save()
            command = [sys.executable, "-u", str(CODE / "scripts/run_office_pilot.py"),
                       str(protocol), str(output / name)] + common
            if index == 0:
                command.append("--calibration-only")
            else:
                command += ["--tuning", "--scales-from", str(output / "calibration/frozen-pilot-protocol.json")]
                if name == "balanced":
                    command.append("--include-rule-baseline")
            with (output / "logs" / (name + ".stdout.log")).open("x") as out, \
                    (output / "logs" / (name + ".stderr.log")).open("x") as err:
                result = subprocess.run(command, cwd=CODE, env=env, stdout=out, stderr=err)
            if result.returncode == 75 and (output / name / "budget-stop.json").is_file():
                state.update(status="BUDGET_REACHED", stopped_in=name)
                save()
                return
            if result.returncode:
                raise RuntimeError(f"{name} exited {result.returncode}; retained evidence, no automatic rerun")
            if name == "calibration":
                gate = calibration_gate(output / name, plans[0][1], root)
                (output / "calibration-gate.json").write_text(json.dumps(gate, indent=2) + "\n", encoding="utf-8")
            state["completed"].append(dict(group=name, ended_at=datetime.now(timezone.utc).isoformat()))
            save()
        state.update(status="COMPLETE", current=None)
        save()
    except BaseException as exc:
        state.update(status="STOPPED", error=str(exc))
        save()
        raise


if __name__ == "__main__":
    main()
