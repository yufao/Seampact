"""Selected calls through actual Window/Coordinator and managed-service control.

Arithmetic source Units, fake Docker and real HTTP readiness exercise platform
semantics only. These checks are not multi-source model or quality evidence.
"""
import contextlib
import copy
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from seampact.capabilities import JOINT_SCALES, SCALES_2, VERSION_2
from seampact.hosting.managed_services import ManagedServices
from seampact.scenes.scenarios import FrozenCatalog, freeze_scene
from seampact.scheduling.joint import Window, configure
from seampact.scheduling.training import LEARNING, REFERENCES
import test_joint_014 as joint_fixture
import test_managed_services as service_fixture


ADAPTER = '''
class Scene:
    def __init__(self, config, context):
        self.config, self.context = config, context
    def reset(self, task, seed):
        self.task, self.stage, self.results = task, 0, []
        return self.event()
    def event(self):
        options = self.config["stages"][self.stage]
        return dict(kind="decision", required_units=[],
            scene_observation=dict(modalities=["text"], scopes=["general_text"],
                                   available_inputs=["question"]),
            references=dict(required_only=self.task["choices"][self.stage]),
            candidates=[dict(id=choice, action_type="invoke", units=self.config["calls"][choice], legal=True)
                        for choice in options] + [dict(id="skip", action_type="finish", units=[], legal=True)])
    def step(self, candidate):
        for unit in candidate["units"]:
            self.results.append(self.context.invoke(unit, dict(value=3)))
        self.stage += 1
        if self.stage < len(self.config["stages"]):
            return self.event()
        return dict(kind="terminal", terminated=True, truncated=False, response=self.results,
                    evidence=dict(actual_results=self.results), program_metrics=dict(score=len(self.results)))
def make_scene(config, context):
    return Scene(config, context)
'''


class SelectedCallWindowTests(unittest.TestCase):
    def window(self, choices=("a",), stages=None, *, pinned=False, unreclaimable=()):
        fixture = joint_fixture.Joint014Integration()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        platform = fixture.platform
        manager = None
        services = {}
        amounts = {"dependency": 128, "a": 384, "b": 512}
        if pinned:
            amounts["pinned"] = 64
        docker = service_fixture.FakeDocker()
        for identity, memory in amounts.items():
            service = service_fixture.ServiceTest()
            service.setUp()
            self.addCleanup(service.tearDown)
            if manager is None:
                platform.deployments.configure_backend(dict(kind="docker", coordinator_url=service.spec["endpoint"]))
                manager = ManagedServices(platform, runner=docker)
                platform.managed_services = manager
            spec = copy.deepcopy(service.spec)
            spec.update(id=identity, resource_envelope=dict(host_memory_mb=memory, devices={}, basis="fixture upper bound"))
            if identity == "a":
                spec["depends_on"] = ["dependency"]
            services[identity] = manager.configure(spec)
        catalog = FrozenCatalog(fixture.catalog, fixture.units)
        for identity in services:
            group = "service:" + identity
            catalog.service_placements[group] = manager.placement(identity)
            catalog.service_dependencies[group] = ["service:" + dependency
                for dependency in services[identity].get("depends_on", [])]
        catalog.placement[fixture.units[0]]["resource_dependencies"] = ["service:a", "service:dependency"]
        catalog.placement[fixture.units[1]]["resource_dependencies"] = ["service:b"]
        for identity in (set(unreclaimable) | ({"pinned"} if pinned else set())):
            group = platform.store.get("runtime_group", "service:" + identity)
            # Explicit UNKNOWN fixture evidence stays pinned; no policy is
            # allowed to upgrade this evidence to reclaimable.
            group["contract"]["lifecycle"]["reclaim"]["status"] = "UNKNOWN"
            platform.store.save("runtime_group", "service:" + identity, group)
        path = fixture.home / "selected_calls.py"
        path.write_text(ADAPTER, encoding="utf-8")
        tasks = [dict(id="optional", choices=list(choices))]
        scene = freeze_scene(dict(id="optional-pool", version="1", unit_ids=fixture.units,
            model_roles=[], tasks=tasks,
            representation=dict(version=VERSION_2, scales={key: 1 for key in SCALES_2}),
            native_usage={unit: dict(model_free=True, basis="fixture arithmetic source") for unit in fixture.units},
            adapter=dict(path=str(path), symbol="make_scene", config=dict(
                stages=stages or [["a", "b"]], calls=dict(a=[fixture.units[0]], b=[fixture.units[1]],
                compound=fixture.units)))))
        groups = catalog.remote_groups()
        raw = dict(joint=dict(age_guard_ms=100, max_consecutive_resource_ops=1,
            resource_policy="dependency_cost/1", retention=dict(mode="idle_retire/1", idle_ms=100),
            capacity=dict(call_slots=2, resources={"host_memory_mb": 576 if pinned else 512},
                demands={group: {"host_memory_mb": spec["resource_envelope"]["host_memory_mb"]}
                         for group, spec in groups.items()}),
            workload=dict(tasks_per_window=1, arrival=dict(interval_ms=0)),
            costs=dict(schema="joint-costs/2", task_seconds=0, instance_seconds=0,
                       resource_seconds={"host_memory_mb": 1}),
            scales={key: 1 for key in JOINT_SCALES}, start_timeout_s=2))
        if pinned:
            raw["joint"].update(initial_state="warm", warm_groups=["service:pinned"])
        selected = configure(raw, scene=scene, tasks=tasks, task_ids=["optional"],
            algorithm="required_only", criteria=fixture.criteria,
            contracts={g: platform.store.get("runtime_group", g)["contract"] for g in groups},
            groups=groups, deployments=platform.deployments, learning=LEARNING, references=REFERENCES,
            mode="freeze", placements=catalog.placement, service_dependencies=catalog.service_dependencies,
            online_services=[], scoring_services=[])
        catalog.bind_run("check", selected["resource_control"])
        platform.deployments.lock_groups("check", groups, selected["resource_control"])
        manifest = dict(fixture.window().manifest, **selected, scene=scene, task_ids=["optional"], episodes=1)
        window = Window(fixture.window().jobs, "check", manifest, catalog, platform.profiles, None, 0, tasks, {})
        return window, manager, docker, fixture.units

    @staticmethod
    def started(docker):
        return {argv[argv.index("--name") + 1].removeprefix("seampact-service-")
                for argv in docker.commands if argv[:2] == ["docker", "run"]}

    def test_two_alternatives_fit_one_at_a_time_and_selected_call_reserves_full_closure(self):
        window, manager, docker, units = self.window()
        business, admission = window._business, window._admission
        seen = []
        def before_choice(task):
            self.assertEqual(self.started(docker), set())
            self.assertFalse(any(manager.leases.values()))
            self.assertEqual(window.working.claims[task.context], set())
            return business(task)
        def admitted(task):
            @contextlib.contextmanager
            def enter(unit):
                with admission(task)(unit) as grant:
                    seen.append(set(window.working.claims[task.context]))
                    self.assertEqual(self.started(docker), {"a", "dependency"})
                    self.assertTrue(all(manager.live_instances(group)[0]["state"] == "READY"
                                        for group in ("a", "dependency")))
                    yield grant
            return enter
        with patch.object(window, "_business", side_effect=before_choice), \
             patch.object(window, "_admission", side_effect=admitted):
            result = window.run()
        self.assertTrue(result["eligible"], result["exclusion_reason"])
        self.assertEqual(seen, [{"service:a", "service:dependency"}])
        self.assertEqual([call["unit_id"] for call in result["tasks"][0]["calls"]], [units[0]])
        self.assertEqual(self.started(docker), {"a", "dependency"})
        self.assertEqual(window.config["working_sets"]["tasks"], {"optional": []})
        self.assertFalse(docker.containers)

    def test_skip_creates_no_optional_reservation_or_start_and_scoring_is_deferred(self):
        window, _, docker, _ = self.window(choices=("skip",))
        with patch("seampact.scheduling.joint.evaluate", side_effect=AssertionError("scoring entered business window")):
            result = window.run(defer_evaluation=True)
        self.assertEqual(result["scoring"]["status"], "PENDING")
        self.assertEqual(self.started(docker), set())
        self.assertEqual(window.working.claims, {})
        self.assertEqual(result["tasks"][0]["calls"], [])
        self.assertTrue(all(task.session_closed for task in window.tasks))
        self.assertTrue(window.evaluate_pending()["eligible"])

    def test_cold_unknown_pool_starts_only_selected_complete_closure(self):
        window, _, docker, units = self.window(unreclaimable=("a", "b", "dependency"))
        self.assertEqual(window.config["working_sets"]["pinned"], [])
        result = window.run()
        self.assertTrue(result["eligible"], result["exclusion_reason"])
        self.assertEqual(self.started(docker), {"a", "dependency"})
        self.assertEqual([call["unit_id"] for call in result["tasks"][0]["calls"]], [units[0]])
        self.assertFalse(docker.containers)

    def test_over_capacity_compound_is_blocked_before_step_while_skip_remains_legal(self):
        window, _, docker, _ = self.window(choices=("compound",), stages=[["compound"]])
        result = window.run()
        self.assertTrue(result["eligible"], result["exclusion_reason"])
        business = [row for row in result["decisions"] if row["kind"] == "business"]
        self.assertEqual(business[0]["candidate"], "skip")
        self.assertEqual(business[0]["legal"], ["skip"])
        self.assertEqual(result["tasks"][0]["calls"], [])
        self.assertEqual(self.started(docker), set())

    def test_retained_owner_does_not_partially_wait_for_an_infeasible_successor(self):
        window, _, docker, _ = self.window(choices=("a", "b"), stages=[["a"], ["b"]])
        result = window.run()
        self.assertTrue(result["eligible"], result["exclusion_reason"])
        business = [row for row in result["decisions"] if row["kind"] == "business"]
        self.assertEqual([row["candidate"] for row in business], ["a", "skip"])
        self.assertEqual(self.started(docker), {"a", "dependency"})

    def test_pinned_allocation_is_preserved_beside_selected_closure(self):
        window, _, docker, _ = self.window(pinned=True)
        result = window.run()
        self.assertTrue(result["eligible"], result["exclusion_reason"])
        self.assertEqual(window.config["working_sets"]["pinned"], ["service:pinned"])
        self.assertEqual(self.started(docker), {"pinned", "a", "dependency"})
        self.assertEqual(window.working.used({})["host_memory_mb"], 64)

    def test_live_scoring_evidence_blocks_isolation_and_keeps_real_owner_occupancy(self):
        window, manager, docker, _ = self.window()
        complete = window._complete
        owner = object()
        def capture(task, event, at):
            event["evidence"]["live_owner"] = owner
            return complete(task, event, at)
        with patch.object(window, "_complete", side_effect=capture), \
             patch("seampact.scheduling.joint.evaluate", side_effect=AssertionError("unsafe evidence was scored")):
            result = window.run()
        self.assertFalse(result["eligible"])
        self.assertEqual(result["scoring"]["mode"], "owner_dependent_research_loop")
        self.assertEqual(result["scoring"]["status"], "BLOCKED")
        self.assertEqual(result["tasks"][0]["evaluation_error"]["type"], "PassiveEvidenceGap")
        self.assertFalse(window.tasks[0].session_closed)
        self.assertIs(window.tasks[0].active_evaluation_terminal["evidence"]["live_owner"], owner)
        self.assertEqual(set(result["release"]["left"]), {"service:a", "service:dependency"})
        self.assertEqual(window.working.claims[window.tasks[0].context], {"service:a", "service:dependency"})
        self.assertTrue(manager.leases["service:a"])
        self.assertTrue(docker.containers)

    def test_nonfinite_numpy_scoring_evidence_remains_blocked_with_owner_retained(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest("NumPy is not installed")
        window, manager, docker, _ = self.window()
        complete = window._complete
        def capture(task, event, at):
            event["evidence"]["array"] = np.array([float("nan")])
            return complete(task, event, at)
        with patch.object(window, "_complete", side_effect=capture), \
             patch("seampact.scheduling.joint.evaluate", side_effect=AssertionError("unsafe NumPy evidence was scored")):
            result = window.run()
        self.assertFalse(result["eligible"])
        self.assertEqual(result["scoring"]["status"], "BLOCKED")
        self.assertEqual(result["tasks"][0]["evaluation_error"]["type"], "PassiveEvidenceGap")
        self.assertFalse(window.tasks[0].session_closed)
        self.assertTrue(manager.leases["service:a"])
        self.assertTrue(docker.containers)

    def test_budget_report_preserves_known_dependencies_but_marks_local_body_unknown(self):
        window, _, _, units = self.window()
        result = window.run()
        self.assertTrue(result["eligible"], result["exclusion_reason"])
        # Exercise the budget completeness view of the actual finished window;
        # no synthetic local-body measurement or optimization is introduced.
        window.budget_policy = joint_fixture.budget_fixture(window_count=1, freeze=True)
        envelope = window._item()["costs"]["resource_envelope"]
        self.assertEqual(envelope["status"], "UNKNOWN")
        self.assertIsNone(envelope["resource_seconds"])
        self.assertGreater(envelope["known_resource_seconds"]["host_memory_mb"], 0)
        self.assertEqual(envelope["local_allocation_gaps"][0]["unit_id"], units[0])
        self.assertTrue(envelope["local_allocation_gaps"][0]["root_ids"])
        self.assertEqual(envelope["local_allocation_gaps"][1]["unit_id"], units[1])
        self.assertEqual(envelope["local_allocation_gaps"][1]["root_ids"], [])
        self.assertIn("frozen_local_owner_has_no_attributable_full_lifetime_resource_integral",
                      envelope["local_allocation_gaps"][0]["reason"])

    def test_skipped_local_pool_has_the_same_budget_completeness_gap(self):
        window, _, _, units = self.window(choices=("skip",))
        result = window.run()
        self.assertEqual(result["tasks"][0]["calls"], [])
        window.budget_policy = joint_fixture.budget_fixture(window_count=1, freeze=True)
        envelope = window._item()["costs"]["resource_envelope"]
        self.assertEqual(envelope["status"], "UNKNOWN")
        self.assertIsNone(envelope["resource_seconds"])
        self.assertEqual({gap["unit_id"] for gap in envelope["local_allocation_gaps"]}, set(units))
        self.assertTrue(all(gap["root_ids"] == [] for gap in envelope["local_allocation_gaps"]))


if __name__ == "__main__":
    unittest.main()
