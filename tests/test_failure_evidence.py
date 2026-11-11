"""Failure capture through actual SceneSession, episode and _run finally paths.

Arithmetic originals and in-memory resource owners check platform semantics;
they do not call a model, API or deployment backend.
"""
import copy
import json
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import test_joint_014 as fixture
from seampact.scenes.evaluation_evidence import PassiveEvidenceGap
from seampact.scenes.scenarios import FrozenCatalog, SceneContext, freeze_scene
from seampact.scheduling.joint import Window, run_windows
from seampact.scheduling.training import TrainingJobs, digest


ADAPTER = '''
class Opaque:
    def __deepcopy__(self, memo):
        raise AssertionError("opaque deepcopy hook")
    def __reduce_ex__(self, protocol):
        raise AssertionError("opaque serializer hook")
class Scene:
    def __init__(self, config, context):
        self.config, self.context = config, context
    def reset(self, task, seed):
        self.task = task
        return dict(kind="decision", scene_observation=dict(modalities=["text"],
            scopes=["general_text"], available_inputs=["question"]),
            required_units=[self.config["unit"]], references=dict(required_only="invoke"),
            candidates=[dict(id="invoke", units=[self.config["unit"]], legal=True)])
    def step(self, candidate):
        owner = self.context.platform.deployments
        owner.hold(self.context.run_id)
        result = self.context.invoke(self.config["unit"], dict(value=3))
        self.payload = dict(original_results=dict(check=[dict(answer=result)]))
        if self.task.get("blocked"):
            self.payload["owner"] = Opaque()
        self.context.record_failure_evidence(self.payload)
        self.payload["original_results"]["check"][0]["answer"] = 999
        self.payload.pop("owner", None)
        raise ValueError("original continuation failed")
    def close(self):
        self.context.platform.closed_scenes.append(self)
        self.payload.clear()
def make_scene(config, context):
    return Scene(config, context)
'''


class ResourceOwner:
    def __init__(self):
        self.lock, self.closed = threading.RLock(), threading.Event()
        self.environment_changes, self.instances = {}, []
        self.locks, self.releases, self.lease = set(), [], False
    def lock_groups(self, rid, groups, control):
        self.locks.add(rid)
    def hold(self, rid):
        self.instances = [dict(id="held-instance", run_id=rid)]
        self.lease = True
    def live_instances(self, group):
        return list(self.instances)
    def release_run(self, group, rid):
        self.releases.append((group, rid))
        self.instances, self.lease = [], False
        return dict(group_id=group, instances=[])
    def unlock(self, rid):
        self.locks.discard(rid)
    def residency(self, groups, start, end):
        return dict(resident_ms=0, instances=[])


class ImageOwner:
    def __init__(self):
        self.closed = False
    def begin(self, scope, rid):
        pass
    def get(self, scope):
        return dict(owner="training", run_ids=["check"], failure=None)
    def record_run_end(self, scope, rid, status):
        self.run_status = status
    def close(self, scope):
        self.closed = True


class FailureEvidenceTests(unittest.TestCase):
    def test_all_local_blocked_owner_stops_before_second_window_after_recording_denominator(self):
        original = fixture.Joint014Integration()
        original.setUp()
        self.addCleanup(original.doCleanups)
        original.platform.closed_scenes = []
        window = original.window(scope="roots")
        path = original.home / "failure_scene.py"
        adapter = ADAPTER.replace("owner.hold(self.context.run_id)", "pass")
        adapter = adapter.replace('self.payload.pop("owner", None)', "pass")
        adapter = adapter.replace('raise ValueError("original continuation failed")',
            'return dict(kind="terminal", terminated=True, truncated=False, evidence=self.payload)')
        path.write_text(adapter, encoding="utf-8")
        scene = copy.deepcopy(original.scene)
        for route, unit in zip(scene["routes"].values(), original.units):
            route["adapter"] = dict(path=str(path), symbol="make_scene", config=dict(unit=unit))
        for task in scene["tasks"]:
            task["blocked"] = True
        manifest = dict(window.manifest, scene=freeze_scene(scene), episodes=2)
        jobs = TrainingJobs(original.platform)
        self.addCleanup(jobs.close)
        jobs.stops["check"] = threading.Event()
        folder = jobs.directory / "check"
        folder.mkdir()
        run = dict(id="check", episodes=0, interactions=0, choice_interactions=0,
                   no_choice_episodes=0, eligible_episodes=0, excluded_episodes=0)
        with self.assertRaisesRegex(ValueError, "owner"):
            run_windows(jobs, "check", run, folder, manifest, window.catalog, window.bound, None)
        self.assertEqual(run["episodes"], 1)
        self.assertEqual(run["joint"]["tasks"], 2)
        self.assertEqual(run["excluded_episodes"], 1)
        history = [json.loads(line) for line in (folder / "history.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["release"]["left"], {})
        self.assertTrue(all(task["evaluation_capture"]["status"] == "BLOCKED" for task in history[0]["tasks"]))
        self.assertEqual(len(jobs.retained_owners["check"]), 1)
        self.assertEqual(original.platform.closed_scenes, [])

    def test_unknown_metaclass_equality_and_serializer_hooks_are_never_run(self):
        hooks = []
        class Meta(type):
            def __eq__(cls, other):
                hooks.append("equality")
                return False
            def __hash__(cls):
                hooks.append("hash")
                return 0
        class Opaque(metaclass=Meta):
            def __deepcopy__(self, memo):
                hooks.append("deepcopy")
                return None
            def __reduce_ex__(self, protocol):
                hooks.append("serializer")
                return dict, ()
        context = SceneContext(type("Platform", (), dict(profiles=object()))(), "fixture", catalog=object())
        value = Opaque()
        source = dict(owner=value, ordinary=[1])
        context.record_failure_evidence(source)
        source.pop("owner")
        source["ordinary"].append(2)
        with self.assertRaises(PassiveEvidenceGap):
            context.freeze_failure_evidence()
        self.assertIs(context.active_failure_terminal["evidence"]["owner"], value)
        self.assertEqual(context.active_failure_terminal["evidence"]["ordinary"], [1])
        self.assertEqual(hooks, [])

    def test_actual_joint_window_failure_freezes_or_retains_its_business_owner(self):
        for blocked in (False, True):
            with self.subTest(blocked=blocked):
                original = fixture.Joint014Integration()
                original.setUp()
                self.addCleanup(original.doCleanups)
                original.platform.closed_scenes = []
                window = original.window(scope="roots")
                path = original.home / "failure_scene.py"
                path.write_text(ADAPTER.replace("owner.hold(self.context.run_id)", "pass"), encoding="utf-8")
                scene = copy.deepcopy(original.scene)
                for route, unit in zip(scene["routes"].values(), original.units):
                    route["adapter"] = dict(path=str(path), symbol="make_scene", config=dict(unit=unit))
                for task in scene["tasks"]:
                    task["blocked"] = blocked
                scene = freeze_scene(scene)
                window.manifest["scene"] = scene
                window = Window(window.jobs, window.rid, window.manifest, window.catalog, window.bound,
                                None, 0, scene["tasks"], {})
                result = window.run()
                self.assertFalse(result["eligible"])
                failed = next(task for task in result["tasks"] if task["status"] == "FAILED")
                self.assertEqual(failed["evaluation_capture"]["status"], "BLOCKED" if blocked else "FROZEN")
                owner = next(task for task in window.tasks if task.task["id"] == failed["task_id"])
                self.assertEqual(owner.session_closed, not blocked)
                self.assertEqual(failed["calls"][0]["status"], "RETURN")
                self.assertEqual(next(row for row in result["route_metrics"] if row["route"] ==
                    failed["route"])["tasks"], 1)
                if blocked:
                    self.assertIs(window.jobs.retained_owners[window.rid][0], window)
                    self.assertTrue(owner.evidence_blocked)
                else:
                    self.assertEqual(failed["evidence"]["actual_unit_results"][0]["result"],
                                     4 if failed["route"] == "a" else 5)

    def run_failure(self, blocked):
        original = fixture.Joint014Integration()
        original.setUp()
        self.addCleanup(original.doCleanups)
        platform = original.platform
        platform.deployments = ResourceOwner()
        platform.images, platform.closed_scenes = ImageOwner(), []
        path = original.home / "failure_scene.py"
        path.write_text(ADAPTER, encoding="utf-8")
        unit = original.units[0]
        scene = freeze_scene(dict(id="failure-fixture", version="1", unit_ids=[unit], model_roles=[],
            representation=original.scene["representation"],
            native_usage={unit: dict(model_free=True, basis="fixture arithmetic source")},
            tasks=[dict(id="task", blocked=blocked)],
            adapter=dict(path=str(path), symbol="make_scene", config=dict(unit=unit))))
        catalog = FrozenCatalog(original.catalog, [unit])
        catalog.service_placements["owner"] = dict(kind="REMOTE", group_id="owner")
        original.store.save("runtime_group", "owner", dict(contract=dict(digest="fixture-contract")))
        manifest = dict(scene=scene, task_ids=["task"], episodes=1, seed=1, algorithm="required_only",
            mode="freeze", criteria=original.criteria, evaluation_criteria=original.criteria,
            runtime_contracts=dict(owner=dict(digest="fixture-contract")),
            resource_control=dict(mode="functional/1", initial_state="cold", start_timeout_s=0))
        jobs = TrainingJobs(platform)
        self.addCleanup(jobs.close)
        folder = jobs.directory / "check"
        folder.mkdir()
        (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        run = dict(id="check", status="QUEUED", config_digest=digest(manifest), retention_scope_id="images",
            episodes=0, interactions=0, choice_interactions=0, rejudged_episodes=0, no_choice_episodes=0,
            eligible_episodes=0, excluded_episodes=0, metrics=[], error=None)
        original.store.save("scheduler_run", "check", run)
        jobs.stops["check"] = threading.Event()
        jobs.bindings["check"] = (platform.profiles, catalog)
        jobs._run("check")
        result = original.store.get("scheduler_run", "check")
        self.assertTrue((folder / "history.jsonl").exists(), result.get("error"))
        history = json.loads((folder / "history.jsonl").read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["episodes"], 1)
        self.assertEqual(result["excluded_episodes"], 1)
        self.assertFalse(history["eligible"])
        self.assertEqual(history["execution_error"]["message"], "original continuation failed")
        self.assertEqual(history["calls"][0]["status"], "RETURN")
        self.assertTrue(all(metric["population"] == 1 for metric in result["metrics"]))
        return jobs, platform, result, history

    def test_frozen_failure_runs_actual_finally_release_and_keeps_original_values(self):
        jobs, platform, run, history = self.run_failure(False)
        self.assertEqual(history["evaluation_capture"]["status"], "FROZEN")
        self.assertEqual(history["evidence"]["original_results"]["check"][0]["answer"], 4)
        raw = history["evidence"]["actual_unit_results"][0]
        self.assertEqual((raw["arguments"], raw["result"]), (dict(value=3), 4))
        self.assertEqual(len(platform.closed_scenes), 1)
        self.assertEqual(platform.deployments.releases, [("owner", "check")])
        self.assertFalse(platform.deployments.lease)
        self.assertFalse(platform.deployments.locks)
        self.assertTrue(platform.images.closed)
        self.assertNotIn("check", jobs.bindings)
        self.assertFalse(run["runtime_window"]["left"])

    def test_blocked_failure_runs_actual_finally_and_retains_owner_lease_and_images(self):
        jobs, platform, run, history = self.run_failure(True)
        self.assertEqual(history["evaluation_capture"]["status"], "BLOCKED")
        self.assertEqual(history["evidence"], {})
        self.assertEqual(history["feedback"]["usage"]["request_count"], 0)
        self.assertEqual(platform.closed_scenes, [])
        self.assertEqual(platform.deployments.releases, [])
        self.assertTrue(platform.deployments.lease)
        self.assertEqual(platform.deployments.locks, {"check"})
        self.assertFalse(platform.images.closed)
        self.assertIn("check", jobs.bindings)
        self.assertEqual(run["runtime_window"]["status"], "UNKNOWN")
        self.assertEqual(run["runtime_window"]["left"], dict(owner=["held-instance"]))
        session = jobs.retained_owners["check"][0]
        session.close()
        self.assertEqual(platform.closed_scenes, [])
        with self.assertRaisesRegex(ValueError, "owner"):
            session.reset(dict(id="task"), 1)


if __name__ == "__main__":
    unittest.main()
