"""Critical accounting paths for the current Pareto scheduler."""
import copy
import importlib.util
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from seampact.scheduling.unified_objective import TaskLedger, freeze_objective
from seampact.scheduling.capability_scheduler import CapabilityScheduler


def objective(**changes):
    value = dict(weights=dict(quality=.2, delivery=0, latency=.2, ram=.2, vram=.2, api=.2),
                 scales=dict(latency_s=10, ram_byte_s=100, vram_byte_s=100, api_tokens=100),
                 quality_evaluator="fixture-native/1", deadline_s=None, resource_source="reserved",
                 pareto=dict(preference=dict(quality=.2, latency=.2, ram=.2, vram=.2, api=.2), smoothing=.1))
    value.update(changes)
    return value


class ParetoObjectiveTests(unittest.TestCase):
    def test_no_sla_keeps_actual_latency_and_delivery_after_900_seconds(self):
        stamp = [0.0]
        ledger = TaskLedger(objective(), clock=lambda: stamp[0])
        ledger.begin("task")
        stamp[0] = 2000
        result = ledger.finish("task", delivered=True, quality=1, metrics=dict(api_tokens=3))
        self.assertEqual(result["latency_s"], 2000)
        self.assertTrue(result["delivered"])
        summary = ledger.summary(dict(reserved=dict(ram_byte_s=0, vram_byte_s=0)))
        self.assertTrue(summary["training_ready"])
        self.assertEqual(summary["components"]["latency"], 200)
        self.assertEqual(summary["utility_kind"], "pareto_linear_reference")

    def test_pareto_requires_real_tokens_and_all_five_directions(self):
        value = objective()
        ledger = TaskLedger(value, clock=lambda: 0)
        ledger.begin("task")
        ledger.finish("task", delivered=True, quality=1)
        self.assertFalse(ledger.summary(dict(reserved=dict(ram_byte_s=0, vram_byte_s=0)))["training_ready"])
        bad = copy.deepcopy(value)
        bad["pareto"]["preference"]["quality"] = 1
        with self.assertRaises(ValueError):
            freeze_objective(bad)

    @unittest.skipUnless(importlib.util.find_spec("torch"), "optional PyTorch dependency")
    def test_intervals_conserve_components_when_coefficients_change(self):
        deployment = SimpleNamespace(lock=threading.RLock(), instances={}, affinity={},
                                     _held=lambda row: False, release_task=lambda task: None)
        scheduler = CapabilityScheduler(SimpleNamespace(store=None), deployment, config=dict(objective=objective()))
        scheduler.close()
        scheduler.closed = False
        scheduler.start_workload("pareto", ["first", "second"], training=True, continuous=True,
                                 learner_config=dict(hidden_sizes=[4], epochs=1, batch_size=8))
        scheduler.begin("first")
        scheduler.choose_optional("first", "input_received", [dict(name="optional")])
        scheduler.finish("first", delivered=True, quality=1, metrics=dict(api_tokens=3))
        self.assertTrue(scheduler.request_update(minimum_tasks=1)["pending"])
        scheduler.begin("second")
        scheduler.choose_optional("second", "input_received", [dict(name="optional")])
        scheduler.finish("second", delivered=False, quality=0, metrics=dict(api_tokens=4))
        result = scheduler.finish_workload(update=True)
        self.assertEqual(len(result["updates"]), 2)
        self.assertTrue(all(row["updated"] for row in result["updates"]))
        for term in ("quality", "latency", "ram", "vram", "api"):
            total = sum(row["component_settlements"][term]["before_first"]
                        + sum(row["component_settlements"][term]["after_decisions"])
                        for row in result["updates"])
            self.assertAlmostEqual(total, result["summary"]["components"][term])
        self.assertEqual(scheduler.policy.coefficient_version, 2)
        self.assertNotEqual(result["updates"][0]["policy_version"], result["updates"][1]["policy_version"])


if __name__ == "__main__":
    unittest.main()
