"""SpreadsheetBench task preparation and frozen replays through the common host.

Only original input workbooks enter the project. Answers and native scoring
remain in a separate grading process. Generated Python runs solely through
the platform's registered DockerPythonExecutor.
"""

import copy
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import time
import uuid

from seampact.agent_runtime import AgentHook, SYSTEM_PROMPT
from seampact.project_spaces import _checked

SCHEMA = "seampact.office-spreadsheet-runtime/1"
SUBSET_SCHEMA = "seampact.office-spreadsheetbench-v1-subset.v1"
SOURCE_REVISION = "49b73a94775fb489063f60ca1865e3a650079a79"
FEEDBACK_SCHEMA = "seampact.spreadsheet-native-feedback/1"


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def apply_native_score(platform, body):
    """Attach separate native scoring to the settled delivery, never to model context."""
    keys = {"schema", "task_id", "case_id", "manifest_sha256", "original_manifest_sha256",
            "outputs", "source_revision", "cases", "hard", "soft"}
    if not isinstance(body, dict) or set(body) != keys or body.get("schema") != FEEDBACK_SCHEMA:
        raise ValueError("Invalid spreadsheet feedback envelope")
    cases = body["cases"]
    if (not isinstance(cases, list) or len(cases) != 3 or
            any(value is not None and type(value) is not bool for value in cases)):
        raise ValueError("Native case scores must be three booleans or unknowns")
    hard = False if False in cases else True if None not in cases else None
    soft = sum(cases) / 3 if None not in cases else None
    if (type(body["hard"]) is not type(hard) or body["hard"] != hard or
            body["soft"] != soft or type(body["soft"]) is bool):
        raise ValueError("Native aggregate scores do not match the case scores")
    outputs = body["outputs"]
    if (not isinstance(outputs, list) or len(outputs) != 3 or
            any(not isinstance(row, dict) or set(row) != {"index", "sha256"} or row["index"] != index or
                (row["sha256"] is not None and (not isinstance(row["sha256"], str) or
                 not re.fullmatch(r"[0-9a-f]{64}", row["sha256"])))
                for index, row in enumerate(outputs, 1))):
        raise ValueError("Expected ordered final output identities")
    evidence_id = sha(json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    scheduler = platform.execution
    with scheduler.condition, platform.store.transaction():
        task = platform.store.get("task", body["task_id"])
        if not task or task.get("status") in {"RUNNING", "CANCELLING"} or task.get("workspace_status") != "FINISHED":
            raise ValueError("Spreadsheet task has not finished and drained")
        experiment, evaluation = task.get("experiment") or {}, task.get("evaluation") or {}
        if (experiment.get("case") != body["case_id"] or
                experiment.get("manifest_sha256") != body["manifest_sha256"] or
                experiment.get("original_manifest_sha256") != body["original_manifest_sha256"] or
                body["source_revision"] != SOURCE_REVISION or evaluation.get("source_revision") != SOURCE_REVISION or
                evaluation.get("outputs") != outputs):
            raise ValueError("Feedback does not match the admitted case, manifests and final output identities")
        delivered = {(f.get("replay_index"), f.get("sha256")) for f in task.get("files", [])}
        if any(row["sha256"] is not None and (row["index"], row["sha256"]) not in delivered for row in outputs):
            raise ValueError("Feedback includes an output absent from final delivery")
        if any(row["sha256"] is None and cases[i] is True for i, row in enumerate(outputs)):
            raise ValueError("A missing delivery cannot pass native scoring")
        if evaluation.get("execution_infrastructure_error") and any(value is not None for value in cases):
            raise ValueError("Infrastructure failure cannot produce known scores")
        prior = evaluation.get("native_feedback")
        if prior and prior["evidence_id"] != evidence_id and prior.get("quality") is not None:
            raise ValueError("Conflicting native feedback is already settled")
        quality = hard if None not in cases else None
        metrics = dict(native_cases=cases, native_soft=soft, native_hard=hard)
        workload = scheduler.workload
        if workload and body["task_id"] in workload.get("task_ids", []) and quality is not None:
            # The scheduler owns per-task settlement/update boundaries; a later
            # task may be graded while earlier batches have already updated.
            task["objective_result"] = scheduler.settle_quality(body["task_id"], quality=quality,
                metrics=metrics, evidence_id=evidence_id)
        evaluation.update(status="NATIVE_SCORED" if quality is not None else "UNKNOWN_INFRASTRUCTURE",
                          quality=quality, metrics=metrics,
                          native_feedback=dict(evidence_id=evidence_id, quality=quality, **metrics))
        task["evaluation"] = evaluation
        platform.store.save("task", task["id"], task)
        return dict(task_id=task["id"], status=evaluation["status"], quality=quality, evidence_id=evidence_id)


def load_case(manifest_path, expected_sha, case_id):
    manifest_path = Path(manifest_path)
    raw = manifest_path.read_bytes()
    if sha(raw) != expected_sha:
        raise ValueError("Spreadsheet subset identity changed")
    subset = json.loads(raw.decode("utf-8-sig"))
    if subset.get("schema") != SUBSET_SCHEMA or subset["scoring_source"]["revision"] != SOURCE_REVISION:
        raise ValueError("Expected the frozen SpreadsheetBench V1 subset and scorer")
    case = next((row for row in subset["cases"] if row["id"] == case_id and row["split"] in
                 {"evaluation", "train_pool", "development_pool"}), None)
    if case is None or not re.fullmatch(r"[A-Za-z0-9_-]+", str(case.get("source_id", ""))):
        raise ValueError("Question is absent from the frozen subset")
    records = {PurePosixPath(item["path"]).name: item for item in case["files"] if item["role"] == "input"}
    inputs = []
    base = manifest_path.parent.resolve()
    for index in (1, 2, 3):
        name = f"{index}_{case['source_id']}_input.xlsx"
        record = records.get(name)
        if record is None:
            raise ValueError("Three original input cases are required")
        path = (base / record["path"]).resolve()
        if not path.is_relative_to(base):
            raise ValueError("Input workbook escapes the frozen subset")
        _checked(path, directory=False)
        if path.stat().st_size > 16 * 1024 * 1024:
            raise ValueError("Original input workbook is too large")
        data = path.read_bytes()
        if sha(data) != record["sha256"]:
            raise ValueError("Original input workbook changed or is too large")
        inputs.append(dict(name=name, data=data, sha256=record["sha256"]))
    case = dict(copy.deepcopy(case), original_manifest_sha256=subset.get("source_manifest_sha256", expected_sha))
    return case, inputs


class ReplayProjects:
    """Narrow the existing active lease's mounts to one hidden replay case."""
    def __init__(self, projects, task_id, data, workspace, scratch, solution):
        self.projects, self.task_id = projects, task_id
        self.paths = [(data, "/data", True), (workspace, "/workspace", False),
                      (scratch, "/scratch", False), (solution, "/solution.py", True)]
        self.lock = projects.lock

    def task_scope(self, identity):
        return self.projects.task_scope(identity)

    def container_mounts(self, identity):
        if identity != self.task_id:
            raise ValueError("Replay task ownership mismatch")
        self.projects.container_mounts(identity)  # validate active project/session lease
        return [dict(source=str(path), target=target, read_only=readonly) for path, target, readonly in self.paths]


class SpreadsheetTask:
    def __init__(self, platform, task_id, project_id, case, inputs, namespace):
        self.platform, self.task_id, self.project_id = platform, task_id, project_id
        self.case, self.inputs, self.namespace = case, inputs, namespace
        self.executor = platform.code_execution
        self.task = self.scope = self.control = self.emit = None
        self.frozen = False
        self.infrastructure_error = None
        self.files, self.replays = [], []
        self.solution_sha256 = None
        self.files_attached = False

    def run(self, projects, scope, code, timeout_s, *, control, task, emit):
        """Use the ordinary Python tool, retaining its live task/control references."""
        if self.frozen:
            raise ValueError("The submitted solution is frozen; no further model execution is allowed")
        self.task, self.scope, self.control, self.emit = task, dict(scope), control, emit
        try:
            return self.executor.run(projects, scope, code, timeout_s, control=control, task=task, emit=emit)
        except Exception as error:
            self.infrastructure_error = type(error).__name__
            raise

    def gate(self, payload, context):
        if self.frozen:
            raise ValueError("Solution frozen: further tools and model edits are disabled")

    def tools(self):
        return [dict(type="function", function=dict(name="submit_spreadsheet_solution",
            description="Freeze the tested reusable solution.py and replay it unchanged on all three original inputs. "
                        "After submission no tool calls or code edits are allowed. Hidden replay results are not model feedback.",
            parameters=dict(type="object", properties={}, additionalProperties=False)))]

    def _artifact(self, name, data, **metadata):
        identity = sha(data)
        self.platform.capabilities.artifacts.save(io.BytesIO(data), identity)
        return dict(name=name, sha256=identity, size=len(data),
                    media_type="text/x-python" if name.endswith(".py") else
                               "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", **metadata)

    def submit(self, name, arguments, call_id=None):
        if name != "submit_spreadsheet_solution" or arguments != {} or self.frozen:
            raise ValueError("Only one frozen solution submission is allowed")
        if self.task is None:
            raise ValueError("Use run_python to inspect/test the first input before submitting")
        self.control.check()
        projects = self.platform.projects
        with projects.lock:
            source = projects.file_path(self.project_id, self.scope["conversation_id"], "workspace", "solution.py")
            solution = source.read_bytes()
            if not solution or len(solution) > 128 * 1024:
                raise ValueError("solution.py must contain at most 128 KiB of reusable Python")
            solution.decode("utf-8")
            self.frozen, self.solution_sha256 = True, sha(solution)
            self.files.append(self._artifact("solution.py", solution, source=dict(area="workspace", path="solution.py")))
            record = dict(schema=SCHEMA, task_id=self.task_id, project_id=self.project_id,
                          case_id=self.case["id"], solution_sha256=self.solution_sha256, replays=self.replays,
                          status="REPLAYING", implementation="same solution bytes; input/output argv vary; fresh isolated process per case")
            self.task["spreadsheet_replay"] = record
        started = time.perf_counter()
        replay_namespace = "spreadsheet-replay-" + uuid.uuid4().hex
        for index, original in enumerate(self.inputs, 1):
            self.control.check()
            parts = ["tasks", self.task_id, "scratch", replay_namespace, str(index)]
            with projects.lock:
                data = projects._directory(self.project_id, [*parts, "data"], create=True)
                work = projects._directory(self.project_id, [*parts, "workspace"], create=True)
                scratch = projects._directory(self.project_id, [*parts, "scratch"], create=True)
                frozen = projects._directory(self.project_id, parts, create=True) / "solution.py"
                with frozen.open("xb") as stream:
                    stream.write(solution)
                with (data / original["name"]).open("xb") as stream:
                    stream.write(original["data"])
            output_name = f"{index}_{self.case['source_id']}_output.xlsx"
            mounts = ReplayProjects(projects, self.task_id, data, work, scratch, frozen)
            # This trusted shim is passed as stdin to the existing isolated
            # executor. It is never evaluated by the host Python interpreter.
            shim = ("import runpy, sys\n" + "sys.argv = " + repr(["/solution.py", "/data/" + original["name"],
                    "/workspace/" + output_name]) + "\nrunpy.run_path('/solution.py', run_name='__main__')\n")
            before = time.perf_counter()
            try:
                result = self.executor.run(mounts, self.scope, shim, 120, control=self.control,
                                           task=self.task, emit=self.emit)
            except Exception as error:
                self.infrastructure_error = type(error).__name__
                raise
            row = dict(index=index, input_sha256=original["sha256"], solution_sha256=self.solution_sha256,
                       status=result["status"], exit_code=result["exit_code"],
                       execution_ms=(time.perf_counter() - before) * 1000, output=None)
            output = work / output_name
            if result["status"] == "COMPLETED" and result["exit_code"] == 0 and output.exists():
                _checked(output, directory=False)
                if output.stat().st_size > 16 * 1024 * 1024:
                    row["reason"] = "replay output exceeds 16 MiB"
                else:
                    content = output.read_bytes()
                    artifact = self._artifact(output_name, content, replay_index=index,
                                              solution_sha256=self.solution_sha256)
                    self.files.append(artifact)
                    row["output"] = artifact
            else:
                row["reason"] = "replay failed or did not produce the required workbook"
            self.replays.append(row)
        record.update(status="REPLAYED", replay_ms=(time.perf_counter() - started) * 1000)
        self.platform.store.save("spreadsheet_replay", self.task_id, copy.deepcopy(record))
        return dict(content=json.dumps(dict(status="SOLUTION_FROZEN", solution_sha256=self.solution_sha256,
            note="All replays are complete. No hidden inputs, outputs or native scores are returned to the model. "
                 "Finish with a short final response; do not call tools.")), error=None)

    def deliver(self, payload, context):
        if self.frozen and not self.files_attached and self.task["spreadsheet_replay"]["status"] == "REPLAYED":
            self.files_attached = True
            return dict(action="replace", files=copy.deepcopy(payload.get("files", [])) + copy.deepcopy(self.files))

    def evaluate(self, task):
        delivered = {file.get("sha256") for file in task.get("files", [])}
        outputs = [dict(index=index, sha256=next((row["output"]["sha256"] for row in self.replays
                    if row["index"] == index and row.get("output") and row["output"]["sha256"] in delivered), None))
                   for index in (1, 2, 3)]
        return dict(status="PENDING_NATIVE_SCORING", quality=None, metrics={}, evaluator="separate pinned SpreadsheetBench scorer",
                    source_revision=SOURCE_REVISION, solution_sha256=self.solution_sha256,
                    delivered_solution=self.solution_sha256 in delivered, outputs=outputs,
                    execution_infrastructure_error=self.infrastructure_error,
                    scope="Only final delivered artifact identities are eligible for the separate native scorer; missing outputs remain failures.")


def start_spreadsheet_task(platform, body, *, internal_task_id=None, pilot_config=None):
    if not isinstance(body, dict) or set(body) != {"manifest", "manifest_sha256", "case_id", "project_id"}:
        raise ValueError("Spreadsheet tasks require a frozen manifest identity, case and project")
    if platform.code_execution is None:
        raise ValueError("A fixed local Docker Python image with openpyxl is required")
    case, inputs = load_case(body["manifest"], body["manifest_sha256"], body["case_id"])
    identity = internal_task_id or uuid.uuid4().hex
    namespace = "spreadsheetbench/" + identity
    environment = SpreadsheetTask(platform, identity, body["project_id"], case, inputs, namespace)
    prompt = ("Instruction: " + case["instruction"] +
        "\nInstruction type: " + case["instruction_type"] + "\nAnswer position: " + str(case["answer_position"]))
    # Host execution rules are trusted system context. Keep the original task
    # in user context so input guards still inspect every supplied instruction.
    system_prompt = (SYSTEM_PROMPT + "\n\nComplete this original SpreadsheetBench office workbook task.\n"
        "The only visible original input is /data/" + namespace + "/" + inputs[0]["name"] +
        ". Inspect it using run_python in the ordinary isolated execution environment. "
        "Write /workspace/solution.py as a self-contained reusable Python program. It must read input_path=sys.argv[1], "
        "write output_path=sys.argv[2], and implement the requested manipulation for arbitrary values and row counts. "
        "Use run_python to test this program on the visible first workbook. Do not hard-code output values. "
        "Finally call submit_spreadsheet_solution exactly once. That freezes your script and replays the identical bytes "
        "on the three original test inputs with different argv paths, in fresh isolated processes. "
        "No further tools or edits are accepted after submission. Answers and native scores are unavailable to you.")
    capabilities = platform.capabilities.public()
    hooks = (AgentHook("spreadsheet_frozen_submission", "before_tool", environment.gate),
             AgentHook("spreadsheet_replay_delivery", "before_delivery", environment.deliver, mode="complete"))
    options = dict(code_execution=environment, external_tools=environment.tools(), external_execute=environment.submit,
                   hooks=hooks, allowed_capabilities=[], system_prompt=system_prompt,
                   allowed_workspace_tools=["list_files", "read_file", "write_file", "run_python"],
                   max_model_turns=15, evaluator=environment.evaluate)
    if pilot_config is not None:
        from seampact.office_application import build_hooks
        application = build_hooks(platform, identity, pilot_config)
        options.update(application, hooks=hooks + application["hooks"])
    with platform.deployments.lock, platform.projects.lock:
        platform.projects.put_file(body["project_id"], None, "data", namespace + "/" + inputs[0]["name"], inputs[0]["data"])
        return platform.chat.start(dict(project_id=body["project_id"], messages=[dict(role="user", content=prompt)],
                                        deadline_s=pilot_config.get("deadline_s", 1200) if pilot_config is not None else 1200),
            runtime_options=options,
            metadata=dict(arm=pilot_config["mode"] if pilot_config else "spreadsheetbench_pi_frozen_replay", case=case["id"], source_revision=SOURCE_REVISION,
                          manifest_sha256=body["manifest_sha256"], family_id=case["family_id"],
                          original_manifest_sha256=case["original_manifest_sha256"],
                          python_image=getattr(platform.code_execution, "image", None),
                          comparison_scope="ordinary Pi host, first-input development, unchanged argv-parameterized solution on three original cases"),
            internal_task_id=identity, capability_snapshot=capabilities, apply_project_application=pilot_config is None)
