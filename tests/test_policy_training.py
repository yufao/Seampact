import copy
from contextlib import nullcontext
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from seampact.policy_training import PolicyTraining
from seampact.store import Store


def objective():
    return dict(weights=dict(quality=.2, delivery=0, latency=.2, ram=.2, vram=.2, api=.2),
        scales=dict(latency_s=1, ram_byte_s=1, vram_byte_s=1, api_tokens=1),
        quality_evaluator="native", deadline_s=None, resource_source="measured",
        pareto=dict(preference=dict(quality=.2, latency=.2, ram=.2, vram=.2, api=.2), smoothing=.1))


class FakePolicy:
    def __init__(self):
        self.version = 0

    def update_interval(self, *args, **kwargs):
        self.version += 1
        return dict(updated=True, policy_version=self.version)

    update_workload = update_interval

    def save(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(str(self.version).encode())

    def snapshot(self):
        return dict(learner=dict(policy_version=self.version), optimizer="included")


class TrainingChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "platform.sqlite3")
        self.plan = dict(schema="seampact.office-comparison/1", continuous_training=True,
            cases=dict(training=["t1", "t2", "t3"], development=["dev"], evaluation=["held"]),
            scheduler=dict(objective=objective()), learner_config={}, seed=5, update_after_tasks=1,
            development_evidence=dict(source_split="training"), prebuild=["t1", "held"])
        source = self.root / "recipe.json"
        source.write_text(json.dumps(self.plan), encoding="utf-8")
        self.recipe = dict(id="recipe", protocol=str(source),
            protocol_sha256=hashlib.sha256(source.read_bytes()).hexdigest(), grader_command=None)
        self.versions = SimpleNamespace(get_recipe=Mock(return_value=self.recipe),
            admission_boundary=nullcontext,
            busy=Mock(return_value=dict(can_switch=True)),
            get_internal=Mock(return_value=dict(id="parent", checkpoint_sha256="a" * 64)),
            capture_training_checkpoint=Mock(side_effect=lambda job, run, receipt: dict(
                id="v" + str(receipt["update_index"]), name="update", checkpoint_sha256=receipt["checkpoint"]["sha256"])),
            capture_run=Mock(return_value=dict(id="final-version")))
        self.execution = SimpleNamespace(policy=FakePolicy(), bind_policy_version=Mock(),
            config=dict(policy=dict(kind="rule")), policy_context=Mock(return_value=dict(id="original", kind="rule")), configure=Mock(),
            finish_workload=Mock(return_value=dict(status="COMPLETE", updated=False)))
        self.platform = SimpleNamespace(home=self.root, store=self.store, execution=self.execution)
        self.ops, self.runs = [], {}
        self.read = patch("seampact.policy_training.office_pilot._read_plan", side_effect=self.read_plan)
        self.pilot = patch("seampact.policy_training.office_pilot.handle", side_effect=self.handle)
        self.ready = patch.object(PolicyTraining, "_models_ready")
        self.read.start()
        self.pilot.start()
        self.ready_mock = self.ready.start()
        self.manager = PolicyTraining(self.platform, self.versions)

    def tearDown(self):
        self.manager.close()
        self.ready.stop()
        self.pilot.stop()
        self.read.stop()
        self.store.close()
        self.temp.cleanup()

    def read_plan(self, body):
        plan = json.loads(Path(body["protocol"]).read_bytes())
        return body["protocol_sha256"], plan, {key: {} for ids in plan["cases"].values() for key in ids}

    def checkpoint(self, run):
        path = self.root / (run["id"] + ".pt")
        self.execution.policy.save(path)
        return dict(path=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest())

    def handle(self, platform, body):
        self.ops.append(copy.deepcopy(body))
        op = body["op"]
        if op == "prebuild":
            return {}
        if op in {"start", "resume"}:
            plan = json.loads(Path(body["protocol"]).read_bytes())
            start = 0
            if op == "resume":
                series = self.store.get("office_pilot_series", body["protocol_sha256"])
                start = series["workloads"]["training:0"]["next_index"]
            ids = plan["cases"]["training"][start:]
            identity = "run-" + str(len(self.runs))
            run = dict(id=identity, protocol_sha256=body["protocol_sha256"], status="RUNNING", mode="training", batch=0,
                case_ids=ids, task_ids=[identity + "-" + key for key in ids], admitted=[], start_index=start,
                arrival_blocks=[dict(indices=[i], offsets_ms=[0], profile="batch") for i in range(len(ids))])
            self.runs[identity] = run
            self.execution.workload = dict(id=identity, task_ids=run["task_ids"], started=[])
            self.store.save("office_pilot", identity, run)
            return copy.deepcopy(run)
        run = self.runs[body["run_id"]]
        if op == "admit":
            index = len(run["admitted"])
            task_id = run["task_ids"][index]
            self.store.save("task", task_id, dict(id=task_id, status="COMPLETED", workspace_status="FINISHED",
                objective_result=dict(quality=1, metrics=dict(api_tokens=10))))
            run["admitted"].append(task_id)
            self.execution.workload["started"].append(task_id)
            self.store.save("office_pilot", run["id"], run)
            return dict(task_id=task_id)
        if op == "update":
            return dict(pending=True, update=self.execution.policy.update_interval())
        if op == "drain":
            return dict(status="AWAITING_SCORING")
        if op in {"finish", "end_prefix"}:
            result = self.execution.policy.update_interval()
            run.update(status="PAUSED" if op == "end_prefix" else "COMPLETE", checkpoint=self.checkpoint(run),
                next_index=run["start_index"] + len(run["admitted"]), workload=dict(updates=[result]))
            if op == "end_prefix":
                self.store.save("office_pilot_series", body["protocol_sha256"], dict(workloads={"training:0": dict(
                    id=run["id"], status="PAUSED", checkpoint=run["checkpoint"], next_index=run["next_index"])}))
            self.store.save("office_pilot", run["id"], run)
            return copy.deepcopy(run)
        raise AssertionError(op)

    def wait(self):
        self.manager.thread.join(3)
        self.assertFalse(self.manager.thread.is_alive())

    def test_fresh_is_training_only_and_captures_each_real_update(self):
        started = self.manager.start(dict(recipe_id="recipe", name="new"))
        self.wait()
        job = self.manager.detail(started["id"])
        self.assertEqual(job["status"], "COMPLETE", job.get("error"))
        self.assertEqual(job["version_id"], "final-version")
        self.assertEqual(job["actual_updates"], 3)
        self.assertEqual(self.versions.capture_training_checkpoint.call_count, 3)
        self.assertTrue(all(Path(row["checkpoint"]["path"]).is_file() for row in job["checkpoints"]))
        self.assertEqual([op["case_id"] for op in self.ops if op["op"] == "prebuild"], ["t1"])
        self.assertTrue(all(op["mode"] == "training" for op in self.ops if "mode" in op))
        self.assertEqual(job["completed_tasks"], 3)
        self.assertEqual(job["restore_status"], "RESTORED_FROZEN_INFERENCE")
        self.execution.configure.assert_called_with(dict(policy=dict(kind="rule")), policy_version=dict(id="original", kind="rule"))
        binding = self.execution.bind_policy_version.call_args[0][0]
        self.assertEqual(binding["id"], "v3")

    def test_branch_metadata_and_unique_protocol_leave_parent_untouched(self):
        self.versions.get_internal.return_value["learner_config"] = dict(entropy_coefficient=.03)
        body = dict(mode="branch", recipe_id="recipe", parent_id="parent",
            learner_overrides=dict(learning_rate=.001), objective_overrides=dict(smoothing=.2), seed=17)
        first = self.manager.start(body)
        self.wait()
        second = self.manager.start(body)
        self.wait()
        a = json.loads(Path(first["protocol"]).read_bytes())
        b = json.loads(Path(second["protocol"]).read_bytes())
        self.assertNotEqual(first["protocol_sha256"], second["protocol_sha256"])
        self.assertNotEqual(a["policy_training"]["run_nonce"], b["policy_training"]["run_nonce"])
        self.assertEqual(a["policy_version_parent"], dict(id="parent", mode="branch"))
        self.assertEqual(a["learner_config"]["learning_rate"], .001)
        self.assertEqual(a["learner_config"]["entropy_coefficient"], .03)
        self.assertEqual(a["scheduler"]["objective"]["pareto"]["smoothing"], .2)
        self.assertEqual(self.versions.capture_run.call_args.kwargs["parent_id"], "parent")
        self.assertEqual(json.loads(Path(self.recipe["protocol"]).read_bytes()), self.plan)

    def test_pause_settles_block_and_resume_uses_original_protocol(self):
        block_complete = threading.Event()
        continue_block = threading.Event()
        original = self.manager._run_block
        def block(*args):
            original(*args)
            block_complete.set()
            self.assertTrue(continue_block.wait(3))
        with patch.object(self.manager, "_run_block", side_effect=block):
            started = self.manager.start(dict(recipe_id="recipe"))
            self.assertTrue(block_complete.wait(3))
            self.manager.pause(dict(job_id=started["id"]))
            continue_block.set()
            self.wait()
        paused = self.manager.detail(started["id"])
        self.assertEqual(paused["status"], "PAUSED", paused.get("error"))
        self.assertEqual(paused["next_index"], 1)
        self.assertTrue(paused["resumable"])
        with self.assertRaisesRegex(ValueError, "不能修改"):
            self.manager.resume(dict(job_id=started["id"], seed=7))
        self.manager.resume(dict(job_id=started["id"]))
        self.wait()
        finished = self.manager.detail(started["id"])
        self.assertEqual(finished["status"], "COMPLETE", finished.get("error"))
        self.assertEqual(finished["protocol_sha256"], paused["protocol_sha256"])
        self.assertEqual(finished["completed_tasks"], 3)
        self.assertEqual(len(finished["segment_ids"]), 2)
        self.assertEqual(len([op for op in self.ops if op["op"] == "admit"]), 3)

    def test_unknown_native_quality_fails_without_registration(self):
        original = self.handle
        def unscored(platform, body):
            result = original(platform, body)
            if body["op"] == "admit":
                task = self.store.get("task", result["task_id"])
                task["objective_result"]["quality"] = None
                self.store.save("task", task["id"], task)
            return result
        self.pilot.stop()
        self.pilot = patch("seampact.policy_training.office_pilot.handle", side_effect=unscored)
        self.pilot.start()
        job = self.manager.start(dict(recipe_id="recipe"))
        self.wait()
        failed = self.manager.detail(job["id"])
        self.assertEqual(failed["status"], "FAILED")
        self.assertFalse(failed["resumable"])
        self.versions.capture_run.assert_not_called()
        self.assertEqual(len([op for op in self.ops if op["op"] == "admit"]), 1)
        self.assertEqual(failed["cleanup_status"], "RELEASED_WITHOUT_UPDATE")
        self.assertEqual(failed["restore_status"], "RESTORED_FROZEN_INFERENCE")
        self.assertEqual(self.store.get("office_pilot", failed["run_id"])["status"], "UNSETTLED")
        self.execution.finish_workload.assert_called_once_with(admitted_prefix=(failed["run_id"], [failed["run_id"] + "-t1"]))

    def test_recipe_identity_and_measured_scales_are_enforced(self):
        with self.assertRaisesRegex(ValueError, "成本尺度"):
            self.manager.start(dict(recipe_id="recipe", objective_overrides=dict(scales={})))
        Path(self.recipe["protocol"]).write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "发生变化"):
            self.manager.start(dict(recipe_id="recipe"))
        self.assertFalse(self.ops)

    def test_complete_rollout_defaults_allow_explicit_gamma_one(self):
        plan = self.manager._overrides(self.plan, dict(learner_overrides=dict(gamma=1)))
        self.assertEqual(plan["learner_config"]["gamma"], 1)
        with self.assertRaisesRegex(ValueError, "gamma=1"):
            self.manager._overrides(self.plan, dict(learner_overrides=dict(gae_lambda=.9)))

    def test_arrival_blocks_do_not_override_update_cadence(self):
        job = self.manager.start(dict(recipe_id="recipe", update_after_tasks=3))
        self.wait()
        finished = self.manager.detail(job["id"])
        self.assertEqual(finished["status"], "COMPLETE", finished.get("error"))
        self.assertEqual(finished["actual_updates"], 1)
        self.assertEqual([op for op in self.ops if op["op"] == "update"], [])

    def test_restart_marks_live_rollout_interrupted_not_resumable(self):
        job = dict(id="lost", status="RUNNING", resumable=True)
        self.store.save("policy_training", "lost", job)
        recovered = PolicyTraining(self.platform, self.versions)
        self.assertEqual(recovered.detail("lost")["status"], "INTERRUPTED")
        self.assertFalse(recovered.detail("lost")["resumable"])
        with self.assertRaisesRegex(ValueError, "PAUSED"):
            recovered.resume(dict(job_id="lost"))
        recovered.close()

    def test_unavailable_model_fails_before_any_admission(self):
        self.ready_mock.side_effect = RuntimeError("训练业务模型服务尚未启动或尚未 READY")
        job = self.manager.start(dict(recipe_id="recipe"))
        self.wait()
        failed = self.manager.detail(job["id"])
        self.assertEqual(failed["status"], "FAILED")
        self.assertIn("尚未启动", failed["error"])
        self.assertFalse(self.ops)

    def test_busy_platform_rejects_before_freezing_or_dispatch(self):
        self.versions.busy.return_value = dict(can_switch=False, reason="任务正在准备或执行")
        with self.assertRaisesRegex(ValueError, "正在准备"):
            self.manager.start(dict(recipe_id="recipe"))
        self.assertFalse(self.store.list("policy_training"))
        self.assertFalse(self.ops)

    def test_shutdown_with_unsettled_rollout_is_not_resumable(self):
        admitted = threading.Event()
        def blocked(job, run, block):
            admitted.set()
            while True:
                self.manager._wait(.01)
        with patch.object(self.manager, "_run_block", side_effect=blocked):
            job = self.manager.start(dict(recipe_id="recipe"))
            self.assertTrue(admitted.wait(3))
            self.manager.request_shutdown()
            self.manager.shutdown_deadline = 0
            self.manager.close()
        interrupted = self.manager.detail(job["id"])
        self.assertEqual(interrupted["status"], "INTERRUPTED")
        self.assertFalse(interrupted["resumable"])
        self.assertFalse(self.manager.thread.is_alive())
        self.versions.capture_run.assert_not_called()
        self.assertEqual(interrupted["restore_status"], "SKIPPED_SHUTDOWN")
        self.execution.configure.assert_not_called()

    def test_failed_active_prefix_can_be_retried_after_tasks_finish(self):
        run_id, job_id, task_id = "retained-run", "retained-job", "retained-task"
        run = dict(id=run_id, mode="training", status="RUNNING", protocol_sha256="frozen",
            task_ids=[task_id, "never-admitted"], admitted=[task_id])
        job = dict(id=job_id, status="FAILED", error="Native quality missing", resumable=False,
            run_id=run_id, segment_ids=[run_id], protocol_sha256="frozen", planned_tasks=2, actual_updates=0,
            original_config=dict(policy=dict(kind="rule")), original_policy_context=dict(id="original", kind="rule"))
        self.store.save("policy_training", job_id, job)
        self.store.save("office_pilot", run_id, run)
        self.store.save("task", task_id, dict(id=task_id, status="RUNNING", workspace_status="ACTIVE"))
        self.execution.workload = dict(id=run_id, status="RUNNING", task_ids=run["task_ids"], started=[task_id])
        def configure(*args, **kwargs):
            if self.execution.workload["status"] != "COMPLETE":
                raise ValueError("任务或实例尚未收尾")
        def release(**kwargs):
            self.execution.workload["status"] = "COMPLETE"
            return dict(status="COMPLETE", updated=False)
        self.execution.configure.side_effect = configure
        self.execution.finish_workload.side_effect = release
        # Advance only the manual retry deadline; do not wait for a real model.
        with patch("seampact.policy_training.time.monotonic", side_effect=[0, 4]):
            pending = self.manager.cleanup(dict(job_id=job_id))
        self.assertEqual(pending["cleanup_status"], "REQUIRES_DRAIN")
        self.assertIn("仍在执行", pending["cleanup_error"])
        self.assertEqual(pending["restore_status"], "REQUIRES_SELECTION")
        self.execution.finish_workload.assert_not_called()
        self.store.save("task", task_id, dict(id=task_id, status="COMPLETED", workspace_status="FINISHED"))
        released = self.manager.cleanup(dict(job_id=job_id))
        self.assertEqual(released["status"], "FAILED")
        self.assertFalse(released["resumable"])
        self.assertEqual(released["cleanup_status"], "RELEASED_WITHOUT_UPDATE")
        self.assertEqual(released["restore_status"], "RESTORED_FROZEN_INFERENCE")
        self.assertNotIn("cleanup_error", released)
        self.assertNotIn("restore_error", released)
        self.execution.finish_workload.assert_called_once_with(admitted_prefix=(run_id, [task_id]))
        self.assertEqual(self.store.get("office_pilot", run_id)["status"], "UNSETTLED")
        self.assertFalse(self.ops)
        self.versions.capture_training_checkpoint.assert_not_called()
        self.versions.capture_run.assert_not_called()

    def test_cleanup_rejects_unrelated_workload_without_releasing_or_restoring(self):
        run = dict(id="failed-run", mode="training", status="RUNNING", protocol_sha256="frozen")
        job = dict(id="failed-job", status="FAILED", run_id=run["id"], segment_ids=[run["id"]], protocol_sha256="frozen")
        self.store.save("office_pilot", run["id"], run)
        self.store.save("policy_training", job["id"], job)
        self.execution.workload = dict(id="another-run", status="RUNNING")
        with self.assertRaisesRegex(ValueError, "不属于"):
            self.manager.cleanup(dict(job_id=job["id"]))
        self.execution.finish_workload.assert_not_called()
        self.execution.configure.assert_not_called()
        self.assertFalse(self.ops)


class ParentPilotChecks(unittest.TestCase):
    def test_registered_parent_does_not_reinitialize_and_safe_resume_uses_original_checkpoint(self):
        from seampact.office_pilot import handle
        roles = ["sanitize", "injection", "privacy", "secrets", "code_scan"]
        units = [dict(id=role, runtime_group=role, status="READY", generation_verification=dict(status="SUCCEEDED")) for role in roles]
        rag = dict(id="rag", runtime_group="rag", status="READY", generation_verification=dict(status="SUCCEEDED"))
        plan = dict(schema="seampact.office-comparison/1", project_id="project", rag=dict(capability_id="rag"),
            scheduler=dict(objective=objective()), policies=dict(app_config=dict(kind="rule")),
            cases=dict(training=["a", "b"], development=["dev"], evaluation=["held"]),
            continuous_training=True, learner_config=dict(learning_rate=.001), seed=17,
            training_calibration=dict(case_ids=["a"]),
            development_evidence=dict(source_split="training", case_ids=["a"], pool={}, application=dict(frozen=True),
                run_ids=["calibrated"], protocol_sha256="calibration"),
            policy_version_parent=dict(id="parent", mode="branch"))
        order = []
        parent_policy = SimpleNamespace(version_transfer=dict(parent_id="parent"))
        execution = SimpleNamespace(_pool=lambda: {}, configure=Mock(side_effect=lambda _: order.append("configure")),
            config=plan["scheduler"], start_workload=Mock(side_effect=lambda *a, **k: order.append("start") or {}))
        training_policy = Mock(side_effect=lambda *a, **k: order.append("load-parent") or parent_policy)
        project = dict(guard_candidates=[dict(role=role, unit_id=role) for role in roles], enabled_group_ids=["rag"])
        cases = dict(a={}, b={})
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "platform.sqlite3")
            try:
                store.save("office_pilot", "calibrated", dict(mode="training_calibration", status="COMPLETE",
                    protocol_sha256="calibration", case_ids=["a"]))
                platform = SimpleNamespace(store=store, execution=execution,
                    policy_versions=SimpleNamespace(training_policy=training_policy),
                    project_applications=SimpleNamespace(snapshot=lambda *_: (project, units)),
                    capabilities=SimpleNamespace(public=lambda: [], unit=lambda _: rag))
                with patch("seampact.office_pilot._read_plan", return_value=("protocol", plan, cases)), \
                     patch("seampact.office_pilot.frozen_application", return_value=dict(frozen=True)):
                    row = handle(platform, dict(op="start", mode="training", protocol=str(Path(tmp) / "plan.json")))
                    self.assertEqual(order, ["configure", "load-parent", "start"])
                    self.assertIs(execution.policy, parent_policy)
                    self.assertFalse(execution.start_workload.call_args.kwargs["fresh"])
                    self.assertIsNone(execution.start_workload.call_args.kwargs["learner_config"])
                    self.assertEqual(row["policy_version_parent"], dict(id="parent", mode="branch"))
                    self.assertIsNotNone(store.get("office_pilot_protocol", "protocol"))
                    checkpoint = dict(path="paused.pt", sha256="saved")
                    store.save("office_pilot_series", "protocol", dict(id="protocol", workloads={"training:0": dict(
                        id=row["id"], status="PAUSED", checkpoint=checkpoint, next_index=1, segments=[row["id"]])}))
                    execution.start_workload.reset_mock()
                    training_policy.reset_mock()
                    resumed = handle(platform, dict(op="resume", mode="training"))
                    training_policy.assert_not_called()
                    self.assertTrue(execution.start_workload.call_args.kwargs["fresh"])
                    self.assertEqual(execution.start_workload.call_args.kwargs["resume_checkpoint"], checkpoint)
                    self.assertEqual(resumed["case_ids"], ["b"])
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
