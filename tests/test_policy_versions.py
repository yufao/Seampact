"""Registered artifacts stay immutable and bind actual scheduler work."""

import hashlib
import importlib.util
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from seampact.policy_versions import PolicyVersions
from seampact.scheduling.capability_scheduler import (
    CANDIDATE_DIM, OBSERVATION_DIM, CapabilityScheduler, freeze_config, policy_config_id,
)
from seampact.store import Store


SETTINGS = dict(hidden_sizes=[4], epochs=1, batch_size=1, entropy_coefficient=0)


def objective():
    preference = dict(quality=0.2, latency=0.2, ram=0.2, vram=0.2, api=0.2)
    return dict(weights=dict(preference, delivery=0),
        scales=dict(latency_s=1, ram_byte_s=1, vram_byte_s=1, api_tokens=1),
        quality_evaluator="fixture-native-score/1", deadline_s=None, resource_source="reserved",
        pareto=dict(preference=preference, smoothing=0.1))


class FakeDeployment:
    def __init__(self):
        self.lock, self.instances, self.affinity = threading.RLock(), {}, {}
        self.released_tasks = []

    @staticmethod
    def _held(instance):
        return instance.get("released") is not True

    def release_task(self, task_id):
        self.released_tasks.append(task_id)


class PolicyVersionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.home = self.folder / "home"
        self.store = Store(self.home / "state.sqlite3")
        self.addCleanup(self.store.close)
        self.deployment = FakeDeployment()
        self.platform = SimpleNamespace(home=self.home, store=self.store)
        self.platform.execution = self.scheduler(self.store, config=dict(objective=objective()))
        self.versions = PolicyVersions(self.platform)

    def scheduler(self, store, config=None):
        scheduler = CapabilityScheduler(SimpleNamespace(store=store), self.deployment,
                                         store=store, config=config)
        # The real ledger and selection paths run synchronously; these tests do
        # not need controller actions or resource probes in background threads.
        scheduler.close()
        scheduler.closed = False
        self.addCleanup(scheduler.close)
        return scheduler

    def save_rule(self, name="baseline", **policy):
        if policy:
            config = self.platform.execution.config
            config["policy"] = dict(kind="rule", **policy)
            self.platform.execution.configure(config)
        return self.versions.save_current(dict(name=name))

    def make_protocol(self):
        folder = self.home / "experiment"
        folder.mkdir(exist_ok=True)
        cases = [dict(id=split, split=pool, family_id=split, suite="fixture")
                 for split, pool in (("development", "development_pool"), ("training", "train_pool"),
                                     ("evaluation", "evaluation"))]
        manifest = folder / "manifest.json"
        manifest.write_text(json.dumps(dict(cases=cases)), encoding="utf-8")
        plan = dict(schema="seampact.office-comparison/1", manifest=str(manifest),
            manifest_sha256=hashlib.sha256(manifest.read_bytes()).hexdigest(),
            cases={split: [split] for split in ("development", "training", "evaluation")},
            continuous_training=True, update_after_tasks=1, scheduler=freeze_config(dict(objective=objective())),
            learner_config=SETTINGS, seed=7)
        path = folder / "frozen-pilot-protocol.json"
        path.write_text(json.dumps(plan), encoding="utf-8")
        return path, plan

    def make_completed_run(self):
        from seampact.scheduling.capability_ppo import COMPONENTS, CandidatePPO
        path, plan = self.make_protocol()
        policy = CandidatePPO(policy_config_id(plan["scheduler"]), plan["scheduler"]["objective"],
                              config=SETTINGS, seed=7, training=True)
        candidates = [dict(features=[0.0] * CANDIDATE_DIM), dict(features=[1.0] * CANDIDATE_DIM)]
        observation = [0.0] * OBSERVATION_DIM
        decision = dict(policy_config_id=policy.identity["config_id"],
            feature_schema=policy.identity["feature_schema"], session="fixture-run",
            observation=observation, candidates=candidates, selection=policy.choose(observation, candidates))
        components = {term: dict(before_first=0.0, after_decisions=[0.5]) for term in COMPONENTS}
        planned = policy.interval_weights(components, 1, 1)
        reward = policy._compose({term: 0.5 for term in COMPONENTS}, planned["weights"])
        update = policy.update_workload([decision], dict(before_first=0.0, after_decisions=[reward]),
            dict(training_ready=True, complete=True, objective_id=policy.identity["objective_id"],
                 policy_config_id=policy.identity["config_id"], session="fixture-run", utility=reward,
                 completed_tasks=1, normalizer=1, component_settlements=components,
                 optimization_weights=planned["weights"], scalarization=planned["receipt"]))
        checkpoint = path.parent / "source.pt"
        policy.save(checkpoint)
        run = dict(id="fixture-run", mode="training", status="COMPLETE", case_ids=["training"],
            protocol_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            checkpoint=dict(path=str(checkpoint), sha256=hashlib.sha256(checkpoint.read_bytes()).hexdigest()),
            workload=dict(update=update))
        self.store.save("office_pilot", run["id"], run)
        return path, plan, policy, run

    def test_saving_rule_is_immutable_and_same_request_is_idempotent(self):
        first = self.versions.save_current(dict(name="baseline", notes="original note"))
        again = self.versions.save_current(dict(name="baseline", notes="original note"))
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(again["name"], "baseline")
        self.assertEqual(again["notes"], "original note")
        self.assertEqual(len(self.store.list("policy_version")), 1)
        named = self.versions.save_current(dict(name="another label", notes="later note"))
        self.assertNotEqual(named["id"], first["id"])
        self.assertEqual(named["name"], "another label")
        self.assertEqual(self.versions.get_internal(first["id"])["notes"], "original note")
        self.assertIsNone(first["checkpoint"])
        first["config"]["max_in_flight"] = 100
        self.assertEqual(self.versions.get_internal(first["id"])["config"]["max_in_flight"], 4)

    def test_selection_binds_begin_decision_and_finished_result(self):
        record = self.save_rule(optional_hooks="none")
        self.versions.select(dict(version_id=record["id"]))
        scheduler = self.platform.execution
        begun = scheduler.begin("task")
        reference = begun["policy_version"]
        self.assertEqual(reference["id"], record["id"])
        self.assertEqual(reference["config_id"], scheduler.config_id)
        self.assertEqual(scheduler.tasks.active["task"]["policy_version"], reference)
        chosen = scheduler.choose_optional("task", "input_received", [dict(name="optional-check")])
        self.assertEqual(chosen["kind"], "continue")
        decision = self.store.list("capability_decision")[0]
        self.assertEqual(decision["policy_version"], reference)
        finished = scheduler.finish("task", delivered=True, quality=0.5, metrics=dict(api_tokens=0))
        self.assertEqual(finished["policy_version"], reference)
        self.assertEqual(self.store.get("capability_task_result", "task")["policy_version"], reference)
        reference["name"] = "caller mutation"
        self.assertEqual(scheduler.policy_context()["name"], record["name"])

    def test_registered_selection_survives_store_and_scheduler_reopening(self):
        record = self.save_rule(optional_hooks="none")
        self.versions.select(dict(version_id=record["id"]))
        reopened_store = Store(self.home / "state.sqlite3")
        self.addCleanup(reopened_store.close)
        restarted = self.scheduler(reopened_store)
        platform = SimpleNamespace(home=self.home, store=reopened_store, execution=restarted)
        versions = PolicyVersions(platform)
        self.assertEqual(versions.get_internal(record["id"]), record)
        self.assertEqual(versions.snapshot()["active"]["id"], record["id"])
        self.assertEqual(restarted.begin("after-restart")["policy_version"]["id"], record["id"])

    def test_manual_reconfiguration_clears_old_version_binding(self):
        record = self.save_rule()
        self.versions.select(dict(version_id=record["id"]))
        config = self.platform.execution.config
        config["policy"] = dict(kind="rule", optional_hooks="none")
        self.platform.execution.configure(config)
        self.assertNotIn("id", self.platform.execution.policy_context())
        self.assertIsNone(self.versions.snapshot()["active"])
        self.assertNotIn("id", self.scheduler(self.store).policy_context())

    def test_active_task_blocks_save_select_and_direct_configure(self):
        record = self.save_rule()
        self.versions.select(dict(version_id=record["id"]))
        scheduler = self.platform.execution
        scheduler.begin("busy")
        before = scheduler.policy_context()
        for action in (lambda: self.versions.save_current(dict(name="busy snapshot")),
                       lambda: self.versions.select(dict(version_id=record["id"])),
                       lambda: scheduler.configure(scheduler.config)):
            with self.assertRaisesRegex(ValueError, "任务|收尾"):
                action()
        self.assertEqual(scheduler.policy_context(), before)
        self.assertEqual(len(self.store.list("policy_version")), 1)

    def test_preparing_chat_training_and_held_instance_block_switching(self):
        record = self.save_rule()
        scheduler = self.platform.execution
        for status in ("RUNNING", "CANCELLING"):
            with self.subTest(chat_status=status):
                self.store.save("task", "chat", dict(id="chat", status=status))
                with self.assertRaisesRegex(ValueError, "准备或执行"):
                    self.versions.select(dict(version_id=record["id"]))
                self.store.delete("task", "chat")
        for status in ("QUEUED", "RUNNING", "PAUSING"):
            with self.subTest(training_status=status):
                self.store.save("policy_training", "job", dict(id="job", status=status))
                with self.assertRaisesRegex(ValueError, "训练正在进行"):
                    self.versions.select(dict(version_id=record["id"]))
        self.store.save("policy_training", "job", dict(id="job", status="PAUSED"))
        self.versions.select(dict(version_id=record["id"]))
        self.deployment.instances["held"] = dict(released=False)
        scheduler.tracked.add("held")
        with self.assertRaisesRegex(ValueError, "实例尚未收尾"):
            self.versions.select(dict(version_id=record["id"]))
        scheduler.tracked.clear()

    def test_version_labels_stay_out_of_actor_observation(self):
        scheduler = self.platform.execution
        original = scheduler._observation("input_received", dict(text_chars=100))
        scheduler.bind_policy_version(dict(id="version-a", name="unrelated label"))
        self.assertEqual(scheduler._observation("input_received", dict(text_chars=100)), original)
        scheduler.bind_policy_version(dict(id="version-b", name="other label", training_job_id="job"))
        self.assertEqual(scheduler._observation("input_received", dict(text_chars=100)), original)
        self.assertEqual(len(original), OBSERVATION_DIM)

    def test_select_and_save_match_scheduler_and_chat_admission_lock_order(self):
        record = self.save_rule()
        scheduler = self.platform.execution
        held, acquired = [], []
        test = self

        class OrderedLock:
            def __init__(self, wrapped, name, rank):
                self.wrapped, self.name, self.rank, self.depth = wrapped, name, rank, 0

            def __enter__(self):
                if not self.depth:
                    test.assertTrue(not held or held[-1] < self.rank,
                                    "lock order inversion before acquiring " + self.name)
                self.wrapped.__enter__()
                if not self.depth:
                    held.append(self.rank)
                    acquired.append(self.name)
                self.depth += 1
                return self

            def __exit__(self, *args):
                self.depth -= 1
                if not self.depth:
                    test.assertEqual(held.pop(), self.rank)
                return self.wrapped.__exit__(*args)

            def __getattr__(self, name):
                return getattr(self.wrapped, name)

        condition, deployment_lock = scheduler.condition, self.deployment.lock
        scheduler.condition = OrderedLock(condition, "execution", 0)
        self.deployment.lock = OrderedLock(deployment_lock, "deployment", 1)
        # In the real platform project admission uses the same store RLock.
        self.platform.chat = SimpleNamespace(_start_lock=OrderedLock(self.store.lock, "project", 2))
        try:
            self.assertIs(self.versions.lock, self.store.lock)
            for action in (lambda: self.versions.select(dict(version_id=record["id"])),
                           lambda: self.versions.save_current(dict(name="another snapshot"))):
                acquired.clear()
                action()
                self.assertEqual(acquired, ["execution", "deployment", "project"])
                self.assertEqual(held, [])
        finally:
            scheduler.condition, self.deployment.lock = condition, deployment_lock

    def test_protocol_must_exist_inside_platform_home(self):
        path, _ = self.make_protocol()
        outside = self.folder / "outside.json"
        outside.write_bytes(path.read_bytes())
        for candidate in (outside, self.home / ".." / "outside.json", self.home / "missing.json"):
            with self.subTest(candidate=candidate), self.assertRaisesRegex(ValueError, "数据目录内"):
                self.versions.import_protocol(dict(protocol=str(candidate)))
        self.assertEqual(self.store.list("policy_recipe"), [])

    def test_import_reuses_recorded_grader_without_starting_a_process(self):
        path, _ = self.make_protocol()
        command = ["fixture-independent-grader", "--collection", "{collection}"]
        (path.parent / "grader-argv.json").write_text(json.dumps(command), encoding="utf-8")
        recipe = self.versions.import_protocol(dict(protocol=str(path)))["recipe"]
        self.assertEqual(recipe["grader_command"], command)
        self.assertEqual(self.store.list("policy_training"), [])
        self.assertEqual(self.store.list("office_pilot"), [])
        self.assertEqual(len(self.store.list("policy_recipe")), 1)

    def test_protocol_is_retained_and_later_tampering_is_rejected(self):
        path, _ = self.make_protocol()
        imported = self.versions.import_protocol(dict(protocol=str(path), name="fixture"))
        recipe = imported["recipe"]
        retained = Path(recipe["protocol"])
        self.assertNotEqual(retained, path)
        self.assertTrue(retained.is_relative_to(self.home / "policy-versions" / "recipes"))
        path.write_text("source changed", encoding="utf-8")
        self.assertEqual(self.versions.get_recipe(recipe["id"]), recipe)
        retained.write_bytes(retained.read_bytes() + b"changed")
        with self.assertRaisesRegex(ValueError, "内容已变化"):
            self.versions.get_recipe(recipe["id"])

    @unittest.skipUnless(importlib.util.find_spec("torch"), "optional PyTorch dependency")
    def test_reimported_completed_run_has_one_version_and_managed_independent_checkpoint(self):
        path, _, _, run = self.make_completed_run()
        first = self.versions.import_protocol(dict(protocol=str(path), name="fixture"))
        second = self.versions.import_protocol(dict(protocol=str(path), name="renamed recipe"))
        self.assertEqual(first["errors"], [])
        self.assertEqual(second["errors"], [])
        self.assertEqual(first["imported"], second["imported"])
        self.assertEqual(len(first["imported"]), 1)
        self.assertEqual(len(self.store.list("policy_version")), 1)
        record = self.versions.get_internal(first["imported"][0])
        managed = Path(record["checkpoint"]["path"])
        source = Path(run["checkpoint"]["path"])
        self.assertNotEqual(managed, source)
        self.assertTrue(managed.is_relative_to(self.home / "policy-versions" / "weights"))
        original = managed.read_bytes()
        source.write_bytes(b"source checkpoint replaced")
        self.versions.select(dict(version_id=record["id"]))
        self.assertEqual(managed.read_bytes(), original)
        self.assertEqual(self.platform.execution.policy_context()["id"], record["id"])

    @unittest.skipUnless(importlib.util.find_spec("torch"), "optional PyTorch dependency")
    def test_saving_successive_ppo_versions_never_overwrites_parent_weights(self):
        from seampact.scheduling.capability_ppo import CandidatePPO
        scheduler = self.platform.execution
        scheduler.policy = CandidatePPO(policy_config_id(scheduler.config), scheduler.config["objective"],
                                        config=SETTINGS, seed=7, training=True)
        first = self.versions.save_current(dict(name="parent"))
        parent_path = Path(first["checkpoint"]["path"])
        parent_bytes = parent_path.read_bytes()
        with scheduler.policy.learner.torch.no_grad():
            next(scheduler.policy.learner.networks["actor"].parameters()).add_(1)
        second = self.versions.save_current(dict(name="child"))
        self.assertNotEqual(second["checkpoint"]["path"], first["checkpoint"]["path"])
        self.assertNotEqual(second["checkpoint_sha256"], first["checkpoint_sha256"])
        self.assertEqual(parent_path.read_bytes(), parent_bytes)
        self.assertEqual(self.versions.get_internal(first["id"]), first)
        self.assertEqual(list((self.home / "policy-versions").glob("*.pt")), [])

    @unittest.skipUnless(importlib.util.find_spec("torch"), "optional PyTorch dependency")
    def test_managed_checkpoint_tampering_blocks_select_and_training_without_switch(self):
        path, plan, _, _ = self.make_completed_run()
        imported = self.versions.import_protocol(dict(protocol=str(path)))
        record = self.versions.get_internal(imported["imported"][0])
        checkpoint = Path(record["checkpoint"]["path"])
        checkpoint.write_bytes(checkpoint.read_bytes() + b"changed")
        before = self.platform.execution.policy_context()
        for action in (lambda: self.versions.select(dict(version_id=record["id"])),
                       lambda: self.versions.training_policy(record["id"], plan["scheduler"], mode="branch")):
            with self.assertRaisesRegex(ValueError, "身份"):
                action()
        self.assertEqual(self.platform.execution.policy_context(), before)

    @unittest.skipUnless(importlib.util.find_spec("torch"), "optional PyTorch dependency")
    def test_failed_completed_run_import_does_not_publish_a_policy(self):
        path, _, _, run = self.make_completed_run()
        run["checkpoint"]["sha256"] = "0" * 64
        self.store.save("office_pilot", run["id"], run)
        imported = self.versions.import_protocol(dict(protocol=str(path)))
        self.assertEqual(imported["imported"], [])
        self.assertEqual(len(imported["errors"]), 1)
        self.assertEqual(self.store.list("policy_version"), [])
        self.assertEqual(list((self.home / "policy-versions" / "weights").glob("*.pt")), [])


if __name__ == "__main__":
    unittest.main()
