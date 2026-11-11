"""Structure-only value-conditioned plans never execute the original source."""
import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from seampact.source.call_value_conditions import Context, contextual
from seampact.source.dependency_closure import resolve_resource_calls
from seampact.source.value_conditioned_admission import compile_structure_plan, verify_structure_plan
import test_call_value_conditions as fixture_module
import test_activation_prefix as prefix_fixture


GOOD = ('def unknown(value):\n    return eval(value)\n'
        'def score(value: str) -> int:\n    if value == "safe":\n        return 7\n'
        '    return unknown(value)\n')


class ValueConditionedStructureTests(unittest.TestCase):
    def fixture(self, source=GOOD):
        fixture = fixture_module.CallValueConditionTests('test_generic_unknown_is_preserved_and_actual_values_are_separate')
        self.addCleanup(fixture.doCleanups)
        return fixture.fixture(source)

    def test_checked_structure_retains_unknown_body_and_grants_no_operation(self):
        _, inspector, entry, candidate = self.fixture()
        plan = compile_structure_plan(inspector, entry, candidate)
        self.assertEqual(plan['status'], 'STRUCTURE_CHECKED', plan.get('reason'))
        self.assertEqual(plan['body_obligation'], 'UNKNOWN')
        self.assertEqual(plan['result_obligation'], 'REQUIRES_SAME_CALL_COMPATIBILITY')
        self.assertEqual(plan['interface'], dict(type='object', properties={'value': {'type': 'string'}},
                                                 required=['value'], additionalProperties=False))
        self.assertEqual(plan['declared_result_schema'], {'type': 'integer'})
        self.assertTrue(all(value is False for value in plan['permissions'].values()))
        self.assertFalse(plan['whole_w']['runtime_admitted'])
        self.assertEqual(plan['prefix_plan']['status'], 'CHECKED')
        self.assertTrue(verify_structure_plan(inspector, entry, candidate, plan))
        safe = resolve_resource_calls(contextual(inspector, Context(values={'value': 'safe'})), entry, candidate)
        unsafe = resolve_resource_calls(contextual(inspector, Context(values={'value': 'unsafe'})), entry, candidate)
        self.assertEqual(safe['status'], 'CHECKED')
        self.assertNotEqual(unsafe['status'], 'CHECKED')

    def test_complete_parent_prefix_is_covered(self):
        fixture = prefix_fixture.ActivationPrefixTests('test_parent_package_runs_before_child_and_is_not_repeated')
        self.addCleanup(fixture.doCleanups)
        inspector, entry, candidate = fixture.fixture(
            {'pkg/__init__.py': 'FLAG="ready"\n', 'pkg/child.py': GOOD}, 'pkg/child.py')
        entry['boundary_ids'] = ['whole']
        candidate['boundaries'] = [dict(id='whole', mode='W', path=entry['path'], symbol=entry['symbol'])]
        plan = compile_structure_plan(inspector, entry, candidate)
        self.assertEqual(plan['status'], 'STRUCTURE_CHECKED', plan.get('reason'))
        self.assertEqual(plan['prefix_plan']['modules_order'], ['pkg/__init__.py', 'pkg/child.py'])

    def test_original_immutable_defaults_remain_candidates_for_worker_recheck(self):
        source = 'def score(value: str="safe", *, count: int=2) -> int:\n return count\n'
        _, inspector, entry, candidate = self.fixture(source)
        plan = compile_structure_plan(inspector, entry, candidate)
        self.assertEqual(plan['status'], 'STRUCTURE_CHECKED', plan.get('reason'))
        self.assertEqual(plan['original_defaults'], {'value': 'safe', 'count': 2})
        self.assertEqual(plan['interface']['required'], [])
        self.assertIn('ACTUAL_ORIGINAL_CALLABLE_AND_DEFAULTS', plan['worker_predicates'])
        self.assertIn('FULL_DOMAIN_BODY_AND_DEPENDENCY_CLOSURE', plan['unresolved'])
        self.assertFalse(plan['permissions']['invoke'])

    def test_unknown_definition_and_prefix_effects_are_rejected(self):
        cases = {
            'prefix_eval': 'eval("1")\n' + GOOD,
            'other_decorator': '@staticmethod\ndef helper():\n return 1\n' + GOOD,
            'entry_decorator': '@staticmethod\ndef score(value: str) -> int:\n return 7\n',
            'other_dynamic_default': 'def helper(x=eval("1")):\n return x\n' + GOOD,
            'entry_dynamic_default': 'def score(value: str=eval("1")) -> int:\n return 7\n',
            'unknown_import': 'import an_unavailable_example_sdk\n' + GOOD,
            'builtin_alias': 'str=1\n' + GOOD,
            'builtin_namespace': '__builtins__=1\n' + GOOD,
            'entry_rebound': GOOD + 'score=1\n',
            'async_entry': 'async def score(value: str) -> int:\n return 7\n',
            'generator_entry': 'def score(value: str) -> int:\n yield 7\n',
            'untyped_input': 'def score(value) -> int:\n return 7\n',
            'untyped_result': 'def score(value: str):\n return 7\n',
            'native_input': 'def score(value: object) -> int:\n return 7\n',
            'deferred_annotation': 'from __future__ import annotations\n' + GOOD,
        }
        for name, source in cases.items():
            with self.subTest(name=name):
                _, inspector, entry, candidate = self.fixture(source)
                plan = compile_structure_plan(inspector, entry, candidate)
                self.assertEqual(plan['status'], 'UNKNOWN', plan)
                self.assertFalse(verify_structure_plan(inspector, entry, candidate, plan))
                self.assertTrue(all(value is False for value in plan['permissions'].values()))

    def test_mismatched_w_dependency_or_injected_binding_is_rejected(self):
        for case in ('w_mode', 'w_symbol', 'w_extra', 'native_wiring', 'dependency', 'fixed', 'constructor', 'service'):
            with self.subTest(case=case):
                _, inspector, entry, candidate = self.fixture()
                if case == 'w_mode': candidate['boundaries'][0]['mode'] = 'H'
                if case == 'w_symbol': candidate['boundaries'][0]['symbol'] = 'unknown'
                if case == 'w_extra': candidate['boundaries'].append(dict(id='second', mode='W', path=entry['path'], symbol=entry['symbol']))
                if case == 'native_wiring': candidate['boundaries'][0]['native_wiring'] = {}
                if case == 'dependency': candidate['dependencies'] = [dict(id='sdk')]
                if case == 'fixed': entry['fixed_arguments'] = {'value': 'safe'}
                if case == 'constructor': entry['constructor'] = {'symbol': 'Other'}
                if case == 'service': candidate['services'] = [{'id': 'external'}]
                plan = compile_structure_plan(inspector, entry, candidate)
                self.assertEqual(plan['status'], 'UNKNOWN', plan)

    def test_entry_cannot_replace_an_annotation_builtin(self):
        _, inspector, entry, candidate = self.fixture('def str(value: str) -> int:\n return 7\n')
        entry['symbol'] = 'str'
        candidate['boundaries'][0]['symbol'] = 'str'
        plan = compile_structure_plan(inspector, entry, candidate)
        self.assertEqual(plan['status'], 'UNKNOWN', plan)

    def test_verification_replays_source_candidate_and_certificate(self):
        _, inspector, entry, candidate = self.fixture()
        plan = compile_structure_plan(inspector, entry, candidate)
        self.assertTrue(verify_structure_plan(inspector, entry, candidate, plan))
        forged = copy.deepcopy(plan)
        forged['permissions']['invoke'] = True
        self.assertFalse(verify_structure_plan(inspector, entry, candidate, forged))
        forged = copy.deepcopy(plan)
        forged['checker_identity']['structure_sha256'] = '0' * 64
        self.assertFalse(verify_structure_plan(inspector, entry, candidate, forged))
        changed = copy.deepcopy(candidate)
        changed['dependencies'] = [dict(id='new')]
        self.assertFalse(verify_structure_plan(inspector, entry, changed, plan))
        snapshot = inspector.snapshots / inspector.files[entry['path']]['sha256']
        data = snapshot.read_bytes()
        snapshot.write_bytes(data + b'\n# changed\n')
        self.assertFalse(verify_structure_plan(inspector, entry, candidate, plan))


if __name__ == '__main__':
    unittest.main()
