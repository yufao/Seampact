"""Prepare, explicitly dispatch, collect, or separately grade SpreadsheetBench.

prepare copies verified input workbooks and public task metadata only. It
never imports answers, runs generated Python, or invokes a model. run starts
one approved case on an existing service. grade uses the existing pinned
scoring wrapper on final delivered artifacts in an independent grading side.
"""

import argparse
import hashlib
import json
from pathlib import Path
import sys
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from seampact.office_spreadsheet import load_case, SCHEMA, FEEDBACK_SCHEMA, SOURCE_REVISION


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def write(path, value):
    raw = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    temporary = path.with_name(path.name + ".partial")
    try:
        temporary.write_bytes(raw)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return sha(raw)


def api(base, path, data=None, binary=False):
    request = Request(base.rstrip("/") + path, data=None if data is None else json.dumps(data).encode("utf-8"),
                      headers={"Content-Type": "application/json"})
    with urlopen(request, timeout=90) as response:
        return response.read() if binary else json.load(response)


def prepare(manifest, output):
    raw = manifest.read_bytes()
    identity = sha(raw)
    digest_path = manifest.with_suffix(".json.sha256")
    if digest_path.read_text(encoding="ascii").split()[0] != identity:
        raise ValueError("Original frozen manifest identity changed")
    subset = json.loads(raw.decode("utf-8-sig"))
    runtime_cases = []
    output.mkdir(parents=True, exist_ok=False)
    for original in subset["cases"]:
        case, inputs = load_case(manifest, identity, original["id"])
        files = []
        folder = output / "workbooks" / str(case["source_id"])
        folder.mkdir(parents=True)
        for record in inputs:
            path = folder / record["name"]
            path.write_bytes(record["data"])
            files.append(dict(path=path.relative_to(output).as_posix(), role="input", sha256=record["sha256"]))
        public = {key: case[key] for key in ("id", "source_id", "family_id", "split", "instruction",
                                           "instruction_type", "answer_position")}
        public.update(files=files, readiness="runtime_adapter_prepared_not_run")
        runtime_cases.append(public)
    runtime_manifest = dict(schema=subset["schema"], data_source=subset["data_source"],
        scoring_source=subset["scoring_source"], source_manifest_sha256=identity, cases=runtime_cases,
        runtime=dict(schema=SCHEMA, entry="/api/experiments/office/spreadsheet", inputs_only=True,
                     answer_workbooks_included=False, first_input_visible_during_development=True,
                     held_inputs_staged_after_solution_frozen=True, same_solution_bytes_for_all_replays=True,
                     native_scoring="separate score_office_spreadsheet.py after formula recalculation",
                     deployment_status="route/module must be present on the target service; Python image must include openpyxl"))
    runtime_sha = write(output / "runtime-manifest.json", runtime_manifest)
    write(output / "preparation.json", dict(schema=SCHEMA, source_manifest_sha256=identity,
        runtime_manifest_sha256=runtime_sha, original_question_count=len(runtime_cases),
        input_workbook_count=sum(len(case["files"]) for case in runtime_cases), answer_workbook_count=0,
        model_calls=0, case_runs=0, status="PREPARED_NOT_RUN",
        required_before_approved_run=["target service contains spreadsheet entry", "fixed isolated Python image includes openpyxl",
                                      "explicit approval of the evaluation set and fixed runtime configuration"],
        required_for_separate_grading=["original frozen private subset and answer files on grading side",
                                       "pinned official scorer checkout", "working LibreOffice formula recalculation"]))
    return dict(prepared_questions=len(runtime_cases), input_workbooks=sum(len(case["files"]) for case in runtime_cases),
                answers_copied=0, runtime_manifest_sha256=runtime_sha, status="PREPARED_NOT_RUN")


def run(args):
    if args.output.exists():
        raise ValueError("Use a new collection directory; existing run evidence is preserved")
    state = api(args.url, "/api/state")
    if not state.get("code_execution", {}).get("configured"):
        raise ValueError("The service has no fixed isolated Python execution image configured")
    raw = args.manifest.read_bytes()
    obj = json.loads(raw.decode("utf-8-sig"))
    if not obj.get("runtime", {}).get("inputs_only"):
        raise ValueError("Dispatch the prepared inputs-only runtime manifest, not the private grading subset")
    if not any(case["id"] == args.case_id for case in obj["cases"]):
        raise ValueError("Requested case is absent from the prepared manifest")
    args.output.mkdir(parents=True)
    write(args.output / "frozen-runtime.json", dict(schema=SCHEMA, manifest_sha256=sha(raw), case_id=args.case_id,
        original_manifest_sha256=obj["source_manifest_sha256"],
        models=state.get("models"), scheduler=state.get("scheduler", {}).get("execution", {}),
        code_execution=state.get("code_execution"), capability_packages=state.get("capability_packages"),
        project_id=args.project_id, runtime_path="common ChatJobs/Pi/project/isolated Python; unchanged frozen three-case replay"))
    try:
        admission = api(args.url, "/api/experiments/office/spreadsheet", dict(manifest=args.server_manifest,
            manifest_sha256=sha(raw), case_id=args.case_id, project_id=args.project_id))
    except (HTTPError, URLError, TimeoutError) as error:
        record = dict(schema=SCHEMA, case_id=args.case_id, status="ADMISSION_FAILED", planned=1, admitted=0,
                      error_type=type(error).__name__, http_status=getattr(error, "code", None),
                      native_scores=None, reason="The planned case remains in the manifest; no task score was invented")
        write(args.output / "admission.json", record)
        write(args.output / "collection.json", record)
        return record
    write(args.output / "admission.json", admission)
    deadline = time.monotonic() + args.wait_seconds
    while True:
        task = api(args.url, "/api/tasks/" + admission["task_id"])
        if task.get("status") not in {"RUNNING", "CANCELLING"} and task.get("workspace_status"):
            break
        if time.monotonic() >= deadline:
            write(args.output / "task.json", task)
            write(args.output / "collection.json", dict(schema=SCHEMA, task_id=admission["task_id"], case_id=args.case_id,
                status="COLLECTION_TIMEOUT", planned=1, admitted=1, server_task_status=task.get("status"),
                native_scores=None, reason="The task was not resubmitted or implicitly stopped"))
            return dict(status="COLLECTION_TIMEOUT", task_id=admission["task_id"])
        time.sleep(2)
    write(args.output / "task.json", task)
    return collect(args.url, task, args.output)


def collect(base, task, output):
    delivered = {item["sha256"]: item for item in task.get("files", []) if isinstance(item.get("sha256"), str)}
    evaluation = task.get("evaluation") or {}
    if evaluation.get("source_revision") is None or evaluation.get("outputs") is None:
        raise ValueError("Task has no frozen spreadsheet replay evaluation record")
    records = []
    artifacts = output / "delivered"
    artifacts.mkdir(exist_ok=True)
    solution = evaluation.get("solution_sha256")
    if evaluation.get("delivered_solution") and solution in delivered:
        data = api(base, "/api/artifacts/" + solution, binary=True)
        if sha(data) != solution:
            raise ValueError("Delivered solution digest mismatch")
        (artifacts / "solution.py").write_bytes(data)
    for item in evaluation["outputs"]:
        index, identity = item["index"], item.get("sha256")
        record = dict(index=index, sha256=identity, status="MISSING_DELIVERY", path=None)
        if identity is not None and identity in delivered:
            data = api(base, "/api/artifacts/" + identity, binary=True)
            if sha(data) != identity:
                raise ValueError("Delivered workbook digest mismatch")
            path = artifacts / ("replay-" + str(index) + ".xlsx")
            path.write_bytes(data)
            record.update(status="DELIVERED", path=path.relative_to(output).as_posix())
        records.append(record)
    result = dict(schema=SCHEMA, task_id=task["id"], case_id=task["experiment"]["case"],
        task_status=task["status"], solution_sha256=solution, records=records,
        common_business_latency_ms=task.get("latency_ms"), replay=task.get("spreadsheet_replay"),
        api_usage=task.get("api_usage"), objective_result=task.get("objective_result"),
        execution_infrastructure_error=evaluation.get("execution_infrastructure_error"),
        native_score_status="PENDING_SEPARATE_GRADING", scoring_cost_included_in_business_cost=False)
    write(output / "collection.json", result)
    return dict(task_id=task["id"], status=task["status"], delivered_replay_count=sum(r["status"] == "DELIVERED" for r in records))


def build_feedback(collection_dir):
    """Build the service receipt from the recorded task and separate grader result."""
    collection_dir = Path(collection_dir)
    task = json.loads((collection_dir / "task.json").read_text(encoding="utf-8"))
    collection = json.loads((collection_dir / "collection.json").read_text(encoding="utf-8"))
    result = json.loads((collection_dir / "native-score.json").read_text(encoding="utf-8"))
    experiment, evaluation = task["experiment"], task["evaluation"]
    if (collection.get("task_id") != task["id"] or result.get("task_id") != task["id"] or
            collection.get("case_id") != experiment["case"] or result.get("case_id") != experiment["case"]):
        raise ValueError("Task, collection and native result identities differ")
    cases = result.get("cases")
    if not isinstance(cases, list) or len(cases) != 3:
        raise ValueError("Native result must contain three case scores")
    if (result.get("source_revision") != SOURCE_REVISION and
            (any(value is not None for value in cases) or result.get("source_revision") is not None)):
        raise ValueError("Native scorer revision differs from the admitted case")
    if (result.get("outputs") != evaluation.get("outputs") or
            result.get("manifest_sha256") != experiment.get("manifest_sha256") or
            result.get("original_manifest_sha256") != experiment.get("original_manifest_sha256")):
        raise ValueError("Native result is not bound to this task's manifests and delivered outputs")
    records = collection.get("records", [])
    outputs = evaluation["outputs"]
    if len(records) != 3 or len(outputs) != 3:
        raise ValueError("The collection must contain three final replay records")
    for index, (record, output) in enumerate(zip(records, outputs), 1):
        path = collection_dir / "delivered" / ("replay-" + str(index) + ".xlsx")
        if (record.get("index") != index or output.get("index") != index or
                record.get("sha256") != output.get("sha256")):
            raise ValueError("Collected outputs differ from the final task delivery")
        if output.get("sha256") is None:
            if record.get("status") != "MISSING_DELIVERY" or path.exists() or path.is_symlink():
                raise ValueError("An unrecorded workbook cannot replace a missing delivery")
        elif (record.get("status") != "DELIVERED" or path.is_symlink() or not path.is_file() or
              sha(path.read_bytes()) != output["sha256"]):
            raise ValueError("Graded workbook bytes differ from the final task delivery")
    return dict(schema=FEEDBACK_SCHEMA, task_id=task["id"], case_id=experiment["case"],
                manifest_sha256=experiment["manifest_sha256"],
                original_manifest_sha256=experiment["original_manifest_sha256"],
                outputs=evaluation["outputs"], source_revision=SOURCE_REVISION,
                cases=cases, hard=result.get("hard"), soft=result.get("soft"))


def grade(args):
    # Import only the existing trusted scorer. Generated solution.py is never
    # imported, compiled or executed in this process.
    from score_office_spreadsheet import score
    collection = json.loads((args.collection / "collection.json").read_text(encoding="utf-8"))
    if collection.get("status") == "ADMISSION_FAILED":
        result = dict(case_id=collection["case_id"], task_id=None, status="NOT_ADMITTED", cases=[None, None, None],
                      soft=None, hard=None, quality=None, planned=1, admitted=0,
                      reason="Admission failed; the planned original case remains in the inventory", grading_cost_separate=True)
        write(args.collection / "native-score.json", result)
        return result
    task = json.loads((args.collection / "task.json").read_text(encoding="utf-8"))
    if collection["task_id"] != task["id"] or collection["case_id"] != task["experiment"]["case"]:
        raise ValueError("Collection/task identities differ")
    if sha(args.manifest.read_bytes()) != task["experiment"]["original_manifest_sha256"]:
        raise ValueError("The grading subset differs from the source of the admitted runtime manifest")
    binding = dict(outputs=task["evaluation"]["outputs"], manifest_sha256=task["experiment"]["manifest_sha256"],
                   original_manifest_sha256=task["experiment"]["original_manifest_sha256"])
    if (collection.get("execution_infrastructure_error") or task.get("workspace_status") != "FINISHED"
            or collection.get("status") == "COLLECTION_TIMEOUT"):
        result = dict(case_id=collection["case_id"], task_id=task["id"], status="UNKNOWN_INFRASTRUCTURE",
                      cases=[None, None, None], soft=None, hard=None, quality=None,
                      reason="Execution/collection did not settle; preserve the reached case with unknown scores",
                      grading_cost_separate=True, **binding)
        write(args.collection / "native-score.json", result)
        return result
    delivered = {file["sha256"] for file in task.get("files", [])}
    outputs = []
    for index in (1, 2, 3):
        record = next(row for row in collection["records"] if row["index"] == index)
        if (task["evaluation"]["outputs"][index - 1] !=
                dict(index=index, sha256=record.get("sha256"))):
            raise ValueError("Collected output differs from the final task delivery")
        path = args.collection / "delivered" / ("replay-" + str(index) + ".xlsx")
        if record["status"] == "DELIVERED":
            if record["sha256"] not in delivered or not path.is_file() or sha(path.read_bytes()) != record["sha256"]:
                raise ValueError("Native grading accepts only the final delivered artifact bytes")
        elif path.exists():
            raise ValueError("An unrecorded workbook cannot substitute for missing delivery")
        outputs.append(path)
    started = time.perf_counter()
    result = score(args.manifest, collection["case_id"], args.checkout, outputs, args.soffice)
    result.update(task_id=task["id"], grading_wall_ms=(time.perf_counter() - started) * 1000,
                  common_business_latency_ms=collection.get("common_business_latency_ms"),
                  grading_cost_separate=True, quality=result["hard"] if None not in result["cases"] else None,
                  **binding)
    write(args.collection / "native-score.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prep = commands.add_parser("prepare", help="copy original inputs only; no model or execution")
    prep.add_argument("manifest", type=Path)
    prep.add_argument("output", type=Path)
    dispatch = commands.add_parser("run", help="explicitly run one approved case on an existing service")
    dispatch.add_argument("manifest", type=Path, help="local prepared runtime manifest")
    dispatch.add_argument("case_id")
    dispatch.add_argument("output", type=Path)
    dispatch.add_argument("--server-manifest", required=True, help="runtime manifest path as seen by the service")
    dispatch.add_argument("--project-id", required=True)
    dispatch.add_argument("--url", default="http://127.0.0.1:8880")
    dispatch.add_argument("--wait-seconds", type=int, default=1800)
    scorer = commands.add_parser("grade", help="separate grading side; never executes generated code")
    scorer.add_argument("manifest", type=Path, help="original private grading manifest with answer workbooks")
    scorer.add_argument("checkout", type=Path, help="pinned official scoring checkout")
    scorer.add_argument("collection", type=Path)
    scorer.add_argument("--soffice")
    feedback = commands.add_parser("feedback", help="submit separately graded evidence for the final delivery")
    feedback.add_argument("collection", type=Path)
    feedback.add_argument("--url", default="http://127.0.0.1:8880")
    args = parser.parse_args()
    result = (prepare(args.manifest, args.output) if args.command == "prepare" else
              run(args) if args.command == "run" else grade(args) if args.command == "grade" else
              api(args.url, "/api/experiments/office/spreadsheet/score", build_feedback(args.collection)))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
