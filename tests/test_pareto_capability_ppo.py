"""Focused checks for five-objective critics and measured Pareto coefficients."""

import copy
import hashlib
import importlib.util
import math
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from seampact.scheduling.capability_ppo import COMPONENTS, CandidatePPO
from seampact.scheduling.capability_scheduler import CANDIDATE_DIM, FEATURE_SCHEMA, OBSERVATION_DIM
from seampact.scheduling.ppo_transfer import source_fingerprints, transfer

SETTINGS = dict(hidden_sizes=[4], epochs=1, batch_size=4, entropy_coefficient=0)


def objective(pareto=True):
    preference = {term: 0.2 for term in COMPONENTS}
    result = dict(weights=dict(preference, delivery=0),
        scales=dict(latency_s=1, ram_byte_s=1, vram_byte_s=1, api_tokens=1),
        quality_evaluator="fixture-native-score/1", deadline_s=None, resource_source="reserved")
    if pareto:
        result["pareto"] = dict(preference=preference, smoothing=0.1)
    return result


@unittest.skipUnless(importlib.util.find_spec("torch"), "optional PyTorch dependency")
class ParetoPPO(unittest.TestCase):
    def policy(self, **kwargs):
        return CandidatePPO("config", objective(**kwargs), config=SETTINGS, seed=7, training=True)

    def decision(self, policy, value=0):
        candidates = [dict(features=[0.0] * CANDIDATE_DIM), dict(features=[1.0] * CANDIDATE_DIM)]
        result = dict(policy_config_id="config", feature_schema=FEATURE_SCHEMA, session="stream",
                      observation=[float(value)] * OBSERVATION_DIM, candidates=candidates)
        result["selection"] = policy.choose(result["observation"], candidates)
        return result

    def interval(self, policy, decisions, values, identity="one", completed_tasks=1, normalizer=1):
        components = {term: dict(before_first=0.0, after_decisions=list(values.get(term, [0] * len(decisions))))
                      for term in COMPONENTS}
        planned = policy.interval_weights(components, normalizer, completed_tasks)
        rewards = [policy._compose({term: components[term]["after_decisions"][index] for term in COMPONENTS},
                                   planned["weights"]) for index in range(len(decisions))]
        summary = dict(training_ready=True, objective_id=policy.identity["objective_id"],
            policy_config_id="config", session="stream", interval_id=identity,
            utility=sum(rewards), completed_tasks=completed_tasks, normalizer=normalizer,
            component_settlements=components, optimization_weights=planned["weights"], scalarization=planned["receipt"])
        return dict(before_first=0.0, after_decisions=rewards), summary

    def test_nonlinear_coefficients_follow_measured_gaps_and_match_derivative(self):
        policy = self.policy()
        components = {term: dict(before_first=value / 8, after_decisions=[])
                      for term, value in zip(COMPONENTS, [0.5, 2, 3, 0, 4])}
        before = policy.snapshot()
        planned = policy.interval_weights(components, 8, 1)
        logits = [1, 4, 6, 0, 8]
        denominator = sum(math.exp(value - 8) for value in logits)
        for term, logit in zip(COMPONENTS, logits):
            self.assertAlmostEqual(planned["weights"][term], 0.2 * math.exp(logit - 8) / denominator)
        self.assertGreater(planned["weights"]["api"], planned["weights"]["latency"])
        self.assertEqual(planned["receipt"]["means"]["quality"], 0.5)
        self.assertAlmostEqual(planned["receipt"]["scalarization_value"], 0.1 * (8 + math.log(denominator)))
        self.assertEqual(policy.snapshot(), before)  # Weight preparation is pure.
        for term in COMPONENTS:
            perturbed = copy.deepcopy(components)
            perturbed[term]["before_first"] += 1e-7 / 8
            changed = policy.interval_weights(perturbed, 8, 1)["receipt"]["scalarization_value"]
            derivative = (changed - planned["receipt"]["scalarization_value"]) / 1e-7
            self.assertAlmostEqual(derivative, planned["weights"][term] * (-1 if term == "quality" else 1), places=6)
        components["api"]["before_first"] = 1000000
        self.assertTrue(math.isfinite(policy.interval_weights(components, 8, 1)["receipt"]["scalarization_value"]))

    def test_current_batch_coefficients_and_five_independent_bootstrapped_critics(self):
        policy = self.policy()
        with policy.learner.torch.no_grad():
            for term in COMPONENTS:
                for parameter in policy.learner.networks["value_" + term].parameters():
                    parameter.zero_()
                policy.learner.networks["value_" + term][-1].bias.fill_(3 if term == "quality" else 5)
        decision = self.decision(policy)
        settlements, summary = self.interval(policy, [decision], dict(quality=[0.5], latency=[2], ram=[3], vram=[0], api=[4]))
        captured, observe = [], policy.learner.observe
        def record(row):
            captured.append(copy.deepcopy(row))
            observe(row)
        with patch.object(policy.learner, "observe", side_effect=record):
            result = policy.update_interval([decision], settlements, summary, bootstrap=self.decision(policy, 1))
        self.assertEqual(set(policy.learner.networks), {"actor", *("value_" + term for term in COMPONENTS)})
        for term, loss in zip(COMPONENTS, [0.25, 4, 9, 0, 16]):
            self.assertAlmostEqual(result["value_loss_" + term], loss)
            expected = summary["optimization_weights"][term] * (1 if term == "quality" else -1)
            self.assertEqual(captured[0]["component_weights"][term], expected)
        self.assertNotEqual(decision["selection"]["coefficient_snapshot"], policy.coefficients)
        self.assertEqual(policy.coefficients, summary["optimization_weights"])
        self.assertEqual(policy.coefficient_version, 1)
        self.assertEqual(result["scalarization"], policy.last_scalarization)
        self.assertEqual(result["scalarization"]["coefficient_version"], 1)

    def test_invalid_component_or_receipt_is_transactional_and_old_state_rejected(self):
        policy = self.policy()
        decisions = [self.decision(policy), self.decision(policy, 1)]
        settlements, summary = self.interval(policy, decisions, dict(quality=[0.5, 0.5], latency=[1, 1]))
        for field in ("coefficient_snapshot", "component_values"):
            malformed = copy.deepcopy(decisions)
            malformed[1]["selection"][field]["api"] = float("nan")
            with self.assertRaises(ValueError):
                policy.update_interval(malformed, settlements, summary, final=True)
        malformed = copy.deepcopy(summary)
        malformed["optimization_weights"]["quality"] += 0.1
        with self.assertRaises(ValueError):
            policy.update_interval(decisions, settlements, malformed, final=True)
        malformed = copy.deepcopy(decisions)
        malformed[1]["observation"][0] = float("nan")
        with self.assertRaises(ValueError):
            policy.update_interval(malformed, settlements, summary, final=True)
        self.assertEqual(policy.learner.steps, 0)
        self.assertEqual(policy.learner.rollout, [])
        self.assertEqual(policy.coefficient_version, 0)
        policy.update_interval(decisions, settlements, summary, final=True)
        stale = copy.deepcopy(decisions[0])
        stale["selection"]["policy_version"] = policy.learner.policy_version
        settlements, summary = self.interval(policy, [stale], dict(quality=[0.5], latency=[2]), "stale")
        with self.assertRaisesRegex(ValueError, "旧系数"):
            policy.update_interval([stale], settlements, summary, final=True)

    def test_checkpoint_resume_and_actor_only_transfer_preserve_correct_state(self):
        policy = self.policy()
        decision = self.decision(policy)
        settlements, summary = self.interval(policy, [decision], dict(quality=[0.5], latency=[3]))
        policy.update_interval([decision], settlements, summary, final=True)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pareto.pt"
            policy.save(path)
            resumed = self.policy()
            resumed.load(path, optimizer=True)
            self.assertEqual(resumed.snapshot(), policy.snapshot())
            with self.assertRaisesRegex(ValueError, "重复"):
                resumed.update_interval([decision], settlements, summary, final=True)
            frozen = CandidatePPO("config", objective(), config=SETTINGS, training=False)
            frozen.load(path)
            self.assertEqual(frozen.coefficients, policy.coefficients)
            with self.assertRaisesRegex(ValueError, "冻结"):
                frozen.update_interval([decision], settlements, summary, final=True)
            payload = policy.learner.torch.load(path, weights_only=True)
            del payload["checkpoint_metadata"]["pareto_state"]
            policy.learner.torch.save(payload, path)
            with self.assertRaisesRegex(ValueError, "Pareto 检查点"):
                self.policy().load(path, optimizer=True)
            parent, child = self.policy(pareto=False), self.policy()
            critics = {name: copy.deepcopy(network.state_dict()) for name, network in child.learner.networks.items() if name != "actor"}
            with parent.learner.torch.no_grad():
                for parameter in parent.learner.networks["actor"].parameters():
                    parameter.fill_(0.125)
            parent.save(path)
            transfer(child, dict(path=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                mode="actor", parent_config_id="config", parent_objective_id=parent.identity["objective_id"],
                parent_feature_schema=FEATURE_SCHEMA, semantic_sources=source_fingerprints()))
            self.assertEqual(set(parent.learner.networks), {"actor", "value"})
            for parameter in child.learner.networks["actor"].parameters():
                self.assertTrue(child.learner.torch.all(parameter == 0.125))
            for name, state in critics.items():
                for key, value in state.items():
                    self.assertTrue(child.learner.torch.equal(value, child.learner.networks[name].state_dict()[key]))
            self.assertEqual((child.learner.steps, child.learner.updates, child.coefficient_version), (0, 0, 0))
            self.assertEqual(dict(child.learner.optimizers["policy"].state), {})

    def test_zero_task_tail_and_no_decisions_do_not_commit_coefficients(self):
        policy = self.policy()
        for completed, decisions in ((0, []), (0, [self.decision(policy)]), (1, [])):
            with self.subTest(completed=completed, decisions=len(decisions)):
                before = copy.deepcopy(policy.coefficients)
                settlements, summary = self.interval(policy, decisions, dict(latency=[4] * len(decisions)),
                    str((completed, len(decisions))), completed_tasks=completed)
                result = policy.update_interval(decisions, settlements, summary, final=True)
                self.assertFalse(result["scalarization"]["applied"])
                self.assertEqual(policy.coefficients, before)
                self.assertEqual(policy.coefficient_version, 0)
                self.assertIsNone(policy.last_scalarization)


if __name__ == "__main__":
    unittest.main()
