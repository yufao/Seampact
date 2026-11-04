"""Actual installed local Units through a joint Window and its Coordinator.

These small source fixtures check software behavior, not research performance.
"""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from seampact.capabilities import JOINT_SCALES, SCALES_2
from seampact.hosting.deployment import Deployments
from seampact.evaluation import validate_criteria
from seampact.source.inspection import Inspector
from seampact.scheduling.joint import Window, configure, run_windows
from seampact.runtime import Coordinator, OwnerRuntime
from seampact.scenes.scenarios import FrozenCatalog, freeze_scene
from seampact.store import Store
from seampact.scheduling.training import LEARNING, REFERENCES
from seampact.scheduling import budget_policy
from seampact.units import UnitCatalog


ADAPTER = '''
class Scene:
    def __init__(self, config, context):
        self.config, self.context = config, context
    def reset(self, task, seed):
        self.task = task
        return dict(kind="decision", scene_observation=dict(modalities=["text"],
            scopes=["general_text"], available_inputs=["question"]),
            required_units=[self.config["unit"]], candidates=[dict(id="invoke",
                units=[self.config["unit"]], legal=True)])
    def step(self, candidate):
        if self.task.get("fail"):
            raise RuntimeError("fixture failure")
        result = self.context.invoke(self.config["unit"], dict(value=self.task["value"]))
        return dict(kind="terminal", terminated=True, truncated=False, response=result,
                    program_metrics=dict(score=result))
def make_scene(config, context):
    return Scene(config, context)
'''


class Joint014Integration(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name)
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.runtime = OwnerRuntime(Coordinator(self.store, quota=2))
        self.addCleanup(self.runtime.close)
        self.catalog = UnitCatalog(self.home, self.store, self.runtime)
        self.platform = SimpleNamespace(home=self.home, store=self.store, runtime=self.runtime,
                                        catalog=self.catalog, profiles=object())
        self.platform.deployments = Deployments(self.platform)
        self.addCleanup(self.platform.deployments.close)
        self.units = []
        for name, increment in (("a", 1), ("b", 2)):
            root = self.home / name
            root.mkdir()
            (root / "original.py").write_text(
                "def run(value: int) -> int:\n    return value + %d\n" % increment, encoding="utf-8")
            inspector = Inspector.freeze(root, root / "snapshots", name, ["original.py"], "run", "result")
            inspector.request["source_id"] = name
            candidate = dict(entries=[dict(id="run", path="original.py", symbol="run", kind="function",
                boundary_ids=["run-W"])], boundaries=[dict(id="run-W", path="original.py", symbol="run", mode="W")])
            unit = self.catalog.install(inspector, candidate, name, name)[0]
            self.assertEqual(unit["status"], "READY", unit.get("reason"))
            self.units.append(unit["id"])
        adapter = self.home / "adapter.py"
        adapter.write_text(ADAPTER, encoding="utf-8")
        self.criteria = validate_criteria(dict(id="fixture", version="1", dimensions=[dict(id="score",
            provider="program", binding="score", direction="maximize", unit="count", range=[0, 20],
            definition="returned fixture value")], objective=dict(weights=dict(score=1))))
        self.scene = freeze_scene(dict(id="mixed", version="1", model_roles=[],
            representation=dict(version="capability-features/2", scales={key: 1 for key in SCALES_2}),
            routes={name: dict(unit_ids=[uid], model_roles=[], criteria=self.criteria,
                evaluation_criteria=self.criteria,
                native_usage={uid: dict(model_free=True, basis="fixture arithmetic source")},
                adapter=dict(path=str(adapter), symbol="make_scene", config=dict(unit=uid)))
                for name, uid in zip(("a", "b"), self.units)},
            tasks=[dict(id="ta", route="a", value=3), dict(id="tb", route="b", value=5)]))

    def window(self, fail=False, scope="roots_and_boundaries", horizon=False):
        scene = copy.deepcopy(self.scene)
        scene["tasks"][0]["fail"] = fail
        frozen = FrozenCatalog(self.catalog, self.units)
        config = dict(joint=dict(dispatch_scope=scope, boundary_slots=1, age_guard_ms=100,
            max_consecutive_resource_ops=1, capacity=dict(call_slots=2),
            workload=dict(tasks_per_window=2, arrival=dict(interval_ms=0)),
            costs=dict(task_seconds=0, instance_seconds=0), scales={key: 1 for key in JOINT_SCALES},
            observation=dict(interval_s=0.1, include_current_process=True)))
        if horizon:
            config["joint"]["workload"].update(horizon_ms=1, unfinished_reward=-1)
        selected = configure(config, scene=scene, tasks=scene["tasks"], task_ids=["ta", "tb"],
            algorithm="required_only", criteria=self.criteria, contracts={}, groups={},
            deployments=self.platform.deployments, learning=LEARNING, references=REFERENCES,
            mode="freeze", placements=frozen.placement)
        manifest = dict(selected, scene=scene, criteria=self.criteria, evaluation_criteria=self.criteria,
                        mode="freeze", algorithm="required_only", seed=10)
        jobs = SimpleNamespace(platform=self.platform, directory=self.home,
                               stops={"check": threading.Event()}, closed=False)
        return Window(jobs, "check", manifest, frozen, self.platform.profiles, None, 0, scene["tasks"], {})

    def test_mixed_routes_share_actual_coordinator_and_boundaries(self):
        window = self.window()
        result = window.run()
        self.assertIsNone(result["execution_error"], result["execution_error"])
        self.assertTrue(result["eligible"], result["exclusion_reason"])
        self.assertEqual([task["final_response"] for task in result["tasks"]], [4, 7])
        self.assertEqual([task["calls"][0]["unit_id"] for task in result["tasks"]], self.units)
        self.assertEqual({root["task_id"] for root in result["roots"]}, set(window.by_context))
        self.assertTrue(all(root["state"] == "RETIRED" for root in result["roots"]))
        calls = list(self.runtime.snapshot()["calls"].values())
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(call["root_id"] and call["physical"] == "RETIRED" for call in calls))
        self.assertEqual(len([row for row in result["decisions"] if row["kind"] == "boundary"]), 2)
        self.assertEqual(window.config["working_sets"]["tasks"], dict(ta=[], tb=[]))
        self.assertEqual(window.working.claims, {})
        self.assertEqual({row["route"] for row in result["route_metrics"]}, {"a", "b"})
        self.check_observation(result, "COMPLETED")

    def check_observation(self, result, status):
        observation = result["resource_observation"]
        self.assertEqual(observation["status"], status)
        self.assertEqual(observation["window_boundary"], "PREPARATION_THROUGH_RELEASE")
        self.assertEqual(observation["processes"][0]["pid"], os.getpid())
        raw = list((self.home / "runtime" / "check").rglob("*.jsonl"))
        self.assertEqual(len(raw), 1)
        rows = [json.loads(line) for line in raw[0].read_text(encoding="utf-8").splitlines()]
        self.assertEqual((rows[0]["type"], rows[-1]["type"]), ("header", "summary"))

    def test_failed_task_still_closes_observation_and_keeps_metric_denominator(self):
        result = self.window(fail=True).run()
        self.assertEqual(result["execution_error"]["message"], "fixture failure")
        self.assertFalse(result["eligible"])
        failed = next(task for task in result["tasks"] if task["task_id"] == "ta")
        self.assertEqual(failed["status"], "FAILED")
        route = next(row for row in result["route_metrics"] if row["route"] == "a")
        self.assertEqual(route["metrics"]["criteria:score"]["unknown"], 1)
        self.check_observation(result, "FAILED")

    def test_roots_only_scope_completes_real_calls(self):
        result = self.window(scope="roots").run()
        self.assertIsNone(result["execution_error"], result["execution_error"])
        self.assertTrue(result["eligible"], result["exclusion_reason"])
        self.assertEqual([task["final_response"] for task in result["tasks"]], [4, 7])
        self.assertFalse(any(row["kind"] == "boundary" for row in result["decisions"]))

    def test_budget_training_and_freeze_share_artifact_and_change_only_budget_input(self):
        """Two budgets use fresh decisions, a real PPO update and the parent checkpoint."""
        import importlib.util
        if importlib.util.find_spec("torch") is None:
            self.skipTest("PPO requires the optional PyTorch dependency")
        from seampact.scheduling.training import TrainingJobs
        self.platform.profiles = SimpleNamespace(lock=threading.RLock(), role_profile=lambda role: {})
        for uid in self.units:
            sid = self.store.get("unit", uid)["source_id"]
            self.store.save("harness", sid, dict(id=sid, revision="fixture"))
            self.catalog.scheduling_policy(dict(unit_id=uid, visibility="transparent", activation="platform"))
        jobs = TrainingJobs(self.platform)
        self.addCleanup(jobs.close)
        config = dict(scene=self.scene, algorithm="ppo", scheduling_mode="joint/1", criteria=self.criteria,
            episodes=2, parameters=dict(epochs=1, hidden_sizes=[8]), joint=dict(
                functional_policy="required_only", dispatch_policy="learned_ready/1", resource_policy="fixed/1",
                dispatch_scope="roots", capacity=dict(call_slots=1), age_guard_ms=60000,
                max_consecutive_resource_ops=1, workload=dict(tasks_per_window=2, arrival=dict(interval_ms=0)),
                costs=dict(instance_seconds=0, task_seconds=0), scales={k: 1 for k in JOINT_SCALES},
                budget_policy=dict(latency_budgets=[0.5, 2.0], task_seconds_scale=1., instance_seconds_scale=1.,
                                   max_unfinished_fraction=0., dual_step=0.1)))
        # Dispatch competition is deterministic once all arrivals have queued their roots.
        original = Window._dispatch_stage
        def paired_dispatch(window):
            if not any(task.done is not None for task in window.tasks):
                views = window.runtime.call("root_view", window.wid)
                if len(views) < 2:
                    return False
            return original(window)
        def execute():
            rid = jobs.start(dict(config=config))["run_ids"][0]
            limit = time.monotonic() + 25
            while self.store.get("scheduler_run", rid)["status"] in ("RUNNING", "QUEUED"):
                self.assertLess(time.monotonic(), limit)
                time.sleep(.02)
            run = jobs.detail(rid)
            self.assertEqual(run["status"], "COMPLETED", run["error"])
            return run
        with patch.object(Window, "_dispatch_stage", paired_dispatch):
            trained = execute()
            self.assertGreater(trained["updates"], 0)
            self.assertEqual(trained["budget_choice_windows"], [1, 1])
            self.assertTrue(trained["checkpoint"])
            for window in trained["history"]:
                bill = window["budget_policy"]
                decisions = [s["transition"] for s in window["transitions"]]
                for k in range(3):
                    self.assertAlmostEqual(bill["raw"][k], bill["before_first_choice"][k] +
                                           sum(d["raw_cost_delta"][k] for d in decisions))
                self.assertTrue(all(d["state"][-1] == bill["latency_budget"] for d in decisions))
            config.update(mode="freeze", parent_run_id=trained["id"], episodes=1)
            for budget in config["joint"]["budget_policy"]["latency_budgets"]:
                config["joint"]["budget_policy"]["freeze_budget"] = budget
                frozen = execute()
                bill = frozen["history"][0]["budget_policy"]
                self.assertEqual((bill["latency_budget"], frozen["updates"]), (budget, 0))
                self.assertEqual(bill["multipliers_after"], bill["multipliers"])
                self.assertEqual([t["final_response"] for t in frozen["history"][0]["tasks"]], [4, 7])

    def test_wake_after_horizon_keeps_already_due_arrivals(self):
        window = self.window(horizon=True)
        refresh = window._refresh
        def delayed_refresh():
            if window.clock == window.start:
                time.sleep(0.02)
            return refresh()
        with patch.object(window, "_refresh", side_effect=delayed_refresh):
            result = window.run()
        self.assertEqual(result["closing"], "horizon")
        self.assertEqual([task["status"] for task in result["tasks"]], ["UNFINISHED", "UNFINISHED"])
        self.assertTrue(all(task["arrived_ms"] is not None for task in result["tasks"]))
        self.assertEqual(len(result["route_metrics"]), 2)
        self.assertTrue(all(row["metrics"]["criteria:score"]["unknown"] == 1
                            for row in result["route_metrics"]))

    def test_run_windows_preserves_route_population_and_independent_unknowns(self):
        window = self.window()
        manifest = window.manifest
        manifest.update(episodes=2, task_ids=["ta", "tb"])
        # The real calls have known training results but no independent binding.
        # Missing independent measurements must retain every arrived task.
        for route_id, route in manifest["scene"]["routes"].items():
            independent = copy.deepcopy(self.criteria)
            independent["dimensions"][0].update(binding="unmeasured",
                definition="independent fixture check " + route_id)
            route["evaluation_criteria"] = independent
        saved = dict(history=[], curves=[])
        jobs = window.jobs
        jobs.store = self.store
        jobs._append = lambda folder, name, item: saved[name].append(copy.deepcopy(item))
        run = dict(episodes=0, interactions=0, choice_interactions=0, no_choice_episodes=0,
                   eligible_episodes=0, excluded_episodes=0)
        run_windows(jobs, "check", run, self.home, manifest, window.catalog, window.bound, None)
        self.assertEqual((run["episodes"], run["joint"]["windows"], run["joint"]["tasks"]), (2, 2, 4))
        metrics = {(row["scope"], row["route"]): row for row in run["metrics"]}
        self.assertEqual(len(metrics), 4)
        for route_id, expected in (("a", 4), ("b", 7)):
            training = metrics["training", route_id]
            self.assertEqual((training["population"], training["denominator"], training["value"]), (2, 2, expected))
            self.assertEqual(training["status_counts"], dict(KNOWN=2, UNKNOWN=0, NOT_APPLICABLE=0))
            independent = metrics["independent", route_id]
            self.assertEqual((independent["population"], independent["denominator"], independent["value"]), (2, 0, None))
            self.assertEqual(independent["status_counts"], dict(KNOWN=0, UNKNOWN=2, NOT_APPLICABLE=0))
            self.assertEqual(independent["definition"], "independent fixture check " + route_id)
        summary = run["joint"]
        self.assertEqual((summary["failed_tasks"], summary["unfinished_tasks"]), (0, 0))
        self.assertEqual(len(summary["service_windows"]), 2)
        self.assertTrue(all(row["arrived"] == row["completed"] == row["response_denominator"] == 2
                            for row in summary["service_windows"]))
        self.assertEqual(len(summary["resource_windows"]), 2)
        self.assertTrue(all(row["status"] == "COMPLETED" and row["processes"][0]["pid"] == os.getpid()
                            for row in summary["resource_windows"]))
        self.assertEqual(len(summary["route_windows"]), 2)
        self.assertEqual((len(saved["history"]), len(saved["curves"])), (2, 2))
        self.assertEqual(self.store.get("scheduler_run", "check")["metrics"], run["metrics"])

    def test_preparation_failure_keeps_paid_window_but_never_updates_learner(self):
        from seampact.hosting.images import PreparationFailed
        window = self.window()
        window.manifest.update(episodes=1, task_ids=["ta", "tb"], mode="train")
        failure = dict(category="PREPARATION_FAILED", phase="RUNTIME", reason="REFERENCE_MISSING")
        conditions = dict(failure=None)
        self.platform.images = SimpleNamespace(get=lambda identity: dict(conditions))
        saved = []
        jobs = window.jobs
        jobs.store = self.store
        jobs._append = lambda folder, name, item: saved.append((name, copy.deepcopy(item)))
        run = dict(id="check", retention_scope_id="scope", episodes=0, interactions=0, choice_interactions=0,
                   no_choice_episodes=0, eligible_episodes=0, excluded_episodes=0)
        self.store.save("scheduler_run", "check", run)
        learner = SimpleNamespace(snapshot=lambda: dict(updates=0),
            observe=lambda value: self.fail("invalid window reached learner.observe"),
            update=lambda: self.fail("invalid window reached learner.update"))
        original_item = Window._item
        def invalidate_before_return(current):
            conditions["failure"] = failure
            return original_item(current)
        with patch.object(Window, "_item", invalidate_before_return), self.assertRaises(PreparationFailed):
            run_windows(jobs, "check", run, self.home, window.manifest, window.catalog, window.bound, learner)
        history = next(item for kind, item in saved if kind == "history")
        self.assertFalse(history["eligible"])
        self.assertIsNone(history["reward"])
        self.assertEqual(history["preparation_failure"], failure)
        self.assertEqual([t["final_response"] for t in history["tasks"]], [4, 7])
        self.assertEqual(run["excluded_episodes"], 1)


class BudgetLedgerTests(unittest.TestCase):
    def setUp(self):
        self.spec = dict(latency_budgets=[2.0], task_seconds_scale=8.0,
                         instance_seconds_scale=4.0, max_unfinished_fraction=0.0, dual_step=0.1)
        self.item = dict(service=dict(arrived=4, completed=3), task_ids=["a", "b", "c", "d"],
                         parallelism=dict(instance_seconds=12.0, charged_task_seconds=24.0),
                         eligible=True, exclusion_reason=None, window_return=7.0, reward_components={},
                         transitions=[dict(transition=dict(cost_at_decision=start, reward=0.0))
                                      for start in ([2.0, 4.0, 0], [6.0, 12.0, 0])])

    def test_whole_window_budget_and_action_credit(self):
        dual = [0.5, 2.0]
        self.assertTrue(budget_policy.settle(self.item, self.spec, 0, dual, True))
        self.assertEqual(self.item["budget_policy"]["normalized"], [3.0, 3.0, 0.25])
        self.assertEqual([r["transition"]["reward"] for r in self.item["transitions"]], [-1.5, -2.75])
        self.assertEqual(self.item["reward"], -5.0)
        self.assertAlmostEqual(dual[0], 0.6)
        self.assertAlmostEqual(dual[1], 2.025)
        frozen = [0.5, 2.0]
        budget_policy.settle(self.item, self.spec, 0, frozen, False)
        self.assertEqual(frozen, [0.5, 2.0])

    def test_incomplete_arrivals_exclude_window_without_updating_dual(self):
        self.item["service"] = dict(arrived=3, completed=3)
        dual = [0.5, 2.0]
        self.assertFalse(budget_policy.settle(self.item, self.spec, 0, dual, True))
        self.assertFalse(self.item["eligible"])
        self.assertFalse(self.item["budget_policy"]["eligible"])
        self.assertEqual(self.item["exclusion_reason"], "budget_arrival_coverage")
        self.assertIsNone(self.item["reward"])
        self.assertEqual(dual, [0.5, 2.0])
        self.assertTrue(all(r["transition"]["reward"] == 0.0 for r in self.item["transitions"]))


if __name__ == "__main__":
    unittest.main()
