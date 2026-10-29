"""Minimal mixed-task routing checks using installed-catalog call boundaries."""

import contextlib
import copy
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from seampact.capabilities import SCALES_2
from seampact.scenarios import (SceneSession, freeze_scene, route_metric_applicability,
                                route_metric_report, scene_task_criteria, task_route, task_unit_ids)


ADAPTER = '''
class Adapter:
    def __init__(self, config, context):
        self.config, self.context = config, context
    def reset(self, task, seed):
        self.task, self.seed = task, seed
        return dict(kind="decision", observation=[],
            scene_observation=dict(modalities=["text"], scopes=["general_text"],
                available_inputs=["question"]), required_units=[self.config["unit"]],
            candidates=[dict(id="call", units=[self.config.get("candidate", self.config["unit"])], legal=True)])
    def step(self, candidate):
        result = self.context.invoke(self.config.get("invoke", self.config["unit"]),
            dict(task_id=self.task["id"], seed=self.seed))
        return dict(kind="terminal", terminated=True, truncated=False, response=result,
            metric_applicability={"conditional": False})
def make_scene(config, context):
    return Adapter(config, context)
'''


class Catalog:
    def __init__(self):
        self.facts, self.calls = {}, []
    def public(self):
        return [dict(id=uid) for uid in ("installed-a", "installed-b")]
    def business_units(self):
        return self.public()
    def unit(self, uid):
        return dict(id=uid, initialization="BOUND")
    def dispatch_status(self, uid):
        return dict(status="READY_LOCAL")
    def runtime_state(self, units):
        return dict(local=True, ready_instance=True, warm=True, cold_startable=False,
                    accepting=True, busy=False, queue=0, state_age_ms=0)
    def invoke(self, uid, arguments, native_arguments=()):
        self.calls.append((uid, arguments))
        return arguments


class MixedScenes(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name)
        self.adapters = []
        for name in ("a", "b"):
            path = self.path / (name + ".py")
            path.write_text(ADAPTER, encoding="utf-8")
            self.adapters.append(path)
        self.spec = dict(id="mixed", version="1", environment_version="fixture/1",
            representation=dict(version="capability-features/2", scales={k: 1 for k in SCALES_2}),
            routes={name: dict(unit_ids=["installed-" + name], model_roles=[],
                adapter=dict(path=str(path), symbol="make_scene", config=dict(unit="installed-" + name)),
                metric_applicability={"other": False})
                for name, path in zip(("a", "b"), self.adapters)},
            tasks=[dict(id="task-a", route="a"), dict(id="task-b", route="b")])
        self.catalog = Catalog()
        self.platform = SimpleNamespace(profiles=object(), runtime=SimpleNamespace(
            context=lambda _: contextlib.nullcontext()))

    def session(self, spec=None):
        frozen = freeze_scene(spec or self.spec)
        session = SceneSession(frozen, self.platform, "run", catalog=self.catalog)
        self.addCleanup(session.close)
        return frozen, session

    def test_routes_keep_shared_features_and_native_seed_mapping(self):
        frozen, session = self.session()
        self.assertIsNone(session.environment)
        self.assertEqual(set(frozen["unit_ids"]), {"installed-a", "installed-b"})
        first = session.reset(frozen["tasks"][0], 42)
        result = session.step(0)
        self.assertEqual(result["response"], dict(task_id="task-a", seed=42))
        self.assertEqual(result["route"], "a")
        self.assertEqual(result["task_class"], "a")
        self.assertEqual(result["metric_applicability"], dict(other=False, conditional=False))
        second = session.reset(frozen["tasks"][1], 43)
        self.assertEqual(first["observation"], second["observation"])
        self.assertEqual(len(first["candidates"][0]["features"]), len(second["candidates"][0]["features"]))
        self.assertEqual([u["id"] for u in session.context.catalog.public()], ["installed-b"])
        session.step(0)
        self.assertEqual(self.catalog.calls[-1], ("installed-b", dict(task_id="task-b", seed=43)))

    def test_foreign_candidate_and_invocation_are_rejected(self):
        self.spec["routes"]["a"]["adapter"]["config"]["candidate"] = "installed-b"
        frozen, session = self.session()
        with self.assertRaisesRegex(ValueError, "Unit"):
            session.reset(frozen["tasks"][0], 1)
        self.assertEqual(self.catalog.calls, [])
        self.spec["routes"]["a"]["adapter"]["config"].pop("candidate")
        self.spec["routes"]["a"]["adapter"]["config"]["invoke"] = "installed-b"
        frozen, session = self.session()
        session.reset(frozen["tasks"][0], 1)
        with self.assertRaises(ValueError):
            session.step(0)
        self.assertEqual(self.catalog.calls, [])
        with self.assertRaises(ValueError):
            session.context.catalog.invoke("installed-b", {})

    def test_all_adapter_dependencies_and_resources_are_frozen(self):
        dependency = self.path / "dep.py"
        resource = self.path / "data.json"
        dependency.write_text("VERSION = 1", encoding="utf-8")
        resource.write_text("{}", encoding="utf-8")
        self.spec["routes"]["b"]["adapter"]["dependencies"] = [str(dependency)]
        self.spec["routes"]["b"]["resources"] = [str(resource)]
        frozen, session = self.session()
        self.assertEqual(len(frozen["adapter"]["files"]), 3)
        self.assertEqual(len(frozen["resource_files"]), 1)
        dependency.write_text("VERSION = 2", encoding="utf-8")
        with self.assertRaises(ValueError):
            session.reset(frozen["tasks"][0], 1)

    def test_task_route_mapping_is_immutable(self):
        frozen, session = self.session()
        task = dict(frozen["tasks"][0], route="b")
        with self.assertRaises(ValueError):
            session.reset(task, 1)
        with self.assertRaises(ValueError):
            task_route(frozen, dict(frozen["tasks"][0], task_class="changed"))

    def test_task_unit_subset_constrains_candidates_and_catalog(self):
        self.spec["routes"]["a"]["unit_ids"].append("installed-b")
        self.spec["tasks"][0]["unit_ids"] = ["installed-a"]
        frozen, session = self.session()
        self.assertEqual(task_unit_ids(frozen, frozen["tasks"][0]), ["installed-a"])
        session.reset(frozen["tasks"][0], 1)
        with self.assertRaises(ValueError):
            session.context.catalog.unit("installed-b")
        with self.assertRaises(ValueError):
            task_unit_ids(frozen, dict(frozen["tasks"][0], unit_ids=["installed-a", "installed-b"]))
        self.spec["routes"]["a"]["adapter"]["config"]["candidate"] = "installed-b"
        frozen, session = self.session()
        with self.assertRaises(ValueError):
            session.reset(frozen["tasks"][0], 1)

    def test_metric_applicability_and_denominators_are_route_specific(self):
        frozen, _ = self.session()
        frozen["routes"]["a"]["criteria"] = dict(dimensions=[dict(id="quality", definition="route a label")])
        selected = scene_task_criteria(frozen, frozen["tasks"][0], dict(id="fallback"))
        self.assertEqual(selected["dimensions"][0]["definition"], "route a label")
        with self.assertRaises(ValueError):
            route_metric_applicability(frozen, frozen["tasks"][0], dict(metric_applicability={"other": True}))
        records = [dict(task_id="task-a", feedback=dict(metrics=[dict(id="quality", status=status, value=value)]))
                   for status, value in (("KNOWN", 1), ("UNKNOWN", None), ("NOT_APPLICABLE", None))]
        report = route_metric_report(frozen, records)[0]
        row = report["metrics"]["criteria:quality"]
        self.assertEqual((row["total"], row["applicable"], row["known"], row["unknown"], row["not_applicable"]), (3, 2, 1, 1, 1))
        self.assertEqual(row["definition"]["definition"], "route a label")

    def test_single_scene_keeps_existing_contract(self):
        spec = copy.deepcopy(self.spec["routes"]["a"])
        spec.update(id="single", version="1", tasks=[dict(id="single-task")],
                    observation_schema=["value"], candidate_schema=["value"])
        # Existing adapters can already use the common representation independently.
        spec["representation"] = self.spec["representation"]
        frozen, session = self.session(spec)
        self.assertIsNotNone(session.environment)
        event = session.reset(frozen["tasks"][0], 9)
        self.assertNotIn("route", event)
        self.assertEqual(session.step(0)["response"]["seed"], 9)

    def test_single_scene_task_subset_is_enforced(self):
        spec = copy.deepcopy(self.spec["routes"]["a"])
        spec.update(id="single", version="1", unit_ids=["installed-a", "installed-b"],
            tasks=[dict(id="single-task", unit_ids=["installed-a"])],
            representation=self.spec["representation"])
        frozen, session = self.session(spec)
        session.reset(frozen["tasks"][0], 1)
        with self.assertRaises(ValueError):
            session.context.catalog.unit("installed-b")
        with self.assertRaises(ValueError):
            session.reset(dict(frozen["tasks"][0], unit_ids=["installed-b"]), 1)
        spec["adapter"]["config"]["candidate"] = "installed-b"
        frozen, session = self.session(spec)
        with self.assertRaises(ValueError):
            session.reset(frozen["tasks"][0], 1)

    def test_failed_and_unfinished_tasks_keep_defined_metric_denominators(self):
        frozen, _ = self.session()
        criterion = dict(dimensions=[dict(id="quality", definition="global quality"),
                                    dict(id="conditional", applicable=False)])
        final = dict(dimensions=[dict(id="checked", definition="independent check")])
        records = [dict(task_id="task-a", status=status) for status in ("FAILED", "UNFINISHED")]
        records += [dict(task_id="task-a", status="DONE", feedback=dict(metrics=[
            dict(id="quality", status="KNOWN", value=1)])),
            dict(task_id="task-b", status="DONE"), dict(task_id="task-b", status="NOT_ARRIVED")]
        report = {b["route"]: b for b in route_metric_report(frozen, records, criterion, final)}
        self.assertEqual(report["a"]["tasks"], 3)
        quality = report["a"]["metrics"]["criteria:quality"]
        self.assertEqual((quality["total"], quality["known"], quality["unknown"]), (3, 1, 2))
        self.assertEqual(report["a"]["metrics"]["criteria:conditional"]["not_applicable"], 3)
        self.assertEqual(report["a"]["metrics"]["evaluation_criteria:checked"]["unknown"], 3)
        self.assertEqual(report["b"]["tasks"], 1)
        self.assertEqual(report["b"]["metrics"]["criteria:quality"]["values"], [])

    def test_late_quality_keeps_explicit_deadline_outcome(self):
        frozen, _ = self.session()
        records = [dict(task_id="task-a", status="DONE", deadline_status="UNFINISHED",
            feedback=dict(metrics=[dict(id="quality", status="KNOWN", value=1)]))]
        bucket = route_metric_report(frozen, records)[0]
        self.assertEqual(bucket["outcomes"], dict(completed=1, failed=0, unfinished=1, late_completed=1))
        self.assertEqual(bucket["metrics"]["criteria:quality"]["values"], [1])


if __name__ == "__main__":
    unittest.main()
