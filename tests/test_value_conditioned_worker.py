"""Partial worker target evidence from a prospective original import."""
import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from seampact.source.value_conditioned_admission import compile_structure_plan
from seampact.source.value_conditioned_calls import check_call_source
from seampact.source.value_conditioned_worker import (begin_capture, finish_capture, forget_capture,
                                                       check_worker_targets)
from seampact.units import NativeBindings, _native_context_lock
import test_call_value_conditions as fixture_module
import test_activation_prefix as prefix_fixture


IDENTITY = dict(session='s', instance_id='i', generation=1, process_nonce='n')
SOURCE = ('LIMIT=2\ndef unknown(value):\n    return eval(value)\n'
          'def score(value: str="safe") -> int:\n    if value == "safe":\n'
          '        return 7\n    return unknown(value)\n')


class ValueConditionedWorkerTests(unittest.TestCase):
    def fixture(self, source=SOURCE):
        fixture = fixture_module.CallValueConditionTests('test_generic_unknown_is_preserved_and_actual_values_are_separate')
        self.addCleanup(fixture.doCleanups)
        _, inspector, entry, candidate = fixture.fixture(source)
        return inspector, entry, candidate

    def occurrence(self, inspector, entry, candidate, arguments):
        structure = compile_structure_plan(inspector, entry, candidate)
        self.assertEqual(structure['status'], 'STRUCTURE_CHECKED', structure.get('reason'))
        proof = check_call_source(inspector, entry, candidate, structure, arguments)
        self.assertEqual(proof['status'], 'CONTROLLER_SOURCE_CHECKED', proof.get('reason'))
        binding = NativeBindings(inspector, candidate, None)
        self.addCleanup(binding.modules.clear)
        unit = {'id': 'unit'}
        with binding.lock, _native_context_lock:
            token = begin_capture(binding, entry, unit, structure, IDENTITY)
            self.addCleanup(forget_capture, token)
            with binding._context():
                namespace = binding._namespace(entry)
                receipt = binding._capture_import_only_target(entry, namespace)
                receipt['helpers'] = binding._capture_imported_helpers(structure['prefix_plan'])
                receipt.update(plan_digest=structure['prefix_plan']['plan_digest'], instance_identity=dict(IDENTITY))
                binding.import_only_entries[unit['id']] = receipt
                finish_capture(token, receipt)
        return binding, unit, structure, proof, receipt

    @staticmethod
    def check(binding, entry, unit, structure, proof, arguments, identity=IDENTITY):
        with binding.lock, binding._context():
            return check_worker_targets(binding, entry, unit, structure, proof, arguments, identity)

    def test_actual_targets_checked_but_full_worker_and_permissions_remain_unknown(self):
        inspector, entry, candidate = self.fixture()
        args = {'value': 'safe'}
        binding, unit, structure, proof, _ = self.occurrence(inspector, entry, candidate, args)
        without_lock = check_worker_targets(binding, entry, unit, structure, proof, args, IDENTITY)
        self.assertEqual(without_lock['target_status'], 'UNKNOWN')
        result = self.check(binding, entry, unit, structure, proof, args)
        self.assertEqual(result['target_status'], 'WORKER_TARGETS_CHECKED', result)
        self.assertEqual(result['status'], 'UNKNOWN')
        self.assertEqual(result['worker_proof'], 'UNKNOWN')
        self.assertEqual(result['no_writer'], 'UNKNOWN')
        self.assertTrue(all(value is False for value in result['permissions'].values()))

    def test_changed_entry_helper_globals_defaults_identity_and_payload_are_rejected(self):
        for field in ('global', 'missing_none_global', 'entry_default', 'helper_default', 'annotation',
                      'entry_code', 'helper_binding', 'extra_binding', 'identity', 'payload', 'receipt',
                      'owner_table', 'verification_hook'):
            with self.subTest(field=field):
                source = 'FLAG=None\n' + SOURCE.replace('def unknown(value):', 'def unknown(value, option=2):')
                inspector, entry, candidate = self.fixture(source)
                args = {'value': 'safe'}
                binding, unit, structure, proof, receipt = self.occurrence(inspector, entry, candidate, args)
                namespace = receipt['namespace']
                if field == 'global': namespace['LIMIT'] = 3
                if field == 'missing_none_global': del namespace['FLAG']
                if field == 'entry_default': namespace['score'].__defaults__ = ('unsafe',)
                if field == 'helper_default': namespace['unknown'].__defaults__ = (3,)
                if field == 'annotation': namespace['score'].__annotations__['value'] = int
                if field == 'entry_code':
                    original = namespace['score']
                    original.__code__ = original.__code__.replace(co_consts=original.__code__.co_consts + (999,))
                if field == 'helper_binding': namespace['unknown'] = lambda value: 1
                if field == 'extra_binding': namespace['new_name'] = 1
                if field == 'receipt': binding.import_only_entries[unit['id']] = copy.copy(receipt)
                if field == 'owner_table': binding.modules['original'] = None
                if field == 'verification_hook': binding._verify_import_only_target = lambda *args: (_ for _ in ()).throw(AssertionError('hook ran'))
                identity = dict(IDENTITY, generation=2) if field == 'identity' else IDENTITY
                actual_args = {'value': 'unsafe'} if field == 'payload' else args
                result = self.check(binding, entry, unit, structure, proof, actual_args, identity)
                self.assertEqual(result['target_status'], 'UNKNOWN', result)
                self.assertTrue(all(value is False for value in result['permissions'].values()))

    def test_a_warm_or_posthoc_receipt_cannot_start_a_prospective_occurrence(self):
        inspector, entry, candidate = self.fixture()
        args = {'value': 'safe'}
        binding, unit, structure, proof, _ = self.occurrence(inspector, entry, candidate, args)
        with binding.lock, _native_context_lock:
            with self.assertRaises(ValueError):
                begin_capture(binding, entry, unit, structure, IDENTITY)

    def test_receipt_captured_only_after_import_has_no_prospective_occurrence(self):
        inspector, entry, candidate = self.fixture()
        args = {'value': 'safe'}
        structure = compile_structure_plan(inspector, entry, candidate)
        proof = check_call_source(inspector, entry, candidate, structure, args)
        binding = NativeBindings(inspector, candidate, None)
        self.addCleanup(binding.modules.clear)
        unit = {'id': 'posthoc'}
        with binding.lock, binding._context():
            namespace = binding._namespace(entry)
            receipt = binding._capture_import_only_target(entry, namespace)
            receipt['helpers'] = binding._capture_imported_helpers(structure['prefix_plan'])
            receipt.update(unit_id=unit['id'], plan_digest=structure['prefix_plan']['plan_digest'],
                           instance_identity=dict(IDENTITY))
            binding.import_only_entries[unit['id']] = receipt
            result = check_worker_targets(binding, entry, unit, structure, proof, args, IDENTITY)
        self.assertEqual(result['target_status'], 'UNKNOWN')

    def test_modified_function_recaptured_during_prefix_is_not_source_identity(self):
        inspector, entry, candidate = self.fixture()
        structure = compile_structure_plan(inspector, entry, candidate)
        binding = NativeBindings(inspector, candidate, None)
        self.addCleanup(binding.modules.clear)
        unit = {'id': 'recaptured'}
        with binding.lock, _native_context_lock:
            token = begin_capture(binding, entry, unit, structure, IDENTITY)
            with binding._context():
                namespace = binding._namespace(entry)
                original = namespace['score']
                original.__code__ = original.__code__.replace(co_consts=original.__code__.co_consts + (999,))
                receipt = binding._capture_import_only_target(entry, namespace)
                receipt['helpers'] = binding._capture_imported_helpers(structure['prefix_plan'])
                receipt.update(plan_digest=structure['prefix_plan']['plan_digest'], instance_identity=dict(IDENTITY))
                binding.import_only_entries[unit['id']] = receipt
                with self.assertRaises(ValueError):
                    finish_capture(token, receipt)

    def test_original_source_alias_is_checked_against_actual_imported_object(self):
        fixture = prefix_fixture.ActivationPrefixTests('test_parent_package_runs_before_child_and_is_not_repeated')
        self.addCleanup(fixture.doCleanups)
        inspector, entry, candidate = fixture.fixture({
            'pkg/__init__.py': 'FLAG="ready"\n',
            'pkg/helper.py': 'def helper(value):\n return value\n',
            'pkg/child.py': 'from .helper import helper\ndef score(value: str) -> int:\n return 7\n',
        }, 'pkg/child.py')
        entry['boundary_ids'] = ['whole']
        candidate['boundaries'] = [dict(id='whole', mode='W', path=entry['path'], symbol=entry['symbol'])]
        args = {'value': 'safe'}
        binding, unit, structure, proof, receipt = self.occurrence(inspector, entry, candidate, args)
        result = self.check(binding, entry, unit, structure, proof, args)
        self.assertEqual(result['target_status'], 'WORKER_TARGETS_CHECKED', result)
        receipt['namespace']['helper'] = lambda value: value
        self.assertEqual(self.check(binding, entry, unit, structure, proof, args)['target_status'], 'UNKNOWN')


if __name__ == '__main__':
    unittest.main()
