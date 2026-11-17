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





if __name__ == "__main__":
    unittest.main()
