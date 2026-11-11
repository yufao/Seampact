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
from seampact.scheduling.joint import Window, configure, run_windows, window_tasks
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
        if self.config.get("optional"):
            return dict(kind="decision", scene_observation=dict(modalities=["text"],
                scopes=["general_text"], available_inputs=["question"]), required_units=[],
                candidates=[dict(id="skip", action_type="finish", units=[], legal=True),
                            dict(id="invoke", action_type="invoke", units=[self.config["unit"]], legal=True)])
        return dict(kind="decision", scene_observation=dict(modalities=["text"],
            scopes=["general_text"], available_inputs=["question"]),
            required_units=[self.config["unit"]], candidates=[dict(id="invoke",
                units=[self.config["unit"]], legal=True)])
    def step(self, candidate):
        if self.task.get("fail"):
            raise RuntimeError("fixture failure")
        result = (0 if candidate["id"] == "skip" else
                  self.context.invoke(self.config["unit"], dict(value=self.task["value"])))
        return dict(kind="terminal", terminated=True, truncated=False, response=result,
                    program_metrics=dict(score=result))
def make_scene(config, context):
    return Scene(config, context)
'''


def budget_fixture(window_count=2, members=None, freeze=False):
    names = ['ram', 'vram', 'latency', 'unfinished'] + (['quality:score'] if members else [])
    points = [dict(id=point, objective='ram', limits={n: (limit if n == 'latency' else .1) for n in names if n != 'ram'})
              for point, limit in [('small', .5), ('large', 2.)]]
    groups = ([dict(group_id='fixture', stratum='fixture', weight=1., requests=[dict(task_id=t, weight=1 / len(members)) for t in members])]
              if members else None)
    blocks = [dict(windows=[i], point=('small' if i % 2 == 0 else 'large'),
                   cost_weights={n: [1.] for n in ['ram', 'vram', 'latency', 'unfinished']},
                   quality={'score': copy.deepcopy(groups)} if groups else {}) for i in range(window_count)]
    result = dict(schema='budget-policy/2', points=points, scales={n: 1. for n in names},
        initial_multipliers={n: 0. for n in names}, dual_step=.1, normalizer=1.,
        resource_keys=dict(ram=['ram'], vram=['vram']), resource_units=dict(ram='GiB', vram='GiB'),
        api=dict(status='NOT_APPLICABLE'), blocks=blocks,
        quality_budgets=[dict(metric='score', undelivered_loss=1.)] if members else [])
    if freeze:
        result['freeze_point'] = 'small'
    return result


def budget_resources():
    return dict(call_slots=1, resources={'ram': 4., 'vram': 4.}, demands={})


def budget_costs():
    return dict(schema='joint-costs/2', instance_seconds=0, task_seconds=0, resource_seconds={'ram': 0, 'vram': 0})


def synthetic_budget_envelope(window):
    """SYNTHETIC_LEDGER: exercise PPO plumbing without claiming real resource measurement."""
    result = _original_window_item(window)
    result['costs']['resource_envelope'] = dict(status='KNOWN', resource_seconds={'ram': 2., 'vram': 1.},
        instances=[], synthetic=True, semantics='synthetic test proxy; not a local measurement')
    return result


_original_window_item = Window._item


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
                                        catalog=self.catalog, profiles=object(), source_lock=threading.RLock())
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
            representation=dict(version="capability-features/3", scales={key: 1 for key in SCALES_2}),
            routes={name: dict(unit_ids=[uid], model_roles=[], criteria=self.criteria,
                evaluation_criteria=self.criteria,
                native_usage={uid: dict(model_free=True, basis="fixture arithmetic source")},
                adapter=dict(path=str(adapter), symbol="make_scene", config=dict(unit=uid)))
                for name, uid in zip(("a", "b"), self.units)},
            tasks=[dict(id="ta", route="a", value=3), dict(id="tb", route="b", value=5)]))

    def window(self, fail=False, scope="roots_and_boundaries", horizon=False, workload=None):
        scene = copy.deepcopy(self.scene)
        scene["tasks"][0]["fail"] = fail
        frozen = FrozenCatalog(self.catalog, self.units)
        config = dict(joint=dict(dispatch_scope=scope, boundary_slots=1, age_guard_ms=100,
            max_consecutive_resource_ops=1, capacity=dict(call_slots=2),
            workload=dict(tasks_per_window=2, arrival=dict(interval_ms=0)),
            costs=dict(task_seconds=0, instance_seconds=0), scales={key: 1 for key in JOINT_SCALES},
            observation=dict(interval_s=0.1, include_current_process=True)))
        if workload is not None:
            config["joint"]["workload"] = copy.deepcopy(workload)
        if horizon:
            config["joint"]["workload"].update(horizon_ms=1, unfinished_reward=-1)
        selected = configure(config, scene=scene, tasks=scene["tasks"], task_ids=["ta", "tb"],
            algorithm="required_only", criteria=self.criteria, contracts={}, groups={},
            deployments=self.platform.deployments, learning=LEARNING, references=REFERENCES,
            mode="freeze", placements=frozen.placement)
        manifest = dict(selected, scene=scene, criteria=self.criteria, evaluation_criteria=self.criteria,
                        mode="freeze", algorithm="required_only", seed=10, episodes=2, task_ids=["ta", "tb"])
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
        business = [row for row in result["decisions"] if row["kind"] == "business"]
        self.assertEqual(len(business), 2)
        for row in business:
            self.assertGreaterEqual(row["representation_ms"], row["joint_observation_build_ms"] +
                row["scene_represent_ms"] + row["scene_dispatch_conditions_ms"])
        online = result["costs"]["online"]
        self.assertGreater(online["joint_state_snapshot_calls"], 0)
        self.assertEqual(online["joint_state_snapshot_calls"], window.state_snapshot_calls)
        self.assertGreaterEqual(online["joint_state_snapshot_ms"], 0)
        for key in ("joint_observation_build_ms", "scene_represent_ms", "scene_dispatch_conditions_ms"):
            self.assertEqual(online[key], sum(row.get(key, 0) for row in result["decisions"]))
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

    def test_local_calls_without_declared_integrals_remain_unknown_for_budget(self):
        window = self.window(scope="roots")
        window.budget_policy = {"schema": "budget-policy/2"}
        result = window.run()
        self.assertTrue(result["tasks"][0]["calls"])
        envelope = result["costs"]["resource_envelope"]
        self.assertEqual(envelope["status"], "UNKNOWN")
        self.assertIsNone(envelope["resource_seconds"])

    def test_synthetic_ledger_training_and_freeze_share_budget_conditioned_artifact(self):
        """SYNTHETIC_LEDGER: two budgets use real PPO and the parent checkpoint."""
        proxy = patch.object(Window, '_item', synthetic_budget_envelope)
        proxy.start()
        self.addCleanup(proxy.stop)
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
                dispatch_scope="roots", capacity=budget_resources(), age_guard_ms=60000,
                max_consecutive_resource_ops=1, workload=dict(tasks_per_window=2, arrival=dict(interval_ms=0)),
                costs=budget_costs(), scales={k: 1 for k in JOINT_SCALES}, budget_policy=budget_fixture()))
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
                decisions = [row["transition"] for row in window["transitions"]]
                index = 0 if bill["point"] == "small" else 1
                expected = budget_policy.condition(config["joint"]["budget_policy"], index, bill["multipliers"])
                self.assertTrue(all(d["state"][-len(expected):] == expected for d in decisions))
            config.update(mode="freeze", parent_run_id=trained["id"], episodes=1)
            for point in ("small", "large"):
                policy = config["joint"]["budget_policy"]
                policy["freeze_point"] = point
                policy["blocks"] = [dict(windows=[0], point=point, quality={})]
                frozen = execute()
                bill = frozen["history"][0]["budget_policy"]
                self.assertEqual((bill["point"], frozen["updates"]), (point, 0))
                self.assertEqual(bill["multipliers_after"], bill["multipliers"])
                self.assertEqual([t["final_response"] for t in frozen["history"][0]["tasks"]], [4, 7])

    def test_synthetic_ledger_optional_business_and_quality_budget_update_same_ppo(self):
        """SYNTHETIC_LEDGER: optional choices and a genuine PPO update."""
        proxy = patch.object(Window, '_item', synthetic_budget_envelope)
        proxy.start()
        self.addCleanup(proxy.stop)
        import importlib.util
        if importlib.util.find_spec("torch") is None:
            self.skipTest("PPO requires the optional PyTorch dependency")
        from seampact.scheduling.training import TrainingJobs
        self.platform.profiles = SimpleNamespace(lock=threading.RLock(), role_profile=lambda role: {})
        for uid in self.units:
            sid = self.store.get("unit", uid)["source_id"]
            self.store.save("harness", sid, dict(id=sid, revision="fixture"))
            self.catalog.scheduling_policy(dict(unit_id=uid, visibility="transparent", activation="platform"))
        scene = copy.deepcopy(self.scene)
        criterion = copy.deepcopy(self.criteria)
        criterion["dimensions"][0]["bounds"] = [0, 20]
        for route in scene["routes"].values():
            route["adapter"]["config"]["optional"] = True
            route["criteria"] = route["evaluation_criteria"] = criterion
        jobs = TrainingJobs(self.platform)
        self.addCleanup(jobs.close)
        config = dict(scene=scene, algorithm="ppo", scheduling_mode="joint/1", criteria=criterion,
            episodes=2, parameters=dict(epochs=1, hidden_sizes=[8]), joint=dict(
                functional_policy="learned", dispatch_policy="learned_ready/1", resource_policy="learned/1",
                dispatch_scope="roots", capacity=budget_resources(), age_guard_ms=60000,
                max_consecutive_resource_ops=1, workload=dict(tasks_per_window=2, arrival=dict(interval_ms=0)),
                costs=budget_costs(), scales={k: 1 for k in JOINT_SCALES}, budget_policy=budget_fixture(members=["ta", "tb"])))
        rid = jobs.start(dict(config=config))["run_ids"][0]
        limit = time.monotonic() + 25
        while self.store.get("scheduler_run", rid)["status"] in ("RUNNING", "QUEUED"):
            self.assertLess(time.monotonic(), limit)
            time.sleep(.02)
        run = jobs.detail(rid)
        self.assertEqual(run["status"], "COMPLETED", run["error"])
        self.assertGreater(run["updates"], 0)
        for window in run["history"]:
            bill = window["budget_policy"]
            self.assertEqual(bill["status"], "EVALUATED")
            self.assertEqual(len(bill["multipliers_after"]), 5)
            self.assertGreater(bill["multipliers_after"][4], bill["multipliers"][4])
            self.assertTrue(any(t["kind"] == "business" for t in window["transitions"]))
            self.assertEqual(len(bill["quality"]["score"]), 2)
        # A held-out complete cohort can use the trained policy after the
        # manifest/checkpoint has made a JSON round trip through the store.
        heldout = copy.deepcopy(config)
        heldout.update(mode="freeze", parent_run_id=run["id"], episodes=1)
        for task in heldout["scene"]["tasks"]:
            task["id"] += "-heldout"
        policy = heldout["joint"]["budget_policy"]
        policy["freeze_point"] = "small"
        policy["blocks"] = budget_fixture(1, ["ta-heldout", "tb-heldout"])["blocks"]
        frozen_id = jobs.start(dict(config=heldout))["run_ids"][0]
        limit = time.monotonic() + 25
        while self.store.get("scheduler_run", frozen_id)["status"] in ("RUNNING", "QUEUED"):
            self.assertLess(time.monotonic(), limit)
            time.sleep(.02)
        frozen = jobs.detail(frozen_id)
        self.assertEqual(frozen["status"], "COMPLETED", frozen["error"])
        self.assertEqual(frozen["updates"], 0)
        bill = frozen["history"][0]["budget_policy"]
        self.assertEqual(bill["multipliers"], bill["multipliers_after"])
        self.assertEqual({t["task_id"] for t in bill["quality"]["score"]}, {"ta-heldout", "tb-heldout"})

    def test_synthetic_ledger_two_window_block_scores_after_release_and_updates_once(self):
        """SYNTHETIC_LEDGER tests the real TrainingJobs block lifecycle, not resource performance."""
        import importlib.util
        if importlib.util.find_spec("torch") is None:
            self.skipTest("PPO requires the optional PyTorch dependency")
        from seampact.scheduling.training import TrainingJobs
        from seampact.scheduling.learners import Learner
        self.platform.profiles = SimpleNamespace(lock=threading.RLock(), role_profile=lambda role: {})
        for uid in self.units:
            sid = self.store.get("unit", uid)["source_id"]
            self.store.save("harness", sid, dict(id=sid, revision="fixture"))
            self.catalog.scheduling_policy(dict(unit_id=uid, visibility="transparent", activation="platform"))
        scene, criterion = copy.deepcopy(self.scene), copy.deepcopy(self.criteria)
        criterion["dimensions"][0]["bounds"] = [0, 20]
        for route in scene["routes"].values():
            route["adapter"]["config"]["optional"] = True
            route["criteria"] = route["evaluation_criteria"] = criterion
        policy = budget_fixture(1, ["ta", "tb"])
        policy["points"] = policy["points"][:1]
        policy["blocks"][0]["windows"] = [0, 1]
        policy["blocks"][0]["cost_weights"] = {name: [.5, .5] for name in
            ["ram", "vram", "latency", "unfinished"]}
        config = dict(scene=scene, algorithm="ppo", scheduling_mode="joint/1", criteria=criterion, episodes=2,
            parameters=dict(epochs=1, hidden_sizes=[8]), joint=dict(functional_policy="learned",
                dispatch_scope="roots", capacity=budget_resources(), age_guard_ms=60000,
                max_consecutive_resource_ops=1, workload=dict(tasks_per_window=1, arrival=dict(interval_ms=0)),
                costs=budget_costs(), scales={k: 1 for k in JOINT_SCALES}, budget_policy=policy))
        jobs = TrainingJobs(self.platform)
        self.addCleanup(jobs.close)
        timeline = []
        run_window, evaluate, update, update_dual = Window.run, Window.evaluate_pending, Learner.update, budget_policy.update_dual
        def recording_run(window, defer_evaluation=False):
            result = run_window(window, defer_evaluation=defer_evaluation)
            self.assertTrue(defer_evaluation)
            self.assertFalse(result["release"]["left"])
            self.assertFalse(window.working.claims)
            timeline.append(('business_released', window.number))
            return result
        def recording_score(window):
            self.assertEqual(timeline[:2], [('business_released', 0), ('business_released', 1)])
            self.assertTrue(all(not self.platform.deployments.live_instances(group) for group in window.groups))
            timeline.append(('score', window.number))
            return evaluate(window)
        def recording_update(learner, *args, **kwargs):
            if learner.rollout:
                timeline.append(('ppo', None))
            return update(learner, *args, **kwargs)
        def recording_dual(*args, **kwargs):
            timeline.append(('dual', None))
            return update_dual(*args, **kwargs)
        with patch.object(Window, '_item', synthetic_budget_envelope), patch.object(Window, 'run', recording_run), \
             patch.object(Window, 'evaluate_pending', recording_score), patch.object(Learner, 'update', recording_update), \
             patch.object(budget_policy, 'update_dual', recording_dual):
            rid = jobs.start(dict(config=config))["run_ids"][0]
            limit = time.monotonic() + 25
            while self.store.get("scheduler_run", rid)["status"] in ("RUNNING", "QUEUED"):
                self.assertLess(time.monotonic(), limit)
                time.sleep(.02)
            run = jobs.detail(rid)
        self.assertEqual(run["status"], "COMPLETED", run["error"])
        self.assertEqual(timeline, [('business_released', 0), ('business_released', 1),
                                    ('score', 0), ('score', 1), ('ppo', None), ('dual', None)])
        self.assertEqual(run["budget_choice_windows"], [2])
        import torch
        checkpoint = run["checkpoint"]
        payload = torch.load(checkpoint["path"], map_location="cpu", weights_only=True)
        self.assertEqual(checkpoint["committed_window_end"], 2)
        self.assertEqual(checkpoint["config_digest"], run["config_digest"])
        self.assertEqual(payload["checkpoint_metadata"]["budget_multipliers"], run["budget_multipliers"])
        self.assertEqual(payload["policy_version"], checkpoint["policy_version"])
        self.assertTrue(payload["optimizers"])
        transitions = [entry['transition'] for window in run['history'] for entry in window['transitions']]
        self.assertEqual({entry['episode_id'] for entry in transitions}, {rid + ':w0', rid + ':w1'})
        self.assertTrue(all(entry['terminated'] for entry in transitions))

    def test_requested_stop_waits_for_complete_block_and_checkpoint(self):
        import importlib.util
        if importlib.util.find_spec("torch") is None:
            self.skipTest("PPO requires the optional PyTorch dependency")
        from seampact.scheduling.training import TrainingJobs
        self.platform.profiles = SimpleNamespace(lock=threading.RLock(), role_profile=lambda role: {})
        for uid in self.units:
            sid = self.store.get("unit", uid)["source_id"]
            self.store.save("harness", sid, dict(id=sid, revision="fixture"))
            self.catalog.scheduling_policy(dict(unit_id=uid, visibility="transparent", activation="platform"))
        scene, criterion = copy.deepcopy(self.scene), copy.deepcopy(self.criteria)
        criterion["dimensions"][0]["bounds"] = [0, 20]
        for route in scene["routes"].values():
            route["adapter"]["config"]["optional"] = True
            route["criteria"] = route["evaluation_criteria"] = criterion
        policy = budget_fixture(2, ["ta", "tb"])
        policy["blocks"][0]["windows"] = [0, 1]
        policy["blocks"][0]["cost_weights"] = {name: [.5, .5] for name in ["ram", "vram", "latency", "unfinished"]}
        policy["blocks"][1]["windows"] = [2, 3]
        policy["blocks"][1]["cost_weights"] = copy.deepcopy(policy["blocks"][0]["cost_weights"])
        config = dict(scene=scene, algorithm="ppo", scheduling_mode="joint/1", criteria=criterion, episodes=4,
            parameters=dict(epochs=1, hidden_sizes=[8]), joint=dict(functional_policy="learned",
                dispatch_scope="roots", capacity=budget_resources(), age_guard_ms=60000,
                max_consecutive_resource_ops=1, workload=dict(tasks_per_window=1, arrival=dict(interval_ms=0)),
                costs=budget_costs(), scales={k: 1 for k in JOINT_SCALES}, budget_policy=policy))
        jobs = TrainingJobs(self.platform)
        self.addCleanup(jobs.close)
        original = Window.run
        started = []
        def requesting_stop(window, defer_evaluation=False):
            started.append(window.number)
            if window.number == 0:
                for invalid in (1, 5, True, None):
                    with self.assertRaises(ValueError):
                        jobs.stop(window.rid, invalid)
                jobs.stop(window.rid, 2)
            return original(window, defer_evaluation=defer_evaluation)
        with patch.object(Window, '_item', synthetic_budget_envelope), patch.object(Window, 'run', requesting_stop):
            rid = jobs.start(dict(config=config))["run_ids"][0]
            limit = time.monotonic() + 25
            while self.store.get("scheduler_run", rid)["status"] in ("RUNNING", "QUEUED"):
                self.assertLess(time.monotonic(), limit)
                time.sleep(.02)
            run = jobs.detail(rid)
        self.assertEqual(run["status"], "STOPPED", run["error"])
        self.assertEqual(started, [0, 1])
        self.assertEqual(run["episodes"], 2)
        self.assertEqual(run["checkpoint"]["committed_window_end"], 2)
        self.assertEqual(run["stop_request"]["after_windows"], 2)

    def test_failed_tail_keeps_last_committed_budget_checkpoint(self):
        import importlib.util
        if importlib.util.find_spec("torch") is None:
            self.skipTest("PPO requires the optional PyTorch dependency")
        import torch
        from seampact.scheduling.training import TrainingJobs
        self.platform.profiles = SimpleNamespace(lock=threading.RLock(), role_profile=lambda role: {})
        for uid in self.units:
            sid = self.store.get("unit", uid)["source_id"]
            self.store.save("harness", sid, dict(id=sid, revision="fixture"))
            self.catalog.scheduling_policy(dict(unit_id=uid, visibility="transparent", activation="platform"))
        scene, criterion = copy.deepcopy(self.scene), copy.deepcopy(self.criteria)
        criterion["dimensions"][0]["bounds"] = [0, 20]
        for route in scene["routes"].values():
            route["adapter"]["config"]["optional"] = True
            route["criteria"] = route["evaluation_criteria"] = criterion
        config = dict(scene=scene, algorithm="ppo", scheduling_mode="joint/1", criteria=criterion, episodes=2,
            parameters=dict(epochs=1, hidden_sizes=[8]), joint=dict(functional_policy="learned",
                dispatch_scope="roots", capacity=budget_resources(), age_guard_ms=60000,
                max_consecutive_resource_ops=1, workload=dict(tasks_per_window=2, arrival=dict(interval_ms=0)),
                costs=budget_costs(), scales={k: 1 for k in JOINT_SCALES},
                budget_policy=budget_fixture(2, ["ta", "tb"])))
        jobs = TrainingJobs(self.platform)
        self.addCleanup(jobs.close)
        evaluate = Window.evaluate_pending
        def fail_tail(window):
            item = evaluate(window)
            if window.number == 1:
                item.update(eligible=False, exclusion_reason="provider_http_500",
                            execution_error=dict(message="provider HTTP 500"))
            return item
        with patch.object(Window, '_item', synthetic_budget_envelope), patch.object(Window, 'evaluate_pending', fail_tail):
            rid = jobs.start(dict(config=config))["run_ids"][0]
            limit = time.monotonic() + 25
            while self.store.get("scheduler_run", rid)["status"] in ("RUNNING", "QUEUED"):
                self.assertLess(time.monotonic(), limit)
                time.sleep(.02)
            run = jobs.detail(rid)
        self.assertEqual(run["status"], "FAILED")
        self.assertIn("provider HTTP 500", run["error"])
        self.assertEqual((run["eligible_episodes"], run["excluded_episodes"]), (1, 1))
        checkpoint = run["checkpoint"]
        self.assertEqual(checkpoint["committed_window_end"], 1)
        payload = torch.load(checkpoint["path"], map_location="cpu", weights_only=True)
        self.assertEqual(payload["checkpoint_metadata"]["checkpoint_id"], checkpoint["checkpoint_id"])
        self.assertEqual(payload["policy_version"], checkpoint["policy_version"])

    def test_checkpoint_save_failure_keeps_previous_file_and_receipt(self):
        from seampact.scheduling.training import TrainingJobs
        jobs = TrainingJobs.__new__(TrainingJobs)
        jobs.store = SimpleNamespace(save=lambda *args: self.fail("failed save published a receipt"))
        path = self.home / "policy.pt"
        path.write_bytes(b"previous checkpoint")
        previous = dict(checkpoint_id="previous")
        run = dict(config_digest="fixture", checkpoint=previous, budget_multipliers=[[1.]])
        def fail_save(*args, **kwargs):
            raise OSError("checkpoint write failed")
        learner = SimpleNamespace(policy_version=2, save=fail_save)
        with self.assertRaisesRegex(OSError, "checkpoint write failed"):
            jobs._save_checkpoint("check", run, self.home, {}, learner, committed_window_end=2)
        self.assertEqual(path.read_bytes(), b"previous checkpoint")
        self.assertEqual(run["checkpoint"], previous)
        self.assertEqual(list(self.home.glob('.policy-*.tmp')), [])

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

    def test_explicit_tail_window_executes_and_records_only_declared_occurrences(self):
        window = self.window(workload=dict(windows=[
            dict(task_ids=["ta", "tb"], arrival=dict(offsets_ms=[0, 2])),
            dict(task_ids=["ta"], arrival=dict(offsets_ms=[4]))]))
        histories = []
        jobs = window.jobs
        jobs.store = self.store
        jobs._append = lambda folder, name, item: histories.append(copy.deepcopy(item)) if name == "history" else None
        run = dict(episodes=0, interactions=0, choice_interactions=0, no_choice_episodes=0,
                   eligible_episodes=0, excluded_episodes=0)
        run_windows(jobs, "check", run, self.home, window.manifest, window.catalog, window.bound, None)
        self.assertEqual([item["task_ids"] for item in histories], [["ta", "tb"], ["ta"]])
        self.assertEqual([[task["arrival_offset_ms"] for task in item["tasks"]] for item in histories], [[0, 2], [4]])
        self.assertEqual([[task["final_response"] for task in item["tasks"]] for item in histories], [[4, 7], [4]])
        self.assertEqual(len({task["context"] for item in histories for task in item["tasks"]}), 3)
        self.assertEqual((run["joint"]["windows"], run["joint"]["tasks"]), (2, 3))
        self.assertTrue(all(item["eligible"] for item in histories))

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


class JointWorkloadTests(unittest.TestCase):
    def manifest(self, workload, episodes, quality=False):
        tasks = [dict(id=identity) for identity in ("a", "b", "c")]
        scene = dict(unit_ids=[], tasks=tasks, representation=dict(version="capability-features/3", cards={}))
        criteria = validate_criteria(dict(id="workload", version="1", dimensions=[dict(id="score",
            provider="program", binding="score", direction="maximize", unit="ratio", range=[0, 1],
            bounds=[0, 1], definition="fixture quality")], objective=dict(weights=dict(score=1))))
        config = dict(episodes=episodes, joint=dict(workload=workload, age_guard_ms=1,
            max_consecutive_resource_ops=1, costs=dict(task_seconds=0, instance_seconds=0),
            scales={key: 1 for key in JOINT_SCALES}))
        if quality:
            policy = budget_fixture(1, ["a", "b", "c"], freeze=True)
            policy["blocks"][0]["windows"] = list(range(episodes))
            config["joint"].update(dispatch_policy="learned_ready/1", capacity=budget_resources(),
                                   costs=budget_costs(), budget_policy=policy)
        selected = configure(config, scene=scene, tasks=tasks, task_ids=["a", "b", "c"],
            algorithm="ppo" if quality else "required_only", criteria=criteria, contracts={}, groups={},
            deployments=SimpleNamespace(), learning=LEARNING, references=REFERENCES, mode="freeze", placements={})
        return dict(selected, scene=scene, mode="freeze", seed=1, episodes=episodes)

    def test_compact_windows_keep_tail_before_starting_next_epoch(self):
        manifest = self.manifest(dict(tasks_per_window=2, arrival=dict(offsets_ms=[0, 10])), 4)
        windows = manifest["joint"]["workload"]["windows"]
        self.assertEqual([window_tasks(manifest, number) for number in range(4)],
                         [["a", "b"], ["c"], ["a", "b"], ["c"]])
        self.assertEqual([window["offsets_ms"] for window in windows], [[0, 10], [0], [0, 10], [0]])

    def test_explicit_windows_preserve_members_offsets_and_repeated_occurrences(self):
        workload = dict(windows=[dict(task_ids=["b", "a"], arrival=dict(offsets_ms=[0, 7])),
                                 dict(task_ids=["c"], arrival=dict(interval_ms=10)),
                                 dict(task_ids=["b"], arrival=dict(offsets_ms=[3]))])
        original = copy.deepcopy(workload)
        manifest = self.manifest(workload, 3)
        catalog = SimpleNamespace(remote_groups=lambda: {}, placement={})
        jobs = SimpleNamespace(platform=SimpleNamespace(runtime=None, deployments=None))
        occurrences = []
        for number in range(3):
            tasks = [{"id": identity} for identity in window_tasks(manifest, number)]
            window = Window(jobs, "run", manifest, catalog, None, None, number, tasks, {})
            occurrences.extend(task.task["id"] for task in window.tasks)
            self.assertEqual([task.offset_ms for task in window.tasks], [[0, 7], [0], [3]][number])
        self.assertEqual(occurrences, ["b", "a", "c", "b"])
        self.assertEqual(workload, original)
        with self.assertRaisesRegex(ValueError, "冻结成员清单"):
            Window(jobs, "run", manifest, catalog, None, None, 1, [{"id": "a"}], {})

    def test_explicit_windows_reject_incomplete_or_ambiguous_declarations(self):
        good = dict(task_ids=["a"], arrival=dict(interval_ms=0))
        invalid = [dict(windows=[]), dict(windows=[good, good]),
            dict(windows=[dict(task_ids=[], arrival=dict(interval_ms=0))]),
            dict(windows=[dict(task_ids=["a", "a"], arrival=dict(interval_ms=0))]),
            dict(windows=[dict(task_ids=["unknown"], arrival=dict(interval_ms=0))]),
            dict(windows=[dict(task_ids=["a"], arrival=dict(offsets_ms=[0, 1]))]),
            dict(windows=[dict(task_ids=["a", "b"], arrival=dict(offsets_ms=[2, 1]))]),
            dict(windows=[good], tasks_per_window=1),
            dict(windows=[good], arrival=dict(interval_ms=0)),
            dict(windows=[dict(task_ids=["a"], arrival=dict(offsets_ms=[3]))],
                 horizon_ms=2, unfinished_reward=-1)]
        for workload in invalid:
            with self.subTest(workload=workload), self.assertRaises(ValueError):
                self.manifest(workload, 1)

    def test_quality_denominator_spans_windows_and_rejects_missing_members(self):
        workload = dict(windows=[dict(task_ids=["a", "b"], arrival=dict(interval_ms=0)),
                                 dict(task_ids=["c"], arrival=dict(interval_ms=0))])
        manifest = self.manifest(workload, 2, quality=True)
        self.assertEqual(manifest["joint"]["budget_policy"]["blocks"][0]["windows"], [0, 1])
        workload["windows"][1]["task_ids"] = ["b"]
        with self.assertRaises(ValueError):
            self.manifest(workload, 2, quality=True)


if __name__ == "__main__":
    unittest.main()
