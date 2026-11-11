"""Exact-call source facts never inherit static or worker execution rights."""
import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from seampact.source.value_conditioned_admission import compile_structure_plan
from seampact.source.value_conditioned_calls import check_call_source, verify_call_source
import test_value_conditioned_admission as fixtures


class ValueConditionedCallTests(unittest.TestCase):
    def test_normal_catalog_exposes_material_and_same_call_facts_without_start_or_invoke(self):
        from seampact.store import Store
        from seampact.units import UnitCatalog
        from seampact.typed_contract import operation_status
        inspector, entry, candidate, _ = self.fixture()
        store = Store(':memory:')
        self.addCleanup(store.close)
        catalog = UnitCatalog(inspector.root, store, None)
        unit = catalog.install(inspector, candidate, 'analysis', 'installation')[0]
        self.assertEqual(unit['status'], 'STRUCTURE_ONLY', unit['reason'])
        self.assertNotIn(unit['id'], catalog.bindings)
        self.assertNotIn(unit['id'], catalog.deferred)
        material = catalog.structure_materialization(unit['id'])
        self.assertFalse(material['semantic_export'])
        safe = catalog.check_structural_call_source(unit['id'], {'value': 'safe'})
        unsafe = catalog.check_structural_call_source(unit['id'], {'value': 'unsafe'})
        self.assertEqual(safe['status'], 'CONTROLLER_SOURCE_CHECKED', safe['reason'])
        self.assertEqual(safe['reason_code'], 'NEEDS_ACTUAL_WORKER_AND_RESOURCE_PROOF')
        self.assertFalse(safe['allowed'])
        self.assertFalse(safe['can_start'])
        normal = catalog.check_call_conditions(unit['id'], {'value': 'safe'})
        self.assertFalse(normal['allowed'])
        self.assertNotEqual(normal['status'], 'NOT_REQUIRED')
        self.assertEqual(catalog.check_call_conditions(unit['id'], {'value': 'unsafe'})['reason_code'],
                         'CONDITION_UNKNOWN')
        self.assertEqual(unsafe['reason_code'], 'CONDITION_UNKNOWN')
        group = store.get('runtime_group', unit.get('runtime_group'))
        binding, _ = catalog.structural[unit['id']]
        evidence = catalog._binding_evidence(binding, unit, full_source_check=True)
        for action in ('invoke', 'export_entry', 'dispatch', 'prepare', 'remote'):
            self.assertFalse(operation_status(unit, action, group=group, evidence=evidence)['allowed'])
        with self.assertRaises(ValueError):
            catalog.invoke(unit['id'], {'value': 'safe'})

    def test_structure_fallback_preserves_prior_entry_identity_rejections(self):
        from seampact.store import Store
        from seampact.units import UnitCatalog
        inspector, entry, candidate, _ = self.fixture()
        entry['id'] = 'bad/entry'
        self.assertEqual(compile_structure_plan(inspector, entry, candidate)['status'], 'UNKNOWN')
        store = Store(':memory:')
        self.addCleanup(store.close)
        catalog = UnitCatalog(inspector.root, store, None)
        unit = catalog.install(inspector, candidate, 'analysis', 'installation')[0]
        self.assertEqual(unit['status'], 'MISSING')
        self.assertFalse(catalog.structural)
        with self.assertRaises(ValueError):
            catalog.structure_materialization(unit['id'])

    def fixture(self, source=fixtures.GOOD):
        case = fixtures.ValueConditionedStructureTests('test_checked_structure_retains_unknown_body_and_grants_no_operation')
        self.addCleanup(case.doCleanups)
        _, inspector, entry, candidate = case.fixture(source)
        return inspector, entry, candidate, compile_structure_plan(inspector, entry, candidate)

    def test_exact_safe_path_is_source_checked_but_unsafe_and_worker_rights_are_not(self):
        inspector, entry, candidate, structure = self.fixture()
        safe = check_call_source(inspector, entry, candidate, structure, {'value': 'safe'})
        self.assertEqual(safe['status'], 'CONTROLLER_SOURCE_CHECKED', safe['reason'])
        self.assertEqual(safe['global_body_status'], 'UNKNOWN')
        self.assertTrue(safe['residual_worker_obligations'])
        self.assertTrue(all(value is False for value in safe['permissions'].values()))
        self.assertTrue(verify_call_source(inspector, entry, candidate, structure, {'value': 'safe'}, safe))
        self.assertFalse(verify_call_source(inspector, entry, candidate, structure, {'value': 'unsafe'}, safe))
        unsafe = check_call_source(inspector, entry, candidate, structure, {'value': 'unsafe'})
        self.assertEqual(unsafe['status'], 'UNKNOWN')
        self.assertTrue(unsafe['source_trace']['unresolved_targets'])

    def test_result_contract_is_per_call_and_incompatible_selected_result_rejects(self):
        inspector, entry, candidate, structure = self.fixture(fixtures.GOOD.replace('return 7', 'return "wrong"'))
        result = check_call_source(inspector, entry, candidate, structure, {'value': 'safe'})
        self.assertEqual(result['status'], 'UNKNOWN')
        self.assertIn('return', result['reason'])

    def test_schema_numeric_union_does_not_change_actual_value_type(self):
        source = 'def score(value: float) -> float:\n    return value\n'
        inspector, entry, candidate, structure = self.fixture(source)
        integer = check_call_source(inspector, entry, candidate, structure, {'value': 1})
        floating = check_call_source(inspector, entry, candidate, structure, {'value': 1.0})
        self.assertEqual(integer['actual_value_kinds'], {'value': 'int'})
        self.assertEqual(floating['actual_value_kinds'], {'value': 'float'})
        self.assertNotEqual(integer['payload_digest'], floating['payload_digest'])
        self.assertEqual(integer['status'], 'CONTROLLER_SOURCE_CHECKED', integer['reason'])
        self.assertEqual(floating['status'], 'CONTROLLER_SOURCE_CHECKED', floating['reason'])
        self.assertEqual(check_call_source(inspector, entry, candidate, structure, {'value': True})['status'], 'UNKNOWN')

    def test_original_default_is_only_a_worker_checked_candidate(self):
        source = 'def score(value: str, mode: str="safe") -> int:\n    if mode == "safe":\n        return 7\n    return eval(value)\n'
        inspector, entry, candidate, structure = self.fixture(source)
        result = check_call_source(inspector, entry, candidate, structure, {'value': 'ordinary'})
        self.assertEqual(result['status'], 'CONTROLLER_SOURCE_CHECKED', result['reason'])
        self.assertEqual(result['omitted'], ['mode'])
        self.assertIn('ORIGINAL_CALLABLE_CODE_DEFAULTS_GLOBALS_AND_BINDING', result['residual_worker_obligations'])
        changed = check_call_source(inspector, entry, candidate, structure, {'value': 'ordinary', 'mode': 'unsafe'})
        self.assertEqual(changed['status'], 'UNKNOWN')

    def test_metadata_flags_or_nonordinary_values_cannot_make_a_source_proof(self):
        inspector, entry, candidate, structure = self.fixture()
        changed = copy.deepcopy(structure)
        changed['permissions']['invoke'] = True
        self.assertEqual(check_call_source(inspector, entry, candidate, changed, {'value': 'safe'})['status'], 'UNKNOWN')
        touched = []
        class Unknown(str):
            def __eq__(self, other): touched.append('eq'); return True
        result = check_call_source(inspector, entry, candidate, structure, {'value': Unknown('safe')})
        self.assertEqual(result['status'], 'UNKNOWN')
        self.assertEqual(touched, [])


if __name__ == '__main__':
    unittest.main()
