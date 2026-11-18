"""Policy versions preserve exact continuation and start honest new branches."""

import copy
import hashlib
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from seampact.scheduling.capability_ppo import COMPONENTS, CandidatePPO
from seampact.scheduling.capability_scheduler import (
    CANDIDATE_DIM, FEATURE_SCHEMA, OBSERVATION_DIM, RulePolicy, freeze_config, policy_config_id,
)
from seampact.scheduling.policy_checkpoint import load_version_policy

SETTINGS = dict(hidden_sizes=[4], epochs=1, batch_size=2, entropy_coefficient=0)


def objective():
    preference = {term: 0.2 for term in COMPONENTS}
    return dict(weights=dict(preference, delivery=0),
        scales=dict(latency_s=1, ram_byte_s=1, vram_byte_s=1, api_tokens=1),
        quality_evaluator="fixture-native-score/1", deadline_s=None, resource_source="reserved",
        pareto=dict(preference=preference, smoothing=0.1))


@unittest.skipUnless(importlib.util.find_spec("torch"), "optional PyTorch dependency")
class PolicyCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "parent.pt"
        base = freeze_config(dict(objective=objective()))
        self.parent = CandidatePPO(policy_config_id(base), base["objective"],
                                   config=SETTINGS, seed=7, training=True)
        self.train(self.parent, "past-workload", workload=True)
        self.train(self.parent, "past-stream", interval="one")
        # PPO consumes the torch generator; advance the independent Python RNG
        # too so a reset cannot accidentally pass the exact-resume comparison.
        for _ in range(3):
            self.parent.learner.random.random()
        self.parent.save(self.path)
        checkpoint = dict(path=str(self.path), sha256=hashlib.sha256(self.path.read_bytes()).hexdigest())
        config = freeze_config(dict(base, policy=dict(kind="ppo", checkpoint=str(self.path),
            checkpoint_sha256=checkpoint["sha256"], learner_config=SETTINGS, seed=7)))
        self.record = dict(id="parent-version", name="parent", config=config, checkpoint=checkpoint,
                           learner_config=copy.deepcopy(SETTINGS), seed=7, feature_schema=FEATURE_SCHEMA)

    def train(self, policy, session, interval=None, workload=False):
        decisions = []
        for value in (0.0, 1.0):
            candidates = [dict(features=[0.0] * CANDIDATE_DIM), dict(features=[1.0] * CANDIDATE_DIM)]
            observation = [value] * OBSERVATION_DIM
            decisions.append(dict(policy_config_id=policy.identity["config_id"], feature_schema=FEATURE_SCHEMA,
                session=session, observation=observation, candidates=candidates,
                selection=policy.choose(observation, candidates)))
        components = {term: dict(before_first=0.0, after_decisions=values) for term, values in
                      zip(COMPONENTS, ([0.25, 0.5], [1.0, 2.0], [3.0, 1.0], [0.0, 1.0], [4.0, 2.0]))}
        planned = policy.interval_weights(components, 1, 1)
        rewards = [policy._compose({term: components[term]["after_decisions"][index] for term in COMPONENTS},
                                   planned["weights"]) for index in range(2)]
        settlements = dict(before_first=0.0, after_decisions=rewards)
        summary = dict(training_ready=True, complete=True, objective_id=policy.identity["objective_id"],
            policy_config_id=policy.identity["config_id"], session=session, utility=sum(rewards),
            completed_tasks=1, normalizer=1, component_settlements=components,
            optimization_weights=planned["weights"], scalarization=planned["receipt"])
        if workload:
            return policy.update_workload(decisions, settlements, summary)
        summary["interval_id"] = interval
        return policy.update_interval(decisions, settlements, summary, final=True)

    def assert_nested_equal(self, left, right):
        if isinstance(left, self.parent.learner.torch.Tensor):
            self.assertTrue(self.parent.learner.torch.equal(left, right))
        elif isinstance(left, dict):
            self.assertEqual(left.keys(), right.keys())
            for key in left:
                self.assert_nested_equal(left[key], right[key])
        elif isinstance(left, (list, tuple)):
            self.assertEqual(len(left), len(right))
            for first, second in zip(left, right):
                self.assert_nested_equal(first, second)
        else:
            self.assertEqual(left, right)

    def assert_networks_equal(self, first, second, names=None):
        for name in names or first.learner.networks:
            self.assert_nested_equal(first.learner.networks[name].state_dict(),
                                     second.learner.networks[name].state_dict())

    def assert_fresh_state(self, child, seed=7):
        fresh = CandidatePPO(child.identity["config_id"], child.objective,
                             config=child.learner.config, seed=seed, training=True)
        learner = child.learner
        self.assertTrue(child.training)
        self.assertEqual((learner.steps, learner.updates, learner.policy_version), (0, 0, 0))
        self.assertEqual(dict(learner.optimizers["policy"].state), {})
        self.assertEqual(learner.rollout, [])
        self.assertEqual(list(learner.replay), [])
        self.assertEqual(learner.last_stats, {})
        self.assertEqual(learner.random.getstate(), fresh.learner.random.getstate())
        self.assert_nested_equal(learner.generator.get_state(), fresh.learner.generator.get_state())
        self.assertEqual(child._pareto_state(), fresh._pareto_state())
        return fresh

    def rehash_record(self):
        checksum = hashlib.sha256(self.path.read_bytes()).hexdigest()
        self.record["checkpoint"]["sha256"] = checksum
        self.record["config"]["policy"]["checkpoint_sha256"] = checksum

    def test_resume_restores_full_state_and_produces_identical_next_update(self):
        before = self.path.read_bytes()
        resumed = load_version_policy(self.record, mode="resume")
        self.assertEqual(resumed.snapshot(), self.parent.snapshot())
        self.assert_networks_equal(self.parent, resumed)
        self.assert_nested_equal(self.parent.learner.optimizers["policy"].state_dict(),
                                 resumed.learner.optimizers["policy"].state_dict())
        self.assertEqual(self.parent.learner.random.random(), resumed.learner.random.random())
        first = self.train(self.parent, "next", interval="two")
        second = self.train(resumed, "next", interval="two")
        self.assertEqual(first, second)
        self.assert_networks_equal(self.parent, resumed)
        self.assert_nested_equal(self.parent.learner.optimizers["policy"].state_dict(),
                                 resumed.learner.optimizers["policy"].state_dict())
        self.assert_nested_equal(self.parent.learner.generator.get_state(), resumed.learner.generator.get_state())
        self.assertEqual(resumed._pareto_state(), self.parent._pareto_state())
        self.assertIn("optimizer", resumed.version_transfer["transferred"])
        self.assertEqual(self.path.read_bytes(), before)

    def test_learning_rate_branch_inherits_all_networks_with_fresh_training_state(self):
        before, record_before = self.path.read_bytes(), copy.deepcopy(self.record)
        child = load_version_policy(self.record, learner_config=dict(learning_rate=0.012), seed=13, mode="branch")
        self.assert_networks_equal(self.parent, child)
        self.assert_fresh_state(child, seed=13)
        self.assertEqual(child.learner.optimizers["policy"].param_groups[0]["lr"], 0.012)
        self.assertEqual(child.version_transfer["transferred"], ["actor", "critics"])
        self.assertIn("pareto", child.version_transfer["reset"])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.record, record_before)
        with child.learner.torch.no_grad():
            next(child.learner.networks["actor"].parameters()).add_(1)
        self.assertEqual(self.path.read_bytes(), before)

    def test_preference_and_smoothing_branches_reuse_component_critics(self):
        for change in ("preference", "smoothing"):
            with self.subTest(change=change):
                target = copy.deepcopy(self.record["config"])
                if change == "preference":
                    preference = dict(quality=0.4, latency=0.15, ram=0.15, vram=0.15, api=0.15)
                    target["objective"]["pareto"]["preference"] = preference
                    target["objective"]["weights"] = dict(preference, delivery=0)
                else:
                    target["objective"]["pareto"]["smoothing"] = 0.35
                child = load_version_policy(self.record, target_config=target, mode="branch")
                self.assert_networks_equal(self.parent, child)
                self.assert_fresh_state(child)
                self.assertNotEqual(child.identity["objective_id"], self.parent.identity["objective_id"])
                self.assertEqual(child.coefficients, target["objective"]["pareto"]["preference"])
                self.assertEqual(child.version_transfer["critics"], "inherited")

    def test_scale_and_return_definition_branches_inherit_only_actor(self):
        for change in ("scales", "quality_evaluator", "deadline_s", "resource_source"):
            with self.subTest(change=change):
                target = copy.deepcopy(self.record["config"])
                if change == "scales":
                    target["objective"][change]["latency_s"] = 10
                else:
                    target["objective"][change] = dict(quality_evaluator="new-score/1",
                        deadline_s=10, resource_source="measured")[change]
                child = load_version_policy(self.record, target_config=target, seed=13, mode="branch")
                fresh = self.assert_fresh_state(child, seed=13)
                self.assert_networks_equal(self.parent, child, ["actor"])
                self.assert_networks_equal(fresh, child, child.learner.value_network_names)
                self.assertEqual(child.version_transfer["transferred"], ["actor"])
                self.assertEqual(child.version_transfer["critics"], "fresh")

    def test_resume_rejects_changed_objective_scheduler_learner_or_seed(self):
        objective_target = copy.deepcopy(self.record["config"])
        objective_target["objective"]["pareto"]["smoothing"] = 0.3
        scheduler_target = copy.deepcopy(self.record["config"])
        scheduler_target["idle_timeout_s"] += 1
        for kwargs in (dict(target_config=objective_target), dict(target_config=scheduler_target),
                       dict(learner_config=dict(learning_rate=0.01)), dict(seed=13)):
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(ValueError, "精确续训"):
                load_version_policy(self.record, mode="resume", **kwargs)

    def test_branch_authenticates_parent_before_loading_actor_for_new_objective(self):
        target = copy.deepcopy(self.record["config"])
        target["objective"]["scales"]["latency_s"] = 10
        original = self.path.read_bytes()
        for change in ("identity", "pareto", "critic"):
            with self.subTest(change=change):
                self.path.write_bytes(original)
                payload = self.parent.learner.torch.load(self.path, weights_only=True)
                if change == "identity":
                    payload["checkpoint_metadata"]["config_id"] = policy_config_id(target)
                elif change == "pareto":
                    del payload["checkpoint_metadata"]["pareto_state"]
                else:
                    del payload["networks"]["value_api"]
                self.parent.learner.torch.save(payload, self.path)
                self.rehash_record()
                with self.assertRaises((ValueError, KeyError)):
                    load_version_policy(self.record, target_config=target, mode="branch")

    def test_incompatible_schema_dimensions_and_actor_shape_are_rejected(self):
        record = copy.deepcopy(self.record)
        record["feature_schema"] = "obsolete-features"
        with self.assertRaisesRegex(ValueError, "schema"):
            load_version_policy(record, mode="branch")
        with self.assertRaisesRegex(ValueError, "网络结构"):
            load_version_policy(self.record, learner_config=dict(hidden_sizes=[8]), mode="branch")
        original = self.path.read_bytes()
        for dimension in ("observation_dim", "candidate_dim"):
            self.path.write_bytes(original)
            payload = self.parent.learner.torch.load(self.path, weights_only=True)
            payload[dimension] += 1
            self.parent.learner.torch.save(payload, self.path)
            self.rehash_record()
            with self.assertRaisesRegex(ValueError, "dimensions"):
                load_version_policy(self.record, mode="branch")

    def test_saved_old_rollout_is_never_imported(self):
        self.parent.learner.rollout.append(dict(episode_id="old-run"))
        self.parent.save(self.path)
        self.rehash_record()
        for mode in ("resume", "branch"):
            with self.subTest(mode=mode):
                child = load_version_policy(self.record, mode=mode)
                self.assertEqual(child.learner.rollout, [])
                self.assertIn("rollout", child.version_transfer["reset"])

    def test_inference_loads_frozen_version_and_rejects_file_or_record_tampering(self):
        frozen = load_version_policy(self.record)
        self.assertFalse(frozen.training)
        self.assert_networks_equal(self.parent, frozen)
        self.assertEqual(frozen._pareto_state(), self.parent._pareto_state())
        record = copy.deepcopy(self.record)
        record["metadata"] = dict(config_id="wrong-config")
        with self.assertRaisesRegex(ValueError, "metadata"):
            load_version_policy(record)
        record = copy.deepcopy(self.record)
        record["learner_config"]["learning_rate"] = 0.01
        with self.assertRaisesRegex(ValueError, "训练配置"):
            load_version_policy(record)
        self.path.write_bytes(self.path.read_bytes() + b"changed")
        with self.assertRaisesRegex(ValueError, "身份"):
            load_version_policy(self.record)

    def test_rule_version_is_inference_policy_or_fresh_training_seed(self):
        record = dict(id="rule", name="rule", config=freeze_config(dict(objective=objective())),
                      checkpoint=None, learner_config={}, seed=7, feature_schema=FEATURE_SCHEMA)
        frozen = load_version_policy(record)
        self.assertIsInstance(frozen, RulePolicy)
        child = load_version_policy(record, learner_config=SETTINGS, mode="branch")
        self.assert_fresh_state(child)
        self.assertEqual(child.version_transfer["transferred"], [])
        self.assertTrue(child.version_transfer["fresh"])
        with self.assertRaisesRegex(ValueError, "规则策略.*不能续训"):
            load_version_policy(record, mode="resume")


if __name__ == "__main__":
    unittest.main()
