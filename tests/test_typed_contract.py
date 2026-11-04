"""Actual installation, replay and finite-scope operation checks."""

import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from seampact.runtime import Coordinator, OwnerRuntime
from seampact.source.inspection import Inspector
from seampact.store import Store
from seampact.typed_contract import operation_status, require_boundary, require_operation, verify_contract
from seampact.units import UnitCatalog


SOURCE = """class Score:
    def __init__(self, bias: float):
        self.bias = bias

    def predict(self, values: list[float]) -> float:
        return sum(values) + self.bias
"""


def install_fixture(case, source=SOURCE, **candidate_fields):
    directory = tempfile.TemporaryDirectory()
    case.addCleanup(directory.cleanup)
    root = Path(directory.name)
    (root / "original.py").write_text(source, encoding="utf-8")
    inspector = Inspector.freeze(root, root / "snapshots", "request", ["original.py"], "Score.predict", "original result")
    inspector.request["source_id"] = "source"
    inspector.request["configuration"] = dict(entries=dict(predict=dict(constructor_arguments=dict(bias=0.5))))
    candidate = dict(entries=[dict(id="predict", path="original.py", symbol="Score.predict", kind="method",
        constructor=dict(symbol="Score", arguments=dict(bias=0.5)), boundary_ids=["predict-W"])],
        boundaries=[dict(id="predict-W", path="original.py", symbol="Score.predict", mode="W")], **candidate_fields)
    store = Store(":memory:")
    case.addCleanup(store.close)
    runtime = OwnerRuntime(Coordinator(store))
    case.addCleanup(runtime.close)
    catalog = UnitCatalog(root, store, runtime)
    unit = catalog.install(inspector, candidate, "analysis", "install")[0]
    group = store.get("runtime_group", unit["runtime_group"])
    return catalog, inspector, candidate, unit, group


def reseal(contract):
    body = {key: value for key, value in contract.items() if key != "digest"}
    contract["digest"] = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False,
        allow_nan=False, separators=(",", ":")).encode("utf-8")).hexdigest()


class TypedContractTests(unittest.TestCase):
    def test_actual_install_replay_public_checks_and_original_execution(self):
        catalog, _, _, unit, group = install_fixture(self)
        self.assertTrue(verify_contract(unit, group=group)["valid"])
        for operation in ("invoke", "dispatch", "export_entry", "remote", "prepare", "prewarm", "scale_out", "scale_in"):
            self.assertTrue(require_operation(unit, operation, group=group)["allowed"], operation)
        self.assertEqual(catalog.invoke(unit["id"], dict(values=[1.0, 2.0])), 3.5)
        exposed = catalog.public()[0]
        self.assertTrue(exposed["contract_check"]["valid"])
        self.assertTrue(exposed["operation_checks"]["invoke"]["allowed"])
        self.assertFalse(unit["typed_contract"]["formal_proof"])

    def test_resealed_claims_cannot_replace_a_replayed_decision(self):
        _, _, _, unit, group = install_fixture(self)
        altered = copy.deepcopy(unit)
        altered["typed_contract"]["operations"]["dispatch"]["guards"] = []
        reseal(altered["typed_contract"])
        self.assertFalse(verify_contract(altered, group=group)["valid"])
        with self.assertRaises(ValueError):
            require_operation(altered, "dispatch", group=group)

    def test_source_boundary_identity_and_group_drift_require_recheck(self):
        _, _, _, unit, group = install_fixture(self)
        for field, value in (("source_version", "other-source"), ("boundary_ids", []),
                             ("id", "other-unit"), ("installation_id", "other-install")):
            with self.subTest(field=field):
                altered = copy.deepcopy(unit)
                altered[field] = value
                reseal(altered["typed_contract"])
                self.assertFalse(verify_contract(altered, group=group)["valid"])
        changed = copy.deepcopy(group)
        changed["contract"]["operations"]["add_replica"]["status"] = "UNKNOWN"
        self.assertFalse(verify_contract(unit, group=changed)["valid"])
        self.assertFalse(verify_contract(unit)["valid"])

    def test_historical_unit_uses_normal_reinstallation(self):
        catalog, inspector, candidate, unit, group = install_fixture(self)
        unit.pop("typed_contract")
        catalog.store.save("unit", unit["id"], unit)
        self.assertEqual(verify_contract(unit, group=group)["status"], "RECHECK_REQUIRED")
        with self.assertRaises(ValueError):
            catalog.invoke(unit["id"], dict(values=[1.0]))
        refreshed = catalog.install(inspector, candidate, "analysis", "install")[0]
        self.assertTrue(verify_contract(refreshed, group=catalog.store.get("runtime_group", refreshed["runtime_group"]))["valid"])
        self.assertEqual(catalog.invoke(unit["id"], dict(values=[1.0])), 1.5)

    def test_unknown_lifecycle_and_candidate_flags_grant_no_scaling(self):
        source = SOURCE.replace("return sum(values) + self.bias", "self.bias = values[0]\n        return self.bias")
        _, _, _, unit, group = install_fixture(self, source, runtime_groups=[dict(entries=["predict"],
            lifecycle=dict(reclaim=True, replicate=True), model_is_read_only=True, trusted=True)])
        self.assertTrue(operation_status(unit, "invoke", group=group)["allowed"])
        for operation in ("scale_out", "scale_in"):
            result = operation_status(unit, operation, group=group)
            self.assertEqual(result["status"], "UNKNOWN")
            self.assertFalse(result["allowed"])
        unit["typed_contract"]["operations"]["scale_out"].update(status="SUPPORTED", allowed=True)
        reseal(unit["typed_contract"])
        self.assertFalse(verify_contract(unit, group=group)["valid"])

    def test_profile_membership_is_independent_of_entry_check(self):
        source = SOURCE.replace("sum(values)", "unknown_function(values)")
        _, _, _, unit, group = install_fixture(self, source)
        self.assertEqual(unit["status"], "MISSING")
        self.assertEqual(unit["typed_contract"]["profile"]["status"], "IN")
        self.assertFalse(any(row["allowed"] for row in unit["typed_contract"]["operations"].values()))

    def test_generator_is_outside_profile(self):
        source = SOURCE.replace("return sum(values) + self.bias", "yield sum(values) + self.bias")
        _, _, _, unit, group = install_fixture(self, source)
        self.assertEqual(unit["typed_contract"]["profile"]["status"], "OUT")
        self.assertFalse(operation_status(unit, "invoke", group=group)["allowed"])

    def test_deferred_checked_host_keeps_cold_start_eligibility(self):
        from seampact.typed_contract import build_contract
        _, _, _, unit, group = install_fixture(self)
        # Target-interpreter installations use this same existing deferred shape.
        unit.update(status="MISSING", host_condition=dict(status="ENVIRONMENT_DEFERRED", gaps=["worker not registered"]))
        unit["typed_contract"] = build_contract(unit, group=group)
        for operation in ("dispatch", "invoke", "remote", "prepare"):
            self.assertTrue(operation_status(unit, operation, group=group)["allowed"])

    def test_changed_live_candidate_or_configuration_and_snapshot_are_rejected(self):
        catalog, inspector, candidate, unit, _ = install_fixture(self)
        frozen = catalog.freeze_resources([unit["id"]])
        live_inspector = catalog.bindings[unit["id"]].inspector
        live_inspector.request["configuration"]["entries"]["predict"]["constructor_arguments"]["bias"] = 2.0
        with self.assertRaises(ValueError):
            catalog.invoke_frozen(frozen, unit["id"], dict(values=[1.0]))
        live_inspector.request["configuration"]["entries"]["predict"]["constructor_arguments"]["bias"] = 0.5
        catalog.bindings[unit["id"]].candidate["entries"][0]["boundary_ids"] = []
        with self.assertRaises(ValueError):
            catalog.invoke(unit["id"], dict(values=[1.0]))
        catalog.bindings[unit["id"]].candidate["entries"][0]["boundary_ids"] = ["predict-W"]
        record = inspector.files["original.py"]
        (inspector.snapshots / record["sha256"]).write_text("changed snapshot", encoding="utf-8")
        with self.assertRaises(ValueError):
            catalog.freeze_resources([unit["id"]])

    def test_omitted_boundaries_normalize_to_the_native_binding_default(self):
        catalog, inspector, candidate, _, _ = install_fixture(self)
        candidate.pop("boundaries")
        candidate["entries"][0]["boundary_ids"] = []
        unit = catalog.install(inspector, candidate, "analysis-2", "install-2")[0]
        self.assertEqual(catalog.invoke(unit["id"], dict(values=[1.0])), 1.5)

    def test_invalid_entry_row_does_not_discard_checked_sibling(self):
        catalog, inspector, candidate, _, _ = install_fixture(self)
        candidate["entries"].append(None)
        valid, missing = catalog.install(inspector, candidate, "mixed", "mixed")
        self.assertEqual(missing["status"], "MISSING")
        self.assertFalse(missing["typed_contract"]["operations"]["dispatch"]["allowed"])
        self.assertEqual(catalog.invoke(valid["id"], dict(values=[1.0])), 1.5)

    def test_actual_gates_cannot_move_between_roots_or_owners(self):
        catalog, inspector, candidate, original, group = install_fixture(self)
        for selected in (True, False):
            with self.subTest(selected=selected):
                unit = original
                if not selected:
                    candidate["entries"][0]["boundary_ids"] = []
                    candidate["boundaries"] = []
                    unit = catalog.install(inspector, candidate, "analysis-2", "install-2")[0]
                    group = catalog.store.get("runtime_group", unit["runtime_group"])
                runtime = catalog.runtime
                binding, entry = catalog.bindings[unit["id"]], catalog.entry_specs[unit["id"]]
                binding.prepare(entry, unit)
                gate = next(row for row in runtime.snapshot()["gates"].values() if row["unit"].get(
                    "root_unit_id", row["unit"]["id"]) == unit["id"])
                runtime.call("configure_roots", dict(run_id="window", call_slots=1, boundaries=True))
                root_id = runtime.call("enable_root", "task", unit["id"], dict(kind="LOCAL"))
                runtime.call("authorize_root", root_id, "local")
                call_id = runtime.call("enable", gate["id"], gate["owner"], task_id="task")
                state = runtime.snapshot()
                root, call = state["roots"][root_id], state["calls"][call_id]
                def check(root, call, gate):
                    return require_boundary(unit, group=group, binding=binding, entry=entry, call=call, root=root, gate=gate)
                decision = check(root, call, gate)
                self.assertEqual(decision["boundary_id"], "predict-W" if selected else None)
                self.assertEqual(decision["controller_schedule"], "UNKNOWN")
                for wrong in ((dict(root, unit_id="another"), call, gate), (root, dict(call, generation=2), gate),
                              (root, call, dict(gate, unit=dict(gate["unit"], symbol="another")))):
                    with self.assertRaises(ValueError):
                        check(*wrong)
                runtime.call("cancel", call_id)
                runtime.call("finish_root", root_id, "RETURN")
                runtime.call("configure_roots", None)

    def test_two_original_methods_keep_separate_registered_root_scopes(self):
        source = SOURCE + "\n    def alternative(self, values: list[float]) -> float:\n        return sum(values) - self.bias\n"
        catalog, inspector, candidate, _, _ = install_fixture(self, source)
        alternative = dict(candidate["entries"][0], id="alternative", symbol="Score.alternative", boundary_ids=["alternative-W"])
        candidate["entries"].append(alternative)
        candidate["boundaries"].append(dict(id="alternative-W", path="original.py", symbol="Score.alternative", mode="W"))
        inspector.request["configuration"]["entries"]["alternative"] = dict(constructor_arguments=dict(bias=0.5))
        units = catalog.install(inspector, candidate, "analysis-2", "install-2")
        self.assertEqual([catalog.invoke(unit["id"], dict(values=[1.0])) for unit in units], [1.5, 0.5])
        gates = catalog.runtime.snapshot()["gates"].values()
        scopes = {gate["unit"]["root_unit_id"]: (gate["unit"]["boundary_id"], gate["owner"]) for gate in gates}
        self.assertEqual([scopes[unit["id"]][0] for unit in units], ["predict-W", "alternative-W"])
        self.assertNotEqual(scopes[units[0]["id"]][1], scopes[units[1]["id"]][1])

if __name__ == "__main__":
    unittest.main()
