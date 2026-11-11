"""Normal typed material evidence never grants semantic export or execution."""
import copy
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from seampact.source.inspection import Inspector
from seampact.source.value_conditioned_admission import compile_structure_plan
from seampact.source.verification import check_candidate
from seampact.typed_contract import (binding_evidence, build_contract, entry_evidence,
                                    installation_evidence, operation_status, verify_contract)


SOURCE = ('def unknown(value):\n    return eval(value)\n'
          'def score(value: str) -> int:\n    if value == "safe":\n        return 7\n'
          '    return unknown(value)\n')


class ValueConditionedContractTests(unittest.TestCase):
    def fixture(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root / "original.py").write_text(SOURCE, encoding="utf-8")
        inspector = Inspector.freeze(root, root / "snapshots", "request", ["original.py"], "score", "result")
        inspector.request["source_id"] = "source"
        entry = dict(id="score", kind="function", path="original.py", symbol="score", boundary_ids=["whole"])
        candidate = dict(entries=[entry], boundaries=[dict(id="whole", mode="W", path="original.py", symbol="score")])
        structure = compile_structure_plan(inspector, entry, candidate)
        self.assertEqual(structure["status"], "STRUCTURE_CHECKED", structure.get("reason"))
        checked = {row["id"]: row for row in check_candidate(inspector, candidate)["boundaries"]}
        unit = dict(id="source:score", entry_id="score", source_id="source",
                    source_version=inspector.request["source_version"], installation_id="installation",
                    analysis_id="analysis", path="original.py", source_unit="file", symbol="score",
                    kind="function", boundary_ids=["whole"], status="STRUCTURE_ONLY",
                    binding="SOURCE_VALUE_CONDITIONED_NATIVE_ENTRY", effective_binding="function",
                    input_schema=copy.deepcopy(structure["interface"]), parameters=copy.deepcopy(structure["interface"]),
                    result_schema=copy.deepcopy(structure["declared_result_schema"]), native_input_schemas={},
                    structure_admission=structure, dependency_closure=dict(status="MISSING", scope_covered=False,
                        unresolved_targets=[dict(reason="unproved generic body")]),
                    operations=dict(export_entry=dict(status="UNKNOWN", formal_proof=False)))
        evidence = dict(installation_evidence(inspector, candidate),
                        **entry_evidence(inspector, candidate, unit, entry, checked))
        unit["typed_evidence"] = evidence
        unit["typed_contract"] = build_contract(unit)
        return root, inspector, entry, candidate, checked, unit

    def current(self, inspector, candidate, unit):
        return binding_evidence(inspector, candidate, unit["typed_evidence"])

    def test_actual_structural_replay_supports_material_only(self):
        _, inspector, _, candidate, _, unit = self.fixture()
        evidence = self.current(inspector, candidate, unit)
        self.assertFalse(evidence["entry_checked"])
        self.assertTrue(evidence["structural_entry_checked"])
        self.assertEqual(evidence["structure_digest"], unit["structure_admission"]["plan_digest"])
        self.assertEqual(evidence, unit["typed_evidence"])
        self.assertTrue(verify_contract(unit, evidence=evidence)["valid"])
        self.assertTrue(operation_status(unit, "materialize_guarded_entry", evidence=evidence)["allowed"])
        for operation in ("invoke", "export_entry", "dispatch", "remote", "prepare", "prewarm", "scale_out", "scale_in"):
            with self.subTest(operation=operation):
                answer = operation_status(unit, operation, evidence=evidence)
                self.assertFalse(answer["allowed"])
                self.assertIn("same-call", answer["reason"])
        self.assertEqual(unit["dependency_closure"]["status"], "MISSING")
        self.assertTrue(all(value is False for value in unit["structure_admission"]["permissions"].values()))

    def test_serialized_unit_without_current_binding_replay_grants_nothing(self):
        _, _, _, _, _, unit = self.fixture()
        self.assertFalse(verify_contract(unit)["valid"])
        self.assertFalse(operation_status(unit, "materialize_guarded_entry")["allowed"])

    def test_original_mapping_drift_is_not_entry_checked(self):
        _, inspector, entry, candidate, checked, unit = self.fixture()
        for field, value in (("input_schema", {"type": "object"}), ("parameters", {}),
                             ("result_schema", {"type": "string"}), ("boundary_ids", []),
                             ("symbol", "unknown"), ("kind", "method"), ("status", "READY"),
                             ("effective_binding", "classmethod"), ("fixed_value_bindings", [{"value": "unsafe"}])):
            with self.subTest(field=field):
                changed = copy.deepcopy(unit)
                changed[field] = value
                replay = entry_evidence(inspector, candidate, changed, entry, checked)
                self.assertFalse(replay["structural_entry_checked"])
                self.assertFalse(replay["entry_checked"])

    def test_forged_H_and_permission_flags_fail_independent_source_check(self):
        _, inspector, entry, candidate, checked, unit = self.fixture()
        for change in (lambda plan: plan["permissions"].update(semantic_export=True),
                       lambda plan: plan["prefix_plan"].update(modules_order=[]),
                       lambda plan: plan.update(declared_result_schema={"type": "string"})):
            changed = copy.deepcopy(unit)
            change(changed["structure_admission"])
            replay = entry_evidence(inspector, candidate, changed, entry, checked)
            self.assertFalse(replay["structural_entry_checked"])
            self.assertFalse(replay["entry_checked"])
            # Resealing typed decisions cannot replace the current source replay.
            changed["typed_contract"] = build_contract(changed)
            self.assertFalse(verify_contract(changed, evidence=self.current(inspector, candidate, changed))["valid"])

    def test_source_snapshot_and_candidate_change_require_recheck(self):
        _, inspector, _, candidate, _, unit = self.fixture()
        changed = copy.deepcopy(candidate)
        changed["entries"][0]["symbol"] = "unknown"
        self.assertFalse(verify_contract(unit, evidence=self.current(inspector, changed, unit))["valid"])
        record = inspector.files["original.py"]
        (inspector.snapshots / record["sha256"]).write_text(SOURCE.replace("return 7", "return 8"), encoding="utf-8")
        fresh = self.current(inspector, candidate, unit)
        self.assertFalse(fresh["structural_entry_checked"])
        self.assertFalse(verify_contract(unit, evidence=fresh)["valid"])

    def test_new_structure_cannot_enter_old_bound_through_host_or_closure_flags(self):
        _, inspector, _, candidate, _, unit = self.fixture()
        unit.update(status="STRUCTURE_ONLY", host_condition=dict(status="ENVIRONMENT_DEFERRED"))
        unit["dependency_closure"] = dict(status="CHECKED", scope_covered=True,
                                           unresolved_targets=[], missing_dependency_ids=[])
        unit["operations"]["export_entry"]["status"] = "CONDITIONAL_BOUND"
        unit["typed_contract"] = build_contract(unit)
        evidence = self.current(inspector, candidate, unit)
        self.assertTrue(operation_status(unit, "materialize_guarded_entry", evidence=evidence)["allowed"])
        self.assertFalse(operation_status(unit, "export_entry", evidence=evidence)["allowed"])
        self.assertFalse(operation_status(unit, "invoke", evidence=evidence)["allowed"])


if __name__ == "__main__":
    unittest.main()
