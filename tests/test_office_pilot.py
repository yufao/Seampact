import hashlib
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from seampact.office_experiment import pilot_quality
from seampact.office_pilot import _read_plan, training_batches, workload_cases
from run_office_pilot import reuse_calibration


class PilotChecks(unittest.TestCase):
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
