"""Run the explicitly frozen small office pilot and preserve every result.

The protocol is a private JSON file, with cases, model bindings and execution
configuration selected before running. Comparison training uses training-only scales;
unknown measurements or scores stop PPO training instead of being imputed.
"""
import argparse
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import closing
import copy
import hashlib
import http.client
import json
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import uuid
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from seampact.office_pilot import workload_cases


MODEL_WAIT_TIMEOUT_S = 300.0
MODEL_WAIT_POLL_S = 2.0
MODEL_PROBE_TIMEOUT_S = 3.0


class UnixHTTPConnection(http.client.HTTPConnection):
    """Minimal HTTP client for a model service bound to a Unix socket."""
    def __init__(self, socket_path, timeout):
        super().__init__("localhost", timeout=timeout)
        self.socket_path = str(socket_path)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)


def probe_model_socket(socket_path, timeout):
    try:
        with closing(UnixHTTPConnection(socket_path, timeout)) as connection:
            connection.request("GET", "/health")
            response = connection.getresponse()
            status = response.status
            response.read(4096)
        return dict(socket=str(socket_path), ready=status == 200, http_status=status)
    except (OSError, http.client.HTTPException, TimeoutError, ValueError):
        return dict(socket=str(socket_path), ready=False, reason="unavailable")


def wait_for_model_sockets(sockets, *, log_path, block_start, admission_deadline):
    """Wait for every configured endpoint, never starting or restarting a service."""
    started = time.time()
    # Honor the run's existing admission budget in full. Without one, bound a
    # standalone wait by the same five-minute startup window used by the model owner.
    wait_deadline = admission_deadline if admission_deadline is not None else started + MODEL_WAIT_TIMEOUT_S

    def observe():
        rows = []
        for path in sockets:
            remaining = wait_deadline - time.time()
            if remaining <= 0:
                rows.append(dict(socket=str(path), ready=False, reason="not_checked_before_deadline"))
            else:
                rows.append(probe_model_socket(path, min(MODEL_PROBE_TIMEOUT_S, remaining)))
        return rows

    def record(event, **fields):
        with log_path.open("a", encoding="utf-8", buffering=1) as stream:
            stream.write(json.dumps(dict(event=event, at_unix_s=time.time(),
                block_start_index=block_start, sockets=[str(path) for path in sockets], **fields),
                ensure_ascii=False) + "\n")

    statuses = observe()
    if all(row["ready"] for row in statuses):
        status = ("admission_deadline" if admission_deadline is not None
                  and time.time() >= admission_deadline else "ready")
        record("ready", status=status, wait_s=0.0, probes=statuses, restart_performed=False)
        return dict(status=status, wait_s=0.0, probes=statuses)

    record("wait_start", probes=statuses, restart_performed=False)
    while True:
        now = time.time()
        if admission_deadline is not None and now >= admission_deadline:
            status = "admission_deadline"
            break
        if now >= wait_deadline:
            status = "timeout"
            break
        time.sleep(min(MODEL_WAIT_POLL_S, wait_deadline - now))
        statuses = observe()
        if all(row["ready"] for row in statuses):
            status = ("admission_deadline" if admission_deadline is not None
                      and time.time() >= admission_deadline else "ready")
            break

    waited = max(0.0, time.time() - started)
    record("wait_end", status=status, wait_s=waited, probes=statuses,
           recovery_required=status == "timeout", restart_performed=False)
    return dict(status=status, wait_s=waited, probes=statuses,
                recovery_required=status == "timeout", restart_performed=False)


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
        for key in ("objective_weights", "reward_sweep", "development_evidence", "parent_transfer"):
            value.pop(key, None)
        value["scheduler"]["objective"] = None
        return value
    if comparable(plan) != comparable(previous):
        raise ValueError("Shared calibration requires the same data, order, seed, capabilities and runtime settings")
    objective, evidence = previous["scheduler"].get("objective"), previous.get("development_evidence")
    origin = (evidence or {}).get("objective_scale_source") or {}
    matching_scales = objective and (objective["scales"] == (evidence or {}).get("scales")
        or origin.get("kind") == "completed_parent_objective" and origin.get("scales") == objective["scales"])
    if not objective or not evidence or not matching_scales or not evidence.get("pool"):
        raise ValueError("Shared calibration needs a real frozen development result")
    result = copy.deepcopy(plan)
    result["scheduler"]["objective"] = dict(copy.deepcopy(objective), weights=copy.deepcopy(plan["objective_weights"]))
    result["development_evidence"] = dict(copy.deepcopy(evidence), reused_from_protocol_sha256=previous_sha)
    return result


def missing_api_usage(tasks, case_ids, start_index=0):
    """Inspect finalized objective ledgers; unknown is never treated as zero."""
    missing = []
    for index, task in enumerate(tasks, start_index):
        objective = task.get("objective_result") or {}
        if (task.get("status") in {"RUNNING", "CANCELLING"}
                or task.get("workspace_status") != "FINISHED"
                or objective.get("quality") is None
                or "api_tokens" not in (objective.get("metrics") or {})):
            raise RuntimeError("Task %s has no finalized objective ledger" % index)
        if objective["metrics"]["api_tokens"] is None:
            usage = task.get("api_usage") or {}
            missing.append(dict(index=index, case_id=case_ids[index], task_id=task["id"],
                unknown_requests=usage.get("unknown_requests"),
                unreported_capability_calls=usage.get("unreported_capability_calls"),
                observed_tokens=usage.get("observed_tokens")))
    return missing


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
    parser.add_argument("--reuse-parent-objective", type=Path,
                        help="for full parent transfer only: verify the parent protocol and retain its objective and scales")
    parser.add_argument("--include-rule-baseline", action="store_true", help="also score the development rule baseline when reusing scales")
    parser.add_argument("--admission-deadline", type=float, help="Unix time to finish at the next dispatch block boundary")
    parser.add_argument("--wait-model-socket", type=Path, action="append", metavar="SOCKET",
                        help="repeatable Unix socket whose /health endpoint must be ready before each training dispatch block")
    parser.add_argument("--resume-training", action="store_true", help="resume a safely paused training protocol into a new output directory")
    parser.add_argument("--attach-training", metavar="RUN_ID", help="continue an existing live continuous training run after its admitted prefix is settled")
    parser.add_argument("--stop-on-missing-api-usage", action="store_true",
                        help="stop continuous training at a completed dispatch block if API accounting is missing")
    args = parser.parse_args()
    plan = json.loads(args.protocol.read_text(encoding="utf-8"))
    if plan.get("schema") not in {"seampact.office-pilot/1", "seampact.office-comparison/1"}:
        raise ValueError("Unsupported pilot protocol")
    parent_objective = None
    if args.reuse_parent_objective:
        parent = plan.get("parent_transfer") or {}
        if (not args.training_only or args.resume_training or args.attach_training or args.scales_from
                or parent.get("mode") != "full"):
            raise ValueError("Parent objective reuse is limited to a new full-transfer training run")
        if digest(args.reuse_parent_objective) != parent.get("protocol_sha256"):
            raise ValueError("Parent objective protocol checksum differs")
        from seampact.scheduling.unified_objective import freeze_objective
        parent_plan = json.loads(args.reuse_parent_objective.read_text(encoding="utf-8"))
        parent_objective = freeze_objective(parent_plan["scheduler"]["objective"])
        target_objective = freeze_objective(plan["scheduler"].get("objective"))
        if (parent_objective is None or target_objective != parent_objective
                or plan.get("objective_weights") != parent_objective["weights"]
                or plan.get("quality_evaluator") != parent_objective["quality_evaluator"]
                or plan.get("deadline_s") != parent_objective["deadline_s"]
                or plan.get("resource_source") != parent_objective["resource_source"]):
            raise ValueError("Full transfer requires the explicitly unchanged parent objective")
    frozen_inference = args.evaluation_only or args.validation_only
    if args.resume_training and (not args.training_only or args.scales_from or not plan.get("development_evidence")
                                 or not plan.get("continuous_training")):
        raise ValueError("Resume needs --training-only and the original frozen training protocol")
    if args.attach_training and (not args.training_only or args.resume_training or args.scales_from
                                 or not plan.get("development_evidence") or not plan.get("continuous_training")):
        raise ValueError("Attach needs --training-only, the original frozen protocol, and no resume or scale reuse")
    if args.wait_model_socket and (not args.training_only or not plan.get("continuous_training")):
        raise ValueError("--wait-model-socket requires --training-only with continuous training")
    if args.wait_model_socket:
        args.wait_model_socket = list(dict.fromkeys(args.wait_model_socket))
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
    if args.attach_training:
        # A separate attempt directory keeps every original receipt and error.
        args.output.mkdir(parents=True, exist_ok=True)
        args.output = args.output / ("attach-" + uuid.uuid4().hex)
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
            end_at_budget(next_mode=mode, admitted=0, stop_reason="admission_deadline")
        checksum = digest(protocol)
        common = dict(protocol=remote(protocol), protocol_sha256=checksum)
        if args.attach_training:
            result = api("/api/experiments/office/pilot", dict(common, op="record", run_id=args.attach_training))
            batch = result["batch"]
        else:
            result = None
        prefix = mode + "-" + str(batch)
        model_wait_log = args.output / (prefix + "-model-waits.jsonl")

        def wait_models(block_start):
            return wait_for_model_sockets(args.wait_model_socket, log_path=model_wait_log,
                block_start=block_start, admission_deadline=args.admission_deadline)

        if args.wait_model_socket and not args.attach_training:
            # Gate before creating/resuming the server-side workload so an
            # unavailable service cannot leave an empty RUNNING segment.
            waiting = wait_models(0)
            if waiting["status"] != "ready":
                reason = ("admission_deadline" if waiting["status"] == "admission_deadline"
                          else "model_unavailable_timeout")
                write(args.output / "model-wait-stop.json", dict(mode=mode, batch=batch, run_id=None,
                    next_index=0, stop_reason=reason, recovery_required=waiting.get("recovery_required", False),
                    restart_performed=False, model_wait=waiting))
                end_at_budget(next_mode=mode, admitted=0, next_index=0, stop_reason=reason,
                    recovery_required=waiting.get("recovery_required", False), restart_performed=False,
                    model_wait=waiting)
            if budget_expired():
                end_at_budget(next_mode=mode, admitted=0, next_index=0, stop_reason="admission_deadline")

        if result is None:
            result = api("/api/experiments/office/pilot", dict(common,
                op="resume" if mode == "training" and args.resume_training else "start", mode=mode, batch=batch))
        run_id = result["id"]
        write(args.output / (prefix + "-start.json"), result)
        def confirm_workspace(task_id, index):
            from office_task_workspace import ensure_workspace_finished
            def evidence(event):
                with (args.output / (prefix + "-workspace-" + str(index) + ".jsonl")).open("a", encoding="utf-8") as out:
                    out.write(json.dumps(event, ensure_ascii=False) + "\n")
            return ensure_workspace_finished(api, task_id, evidence=evidence)
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
        start_index = len(result["admitted"]) if args.attach_training else 0
        stopped_at = None
        stop_reason = None
        model_wait_stop = None
        api_gap = []
        checked_through = 0
        check_api = bool(args.stop_on_missing_api_usage and args.training_only and continuous
                         and (plan.get("scheduler", {}).get("objective") or {}).get("weights", {}).get("api", 0) > 0)
        def inspect_api(start, end):
            for future in score_futures:
                future.result()
            tasks = []
            for n in range(start, end):
                task = api("/api/tasks/" + result["task_ids"][n])
                tasks.append(task)
                write(args.output / (prefix + "-task-" + str(n) + ".json"), task)
            return missing_api_usage(tasks, result["case_ids"], start)
        if args.attach_training:
            workload = result.get("workload") or {}
            original_cases = workload_cases(plan, "training")[batch]
            expected = original_cases[result.get("start_index", 0):]
            if (result.get("id") != args.attach_training or result.get("protocol_sha256") != checksum
                    or result.get("mode") != "training" or result.get("status") != "RUNNING"
                    or result["case_ids"] != expected or len(result["task_ids"]) != len(expected)
                    or len(set(result["task_ids"])) != len(expected)
                    or result["admitted"] != result["task_ids"][:start_index]
                    or not workload.get("training") or not workload.get("continuous")
                    or workload.get("id") != result["id"] or workload.get("task_ids") != result["task_ids"]):
                raise ValueError("Existing run does not match the frozen continuous training sequence")
            flat = [index for block in blocks for index in block["indices"]]
            if flat != list(range(len(expected))) or start_index not in {0, len(expected), *(b["indices"][0] for b in blocks)}:
                raise ValueError("Existing admissions are not a complete frozen dispatch block prefix")
            state = api("/api/state")
            live = state["scheduler"]["execution"]
            from seampact.scheduling.capability_scheduler import freeze_config
            frozen_config = copy.deepcopy(plan["scheduler"])
            frozen_config["policy"] = copy.deepcopy(plan["policies"]["app_config"])
            frozen_config = freeze_config(frozen_config, verify_policy=False)
            pool = {group["id"]: dict(package_digest=group["package_digest"],
                        contract_digest=group["contract"]["digest"], installation_id=group["installation_id"])
                    for group in state["runtime_groups"]
                    if group.get("kind") == "capability-package" and not group.get("detached")}
            objective = live["objective"]
            raw = objective["raw"]
            if (live.get("session") != workload.get("session") or live.get("config") != frozen_config
                    or live.get("policy_config_id") != workload.get("policy_config_id")
                    or live.get("policy") != workload.get("policy") or live.get("policy_error")
                    or pool != workload.get("pool") or pool != plan["development_evidence"].get("pool")
                    or objective.get("denominator") != start_index or raw.get("finished") != start_index
                    or raw.get("quality_known") != start_index or live.get("active_tasks")
                    or live.get("queued_calls") or live.get("running_calls")):
                raise ValueError("Live scheduler, frozen pool, or settled task ledger does not match the run")
            for index, task_id in enumerate(result["admitted"]):
                task = api("/api/tasks/" + task_id)
                if task.get("status") not in {"RUNNING", "CANCELLING"} and task.get("workspace_status") != "FINISHED":
                    task = confirm_workspace(task_id, index)
                settled = task.get("objective_result") or {}
                if (task.get("id") != task_id or task.get("status") in {"RUNNING", "CANCELLING"}
                        or task.get("workspace_status") != "FINISHED" or settled.get("session") != live["session"]
                        or settled.get("config_id") != live.get("config_id") or settled.get("quality") is None):
                    raise ValueError("Admitted task %s is not fully released and scored; settle the prefix first" % index)
            latest = api("/api/experiments/office/pilot", dict(common, op="record", run_id=run_id))
            if latest.get("status") != "RUNNING" or latest.get("admitted") != result["admitted"]:
                raise ValueError("Admissions changed during attach; stop other dispatchers and retry")
            blocks = [block for block in blocks if block["indices"][0] >= start_index]
            write(args.output / "attach-preflight.json", dict(run_id=run_id, admitted=start_index,
                  session=live["session"], config_id=live["config_id"], pool=pool))
            if check_api and start_index:
                api_gap = inspect_api(0, start_index)
                checked_through = start_index
                if api_gap and start_index < len(result["case_ids"]):
                    stopped_at = start_index
                    stop_reason = "missing_api_usage"
            if args.wait_model_socket and not api_gap:
                waiting = wait_models(start_index)
                if waiting["status"] != "ready":
                    stop_reason = ("admission_deadline" if waiting["status"] == "admission_deadline"
                                   else "model_unavailable_timeout")
                    model_wait_stop = dict(mode=mode, batch=batch, run_id=run_id, next_index=start_index,
                        admitted=start_index, stop_reason=stop_reason,
                        recovery_required=waiting.get("recovery_required", False),
                        restart_performed=False, model_wait=waiting)
                    if start_index:
                        stopped_at = start_index
                    else:
                        write(args.output / "model-wait-stop.json", model_wait_stop)
                        end_at_budget(mode=mode, batch=batch, run_id=run_id, admitted=0, next_index=0,
                            stop_reason=stop_reason, recovery_required=model_wait_stop["recovery_required"],
                            restart_performed=False, model_wait=waiting)
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
        update_after = plan.get("update_after_tasks", 16)
        last_requested = 0
        for index in range(start_index, len(result["case_ids"])):
            try:
                if stopped_at is not None:
                    break
                if check_api and index in block_starts and index > checked_through:
                    api_gap = inspect_api(checked_through, index)
                    checked_through = index
                    if api_gap:
                        stopped_at = index
                        stop_reason = "missing_api_usage"
                        break
                if (args.wait_model_socket and index in block_starts and index != start_index):
                    # This gate is before timed_admissions starts the block, so
                    # the frozen offsets inside each block remain unchanged.
                    waiting = wait_models(index)
                    if waiting["status"] != "ready":
                        stopped_at = index
                        stop_reason = ("admission_deadline" if waiting["status"] == "admission_deadline"
                                       else "model_unavailable_timeout")
                        model_wait_stop = dict(mode=mode, batch=batch, run_id=run_id, next_index=index,
                            admitted=index, stop_reason=stop_reason,
                            recovery_required=waiting.get("recovery_required", False),
                            restart_performed=False, model_wait=waiting)
                        break
                if continuous and index in block_starts and index - last_requested >= update_after:
                    # All business in the previous block has finished. Wait for its
                    # native scores before the generator starts the next block's clock.
                    for future in score_futures:
                        future.result()
                    requested = api("/api/experiments/office/pilot", dict(common, op="update", run_id=run_id))
                    with (args.output / (prefix + "-updates.jsonl")).open("a", encoding="utf-8") as updates:
                        updates.write(json.dumps(dict(completed=index, result=requested), ensure_ascii=False) + "\n")
                    if requested.get("pending"):
                        last_requested = index
                    if budget_expired():
                        stopped_at = index
                        break
                for future in score_futures:
                    if future.done():
                        future.result()
                if index in block_starts and budget_expired():
                    stop_reason = "admission_deadline"
                    if index == 0 and start_index == 0:
                        end_at_budget(mode=mode, batch=batch, run_id=run_id, admitted=0, next_index=0,
                            stop_reason=stop_reason, empty_prefix=True)
                    stopped_at = index
                    break
                _, admission = next(stream)
                while True:
                    task = api("/api/tasks/" + admission["task_id"])
                    if task["status"] not in {"RUNNING", "CANCELLING"} and task.get("workspace_status"):
                        break
                    time.sleep(2)
                write(args.output / (prefix + "-task-" + str(index) + ".json"), task)
                if task.get("workspace_status") != "FINISHED":
                    task = confirm_workspace(admission["task_id"], index)
                    write(args.output / (prefix + "-task-" + str(index) + ".json"), task)
                if (task.get("evaluation") or {}).get("source_revision") and "outputs" in task["evaluation"]:
                    from run_office_spreadsheet import collect
                    folder = args.output / (prefix + "-spreadsheet-" + str(index))
                    folder.mkdir()
                    write(folder / "task.json", task)
                    collect(args.url, task, folder)
                    pending_scores.append((index, admission["task_id"], folder))
                    if scoring is not None:
                        score_futures.append(scoring.submit(grade_one, index, admission["task_id"], folder))
            except Exception as error:
                stream.close()
                write(args.output / (prefix + "-error.json"), dict(case_index=index, error=str(error), run_id=run_id))
                if scoring is not None:
                    scoring.shutdown(wait=True)
                raise
        stream.close()
        if check_api and stopped_at is None and checked_through < len(result["case_ids"]):
            api_gap = inspect_api(checked_through, len(result["case_ids"]))
        if stopped_at and continuous and scoring is not None:
            scoring.shutdown(wait=True)
            for future in score_futures:
                future.result()
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
        if api_gap and stopped_at:
            receipt = dict(status=result.get("status"), run_id=run_id, reason="missing_api_usage",
                admitted=len(result.get("admitted", [])),
                next_index=result.get("start_index", 0) + len(result.get("admitted", [])),
                affected_cases=api_gap, pilot_error=result.get("error"))
            write(args.output / (prefix + "-missing-api-usage.json"), receipt)
            if result.get("status") != "UNSETTLED":
                raise RuntimeError("Missing API usage detected, but prefix did not settle as UNSETTLED")
            raise RuntimeError("Training stopped: finalized API token accounting is missing")
        if stopped_at:
            if model_wait_stop:
                model_wait_stop.update(pilot_status=result.get("status"), admitted=len(result.get("admitted", [])))
                write(args.output / "model-wait-stop.json", model_wait_stop)
            if result["status"] != ("PAUSED" if continuous else "PARTIAL") or len(result["admitted"]) != stopped_at:
                raise RuntimeError("The admitted prefix did not settle")
            end_at_budget(mode=mode, run_id=run_id, admitted=stopped_at, next_index=result.get("next_index"),
                          checkpoint=result.get("checkpoint"), frozen_protocol=str(protocol),
                          planned=len(plan["cases"]["training"]) if continuous else len(result["case_ids"]),
                          stop_reason=stop_reason or "admission_deadline",
                          recovery_required=bool(model_wait_stop and model_wait_stop.get("recovery_required")),
                          restart_performed=False if model_wait_stop else None,
                          model_wait=model_wait_stop.get("model_wait") if model_wait_stop else None)
        result = api("/api/experiments/office/pilot", dict(common, op="finish", run_id=run_id))
        write(args.output / (prefix + "-finish.json"), result)
        if api_gap:
            write(args.output / (prefix + "-missing-api-usage.json"), dict(
                status=result.get("status"), run_id=run_id, reason="missing_api_usage",
                admitted=len(result.get("admitted", [])),
                next_index=result.get("start_index", 0) + len(result.get("admitted", [])),
                affected_cases=api_gap, pilot_error=result.get("error")))
            raise RuntimeError("Training stopped: finalized API token accounting is missing")
        if result["status"] != "COMPLETE":
            raise RuntimeError("Workload did not settle: " + str(result.get("error")))
        return result

    if args.attach_training:
        trained = run(args.protocol, "training")
        segments = [api("/api/experiments/office/pilot", dict(op="record", protocol=remote(args.protocol),
                    protocol_sha256=digest(args.protocol), run_id=identity)) for identity in trained.get("previous_segments", [])]
        observed = [case for part in [*segments, trained] for case in part["case_ids"][:len(part["admitted"])]]
        if observed != plan["cases"]["training"]:
            raise RuntimeError("Training records do not cover the frozen sequence exactly once")
        write(args.output / "training-summary.json", dict(status=trained["status"], tasks=len(observed),
              checkpoint=trained["checkpoint"], metrics=combined_summary([*segments, trained]) if segments else trained["workload"]["summary"],
              segment_ids=[part["id"] for part in [*segments, trained]], frozen_protocol=str(args.protocol)))
        return

    state = api("/api/state")
    write(args.output / "initial-runtime.json", dict(models=state["models"],
        capabilities=state["capability_packages"], code_execution=state["code_execution"]))
    development = args.protocol if frozen_inference else args.output / "development-protocol.json"
    if not frozen_inference and not args.resume_training:
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
    if not args.resume_training:
        prepare_indexes(development, preparations)
    if frozen_inference:
        final = args.protocol
    else:
        if args.resume_training:
            pass  # The original protocol already contains the measured training-only scales.
        elif parent_objective is not None:
            # Reuse the parent's already completed calibration evidence; this
            # does not dispatch its historical cases in the new experiment.
            evidence = copy.deepcopy(parent_plan.get("development_evidence") or {})
            if (evidence.get("source_split") != "training" or not evidence.get("run_ids")
                    or not evidence.get("pool") or evidence.get("scales") != parent_objective["scales"]):
                raise ValueError("Completed parent lacks matching training calibration evidence")
            plan = copy.deepcopy(plan)
            plan["scheduler"]["objective"] = copy.deepcopy(parent_objective)
            evidence["objective_scale_source"] = dict(kind="completed_parent_objective",
                protocol=parent["protocol"], protocol_sha256=parent["protocol_sha256"],
                scales=copy.deepcopy(parent_objective["scales"]))
            plan["development_evidence"] = evidence
        elif shared is not None:
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
        final = args.protocol if args.resume_training else args.output / "frozen-pilot-protocol.json"
        if not args.resume_training:
            write(final, plan)
        if args.calibration_only:
            evidence = plan["development_evidence"]
            print(f"Cost calibration finished on {evidence['denominator']} {evidence['source_split']} cases.", flush=True)
            return
        batches = len(workload_cases(plan, "training"))
        for batch in range(batches):
            trained = run(final, "training", batch)
        if args.training_only:
            segments = [api("/api/experiments/office/pilot", dict(op="record", protocol=remote(final),
                protocol_sha256=digest(final), run_id=run_id)) for run_id in trained.get("previous_segments", [])]
            observed = [case for part in [*segments, trained] for case in part["case_ids"][:len(part["admitted"])]]
            if plan.get("continuous_training") and observed != plan["cases"]["training"]:
                raise RuntimeError("Training records do not cover the frozen sequence exactly once")
            write(args.output / "training-summary.json", dict(status=trained["status"],
                  tasks=len(plan["cases"]["training"]), checkpoint=trained["checkpoint"],
                  metrics=combined_summary([*segments, trained]) if segments else trained["workload"]["summary"],
                  segment_ids=[part["id"] for part in [*segments, trained]], frozen_protocol=str(final)))
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
