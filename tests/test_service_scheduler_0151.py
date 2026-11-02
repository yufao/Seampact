"""Dependency admission and real joint Window software checks; no model results."""
import copy
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from seampact.scheduling.residency import WorkingSets, freeze_working_sets
from seampact.capabilities import freeze_joint, joint_candidate, JOINT_SCALES
from seampact.scheduling.costs import envelope_integral
from seampact.scheduling.joint import Window, configure
from seampact.hosting.managed_services import ManagedServices
from seampact.providers import Profiles
from seampact.scheduling.training import LEARNING, REFERENCES, BoundProfiles
from seampact.evaluation import validate_criteria
from seampact.runtime import _task_context
import test_managed_services as service_fixture
import test_joint_014 as joint_fixture


class ServiceSchedulerTests(unittest.TestCase):
    def test_dependency_cost_and_shared_lru_choose_different_legal_retirements(self):
        window = object.__new__(Window)
        window.config = dict(resource_policy="dependency_cost/1", retention=dict(mode="idle_retire/1", idle_ms=100))
        options = [dict(action="scale_in", instance_id="expensive", group_id="a", facts=dict(
            idle_ms=3000, load_ms=2000, marginal_resources={"memory": -0.5})),
            dict(action="scale_in", instance_id="cheap", group_id="b", facts=dict(
                idle_ms=1500, load_ms=100, marginal_resources={"memory": -0.5})),
            dict(action="yield_until_next_event")]
        self.assertEqual(window._resource_rule(options), 1)
        window.config["resource_policy"] = "shared_lru/1"
        self.assertEqual(window._resource_rule(options), 0)
        window.config["resource_policy"] = "dependency_cost/1"
        options[0]["facts"]["idle_ms"] = 200
        options[1]["facts"]["idle_ms"] = 150
        self.assertEqual(window._resource_rule(options), 2)

    def test_transitive_shared_closure_and_atomic_actual_occupancy(self):
        demands = {g: {"memory": v} for g, v in (("a", 1), ("b", 1), ("service:m", 5), ("service:t", 1))}
        spec = freeze_working_sets(dict(unit_ids=["ua", "ub"]),
            [dict(id="a", unit_ids=["ua"]), dict(id="b", unit_ids=["ub"])],
            {"ua": dict(kind="REMOTE", group_id="a", resource_dependencies=["service:m"]),
             "ub": dict(kind="REMOTE", group_id="b", resource_dependencies=["service:m"])},
            {"memory": 8}, demands, list(demands), "idle_retire/1",
            {"service:m": ["service:t"]})
        working = WorkingSets(spec, {"memory": 8}, demands)
        self.assertTrue(working.admit("one", "a"))
        self.assertTrue(working.admit("two", "b"))
        self.assertEqual(working.used({}), {"memory": 8})
        working.release("two")
        before = copy.deepcopy(working.claims)
        self.assertFalse(working.admit("two", "b", {"service:m": 2}))
        self.assertEqual(working.claims, before)

    def test_versioned_candidate_resource_vector(self):
        scales = {k: 1 for k in JOINT_SCALES}
        spec = freeze_joint(scales, {"gpu_memory_mb": 8})
        self.assertEqual(spec["version"], "joint-features/3")
        vector = joint_candidate("prepare", scales, resource_schema=spec["resources"], resource=dict(
            held=0, ready=0, starting=0, draining=0, cap_remaining=1, waiting_roots=2,
            service_target=True, shared_users=2, marginal_resources={"gpu_memory_mb": 0.5}))
        self.assertEqual(len(vector), len(spec["candidate_schema"]))
        self.assertEqual(vector[spec["candidate_schema"].index("marginal:gpu_memory_mb")], 0.5)
        with self.assertRaisesRegex(ValueError, "unavailable"):
            joint_candidate("prepare", scales, resource_schema=spec["resources"], resource=dict(
                held=0, ready=0, starting=0, draining=0, cap_remaining=1, waiting_roots=2))

    def test_full_lifetime_envelope_counts_physical_generation_once(self):
        row = dict(instance_id="physical", generation=2, group_id="service:m", resident_ms=4500,
                   end_observed=False)
        cost = envelope_integral({"instances": [row, row]}, {"service:m": {"memory": 8}}, {"memory": 10})
        self.assertEqual(cost["resource_seconds"], {"memory": 36})
        self.assertFalse(cost["instances"][0]["release_confirmed"])
        self.assertEqual(envelope_integral({"instances": [row]}, {}, {"memory": 10})["status"], "UNKNOWN")

    def test_real_window_prepares_one_shared_service_and_holds_through_evaluation(self):
        self.real_window("cold")

    def test_warm_service_window_accounts_preparation_and_releases(self):
        self.real_window("warm")

    def test_managed_teacher_transport_holds_task_context_and_request_slot(self):
        self.real_window("cold", teacher=True)

    def test_unknown_teacher_request_keeps_service_lease_and_capacity(self):
        self.real_window("cold", teacher=True, unknown=True)

    def real_window(self, initial, teacher=False, unknown=False):
        fixture = joint_fixture.Joint014Integration()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        service = service_fixture.ServiceTest()
        service.setUp()
        self.addCleanup(service.tearDown)
        platform, controller = fixture.platform, fixture.platform.deployments
        controller.configure_backend(dict(kind="docker", coordinator_url=service.spec["endpoint"]))
        manager = ManagedServices(platform, runner=service.docker)
        platform.managed_services = manager
        spec = manager.configure(service.spec)
        profiles = Profiles(fixture.home / "profiles")
        profiles.managed_services = manager
        platform.profiles = profiles
        connection = profiles.save_connection(dict(name="shared fixture model", protocol="openai-compatible",
            base_url=spec["endpoint"], model=spec["model"], resource_kind="managed", managed_service_id=spec["id"],
            capabilities=["text"], auth=dict(mode="none")))
        profiles.select_role("teacher", connection["id"])
        window = fixture.window()
        catalog = window.catalog
        # Freeze the same managed role dependency consumed by normal training.
        catalog.add_role_services(profiles, ["teacher"])
        groups = catalog.remote_groups()
        raw = dict(joint=dict(initial_state=initial, age_guard_ms=100, max_consecutive_resource_ops=1,
            resource_policy="dependency_cost/1", retention=dict(mode="idle_retire/1", idle_ms=100),
            capacity=dict(call_slots=2, resources={"host_memory_mb": 1024},
                demands={g: {"host_memory_mb": 512} for g in groups}),
            workload=dict(tasks_per_window=2, arrival=dict(interval_ms=0)),
            costs=dict(schema="joint-costs/2", task_seconds=0, instance_seconds=0,
                       resource_seconds={"host_memory_mb": 1}), scales={key: 1 for key in JOINT_SCALES},
            start_timeout_s=2, observation=dict(interval_s=0.1, include_current_process=True)))
        selected = configure(raw, scene=fixture.scene, tasks=fixture.scene["tasks"], task_ids=["ta", "tb"],
            algorithm="required_only", criteria=fixture.criteria,
            contracts={g: platform.store.get("runtime_group", g)["contract"] for g in groups},
            groups=groups, deployments=controller, learning=LEARNING, references=REFERENCES,
            mode="freeze", placements=catalog.placement, service_dependencies=catalog.service_dependencies,
            shared_services=catalog.shared_services)
        catalog.bind_run("check", selected["resource_control"])
        controller.lock_groups("check", groups, selected["resource_control"])
        manifest = dict(window.manifest, **selected)
        manifest["managed_services"] = catalog.service_snapshot()
        bound = BoundProfiles(profiles)
        bound.managed_catalog = catalog
        if teacher:
            criteria = validate_criteria(dict(id="teacher-check", version="1", prompt="Evaluate the actual answer.",
                evidence_fields=["answer"], dimensions=[dict(id="quality", provider="teacher", direction="maximize",
                    unit="score", definition="fixture answer quality", range=[0, 1], required_evidence=["answer"])],
                objective=dict(weights={"quality": 1})))
            manifest = copy.deepcopy(manifest)
            manifest.update(criteria=criteria, evaluation_criteria=criteria)
            for route in manifest["scene"]["routes"].values():
                route.update(criteria=criteria, evaluation_criteria=criteria)
        window = Window(window.jobs, "check", manifest, catalog, bound, None, 0, fixture.scene["tasks"], {})
        evaluated = []
        teacher_requests = []
        original_evaluate = window._evaluate
        def evaluate(task, index):
            live = manager.live_instances("model-a")
            evaluated.append(bool(live) and task.context in manager.group_view("model-a")["leases"].get(live[0]["id"], []))
            if teacher:
                task.terminal["evidence"] = dict(answer=task.terminal["response"])
            return original_evaluate(task, index)
        def teacher_send(url, key, body):
            owner = _task_context.get()
            self.assertIn(owner, window.by_context)
            self.assertIn(owner, manager.leases["service:model-a"])
            self.assertGreater(manager.live_instances("model-a")[0]["in_flight"], 0)
            teacher_requests.append(owner)
            if unknown:
                raise TimeoutError("fixture unknown teacher outcome")
            return dict(choices=[dict(message=dict(content=json.dumps(dict(metrics=[dict(id="quality", status="KNOWN",
                value=1, reason="fixture result", evidence=["answer"])]))))],
                usage=dict(prompt_tokens=1, completion_tokens=1, total_tokens=2))
        with patch.object(window, "_evaluate", side_effect=evaluate), \
             patch("seampact.evaluation.Transport.send", side_effect=teacher_send):
            result = window.run()
        self.assertIsNone(result["execution_error"], result["execution_error"])
        if unknown:
            self.assertFalse(result["eligible"])
            self.assertTrue(teacher_requests)
            self.assertTrue(manager.leases["service:model-a"])
            self.assertGreater(manager.live_instances("model-a")[0]["unknown_requests"], 0)
            self.assertTrue(result["release"]["left"])
            self.assertTrue(service.docker.containers)
            return
        self.assertTrue(result["eligible"], result["exclusion_reason"])
        if teacher:
            self.assertEqual(len(teacher_requests), 4)
        self.assertEqual(sum(c[:2] == ["docker", "run"] for c in service.docker.commands), 1)
        self.assertEqual(evaluated, [True, True])
        self.assertEqual([t["final_response"] for t in result["tasks"]], [4, 7])
        self.assertGreater(result["costs"]["resource_envelope"]["resource_seconds"]["host_memory_mb"], 0)
        self.assertEqual(window.working.claims, {})
        self.assertFalse(service.docker.containers)
        self.assertFalse(result["release"]["left"])


if __name__ == "__main__":
    unittest.main()
