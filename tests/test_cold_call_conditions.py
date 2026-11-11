"""Cold call intents never grant permission to run the original entry."""
import queue
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from seampact.hosting.deployment import Deployments
from seampact.scenes.scenarios import SceneSession
from seampact.scheduling.joint import Window
from seampact.scheduling.residency import WorkingSets
from seampact.worker import Worker
import test_call_value_conditions as fixture_module


class Catalog:
    def __init__(self):
        self.states = {"u": "NO_WORKER", "v": "NO_WORKER"}
        self.placement = {"u": dict(kind="REMOTE", group_id="g1"),
                          "v": dict(kind="REMOTE", group_id="g2")}
        self.checked = []
        self.structures = []
        self.plan_checked = True

    def dispatch_status(self, unit):
        return dict(status="COLD_STARTABLE")

    def require_call_condition_structure(self, unit):
        self.structures.append(unit)

    def require_import_only_plan(self, unit):
        if not self.plan_checked:
            raise ValueError('unknown import prefix')
        return dict(status='CHECKED', plan_digest='plan-' + unit)

    def check_call_conditions(self, unit, arguments):
        self.checked.append((unit, arguments))
        code = self.states[unit]
        return dict(status="CHECKED" if code == "CHECKED" else "UNKNOWN", allowed=code == "CHECKED",
                    reason_code=None if code == "CHECKED" else code,
                    recipe_digest="recipe-" + unit, payload_digest=str(arguments),
                    instance_identity=dict(session='s', instance_id=self.placement[unit]['group_id'],
                                           generation=1, process_nonce='n'))


class Scene:
    def __init__(self):
        self.values = [{"n": 1}, {"n": 2}, {"n": 3}]
        self.invoked = 0

    def preview_calls(self, candidate):
        return [dict(unit_id=unit, arguments=value) for unit, value in
                zip(("u", "u", "v"), self.values)] if candidate["units"] else []

    def step(self, candidate):
        self.invoked += 1
        raise AssertionError("test must not execute original entry")


def make_window(policy="required_only", quota=2):
    catalog, scene = Catalog(), Scene()
    session = SceneSession.__new__(SceneSession)
    session.catalog, session.environment = catalog, scene
    session.context = SimpleNamespace(catalog=catalog, calls=[], allowed=set())
    candidate = dict(id="check", action_type="invoke", legal=True, units=["u", "v"], features=[])
    skip = dict(id="skip", action_type="finish", legal=True, units=[], features=[])
    event = dict(candidates=[candidate, skip], observation=[], references={"required_only": "check"})
    task = SimpleNamespace(index=0, task={"id": "t"}, context="t", event=event, session=session,
        committed_units=set(), decisions=[], status="DECISION", inbox=queue.Queue(), pending_action=None,
        pending_units=[], pending_condition_groups=set(), pending_condition_signatures=None)
    window = Window.__new__(Window)
    window.tasks, window.service_groups = [task], set()
    window.config = dict(functional_policy=policy, capacity=dict(resources={"ram": quota}, replica_cap=1))
    window.working = WorkingSets(dict(pinned=[], tasks={"t": []}, allowed={"t": ["u", "v"]},
        calls={"u": ["g1"], "v": ["g2"]}), {"ram": quota}, {"g1": {"ram": 1}, "g2": {"ram": 1}})
    window.pending_groups, window.reserved = set(), {}
    window.platform = SimpleNamespace(managed_services=SimpleNamespace(acquire=lambda *args, **kw: None))
    views = {g: dict(instances=[], operations={"start": "SUPPORTED"}, start_gap=None) for g in ("g1", "g2")}
    window._state = lambda: (views, [])
    window._operation_allowed = lambda *args: True
    window._record = lambda **facts: facts
    window._count = lambda *args: None
    window._services_ready = lambda task, current: True
    masks = []
    def choose(kind, legal, build, rule):
        masks.append(list(legal))
        return (rule() if policy == "required_only" and legal[0] else legal.index(True)), policy, 0, 0
    window._choose = choose
    starts = []
    def execute(option, origin, current):
        starts.append((option.copy(), set(window.working.claims["t"])))
        window.pending_groups.add(option["group_id"])
        current[option["group_id"]]["instances"].append(dict(id=option["group_id"], state="STARTING"))
        return {"id": option["group_id"]}
    window._execute = execute
    return window, task, catalog, scene, views, masks, starts


class ColdIntentTests(unittest.TestCase):
    def test_same_cold_candidate_and_full_reservation_for_rule_and_learning(self):
        masks = []
        for policy in ("required_only", "learned"):
            window, task, catalog, scene, views, seen, starts = make_window(policy)
            self.assertEqual([v["instances"] for v in views.values()], [[], []])
            window._business(task)
            masks.append(seen[0])
            self.assertFalse(task.event["candidates"][0]["decision_legal"])
            window._admit_tasks()
            self.assertEqual(window.working.claims["t"], {"g1", "g2"})
            self.assertEqual(task.status, "WAITING_CALL_RESOURCES")
            self.assertTrue(task.inbox.empty())
            window._on_demand()
            self.assertEqual([s[0]["mode"] for s in starts], ["start", "start"])
            self.assertTrue(all(s[1] == {"g1", "g2"} for s in starts))
            self.assertEqual(scene.invoked, 0)
            for group, view in views.items():
                view["instances"][0]["state"] = "READY"
                window.pending_groups.remove(group)
            catalog.states = {"u": "CHECKED", "v": "CHECKED"}
            window._admit_tasks()
            self.assertEqual(task.inbox.get_nowait(), 0)
            self.assertEqual(scene.invoked, 0)
            self.assertIn(("u", {"n": 1}), catalog.checked)
            self.assertIn(("u", {"n": 2}), catalog.checked)
        self.assertEqual(masks, [[True, True], [True, True]])

    def test_failed_recheck_and_changed_payload_do_not_step(self):
        for changed in (False, True):
            window, task, catalog, scene, views, _, _ = make_window()
            window._business(task)
            window._admit_tasks()
            window._on_demand()
            for group, view in views.items():
                view["instances"][0]["state"] = "READY"
                window.pending_groups.remove(group)
            catalog.states = {"u": "CHECKED", "v": "CHECKED" if changed else "CONDITION_UNKNOWN"}
            if changed:
                scene.values[1] = {"n": 99}
            window._admit_tasks()
            self.assertEqual(task.inbox.get_nowait(), {"condition_rejected": True})
            self.assertEqual(scene.invoked, 0)

    def test_payload_change_during_policy_selection_rejects_the_intent(self):
        window, task, _, scene, _, masks, starts = make_window("learned")
        def choose(kind, legal, build, rule):
            masks.append(list(legal))
            scene.values[1] = {"n": 99}
            return 0, "learned", 0, 0
        window._choose = choose
        window._business(task)
        self.assertEqual(masks, [[True, True]])
        self.assertEqual(task.inbox.get_nowait(), {"condition_rejected": True})
        self.assertEqual(task.status, "RUNNING")
        self.assertEqual(window.working.claims, {})
        self.assertEqual(starts, [])
        self.assertEqual(scene.invoked, 0)

    def test_non_cold_unknowns_are_not_intents(self):
        for code in ("STARTING", "WORKER_BUSY", "WORKER_NONACCEPTING", "MEMBER_MISSING",
                     "WORKER_UNAVAILABLE", "CONDITION_UNKNOWN"):
            with self.subTest(code=code):
                window, task, catalog, _, _, seen, starts = make_window("learned")
                catalog.states["u"] = code
                window._business(task)
                self.assertEqual(seen[0], [False, True])
                self.assertEqual(task.pending_action, 1)
                self.assertEqual(starts, [])

    def test_full_compound_capacity_is_checked_before_selection(self):
        window, task, _, _, _, seen, starts = make_window("learned", quota=1)
        window._business(task)
        self.assertEqual(seen[0], [False, True])
        self.assertEqual(task.pending_action, 1)
        self.assertEqual(starts, [])

    def test_start_failure_is_reported_to_waiting_task_without_retry(self):
        window, task, _, scene, _, _, _ = make_window()
        window._business(task)
        window._admit_tasks()
        op = dict(id="op", action="prepare", mode="start", group_id="g1", instance_id=None,
                  origin="on_demand", status="RUNNING")
        window.op_walls, window.start = {"op": time.time()}, time.time()
        window.wid = "window"
        window.stopping, window.unknown_outcomes = set(), []
        window.runtime = SimpleNamespace(call=lambda *args: [])
        window._settle(op, "FAILED", "start failed")
        self.assertEqual(task.inbox.get_nowait(), {"condition_rejected": True})
        self.assertEqual(task.status, "RUNNING")
        self.assertIsNone(task.pending_action)
        self.assertEqual(scene.invoked, 0)

    def test_probe_does_not_bypass_root_slot(self):
        window, _, _, _, views, _, _ = make_window()
        window.config["capacity"]["call_slots"] = 1
        roots = [dict(state="ACTIVE", host="occupied"), dict(state="ENABLED", enabled_at=0)]
        self.assertEqual(window._dispatchable(views, roots), [])

    def test_ready_default_intent_uses_one_checked_import_after_full_claim(self):
        for policy in ("required_only", "learned"):
            window, task, catalog, scene, views, masks, _ = make_window(policy)
            for group, view in views.items():
                view['instances'] = [dict(id=group, state='READY')]
            catalog.states = {'u': 'DEFAULT_BINDING_UNKNOWN', 'v': 'CHECKED'}
            operations = []
            def execute(option, origin, current):
                self.assertEqual(window.working.claims['t'], {'g1', 'g2'})
                self.assertEqual(option['mode'], 'import_only')
                operation = dict(status='RUNNING', **option)
                operations.append(operation)
                window.pending_groups.add(option['group_id'])
                return operation
            window._execute = execute
            window._business(task)
            self.assertEqual(masks[0], [True, True])
            self.assertFalse(task.event['candidates'][0]['decision_legal'])
            window._admit_tasks()
            self.assertTrue(task.inbox.empty())
            window._on_demand()
            self.assertEqual(len(operations), 1)
            self.assertEqual(operations[0]['plan_digest'], 'plan-u')
            operations[0]['status'] = 'DONE'
            window.pending_groups.remove('g1')
            catalog.states['u'] = 'CHECKED'
            window._admit_tasks()
            self.assertEqual(task.inbox.get_nowait(), 0)
            self.assertEqual(scene.invoked, 0)

    def test_ready_default_unknown_prefix_is_not_an_intent(self):
        window, task, catalog, _, views, masks, starts = make_window('learned')
        for group, view in views.items():
            view['instances'] = [dict(id=group, state='READY')]
        catalog.states = {'u': 'DEFAULT_BINDING_UNKNOWN', 'v': 'CHECKED'}
        catalog.plan_checked = False
        window._business(task)
        self.assertEqual(masks, [[False, True]])
        self.assertEqual(task.pending_action, 1)
        self.assertEqual(starts, [])

    def test_router_never_classifies_held_or_unavailable_worker_as_cold(self):
        router = Deployments.__new__(Deployments)
        router.lock, router.instances = threading.RLock(), {}
        unit, proposal = dict(id="u", runtime_group="g"), dict(recipe_digest="r", payload_digest="p")
        self.assertEqual(router.check_call_conditions(unit, {"x": 1}, proposal)["reason_code"], "NO_WORKER")
        for state, accepting, members, expected in (("STARTING", False, [], "STARTING"),
                ("UNKNOWN", False, [], "WORKER_UNAVAILABLE"), ("READY", False, ["u"], "WORKER_NONACCEPTING"),
                ("READY", True, [], "MEMBER_MISSING")):
            router.instances = {"a": dict(group_id="g", state=state, accepting=accepting, units=members)}
            self.assertEqual(router.check_call_conditions(unit, {"x": 1}, proposal)["reason_code"], expected)

    def test_real_cold_controller_and_worker_checks_do_not_load_original(self):
        fixture = fixture_module.CallValueConditionTests("test_catalog_and_worker_require_actual_same_call_before_source_load")
        self.addCleanup(fixture.doCleanups)
        _, _, _, catalog, unit = fixture.catalog(
            "def score(value):\n    assert isinstance(value,list)\n    return 7\n")
        binding, _ = catalog.deferred[unit["id"]]
        router = Deployments.__new__(Deployments)
        router.lock, router.instances = threading.RLock(), {}
        catalog.remote_router = router
        cold = catalog.check_call_conditions(unit["id"], {"value": []})
        self.assertEqual((cold["allowed"], cold["reason_code"]), (False, "NO_WORKER"))
        self.assertEqual(binding.modules, {})
        identity = dict(session="s", instance_id="i", generation=1, process_nonce="n")
        worker = Worker.__new__(Worker)
        worker.catalog, worker.store = catalog, catalog.store
        worker.session, worker.instance, worker.generation, worker.process_nonce = identity.values()
        worker.units, worker.accepting, worker.lock = {unit["id"]}, True, threading.Lock()
        worker.in_flight, worker.preparing = 0, 0
        router.instances["i"] = dict(id="i", group_id=unit["runtime_group"], state="READY",
            accepting=True, units=[unit["id"]], endpoint="http://unused", **{k: v for k, v in identity.items() if k != "instance_id"})
        def check(endpoint, body, timeout):
            catalog._worker_call_identity = identity
            try:
                return worker.check_call(body)
            finally:
                del catalog._worker_call_identity
        with patch("seampact.hosting.deployment.json_request", side_effect=check):
            self.assertTrue(catalog.check_call_conditions(unit["id"], {"value": []})["allowed"])
            worker.in_flight = 1
            busy = catalog.check_call_conditions(unit["id"], {"value": []})
        self.assertEqual(busy["reason_code"], "WORKER_BUSY")
        self.assertFalse(busy["allowed"])
        self.assertEqual(binding.modules, {})


if __name__ == "__main__":
    unittest.main()
