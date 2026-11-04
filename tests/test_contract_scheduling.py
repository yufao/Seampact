"""Contract consumption through installed Units and the real joint window."""
import copy
import unittest

import test_joint_014 as joint_fixture
from seampact.scenes.scenarios import FrozenCatalog


class ContractSchedulingTests(unittest.TestCase):
    def setUp(self):
        self.fixture = joint_fixture.Joint014Integration("test_mixed_routes_share_actual_coordinator_and_boundaries")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_real_install_frozen_contract_and_joint_calls(self):
        window = self.fixture.window()
        for uid in self.fixture.units:
            evidence = window.catalog.require_operation(uid, "dispatch")
            self.assertTrue(evidence["allowed"])
            self.assertEqual(evidence["contract_digest"], window.catalog.contract_digests[uid])
        result = window.run()
        self.assertIsNone(result["execution_error"], result["execution_error"])
        self.assertEqual([row["final_response"] for row in result["tasks"]], [4, 7])

    def test_frozen_catalog_refuses_drift_and_never_invokes(self):
        uid = self.fixture.units[0]
        frozen = FrozenCatalog(self.fixture.catalog, [uid])
        unit = self.fixture.store.get("unit", uid)
        unit["symbol"] = "tampered"
        self.fixture.store.save("unit", uid, unit)
        self.assertEqual(frozen.dispatch_status(uid)["status"], "UNAVAILABLE")
        with self.assertRaises(ValueError):
            frozen.invoke(uid, {"value": 1})
        self.assertFalse(self.fixture.runtime.snapshot()["calls"])

    def test_missing_contract_rejected_at_frozen_admission(self):
        uid = self.fixture.units[0]
        unit = self.fixture.store.get("unit", uid)
        unit.pop("typed_contract", None)
        self.fixture.store.save("unit", uid, unit)
        with self.assertRaises(ValueError):
            FrozenCatalog(self.fixture.catalog, [uid])

    def test_group_change_rejects_already_frozen_dispatch(self):
        uid = self.fixture.units[0]
        frozen = FrozenCatalog(self.fixture.catalog, [uid])
        unit = self.fixture.store.get("unit", uid)
        group = self.fixture.store.get("runtime_group", unit["runtime_group"])
        changed = copy.deepcopy(group)
        changed["contract"]["digest"] = "changed"
        self.fixture.store.save("runtime_group", group["id"], changed)
        self.assertEqual(frozen.dispatch_status(uid)["status"], "UNAVAILABLE")

    def test_frozen_dispatch_rechecks_binding_configuration(self):
        uid = self.fixture.units[0]
        frozen = FrozenCatalog(self.fixture.catalog, [uid])
        binding = frozen.frozen[uid][0]
        binding.inspector.request["configuration"] = {"unexpected": True}
        self.assertEqual(frozen.dispatch_status(uid)["status"], "UNAVAILABLE")
        with self.assertRaises(ValueError):
            frozen.invoke(uid, {"value": 1})
        self.assertFalse(self.fixture.runtime.snapshot()["calls"])

    def test_joint_window_refuses_changed_evidence_before_call(self):
        window = self.fixture.window()
        uid = self.fixture.units[0]
        unit = self.fixture.store.get("unit", uid)
        unit["symbol"] = "tampered"
        self.fixture.store.save("unit", uid, unit)
        result = window.run()
        self.assertFalse(result["eligible"])
        self.assertNotIn(uid, [call["unit_id"] for task in result["tasks"] for call in task.get("calls", [])])

    def test_boundary_rechecks_binding_after_candidate_selection(self):
        window = self.fixture.window()
        original = window.catalog.require_boundary
        seen = {}

        def change_after_first_check(uid, call, root, gate):
            seen[call["id"]] = seen.get(call["id"], 0) + 1
            if seen[call["id"]] == 2:
                window.catalog.frozen[uid][0].inspector.request["configuration"] = {"changed": True}
            return original(uid, call, root, gate)

        window.catalog.require_boundary = change_after_first_check
        result = window.run()
        self.assertFalse(result["eligible"])
        refused = [row for row in result["decisions"] if row.get("choice") == "contract_refused"]
        self.assertTrue(refused)
        calls = self.fixture.runtime.snapshot()["calls"]
        self.assertTrue(all(calls[row["call_id"]]["starts"] == 0 for row in refused))


if __name__ == "__main__":
    unittest.main()
