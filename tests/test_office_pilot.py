import hashlib
import copy
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from seampact.office_experiment import pilot_quality
from seampact.office_pilot import _read_plan, arrival_waves, frozen_application, handle, scheduled_blocks, training_batches, workload_cases
from run_office_pilot import allowed_prebuild, reuse_calibration, timed_admissions


class PilotChecks(unittest.TestCase):
    def test_timed_arrivals_do_not_wait_for_first_result_and_barrier_precedes_rag(self):
        ids = ["a", "b", "c", "r"]
        blocks = [dict(profile="mixed", offsets_ms=[0, 0, 2]), dict(profile="rag", offsets_ms=[0])]
        plan = dict(continuous_training=True, arrival_width=2, cases={"training": ids},
                    arrival_schedule={"training": blocks})
        cases = {i: {"rag_adaptation": i == "r"} for i in ids}
        frozen = scheduled_blocks(plan, "training", ids, cases)
        issued, later = [], threading.Event()
        def admit(index, timing):
            issued.append(index)
            if index == 2:
                self.assertGreaterEqual(timing["client_send_offset_ms"], 2)
                later.set()
            return ids[index]
        stream = timed_admissions(frozen, admit)
        with self.subTest("independent admissions"):
            self.assertEqual(next(stream), (0, "a"))
            self.assertTrue(later.wait(2))
            self.assertEqual(issued, [0, 1, 2])
            self.assertEqual(list(stream), [(1, "b"), (2, "c"), (3, "r")])
        cases["c"]["rag_adaptation"] = True
        with self.assertRaisesRegex(ValueError, "独占"):
            scheduled_blocks(plan, "training", ids, cases)

    def test_parallel_arrivals_isolate_shared_fixture_changes(self):
        cases = {"a": {}, "b": {}, "c": {"_source": "privacy"}, "d": {"rag_adaptation": True}}
        ids = ["a", "a", "b", "c", "a", "d", "b"]
        self.assertEqual(arrival_waves({"arrival_width": 2}, ids, cases),
                         [[0, 1], [2], [3], [4], [5], [6]])
        self.assertEqual(arrival_waves({}, ids, cases), [[i] for i in range(len(ids))])

    def test_calibration_prebuild_never_uses_training_or_evaluation_cases(self):
        plan = dict(cases=dict(development=["dev"], training=["train"], evaluation=["held"]))
        cases = ["train", "dev", "held"]
        self.assertEqual(allowed_prebuild(plan, cases, calibration_only=True), ["dev"])
        self.assertEqual(allowed_prebuild(plan, cases, tuning=True), ["train", "dev"])
        self.assertEqual(allowed_prebuild(plan, cases, evaluation_only=True), ["held"])

    def test_training_costs_and_prebuilt_corpora_are_confined_to_training(self):
        plan = dict(cases=dict(training=["t1", "t2"], development=["v"], evaluation=["x"]),
                    training_calibration=dict(case_ids=["t2"]))
        candidates = ["x", "t1", "v", "t2"]
        self.assertEqual(workload_cases(plan, "training_calibration"), [["t2"]])
        self.assertEqual(allowed_prebuild(plan, candidates, calibration_only=True), ["t2"])
        self.assertEqual(allowed_prebuild(plan, candidates, training_only=True), ["t1", "t2"])
        self.assertEqual(allowed_prebuild(plan, candidates, validation_only=True), ["v"])
        for wrong in ("v", "x"):
            plan["training_calibration"]["case_ids"] = ["t1", wrong]
            with self.assertRaisesRegex(ValueError, "训练集"):
                workload_cases(plan, "training_calibration")

    def test_development_only_protocol_cannot_start_or_prebuild_other_splits(self):
        plan = dict(local_development_only=True, cases={"development": ["dev"]})
        store = SimpleNamespace(get=lambda *_: None)
        platform = SimpleNamespace(store=store, execution=object())
        with patch("seampact.office_pilot._read_plan", return_value=("identity", plan, {})):
            for mode in ("training", "app_config", "validation_ppo", "ppo"):
                with self.assertRaisesRegex(ValueError, "不允许训练"):
                    handle(platform, dict(op="start", mode=mode))
            with self.assertRaisesRegex(ValueError, "只允许预构开发题"):
                handle(platform, dict(op="prebuild", case_id="held-out"))

    def test_development_admission_routes_opted_in_checks_to_scheduler(self):
        case = dict(id="office-case", _source="privacy")
        plan = dict(project_id="project", deadline_s=30, development_selector="scheduler",
                    manifest="manifest.json", manifest_sha256="digest", execution_timeout_s=None,
                    max_model_turns=64,
                    business_model=dict(connection_id="frozen", config_digest="digest", revision=2, model="model"))
        row = dict(id="run", protocol_sha256="identity", mode="development", status="RUNNING",
                   case_ids=[case["id"]], task_ids=["task"], admitted=[], application={})
        records = {("office_pilot", "run"): row}
        store = SimpleNamespace(get=lambda kind, key: records.get((kind, key)),
                                save=lambda kind, key, value: records.__setitem__((kind, key), value))
        platform = SimpleNamespace(store=store, execution=object())
        def start(_platform, request, *, internal_task_id, pilot_config):
            self.assertEqual(pilot_config["mode"], "scheduler")
            self.assertEqual(pilot_config["deadline_s"], 30)
            self.assertIn("execution_timeout_s", pilot_config)
            self.assertIsNone(pilot_config["execution_timeout_s"])
            self.assertEqual(pilot_config["max_model_turns"], 64)
            self.assertEqual(pilot_config["business_model"], plan["business_model"])
            self.assertEqual(internal_task_id, "task")
            return dict(task_id=internal_task_id)
        with patch("seampact.office_pilot._read_plan", return_value=("identity", plan, {case["id"]: case})), \
             patch("seampact.office_privacy.start_privacy_task", side_effect=start):
            result = handle(platform, dict(op="admit", run_id="run"))
        self.assertEqual(result["task_id"], "task")
        self.assertEqual(records[("office_pilot", "run")]["admitted"], ["task"])

    def test_explicit_null_model_turn_limit_survives_pilot_admission(self):
        case = dict(id="case", _source="privacy")
        plan = dict(project_id="project", deadline_s=None, max_model_turns=None,
                    manifest="manifest.json", manifest_sha256="digest")
        row = dict(id="run", protocol_sha256="identity", mode="training_calibration", status="RUNNING",
                   case_ids=["case"], task_ids=["task"], admitted=[], application={})
        records = {("office_pilot", "run"): row}
        store = SimpleNamespace(get=lambda kind, key: records.get((kind, key)),
                                save=lambda kind, key, value: records.__setitem__((kind, key), value))
        with patch("seampact.office_pilot._read_plan", return_value=("identity", plan, {"case": case})), \
             patch("seampact.office_privacy.start_privacy_task", return_value=dict(task_id="task")) as admit:
            handle(SimpleNamespace(store=store, execution=object()), dict(op="admit", run_id="run"))
        supplied = admit.call_args.kwargs["pilot_config"]
        self.assertIn("max_model_turns", supplied)
        self.assertIsNone(supplied["max_model_turns"])

    def test_native_tool_boundary_reaches_runtime_for_serial_and_parallel_admission(self):
        case = dict(id="case", suite="workspace", user_task_id="user", split="training")
        manifest = dict(protocol=dict(max_model_turns=12, timeout_s=900), cases=[case])
        tools = [dict(type="function", function=dict(name="native_lookup", parameters={}))]
        environment = SimpleNamespace(prompt="Native request", tool_specs=lambda: tools, execute=Mock(), identity={})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            content = json.dumps(manifest).encode()
            path.write_bytes(content)
            base = dict(project_id="project", deadline_s=None, manifest=str(path),
                        manifest_sha256=hashlib.sha256(content).hexdigest())
            scenarios = (({}, None), (dict(arrival_width=1), None),
                         (dict(arrival_width=1, native_environment_only=False), None),
                         (dict(arrival_width=1, native_environment_only=True), []),
                         (dict(arrival_width=2), []),
                         (dict(arrival_width=2, native_environment_only=False), []))
            for declared, allowed in scenarios:
                with self.subTest(declared=declared):
                    plan = dict(base, **declared)
                    row = dict(id="run", protocol_sha256="identity", mode="training_calibration", status="RUNNING",
                               case_ids=["case"], task_ids=["task"], admitted=[], application={})
                    records = {("office_pilot", "run"): row}
                    store = SimpleNamespace(get=lambda kind, key: records.get((kind, key)),
                        save=lambda kind, key, value: records.__setitem__((kind, key), value))
                    platform = SimpleNamespace(store=store, execution=object(),
                        capabilities=SimpleNamespace(public=lambda: []),
                        deployments=SimpleNamespace(lock=threading.RLock()),
                        projects=SimpleNamespace(lock=threading.RLock()),
                        chat=SimpleNamespace(start=Mock(return_value=dict(task_id="task"))))
                    with patch("seampact.office_pilot._read_plan", return_value=("identity", plan, {"case": case})), \
                         patch("seampact.office_experiment.source_identity", return_value=dict(revision="fixture")), \
                         patch("seampact.office_experiment.AgentDojoOfficeTask", return_value=environment), \
                         patch("seampact.office_application.build_hooks", return_value={}):
                        self.assertEqual(handle(platform, dict(op="admit", run_id="run"))["task_id"], "task")
                    request = platform.chat.start.call_args.args[0]
                    runtime = platform.chat.start.call_args.kwargs["runtime_options"]
                    self.assertEqual(runtime["allowed_workspace_tools"], allowed)
                    self.assertEqual(runtime["external_tools"], tools)
                    self.assertIs(runtime["external_execute"], environment.execute)
                    self.assertEqual(request["messages"], [dict(role="user", content=environment.prompt)])
                    self.assertNotIn("native_environment_only", row["application"])

    def test_resource_rule_calibration_routes_the_application_heuristic(self):
        case = dict(id="risk-case", _source="privacy")
        plan = dict(project_id="project", deadline_s=None, development_selector="resource_rule",
                    manifest="manifest.json", manifest_sha256="digest", execution_timeout_s=None)
        row = dict(id="run", protocol_sha256="identity", mode="training_calibration", status="RUNNING",
                   case_ids=[case["id"]], task_ids=["task"], admitted=[], application={})
        records = {("office_pilot", "run"): row}
        store = SimpleNamespace(get=lambda kind, key: records.get((kind, key)),
                                save=lambda kind, key, value: records.__setitem__((kind, key), value))
        def start(_platform, _request, *, internal_task_id, pilot_config):
            self.assertEqual(pilot_config["mode"], "resource_rule")
            self.assertIsNone(pilot_config["deadline_s"])
            return dict(task_id=internal_task_id)
        with patch("seampact.office_pilot._read_plan", return_value=("identity", plan, {case["id"]: case})), \
             patch("seampact.office_privacy.start_privacy_task", side_effect=start):
            handle(SimpleNamespace(store=store, execution=object()), dict(op="admit", run_id="run"))

    def test_disjoint_measured_training_calibration_checks_actual_run_not_output_protocol_series(self):
        with tempfile.TemporaryDirectory() as tmp:
            roles = ["sanitize", "injection", "privacy", "secrets", "code_scan"]
            units = [dict(id=role, runtime_group=role, status="READY", generation_verification=dict(status="SUCCEEDED"))
                     for role in roles]
            rag = dict(id="rag", runtime_group="rag", status="READY", generation_verification=dict(status="SUCCEEDED"))
            objective = dict(weights=dict(quality=1, delivery=0, latency=.1, ram=0, vram=0, api=0),
                scales=dict(latency_s=10, ram_byte_s=1, vram_byte_s=1, api_tokens=1),
                quality_evaluator="native", deadline_s=None, resource_source="measured")
            evidence = dict(protocol_sha256="actual-calibration-input-sha", run_ids=["baseline-run"],
                source_split="training", case_ids=["baseline"], application={}, scales=objective["scales"])
            source_plan = dict(scheduler=dict(objective=objective), development_evidence=evidence,
                training_calibration=dict(case_ids=["baseline"]), cases=dict(training=["baseline"], development=["dev"], evaluation=["test"]))
            source = Path(tmp)/"frozen-calibration.json"
            source.write_text(json.dumps(source_plan), encoding="utf-8")
            origin = dict(kind="completed_training_calibration", protocol=str(source),
                          protocol_sha256=hashlib.sha256(source.read_bytes()).hexdigest(), scales=objective["scales"])
            plan = dict(schema="seampact.office-comparison/1", project_id="project", rag=dict(capability_id="rag"),
                scheduler=dict(objective=objective), policies=dict(app_config=dict(kind="rule")),
                cases=dict(training=["train"], development=["dev"], evaluation=["test"]),
                development_evidence=dict(evidence, objective_scale_source=origin))
            record = dict(mode="training_calibration", status="COMPLETE", protocol_sha256=evidence["protocol_sha256"], case_ids=["baseline"])
            records = {("office_pilot", "baseline-run"): record}
            store = SimpleNamespace(get=lambda kind, key: records.get((kind, key)))
            execution = SimpleNamespace(_pool=lambda: None, configure=Mock(side_effect=RuntimeError("reached configuration")))
            project = dict(guard_candidates=[dict(role=role, unit_id=role) for role in roles], enabled_group_ids=["rag"])
            platform = SimpleNamespace(store=store, execution=execution,
                project_applications=SimpleNamespace(snapshot=lambda *_: (project, units)),
                capabilities=SimpleNamespace(public=lambda: [], unit=lambda _: rag))
            cases = dict(train=dict(family_id="training-family"), baseline=dict(family_id="calibration-family"))
            def read_plan(body):
                return (origin["protocol_sha256"], source_plan, cases) if body.get("protocol") == str(source) else ("target", plan, cases)
            with patch("seampact.office_pilot._read_plan", side_effect=read_plan), \
                 patch("seampact.office_pilot.frozen_application", return_value={}):
                # No series exists for frozen-calibration.json: the measured run belongs to its input protocol.
                evidence["application"] = {"guard_candidates": []}
                plan["development_evidence"]["application"] = evidence["application"]
                source_plan["development_evidence"]["application"] = evidence["application"]
                source.write_text(json.dumps(source_plan), encoding="utf-8")
                origin["protocol_sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
                with self.assertRaisesRegex(RuntimeError, "reached configuration"):
                    handle(platform, dict(op="start", mode="training"))
                cases["train"]["family_id"] = "calibration-family"
                with self.assertRaisesRegex(ValueError, "家族须互斥"):
                    handle(platform, dict(op="start", mode="training"))
                cases["train"]["family_id"] = "training-family"
                record["status"] = "RUNNING"
                with self.assertRaisesRegex(ValueError, "实测运行"):
                    handle(platform, dict(op="start", mode="training"))

    def test_unsettled_training_finish_synchronizes_series_and_preserves_checkpoints_on_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint = Path(tmp) / "retained.pt"
            checkpoint.write_bytes(b"retained checkpoint")
            receipt = dict(path=str(checkpoint), sha256=hashlib.sha256(checkpoint.read_bytes()).hexdigest())
            for history in ("running", "missing_entry", "missing_series"):
                with self.subTest(history=history):
                    row = dict(id="run", protocol_sha256="identity", mode="training", batch=0,
                               status="RUNNING", case_ids=["case"], admitted=["task"], checkpoint=receipt)
                    entry = dict(id="run", status="RUNNING", checkpoint=receipt, actual_updates=True)
                    series = dict(id="identity", workloads={}, checkpoint=receipt)
                    if history == "running":
                        series["workloads"]["training:0"] = entry
                    records = {("office_pilot", "run"): copy.deepcopy(row)}
                    if history != "missing_series":
                        records[("office_pilot_series", "identity")] = copy.deepcopy(series)
                    store = SimpleNamespace(get=lambda kind, key: copy.deepcopy(records.get((kind, key))),
                        save=lambda kind, key, value: records.__setitem__((kind, key), copy.deepcopy(value)))
                    state = dict(status="COMPLETE", updated=False, updates=[],
                                 summary=dict(training_ready=False, reason="missing API token receipt"))
                    execution = SimpleNamespace(finish_workload=Mock(return_value=state),
                                                policy=SimpleNamespace(save=Mock()))
                    platform = SimpleNamespace(store=store, execution=execution)
                    with patch("seampact.office_pilot._read_plan", return_value=("identity", {}, {})):
                        for _ in range(2):
                            self.assertEqual(handle(platform, dict(op="finish", run_id="run"))["status"], "UNSETTLED")
                    saved = records[("office_pilot_series", "identity")]
                    current = saved["workloads"]["training:0"]
                    self.assertEqual((current["id"], current["status"]), ("run", "UNSETTLED"))
                    if history == "running":
                        self.assertEqual(current["checkpoint"], receipt)
                        self.assertTrue(current["actual_updates"])
                    if history != "missing_series":
                        self.assertEqual(saved["checkpoint"], receipt)
                    self.assertEqual(records[("office_pilot", "run")]["checkpoint"], receipt)
                    self.assertEqual(checkpoint.read_bytes(), b"retained checkpoint")
                    self.assertEqual(execution.finish_workload.call_args_list, [unittest.mock.call(), unittest.mock.call()])
                    execution.policy.save.assert_not_called()

    def test_unsettled_training_retry_does_not_replace_another_series_run(self):
        series = dict(id="identity", workloads={"training:0": dict(id="newer-run", status="PAUSED",
                                                                  checkpoint=dict(path="newer.pt", sha256="verified"))})
        row = dict(id="run", protocol_sha256="identity", mode="training", batch=0,
                   status="UNSETTLED", case_ids=["case"], admitted=["task"])
        records = {("office_pilot", "run"): row, ("office_pilot_series", "identity"): copy.deepcopy(series)}
        store = SimpleNamespace(get=lambda kind, key: copy.deepcopy(records.get((kind, key))),
            save=lambda kind, key, value: records.__setitem__((kind, key), copy.deepcopy(value)))
        execution = SimpleNamespace(finish_workload=lambda: dict(status="COMPLETE",
                                     summary=dict(training_ready=False, reason="missing API token receipt")))
        with patch("seampact.office_pilot._read_plan", return_value=("identity", {}, {})):
            result = handle(SimpleNamespace(store=store, execution=execution), dict(op="finish", run_id="run"))
        self.assertEqual(result["status"], "UNSETTLED")
        self.assertEqual(records[("office_pilot_series", "identity")], series)

    def test_abort_waits_for_the_owned_tasks_and_preserves_unreleased_workspace_evidence(self):
        row = dict(id="run", protocol_sha256="identity", mode="training_calibration", batch=0,
                   status="RUNNING", case_ids=["case", "next"], admitted=["task"])
        task = dict(id="task", status="RUNNING")
        records = {("office_pilot", "run"): row, ("task", "task"): task}
        store = SimpleNamespace(get=lambda kind, key: copy.deepcopy(records.get((kind, key))),
            save=lambda kind, key, value: records.__setitem__((kind, key), copy.deepcopy(value)))
        execution = SimpleNamespace(workload=dict(id="other"),
                                    finish_workload=Mock(return_value=dict(id="run", status="COMPLETE")))
        chat = SimpleNamespace(cancel=Mock())
        platform = SimpleNamespace(store=store, execution=execution, chat=chat)
        request = dict(op="abort", run_id="run", reason="driver_error")
        with patch("seampact.office_pilot._read_plan", return_value=("identity", {}, {})):
            with self.assertRaisesRegex(ValueError, "当前拥有"):
                handle(platform, request)
            self.assertEqual(records[("office_pilot", "run")]["status"], "RUNNING")
            execution.workload["id"] = "run"
            self.assertEqual(handle(platform, request)["status"], "DRAINING")
            chat.cancel.assert_called_once_with("task")
            execution.finish_workload.assert_not_called()
            task.update(status="CANCELLED", workspace_status="UNKNOWN")
            settled = handle(platform, request)
            self.assertEqual((settled["status"], settled["unreleased_task_ids"]), ("UNSETTLED", ["task"]))
            self.assertEqual(handle(platform, request), settled)
            execution.finish_workload.assert_called_once_with(abort_run_id="run")
        self.assertEqual(records[("office_pilot_series", "identity")]["workloads"]["training_calibration:0"]["status"],
                         "UNSETTLED")

    def test_frozen_policy_and_semantic_guard_stay_identical_across_arms(self):
        guard = dict(id="p", entry_id="new_source_entry", tool_name="guard_tool", runtime_group="g",
            capability=dict(guard=dict(kind="privacy", subjects=["message"],
                                       provenance=[dict(path="source.py", start=1, end=1)]),
                input=dict(properties={"text": {"type": "string"},
                                       "entities": {"type": "array", "items": {"type": "string"}},
                                       "anonymize": {"type": "boolean"}}),
                output=dict(properties={"findings": {"type": "array"}}, required=["findings"])))
        rag = dict(id="r", tool_name="arbitrary_retriever", capability=dict(
            operation=dict(kind="retrieve", provenance=[dict(path="index.py", start=1, end=1)])))
        policy = {"required_outbound": ["privacy"], "outbound_finding": "block"}
        platform = SimpleNamespace(project_applications=SimpleNamespace(configuration=lambda _: {"guard_policy": policy}))
        plan = dict(project_id="p", bindings={"privacy": "p"})
        frozen = frozen_application(platform, plan, [guard], rag, {})
        self.assertEqual(frozen["guard_policy"]["outbound_finding"], "block")
        self.assertEqual(frozen["guard_candidates"][0]["guard"]["subjects"], ["message"])
        self.assertEqual(frozen["operations"]["arbitrary_retriever"]["kind"], "retrieve")
        policy["outbound_finding"] = "deny_operation"
        self.assertEqual(frozen_application(platform, plan, [guard], rag, {"application": frozen}), frozen)
        changed = copy.deepcopy(guard)
        changed["capability"]["guard"]["subjects"] = ["text"]
        with self.assertRaisesRegex(ValueError, "冻结应用"):
            frozen_application(platform, plan, [changed], rag, {"application": frozen})
        with self.assertRaisesRegex(ValueError, "必需检查"):
            frozen_application(platform, dict(plan, bindings={}), [], rag, {})

        automatic = dict(project_id="p", guard_policy={"required_outbound": []})
        second = dict(guard, id="another-provider")
        project = dict(guard_candidates=[
            dict(role="privacy", unit_id=unit["id"]) for unit in (guard, second)])
        baseline = frozen_application(platform, automatic, [guard, second], rag, {}, project=project)
        self.assertCountEqual([item["unit_id"] for item in baseline["guard_candidates"]], ["p", "another-provider"])
        calibrated = dict(automatic, development_evidence={"application": baseline})
        self.assertEqual(frozen_application(platform, calibrated, [second, guard], rag, {}, project=project), baseline)
        changed_project = dict(project, guard_candidates=project["guard_candidates"][:1])
        with self.assertRaisesRegex(ValueError, "开发校准"):
            frozen_application(platform, calibrated, [guard], rag, {}, project=changed_project)
        with self.assertRaisesRegex(ValueError, "检索声明"):
            frozen_application(platform, automatic, [guard, second], dict(rag, capability={}), {}, project=project)

    def test_reward_sweep_reuses_cost_units_but_rejects_other_data_or_seed(self):
        base = dict(cases=dict(development=["dev"], training=["train"], evaluation=["held"]), seed=7,
                    scheduler=dict(objective=None), objective_weights=dict(quality=1, delivery=1, api=0.1))
        measured = copy.deepcopy(base)
        measured["scheduler"]["objective"] = dict(weights=base["objective_weights"], scales=dict(api_tokens=100))
        measured["development_evidence"] = dict(pool={"library": "frozen"}, scales=dict(api_tokens=100))
        variant = copy.deepcopy(base)
        variant["objective_weights"]["api"] = 0
        reused = reuse_calibration(variant, measured, "source-digest")
        self.assertEqual(reused["scheduler"]["objective"]["scales"], dict(api_tokens=100))
        self.assertEqual(reused["scheduler"]["objective"]["weights"]["api"], 0)
        self.assertEqual(workload_cases(variant, "validation_ppo"), [["dev"]])
        for field, value in (("seed", 8), ("cases", dict(base["cases"], development=["held"]))):
            changed = copy.deepcopy(variant)
            changed[field] = value
            with self.assertRaisesRegex(ValueError, "Shared calibration"):
                reuse_calibration(changed, measured, "source-digest")

    def test_calibration_reuse_preserves_target_pareto_direction_and_smoothing(self):
        from seampact.scheduling.unified_objective import freeze_objective
        source_preference = dict(quality=.1, latency=.1, ram=.1, vram=.1, api=.6)
        target_preference = dict(quality=.6, latency=.1, ram=.1, vram=.1, api=.1)
        measured = dict(seed=7, scheduler=dict(objective=dict(
            weights=dict(source_preference, delivery=0),
            scales=dict(latency_s=10, ram_byte_s=20, vram_byte_s=30, api_tokens=40),
            quality_evaluator="native", deadline_s=None, resource_source="measured",
            pareto=dict(preference=source_preference, smoothing=.1))))
        measured["development_evidence"] = dict(pool={"library": "frozen"},
            scales=copy.deepcopy(measured["scheduler"]["objective"]["scales"]))
        target = copy.deepcopy(measured)
        target.pop("development_evidence")
        target["objective_weights"] = dict(target_preference, delivery=0)
        target["scheduler"]["objective"].update(
            weights=target["objective_weights"], scales=dict(latency_s=1, ram_byte_s=1, vram_byte_s=1, api_tokens=1),
            pareto=dict(preference=target_preference, smoothing=.25))
        before_target, before_source = copy.deepcopy(target), copy.deepcopy(measured)
        reused = reuse_calibration(target, measured, "source-digest")
        objective = freeze_objective(reused["scheduler"]["objective"])
        self.assertEqual(objective["pareto"], dict(preference=target_preference, smoothing=.25))
        self.assertEqual(objective["weights"], target["objective_weights"])
        self.assertEqual(objective["scales"], measured["development_evidence"]["scales"])
        self.assertEqual(reused["development_evidence"]["reused_from_protocol_sha256"], "source-digest")
        reused["scheduler"]["objective"]["pareto"]["preference"]["quality"] = 1
        self.assertEqual(target, before_target)
        self.assertEqual(measured, before_source)

    def test_non_pareto_target_does_not_inherit_calibration_source_pareto(self):
        from seampact.scheduling.unified_objective import freeze_objective
        preference = dict(quality=.2, latency=.2, ram=.2, vram=.2, api=.2)
        measured = dict(seed=7, scheduler=dict(objective=dict(
            weights=dict(preference, delivery=0),
            scales=dict(latency_s=10, ram_byte_s=20, vram_byte_s=30, api_tokens=40),
            quality_evaluator="native", deadline_s=None, resource_source="measured",
            pareto=dict(preference=preference, smoothing=.1))))
        measured["development_evidence"] = dict(pool={"library": "frozen"},
            scales=copy.deepcopy(measured["scheduler"]["objective"]["scales"]))
        for explicit_objective in (True, False):
            with self.subTest(explicit_objective=explicit_objective):
                target = copy.deepcopy(measured)
                target.pop("development_evidence")
                target["objective_weights"] = dict(quality=1, delivery=0, latency=.1, ram=.1, vram=.1, api=.1)
                if explicit_objective:
                    target["scheduler"]["objective"].pop("pareto")
                    target["scheduler"]["objective"]["weights"] = target["objective_weights"]
                    target["scheduler"]["objective"].update(
                        quality_evaluator="unmeasured-target", deadline_s=9, resource_source="reserved")
                else:
                    target["scheduler"]["objective"] = None
                reused = reuse_calibration(target, measured, "source-digest")
                objective = freeze_objective(reused["scheduler"]["objective"])
                self.assertNotIn("pareto", objective)
                self.assertEqual(objective["weights"], target["objective_weights"])
                self.assertEqual(objective["scales"], measured["development_evidence"]["scales"])
                for key in ("quality_evaluator", "deadline_s", "resource_source"):
                    self.assertEqual(objective[key], measured["scheduler"]["objective"][key])
                self.assertIn("pareto", measured["scheduler"]["objective"])

    def test_native_quality_keeps_unknown_and_separate_attack_semantics(self):
        self.assertEqual(pilot_quality(dict(utility=True, attack_success=None), False), 1.0)
        self.assertEqual(pilot_quality(dict(utility=False, attack_success=None), False), 0.0)
        self.assertEqual(pilot_quality(dict(utility=True, attack_success=False), True), 1.0)
        self.assertEqual(pilot_quality(dict(utility=True, attack_success=True), True), 0.0)
        self.assertIsNone(pilot_quality(dict(utility=True, attack_success=None), True))
        self.assertIsNone(pilot_quality(dict(utility=None, attack_success=False), True))

    def test_protocol_requires_exact_frozen_manifest_and_separate_families(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cases, selections = [], {}
            for name, split, count in (("development", "development_pool", 4),
                                       ("training", "train_pool", 8), ("evaluation", "evaluation", 8)):
                selections[name] = []
                for i in range(count):
                    identity = name + str(i)
                    case = dict(id=identity, split=split, suite="workspace", family_id=identity)
                    if name == "evaluation" and i >= 6:
                        case.update(injection_task_id="attack", attack_template="direct" if i == 6 else "system")
                    cases.append(case)
                    selections[name].append(identity)
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps(dict(cases=cases)), encoding="utf-8")
            plan = dict(schema="seampact.office-pilot/1", manifest=str(manifest),
                        manifest_sha256=hashlib.sha256(manifest.read_bytes()).hexdigest(),
                        cases=selections, bindings={}, scheduler={})
            protocol = root / "protocol.json"
            protocol.write_text(json.dumps(plan), encoding="utf-8")
            body = dict(protocol=str(protocol), protocol_sha256=hashlib.sha256(protocol.read_bytes()).hexdigest())
            self.assertEqual(len(_read_plan(body)[2]), 20)
            self.assertNotIn("native_environment_only", _read_plan(body)[1])
            for explicit in (True, False):
                plan["native_environment_only"] = explicit
                protocol.write_text(json.dumps(plan), encoding="utf-8")
                body["protocol_sha256"] = hashlib.sha256(protocol.read_bytes()).hexdigest()
                self.assertIs(_read_plan(body)[1]["native_environment_only"], explicit)
            for invalid in (None, 0, 1, "true", [], {}):
                plan["native_environment_only"] = invalid
                protocol.write_text(json.dumps(plan), encoding="utf-8")
                body["protocol_sha256"] = hashlib.sha256(protocol.read_bytes()).hexdigest()
                with self.subTest(native_environment_only=invalid), self.assertRaisesRegex(ValueError, "native_environment_only"):
                    _read_plan(body)
            plan.pop("native_environment_only")
            protocol.write_text(json.dumps(plan), encoding="utf-8")
            body["protocol_sha256"] = hashlib.sha256(protocol.read_bytes()).hexdigest()
            self.assertNotIn("max_model_turns", _read_plan(body)[1])
            plan["max_model_turns"] = None
            protocol.write_text(json.dumps(plan), encoding="utf-8")
            body["protocol_sha256"] = hashlib.sha256(protocol.read_bytes()).hexdigest()
            self.assertIsNone(_read_plan(body)[1]["max_model_turns"])
            for invalid in (False, 0, -1, 1.5, "15"):
                plan["max_model_turns"] = invalid
                protocol.write_text(json.dumps(plan), encoding="utf-8")
                body["protocol_sha256"] = hashlib.sha256(protocol.read_bytes()).hexdigest()
                with self.subTest(limit=invalid), self.assertRaisesRegex(ValueError, "max_model_turns"):
                    _read_plan(body)
            plan.pop("max_model_turns")
            plan.update(schema="seampact.office-comparison/1", training_batch_size=6)
            selections["development"] *= 4
            selections["training"] *= 8
            protocol.write_text(json.dumps(plan), encoding="utf-8")
            body["protocol_sha256"] = hashlib.sha256(protocol.read_bytes()).hexdigest()
            formal = _read_plan(body)[1]
            self.assertEqual(training_batches(formal), 11)
            batches = [formal["cases"]["training"][i * 6:(i + 1) * 6] for i in range(training_batches(formal))]
            self.assertEqual(sum(map(len, batches)), 64)
            self.assertEqual(len(batches[-1]), 4)
            plan.pop("training_batch_size")
            plan.update(continuous_training=True, update_after_tasks=16)
            protocol.write_text(json.dumps(plan), encoding="utf-8")
            body["protocol_sha256"] = hashlib.sha256(protocol.read_bytes()).hexdigest()
            continuous = _read_plan(body)[1]
            self.assertEqual(training_batches(continuous), 1)
            self.assertEqual(workload_cases(continuous, "training"), [selections["training"]])
            self.assertEqual(workload_cases(continuous, "ppo"), [selections["evaluation"]])
            plan["development_selector"] = "scheduler"
            plan["policies"] = {"app_config": {"kind": "rule", "optional_hooks": "all"}}
            protocol.write_text(json.dumps(plan), encoding="utf-8")
            body["protocol_sha256"] = hashlib.sha256(protocol.read_bytes()).hexdigest()
            self.assertEqual(_read_plan(body)[1]["development_selector"], "scheduler")
            plan["policies"]["app_config"]["kind"] = "ppo"
            protocol.write_text(json.dumps(plan), encoding="utf-8")
            body["protocol_sha256"] = hashlib.sha256(protocol.read_bytes()).hexdigest()
            with self.assertRaisesRegex(ValueError, "规则策略"):
                _read_plan(body)
            plan["policies"]["app_config"]["kind"] = "rule"
            plan["training_batch_size"] = 4
            protocol.write_text(json.dumps(plan), encoding="utf-8")
            body["protocol_sha256"] = hashlib.sha256(protocol.read_bytes()).hexdigest()
            with self.assertRaisesRegex(ValueError, "连续服务"):
                _read_plan(body)
            manifest.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "身份"):
                _read_plan(body)


if __name__ == "__main__":
    unittest.main()
