"""Cold call intents never grant permission to run the original entry."""
import sys
import threading
import unittest
from unittest.mock import patch

from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from seampact.hosting.deployment import Deployments
from seampact.worker import Worker
import test_call_value_conditions as fixture_module








class ColdIntentTests(unittest.TestCase):









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
