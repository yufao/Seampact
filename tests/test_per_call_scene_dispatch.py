"""Finite call preconditions use available values at the real decision boundaries."""

import contextlib
import sys
import queue
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from seampact.scenes.multisource_review_scene import MultiSourceReviewScene
from seampact.scenes.scenarios import FrozenCatalog, SceneSession, arrays, route_metric_report
from seampact.scheduling import budget_policy
from seampact.scheduling.joint import Window
from seampact.scheduling.training import TrainingJobs
from test_budget_block_2 import item as budget_item, specification as budget_specification


class ConditionsCatalog:
    def __init__(self):
        self.checked = []
        self.ready = True
        self.host_available = True

    def dispatch_status(self, unit):
        return dict(status="READY" if self.host_available else "UNAVAILABLE")

    def check_call_conditions(self, unit, arguments, native_arguments=(), deferred_arguments=(), *, instance_identity=None):
        self.checked.append((unit, arguments, instance_identity))
        if unit == "plain":
            return dict(status="NOT_REQUIRED", allowed=True)
        allowed = self.ready and arguments == {"value": "safe"}
        return dict(status="CHECKED" if allowed else "UNKNOWN", allowed=allowed, reason="finite value required")

    def require_call_conditions(self, *args, **kwargs):
        result = self.check_call_conditions(*args, **kwargs)
        if not result["allowed"]:
            raise ValueError(result["reason"])
        return result


class SceneDispatchConditionsTests(unittest.TestCase):
    def session(self, ready=True):
        catalog = ConditionsCatalog()
        catalog.ready = ready
        context = type("Context", (), {})()
        context.catalog, context.calls, context.model_calls = catalog, [], []
        context.allowed = set()
        scene = MultiSourceReviewScene(dict(schema="review-events/1", checks=[
            dict(id="actual", stage="intake", calls=[dict(unit_id="conditional", arguments={"value": {"$task": ["material"]}})]),
            dict(id="future", stage="intake", calls=[dict(unit_id="conditional", arguments={"value": {"$result": ["future", 0]}})]),
            dict(id="output", stage="intake", calls=[dict(unit_id="conditional", arguments={"value": {"$output": "value"}},
                output_artifacts={"value": dict(format="npz", worker_root="/tmp", platform_root=str(Path.cwd()),
                                                filename="output.npz", fields=["result"])})]),
            dict(id="plain", stage="intake", calls=[dict(unit_id="plain", arguments={"value": {"$result": ["plain", 0]}})]),
        ]), context)
        session = SceneSession.__new__(SceneSession)
        session.environment, session.context, session.catalog = scene, context, catalog
        session.spec = dict(observation_schema=[], observation_scales=[], candidate_schema=[])
        session.task_units = ["conditional", "plain"]
        session.task = {}
        event = scene.reset(dict(material="safe"), 1)
        event["observation"] = []
        return session, scene, catalog, session._event(event)

    def test_only_available_real_values_enable_conditional_action(self):
        with patch("seampact.scenes.multisource_review_scene.CallArtifacts.prepare",
                   side_effect=AssertionError("preview created an output namespace")):
            session, scene, catalog, event = self.session()
        legal = {item["id"]: item["decision_legal"] for item in event["candidates"]}
        self.assertEqual(legal, dict(actual=True, future=False, output=False, plain=True, **{"continue": True}))
        self.assertIsNone(scene.call_artifacts)
        self.assertEqual(scene.context.calls, [])
        self.assertTrue(any(unit == "conditional" and value is None for unit, value, _ in catalog.checked))

    def test_cold_unknown_is_not_frozen_after_existing_host_becomes_ready(self):
        session, _, catalog, event = self.session(ready=False)
        actual = next(item for item in event["candidates"] if item["id"] == "actual")
        self.assertTrue(actual["legal"])
        self.assertFalse(actual["decision_legal"])
        catalog.ready = True
        mask = session.decision_mask(event)
        index = event["candidates"].index(actual)
        self.assertTrue(mask[index])
        self.assertTrue(actual["decision_legal"])

    def test_dispatch_availability_is_rechecked_with_the_same_candidate(self):
        session, _, catalog, event = self.session()
        candidate = next(item for item in event["candidates"] if item["id"] == "actual")
        catalog.host_available = False
        self.assertFalse(session.candidate_legal(candidate))
        catalog.host_available = True
        self.assertTrue(session.candidate_legal(candidate))

    def test_value_change_before_step_is_rejected_without_invocation(self):
        session, scene, _, event = self.session()
        index = next(i for i, item in enumerate(event["candidates"]) if item["id"] == "actual")
        scene.visible["material"] = "changed"
        with self.assertRaisesRegex(ValueError, "逐次调用条件"):
            session.step(index)
        self.assertEqual(scene.context.calls, [])
        self.assertIsNone(scene.call_artifacts)

    def test_rule_and_learned_business_decisions_receive_same_mask(self):
        session, _, _, event = self.session()
        masks = []
        for policy in ("required_only", "learned"):
            window = Window.__new__(Window)
            window.config = {"functional_policy": policy}
            window.working = SimpleNamespace(fits=lambda *args, **kwargs: True)
            window._record = lambda **record: record
            window._count = lambda *args: None
            def choose(kind, legal, build, rule):
                masks.append(list(legal))
                selected = rule() if policy == "required_only" else legal.index(True)
                return selected, policy, 0.0, 0.0
            window._choose = choose
            task = SimpleNamespace(event=event, context="task", task={"id": "task"}, index=0,
                                   committed_units=set(), decisions=[], session=session)
            window._business(task)
        self.assertEqual(masks[0], masks[1])
        self.assertEqual(masks[0], [True, False, False, True, True])

    def test_functional_rule_and_learner_use_current_mask_for_choice_and_transition(self):
        session, _, catalog, event = self.session(ready=False)
        jobs = TrainingJobs.__new__(TrainingJobs)
        masks = []
        class Learner:
            def act(self, observation, features, legal, training):
                masks.append(list(legal))
                return {"index": legal.index(True)}
        manifest = dict(mode="freeze", algorithm="required_only")
        jobs._choose(manifest, Learner(), event, session)
        self.assertEqual(arrays(event)["legal"], masks[-1])
        self.assertEqual(masks[-1], [False, False, False, True, True])
        catalog.ready = True
        jobs._choose(manifest, Learner(), event, session)
        rule = jobs._choose(manifest, None, event, session)
        self.assertEqual(masks[-1], [True, False, False, True, True])
        self.assertEqual(arrays(event)["legal"], masks[-1])
        self.assertEqual(event["candidates"][rule["index"]]["id"], "continue")

    def test_changed_value_waiting_for_capacity_fails_before_reservation(self):
        session, scene, _, event = self.session()
        index = next(i for i, candidate in enumerate(event["candidates"]) if candidate["id"] == "actual")
        scene.visible["material"] = "changed"
        inbox = queue.Queue()
        task = SimpleNamespace(status="WAITING_CALL_CAPACITY", event=event, pending_action=index,
                               pending_units=["conditional"], session=session, inbox=inbox)
        window = Window.__new__(Window)
        window.tasks = [task]
        window._state = lambda: ({}, [])
        window._admit_tasks()
        self.assertEqual(inbox.get_nowait(), {"condition_rejected": True})
        self.assertEqual(task.status, "RUNNING")
        self.assertIsNone(task.pending_action)
        with self.assertRaisesRegex(ValueError, "逐次调用条件"):
            session.step(index)
        self.assertEqual(scene.context.calls, [])

    def test_admission_rejection_cannot_execute_if_values_change_again(self):
        class Session:
            def reset(self, task, seed):
                return {"kind": "decision"}
            def step(self, index):
                raise AssertionError("rejected action must never execute")
        task = SimpleNamespace(context="task", session=Session(), task={"id": "task"}, index=0,
                               inbox=queue.Queue())
        task.inbox.put({"condition_rejected": True})
        window = Window.__new__(Window)
        window.runtime = SimpleNamespace(context=lambda _: contextlib.nullcontext())
        window.manifest, window.number, window.tasks = {"seed": 0}, 0, [task]
        window.events = queue.Queue()
        window._task_body(task)
        self.assertEqual(window.events.get_nowait()[0], "task_event")
        failure = window.events.get_nowait()
        self.assertEqual(failure[0], "task_error")
        self.assertIsInstance(failure[2], ValueError)

    def test_conditional_local_placement_does_not_grant_call(self):
        class Core:
            def check_call_conditions(self, *args, **kwargs):
                raise AssertionError("local condition must not contact the host")
        catalog = FrozenCatalog.__new__(FrozenCatalog)
        catalog.catalog, catalog.frozen = Core(), {"u": (None, None, {"call_condition": {"rule": "fixture"}})}
        catalog.placement = {"u": {"kind": "LOCAL"}}
        result = catalog.check_call_conditions("u", {"value": "safe"})
        self.assertFalse(result["allowed"])
        self.assertEqual(result["status"], "UNKNOWN")

    def test_conditional_pool_requires_replayed_recipe_and_matching_host_identity(self):
        class Core:
            store = SimpleNamespace(get=lambda kind, identity: {"contract": {"digest": "group-contract"}})
            forged = False
            def require_call_condition_structure(self, unit, *, frozen):
                if self.forged:
                    raise ValueError("recipe differs from original source")
                return dict(status="STRUCTURE_CHECKED", call_permission="REQUIRES_ACTUAL_CALL",
                            runtime_group="group", contract_digest="typed")
        catalog = FrozenCatalog.__new__(FrozenCatalog)
        catalog.catalog, catalog.frozen = Core(), {"u": (None, None, {})}
        catalog.placement = {"u": dict(kind="REMOTE", group_id="group", contract_digest="group-contract")}
        catalog.contract_digests = {"u": "typed"}
        catalog.router = SimpleNamespace(start_gap=lambda group, image: None)
        catalog.require_operation = lambda unit, operation: None
        self.assertEqual(catalog.require_call_condition_structure("u")["call_permission"], "REQUIRES_ACTUAL_CALL")
        catalog.catalog.forged = True
        with self.assertRaisesRegex(ValueError, "recipe"):
            catalog.require_call_condition_structure("u")
        catalog.catalog.forged = False
        catalog.placement["u"]["group_id"] = "other-group"
        with self.assertRaisesRegex(ValueError, "身份"):
            catalog.require_call_condition_structure("u")
        catalog.placement["u"]["group_id"] = "group"
        catalog.catalog.store.get = lambda kind, identity: ({"build": {"image_id": "changed"}} if kind == "deployment"
                                                            else {"contract": {"digest": "group-contract"}})
        with self.assertRaisesRegex(ValueError, "冻结镜像"):
            catalog.require_call_condition_structure("u")
        catalog.catalog.store.get = lambda kind, identity: {"contract": {"digest": "group-contract"}}
        catalog.router.start_gap = lambda group, image: "worker core changed"
        with self.assertRaisesRegex(ValueError, "worker core changed"):
            catalog.require_call_condition_structure("u")

    def test_failed_root_admission_clears_actual_arguments(self):
        for failed_operation in ("enable_root", "wait_root"):
            with self.subTest(operation=failed_operation):
                class Runtime:
                    def call(self, operation, *args):
                        if operation == failed_operation:
                            raise ValueError("fixture failure")
                        return "root"
                class Catalog:
                    placement = {"u": {"kind": "LOCAL"}}
                    def require_operation(self, *args):
                        pass
                    def require_call_conditions(self, *args):
                        pass
                window = Window.__new__(Window)
                window.manifest = {"scene": {"unit_ids": ["u"]}}
                window.catalog, window.runtime = Catalog(), Runtime()
                window.working = SimpleNamespace(covers=lambda *args: True)
                window.call_conditions, window.call_conditions_lock = {}, threading.Lock()
                context = SimpleNamespace(call_arguments=("u", {"value": "safe"}, ()))
                task = SimpleNamespace(context="task", task={"id": "task"}, session=SimpleNamespace(context=context))
                with self.assertRaisesRegex(ValueError, "fixture failure"):
                    with window._admission(task)("u"):
                        self.fail("failed admission must not yield")
                self.assertEqual(window.call_conditions, {})

    def test_dispatch_rechecks_current_values_and_selected_instance_identity(self):
        catalog = ConditionsCatalog()
        instance = dict(id="host", state="READY", session="s", generation=4, process_nonce="n")
        class Deployments:
            lock = threading.Lock()
            def _instances(self, group):
                return [instance]
            def _identity(self, row):
                return dict(session=row["session"], instance_id=row["id"], generation=row["generation"],
                            process_nonce=row["process_nonce"])
        window = Window.__new__(Window)
        window.catalog, window.deployments = catalog, Deployments()
        window.call_conditions_lock = threading.Lock()
        arguments = {"value": "safe"}
        window.call_conditions = {"task": ("conditional", arguments, ())}
        root = dict(task_id="task", unit_id="conditional", target=dict(kind="REMOTE", group_id="group"))
        window._require_root_conditions(root, "host")
        self.assertEqual(catalog.checked[-1][2], dict(session="s", instance_id="host", generation=4, process_nonce="n"))
        arguments["value"] = "changed"
        with self.assertRaisesRegex(ValueError, "finite value"):
            window._require_root_conditions(root, "host")

    def test_rejected_or_missing_feedback_keeps_population_and_budget_members(self):
        scene = dict(id="route", tasks=[dict(id="a"), dict(id="b")])
        criteria = dict(dimensions=[dict(id="risk", applicable=True)])
        for status in ("FAILED", "DONE"):
            with self.subTest(status=status):
                rows = [dict(task_id="a", status="DONE", feedback={"metrics": [
                    dict(id="risk", status="KNOWN", value=.2)]}),
                    dict(task_id="b", status=status, feedback=None)]
                route = route_metric_report(scene, rows, criteria=criteria)[0]
                metric = route["metrics"]["criteria:risk"]
                self.assertEqual((route["tasks"], metric["known"], metric["unknown"]), (2, 1, 1))
                spec = budget_policy.validate(budget_specification(), "ppo", "train", 2)
                items = [budget_item(0, "a", .2, 4, 2), budget_item(1, "b", .6, 8, 4)]
                items[1]["tasks"][0].update(status=status, feedback=None)
                if status == "FAILED":
                    items[1].update(eligible=False, exclusion_reason="condition_rejected")
                    items[1]["service"]["completed"] = 0
                dual = [0.] * 5
                self.assertFalse(budget_policy.settle_block(items, spec, spec["blocks"][0], 0, dual, True))
                result = items[0]["budget_policy"]
                self.assertEqual([row["task_id"] for row in result["quality"]["risk"]], ["a", "b"])
                self.assertEqual(result["quality"]["risk"][1]["request_weight"], .75)
                self.assertEqual(result["quality"]["risk"][1]["loss"], 1.0 if status == "FAILED" else None)
                if status == "DONE":
                    self.assertIsNone(result["normalized"]["quality:risk"])
                self.assertEqual(result["status"], "INELIGIBLE")
                self.assertEqual(dual, [0.] * 5)
                self.assertTrue(all(row["transitions"][0]["transition"]["reward"] == 0 for row in items))


if __name__ == "__main__":
    unittest.main()
