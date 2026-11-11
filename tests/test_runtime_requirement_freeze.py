"""Conditional source plans retain targets discovered only after value binding."""
import copy
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from seampact.source.call_value_conditions import GAP, compile_condition, runtime_requirements
from seampact.source.sdk_value_summaries import RULE as SDK_RULE, GAP as SDK_GAP


class RuntimeRequirementFreezeTests(unittest.TestCase):
    def setUp(self):
        self.inspector = SimpleNamespace(root=Path('.'), snapshots=Path('.'), files={},
                                        request={'source_version': 'frozen-source', 'files': []})
        self.entry = {'id': 'entry', 'kind': 'function'}
        self.candidate = {'entries': [self.entry]}
        self.requirement = dict(schema=SDK_RULE, target_status='REQUIRES_NORMAL_WORKER_CHECK',
                                execution_permission=False, source_call={'path': 'entry.py', 'line': 3})
        self.generic = self.trace([{'reason': GAP}])
        self.guarded = self.trace([{'reason': SDK_GAP}], [dict(rule=SDK_RULE,
                                      target_requirements=self.requirement)])
        self.prefix = self.trace([])

    def trace(self, gaps, protocols=()):
        return dict(status='CHECKED', unresolved_targets=[], negative_scope_gaps=gaps,
                    negative_scope_status='UNKNOWN' if gaps else 'CHECKED',
                    external_protocols=list(protocols), activation_prefix={'status': 'CHECKED'})

    def compile(self, generic=None, guarded=None, prefix=None):
        with patch('seampact.source.dependency_closure.resolve_resource_calls', side_effect=[
                generic or self.generic, guarded or self.guarded, prefix or self.prefix]):
            return compile_condition(self.inspector, self.entry, self.candidate)

    def test_guarded_only_sdk_target_is_frozen_without_execution_permission(self):
        result = self.compile()
        self.assertEqual(result['status'], 'REQUIRES_ACTUAL_CALL')
        self.assertEqual(result['runtime_protocol_requirements'], [self.requirement])
        self.assertIs(result['runtime_protocol_requirements'][0]['execution_permission'], False)
        self.assertIn('ACTUAL_ORIGINAL_SDK_MODEL_AND_HELPER_TARGET_CONDITIONS', result['required'])

    def test_unknown_non_sdk_effect_is_not_discharged_by_an_sdk_target(self):
        guarded = copy.deepcopy(self.guarded)
        guarded['negative_scope_gaps'].append({'reason': 'unknown callback may change source state'})
        self.assertIsNone(self.compile(guarded=guarded))

    def test_permission_flag_or_wrong_schema_cannot_supply_a_runtime_requirement(self):
        for name, value in [('execution_permission', True), ('schema', 'candidate-permission')]:
            guarded = copy.deepcopy(self.guarded)
            guarded['external_protocols'][0]['target_requirements'][name] = value
            with self.subTest(name=name):
                self.assertIsNone(self.compile(guarded=guarded))

    def test_prefix_runtime_requirements_do_not_authorize_initialization(self):
        prefix = self.trace([], self.guarded['external_protocols'])
        self.assertEqual(self.compile(prefix=prefix)['initialization']['status'], 'UNKNOWN')

    def test_frozen_requirements_do_not_share_mutable_metadata(self):
        result = runtime_requirements(self.guarded)
        result[0]['source_call']['line'] = 99
        self.assertEqual(self.requirement['source_call']['line'], 3)


if __name__ == '__main__':
    unittest.main()
