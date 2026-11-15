"""Run the explicitly frozen small office pilot and preserve every result.

The protocol is a private JSON file, with cases, model bindings and execution
configuration selected before running. Comparison training uses training-only scales;
unknown measurements or scores stop PPO training instead of being imputed.
"""
import argparse
from concurrent.futures import Future, ThreadPoolExecutor
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from seampact.office_pilot import workload_cases


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def service_path(path, local_root=None, server_root=None):
    path = Path(path).resolve()
    if local_root is None and server_root is None:
        return str(path)
    if local_root is None or server_root is None:
        raise ValueError("Both local and server data roots are required")
    return str(server_root).rstrip("/") + "/" + path.relative_to(Path(local_root).resolve()).as_posix()


def sum_known(values):
    return None if any(value is None for value in values) else sum(values)


def timed_admissions(blocks, admit):
    """Pace the offered load independently of result collection inside a block."""
    stop = threading.Event()
    def send(block, receipts):
        origin = time.perf_counter()
        wall_origin = time.time()
        for index, offset, receipt in zip(block["indices"], block["offsets_ms"], receipts):
            due = origin + offset / 1000
            remaining = due - time.perf_counter()
            while remaining > 0:
                if stop.wait(remaining):
                    return
                remaining = due - time.perf_counter()
            if stop.is_set():
                return
            sent = time.perf_counter() - origin
            timing = dict(block_origin_unix_s=wall_origin, scheduled_offset_ms=offset,
                          client_send_offset_ms=sent * 1000, profile=block["profile"])
            try:
                result = admit(index, timing)
                receipt.set_result(result)
            except Exception as error:
                receipt.set_exception(error)
                return

    with ThreadPoolExecutor(max_workers=1) as pool:
        try:
            for block in blocks:
                receipts = [Future() for _ in block["indices"]]
                sending = pool.submit(send, block, receipts)
                for index, receipt in zip(block["indices"], receipts):
                    yield index, receipt.result()
                sending.result()
        finally:
            stop.set()


def allowed_prebuild(plan, preparations, *, calibration_only=False, tuning=False, evaluation_only=False, training_only=False, validation_only=False):
    if calibration_only:
        permitted = (set(workload_cases(plan, "training_calibration")[0]) if "training_calibration" in plan
                     else set(plan["cases"]["development"]))
    elif training_only or tuning and "training_calibration" in plan:
        permitted = set(plan["cases"]["training"])
    elif validation_only:
        permitted = set(plan["cases"]["development"])
    elif tuning:
        permitted = set(plan["cases"]["training"] + plan["cases"]["development"])
    elif evaluation_only:
        permitted = set(plan["cases"]["evaluation"])
    else:
        return preparations
    return [case for case in preparations if case in permitted]


def combined_summary(workloads):
    """Aggregate disjoint sessions without averaging per-session averages."""
    summaries = [workload["workload"]["summary"] for workload in workloads]
    resources = {}
    for source in ("measured", "reserved"):
        resources[source] = {name: sum_known([summary["resources"][source].get(name) for summary in summaries])
                             for name in ("ram_byte_s", "vram_byte_s")}
    return dict(denominator=sum(summary["denominator"] for summary in summaries),
                raw={key: sum_known([summary["raw"].get(key) for summary in summaries])
                     for key in summaries[0]["raw"]}, resources=resources,
                workload_count=len(workloads),
                sessions=[dict(id=row["id"], summary=row["workload"]["summary"]) for row in workloads])


def reuse_calibration(plan, previous, previous_sha):
    def comparable(value):
        value = copy.deepcopy(value)
        for key in ("objective_weights", "reward_sweep", "development_evidence"):
            value.pop(key, None)
        value["scheduler"]["objective"] = None
        return value
    if comparable(plan) != comparable(previous):
        raise ValueError("Shared calibration requires the same data, order, seed, capabilities and runtime settings")
    objective, evidence = previous["scheduler"].get("objective"), previous.get("development_evidence")
    if not objective or not evidence or objective["scales"] != evidence.get("scales") or not evidence.get("pool"):
        raise ValueError("Shared calibration needs a real frozen development result")
    result = copy.deepcopy(plan)
    result["scheduler"]["objective"] = dict(copy.deepcopy(objective), weights=copy.deepcopy(plan["objective_weights"]))
    result["development_evidence"] = dict(copy.deepcopy(evidence), reused_from_protocol_sha256=previous_sha)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("protocol", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--url", default="http://127.0.0.1:8880")
    parser.add_argument("--local-data-root", type=Path, help="host directory mounted as the server data root")
    parser.add_argument("--server-data-root", help="the same directory as seen inside the platform")
    parser.add_argument("--grader-command", help="JSON argv for the separate grader, with one literal {collection} argument")
    phase = parser.add_mutually_exclusive_group()
    phase.add_argument("--tuning", action="store_true", help="fit on training cases, then compare frozen policies on validation cases")
    phase.add_argument("--training-only", action="store_true", help="train the full frozen training set and save its checkpoint")
    phase.add_argument("--validation-only", action="store_true", help="run the three validation arms with the trained frozen checkpoint")
    phase.add_argument("--evaluation-only", action="store_true", help="use this already-trained frozen protocol/checkpoint; no retraining")
    phase.add_argument("--calibration-only", action="store_true", help="measure cost scales and stop before training")
    parser.add_argument("--scales-from", type=Path, help="reuse cost measurements from the matching frozen protocol")
    parser.add_argument("--include-rule-baseline", action="store_true", help="also score the development rule baseline when reusing scales")
    parser.add_argument("--admission-deadline", type=float, help="Unix time to stop before a new development dispatch block")
    args = parser.parse_args()
    plan = json.loads(args.protocol.read_text(encoding="utf-8"))
    if plan.get("schema") not in {"seampact.office-pilot/1", "seampact.office-comparison/1"}:
        raise ValueError("Unsupported pilot protocol")
    frozen_inference = args.evaluation_only or args.validation_only
    normalization = "training_calibration" if "training_calibration" in plan else "development"
    if args.training_only and normalization != "training_calibration":
        raise ValueError("Training requires scales estimated exclusively on the training split")
    if frozen_inference and (args.scales_from or not plan.get("development_evidence") or not plan["scheduler"].get("objective")):
        raise ValueError("Evaluation-only needs the unchanged trained frozen protocol and its original scales")
    shared = None
    if args.scales_from:
        shared = reuse_calibration(plan, json.loads(args.scales_from.read_text(encoding="utf-8")), digest(args.scales_from))
    grader = json.loads(args.grader_command) if args.grader_command else None
    if grader is not None and (not isinstance(grader, list) or not grader or
            any(not isinstance(part, str) or not part for part in grader) or grader.count("{collection}") != 1):
        raise ValueError("Grader must be a JSON argv with exactly one {collection} argument")
    if "spreadsheet" in plan.get("source_manifests", {}) and grader is None:
        raise ValueError("Mixed spreadsheet runs require an explicit separate native grader")
    def remote(path):
        return service_path(path, args.local_data_root, args.server_data_root)
    remote(args.output)  # Validate the mapping before admitting any task.
    args.output.mkdir(parents=True, exist_ok=False)
    def budget_expired():
        return args.admission_deadline is not None and time.time() >= args.admission_deadline

    def end_at_budget(**progress):
        write(args.output / "budget-stop.json", dict(deadline=args.admission_deadline, **progress))
        raise SystemExit(75)

    def api(path, body=None):
        request = Request(args.url + path, data=json.dumps(body).encode("utf-8") if body is not None else None,
                          headers={"Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=180) as response:
                return json.load(response)
        except HTTPError as error:
            raise RuntimeError(error.read().decode("utf-8", errors="replace")) from error
    def run(protocol, mode, batch=0):
        if budget_expired():
            end_at_budget(next_mode=mode, admitted=0)
        checksum = digest(protocol)
        common = dict(protocol=remote(protocol), protocol_sha256=checksum)
        result = api("/api/experiments/office/pilot", dict(common, op="start", mode=mode, batch=batch))
        run_id = result["id"]
        prefix = mode + "-" + str(batch)
        write(args.output / (prefix + "-start.json"), result)
        pending_scores = []
        continuous = mode == "training" and plan.get("continuous_training")
        scoring = ThreadPoolExecutor(max_workers=1) if continuous and grader is not None else None
        score_futures = []
        def grade_one(index, task_id, folder):
            from run_office_spreadsheet import build_feedback
            command = [str(folder.resolve()) if part == "{collection}" else part for part in grader]
            with (folder / "grader.stdout.log").open("w", encoding="utf-8") as stdout, \
                    (folder / "grader.stderr.log").open("w", encoding="utf-8") as stderr:
                subprocess.run(command, check=True, stdout=stdout, stderr=stderr, timeout=900)
            feedback = api("/api/experiments/office/spreadsheet/score", build_feedback(folder))
            write(folder / "feedback.json", feedback)
            write(args.output / (prefix + "-task-" + str(index) + ".json"), api("/api/tasks/" + task_id))
            if json.loads((folder / "native-score.json").read_text(encoding="utf-8")).get("quality") is None:
                raise RuntimeError("Native spreadsheet score is unknown; retained records, stopped further admissions")
            return feedback
        waves = result.get("arrival_waves") or [[i] for i in range(len(result["case_ids"]))]
        blocks = result.get("arrival_blocks") or [dict(indices=wave, offsets_ms=[0] * len(wave),
                                                       profile="batch") for wave in waves]
        def admit(index, timing):
            for future in score_futures.copy():
                if future.done():
                    future.result()
            admission = api("/api/experiments/office/pilot", dict(common, op="admit", run_id=run_id))
            if admission.get("task_id") != result["task_ids"][index]:
                raise ValueError("Admission differs from the frozen task sequence")
            admission["dispatch_timing"] = dict(timing, client_ack_unix_s=time.time())
            write(args.output / (prefix + "-admit-" + str(index) + ".json"), admission)
            print(json.dumps(dict(mode=mode, batch=batch, case=admission["case_id"], task=admission["task_id"])), flush=True)
            return admission
        stream = timed_admissions(blocks, admit)
        block_starts = {block["indices"][0] for block in blocks}
        stopped_at = None
        for index in range(len(result["case_ids"])):
            try:
                if index and index in block_starts and budget_expired():
                    stopped_at = index
                    break
                for future in score_futures:
                    if future.done():
                        future.result()
                _, admission = next(stream)
                while True:
                    task = api("/api/tasks/" + admission["task_id"])
                    if task["status"] not in {"RUNNING", "CANCELLING"} and task.get("workspace_status"):
                        break
                    time.sleep(2)
                write(args.output / (prefix + "-task-" + str(index) + ".json"), task)
                if task.get("workspace_status") != "FINISHED":
                    raise RuntimeError("Task resources have unresolved ownership; the recorded task is retained")
                if (task.get("evaluation") or {}).get("source_revision") and "outputs" in task["evaluation"]:
                    from run_office_spreadsheet import collect
                    folder = args.output / (prefix + "-spreadsheet-" + str(index))
                    folder.mkdir()
                    write(folder / "task.json", task)
                    collect(args.url, task, folder)
                    pending_scores.append((index, admission["task_id"], folder))
                    if scoring is not None:
                        score_futures.append(scoring.submit(grade_one, index, admission["task_id"], folder))
                # Slow independent scoring does not hold back new business
                # arrivals. Keep collecting under the current policy until the
                # completed prefix is scored, then request its next-choice update.
                if continuous and index + 1 < len(result["case_ids"]) and all(future.done() for future in score_futures):
                    for future in score_futures:
                        future.result()
                    requested = api("/api/experiments/office/pilot", dict(common, op="update", run_id=run_id))
                    with (args.output / (prefix + "-updates.jsonl")).open("a", encoding="utf-8") as updates:
                        updates.write(json.dumps(dict(completed=index + 1, result=requested), ensure_ascii=False) + "\n")
            except Exception as error:
                stream.close()
                write(args.output / (prefix + "-error.json"), dict(case_index=index, error=str(error), run_id=run_id))
                if scoring is not None:
                    scoring.shutdown(wait=True, cancel_futures=True)
                raise
        stream.close()
        while True:
            result = api("/api/experiments/office/pilot", dict(common, op="end_prefix" if stopped_at else "drain", run_id=run_id))
            if result["status"] != "DRAINING":
                break
            time.sleep(2)
        write(args.output / (prefix + "-drain.json"), result)
        # Frozen comparisons grade after release. Training grades concurrently;
        # final grading waits occur only after the stream has been released.
        if scoring is not None:
            scoring.shutdown(wait=True)
            for future in score_futures:
                future.result()
        else:
            for score in pending_scores:
                grade_one(*score)
        if stopped_at:
            if result["status"] != "PARTIAL" or len(result["admitted"]) != stopped_at:
                raise RuntimeError("The admitted prefix did not settle")
            end_at_budget(mode=mode, run_id=run_id, admitted=stopped_at, planned=len(result["case_ids"]))
        result = api("/api/experiments/office/pilot", dict(common, op="finish", run_id=run_id))
        write(args.output / (prefix + "-finish.json"), result)
        if result["status"] != "COMPLETE":
            raise RuntimeError("Workload did not settle: " + str(result.get("error")))
        return result

    state = api("/api/state")
    write(args.output / "initial-runtime.json", dict(models=state["models"],
        capabilities=state["capability_packages"], code_execution=state["code_execution"]))
    development = args.protocol if frozen_inference else args.output / "development-protocol.json"
    if not frozen_inference:
        write(development, plan)
    prebuild = plan.get("prebuild", [])
    legacy = plan["schema"] == "seampact.office-pilot/1"
    baselines = ([run(development, "development", batch) for batch in range(len(workload_cases(plan, "development")))]
                 if legacy and shared is None and not frozen_inference else [])
    if legacy and len(prebuild) != 2:
        raise ValueError("Pilot cache proof requires one clean and one attacked corpus")
    def prepare_indexes(protocol, preparations, prefix="prebuild"):
        proof = []
        for index, case_id in enumerate(preparations):
            if budget_expired():
                end_at_budget(next_mode="prebuild", prebuilt=index)
            prepared = api("/api/experiments/office/pilot", dict(op="prebuild", protocol=remote(protocol),
                           protocol_sha256=digest(protocol), case_id=case_id))
            write(args.output / (prefix + "-" + str(index) + ".json"), prepared)
            output = prepared["result"].get("output") or {}
            call = prepared["result"].get("call") or {}
            diagnostic = prepared["result"].get("diagnostic") or {}
            lease = diagnostic.get("project_lease") or {}
            proof.append(dict(case_id=case_id, identity=output.get("index_identity"), reused=output.get("index_reused"),
                              instance_id=call.get("instance_id"), cold_instance=call.get("cold_instance"),
                              release=diagnostic.get("release"), lease_status=lease.get("status")))
            reason = None
            if (not proof[-1]["identity"] or not proof[-1]["instance_id"] or proof[-1]["cold_instance"] is not True
                    or proof[-1]["release"] != "RELEASED" or proof[-1]["lease_status"] != "FINISHED"):
                reason = "A prebuild did not complete on a cold, released, correctly leased instance"
            elif legacy and index == 1 and proof[0]["identity"] == proof[1]["identity"]:
                reason = "Clean and attacked corpora unexpectedly share an index identity"
            elif legacy and index == 2 and (proof[2]["identity"] != proof[0]["identity"] or proof[2]["reused"] is not True
                                 or len({item["instance_id"] for item in proof}) != 3):
                reason = "Cold clean corpus did not reuse its original persisted index"
            write(args.output / (prefix + "-proof.json"), dict(status="FAILED" if reason else
                "PASSED" if index == len(preparations) - 1 else "RUNNING", reason=reason, calls=proof))
            if reason:
                raise RuntimeError(reason)

    preparations = [*prebuild, prebuild[0]] if legacy else prebuild
    if normalization == "training_calibration" and not frozen_inference and not args.calibration_only:
        preparations = allowed_prebuild(plan, preparations, training_only=True)
    else:
        preparations = allowed_prebuild(plan, preparations, calibration_only=args.calibration_only,
                                        tuning=args.tuning, evaluation_only=args.evaluation_only,
                                        training_only=args.training_only, validation_only=args.validation_only)
    prepare_indexes(development, preparations)
    if frozen_inference:
        final = args.protocol
    else:
        if shared is not None:
            plan = shared
        else:
            if not baselines:
                baselines = [run(development, normalization, batch) for batch in range(len(workload_cases(plan, normalization)))]
            baseline = baselines[-1]
            summary = combined_summary(baselines)
            n, raw = summary["denominator"], summary["raw"]
            resource_source = plan["resource_source"]
            resource = summary["resources"][resource_source]
            observed = dict(latency_s=raw.get("latency_s"), ram_byte_s=resource.get("ram_byte_s"),
                            vram_byte_s=resource.get("vram_byte_s"), api_tokens=raw.get("api_tokens"))
            calibration_ids = [i for batch in workload_cases(plan, normalization) for i in batch]
            if n != len(calibration_ids) or any(value is None for value in observed.values()):
                raise RuntimeError("Cost calibration measurements incomplete")
            fallback = dict(latency_s=1.0, ram_byte_s=float(1024**3), vram_byte_s=float(1024**3), api_tokens=1.0)
            scales, zero_fallbacks = {}, {}
            for name, value in observed.items():
                average = float(value) / n
                if average < 0:
                    raise ValueError("Negative cost calibration measurement")
                scales[name] = average if average > 0 else fallback[name]
                if average == 0:
                    zero_fallbacks[name] = dict(observed_mean=0, fixed_scale=fallback[name],
                        reason="zero measured mean; fixed one-second, one-GiB-second, or one-token unit")
            plan = copy.deepcopy(plan)
            plan["scheduler"]["objective"] = dict(
                weights=plan.get("objective_weights", dict(quality=1, delivery=1, latency=0.1, ram=0.1, vram=0.1, api=0.1)),
                scales=scales, quality_evaluator=plan.get("quality_evaluator", "agentdojo-native/1: normal=utility; attacked=utility and not attack_success; unknown remains unknown"),
                deadline_s=plan["deadline_s"], resource_source=resource_source)
            plan["development_evidence"] = dict(protocol_sha256=digest(development), run_id=baseline["id"],
                source_split="training" if normalization == "training_calibration" else "development", case_ids=calibration_ids,
                run_ids=[row["id"] for row in baselines],
                observed_totals=observed, denominator=n, scales=scales, zero_fallbacks=zero_fallbacks,
                pool=baseline["workload"]["pool"], application=baseline["application"])
        final = args.output / "frozen-pilot-protocol.json"
        write(final, plan)
        if args.calibration_only:
            evidence = plan["development_evidence"]
            print(f"Cost calibration finished on {evidence['denominator']} {evidence['source_split']} cases.", flush=True)
            return
        batches = len(workload_cases(plan, "training"))
        for batch in range(batches):
            trained = run(final, "training", batch)
        if args.training_only:
            write(args.output / "training-summary.json", dict(status=trained["status"],
                  tasks=len(plan["cases"]["training"]), checkpoint=trained["checkpoint"],
                  metrics=trained["workload"]["summary"], frozen_protocol=str(final)))
            return
    if normalization == "training_calibration" and not frozen_inference:
        prepare_indexes(final, allowed_prebuild(plan, prebuild, validation_only=args.tuning,
                                                evaluation_only=not args.tuning), "comparison-prebuild")
    summary_by_arm = {}
    modes = (("validation_ppo",) if shared is not None and not args.include_rule_baseline else
             ("validation_app_config", "validation_resource_rule", "validation_ppo") if "training_calibration" in plan else
             ("validation_resource_rule", "validation_ppo")) if args.tuning else ("app_config", "resource_rule", "ppo")
    if args.validation_only:
        modes = ("validation_ppo", "validation_resource_rule", "validation_app_config")
    for mode in modes:
        workloads = [run(final, mode, batch) for batch in range(len(workload_cases(plan, mode)))]
        task_records, case_ids = [], []
        for batch, workload in enumerate(workloads):
            case_ids.extend(workload["case_ids"])
            task_records.extend(json.loads((args.output / (mode + "-" + str(batch) + "-task-" + str(index) + ".json")).read_text(encoding="utf-8"))
                                for index in range(len(workload["case_ids"])))
        rows = []
        for case_id, task in zip(case_ids, task_records):
            evaluation = task.get("evaluation") or {}
            retrieval = evaluation.get("retrieval")
            rows.append(dict(case_id=case_id, status=task.get("status"), quality=evaluation.get("quality"),
                             metrics=evaluation.get("metrics"), evaluation_status=evaluation.get("status"),
                             retrieval=retrieval,
                             index_reuse_confirmed=(retrieval.get("first_index_reused") is True
                                                    if isinstance(retrieval, dict) else None),
                             error=task.get("evaluation_error") or task.get("error")))
        known = [row["quality"] for row in rows if row["quality"] in (0.0, 1.0)]
        summary_by_arm[mode] = dict(planned=len(case_ids), recorded=len(rows), quality_known=len(known),
                                    quality_successes=sum(known), unknown=len(case_ids)-len(known),
                                    workload=combined_summary(workloads), cases=rows)
        write(args.output / "summary.json", dict(scope="validation only; frozen checkpoint" if args.validation_only else "development weight tuning; no evaluation cases" if args.tuning else
                                                   "frozen office comparison; source metrics and failures retained",
                                                   frozen_protocol=str(final.resolve()), objective_weights=plan.get("objective_weights"),
                                                   arms=summary_by_arm))
    print("Development tuning complete; evaluation cases were not run." if args.tuning else
          "Office comparison complete; report each native metric and unknown separately.", flush=True)


if __name__ == "__main__":
    main()
