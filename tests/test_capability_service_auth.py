"""Explicit no-auth survives resource selection and worker resolution."""
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from seampact.capability_contract import ContractError, check_binding
from seampact.capability_generation import resolve_resources
from seampact.capability_worker import CapabilityWorker, WorkerError


class ServiceAuthentication(unittest.TestCase):
    def test_explicit_auth_contract_and_legacy_unknown(self):
        resource = dict(id='checker', kind='service')
        base = dict(form='service', base_url='http://local.invalid/v1')
        check_binding(resource, base)
        check_binding(resource, dict(base, auth_mode='none'))
        check_binding(resource, dict(base, auth_mode='required', api_key_env='CHECKER_KEY'))
        for bad in [dict(base, auth_mode='none', api_key_env='CHECKER_KEY'),
                    dict(base, auth_mode='required'), dict(base, auth_mode='unknown'), dict(base, auth_mode={})]:
            with self.subTest(binding=bad), self.assertRaises(ContractError):
                check_binding(resource, bad)

    def test_worker_no_auth_unknown_and_missing_required_key_remain_distinct(self):
        base = dict(form='service', base_url='http://local.invalid/v1', model='local-guard')
        for declaration, expected in [(dict(base, auth_mode='none'), 'none'), (base, None)]:
            worker = SimpleNamespace(bindings=dict(checker=declaration))
            resolved = CapabilityWorker._resolve_bindings(worker)['checker']
            self.assertEqual(resolved['auth_mode'], expected)
            self.assertEqual(resolved['api_key'], '')
        worker = SimpleNamespace(bindings=dict(checker=dict(base, auth_mode='required', api_key_env='CHECKER_KEY')))
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(WorkerError):
            CapabilityWorker._resolve_bindings(worker)
        with patch.dict(os.environ, {'CHECKER_KEY': 'fixture-only'}, clear=True):
            resolved = CapabilityWorker._resolve_bindings(worker)['checker']
        self.assertEqual(resolved['auth_mode'], 'required')
        self.assertEqual(resolved['api_key'], 'fixture-only')

    def test_resource_selection_records_explicit_auth_mode(self):
        manifest = dict(resources=[dict(id='checker', kind='service', required=True, description='Selected checker')])
        inspector = SimpleNamespace(request=dict(configuration=dict(resource_choices=dict(checker='selected'))))
        selections = dict(checker=dict(configuration_key='checker', operation='text', parameters={}))
        for mode, key, expected in [('none', '', 'none'), ('saved', 'fixture-only', 'required')]:
            profile = dict(id='selected', model='local-guard', base_url='http://local.invalid/v1',
                           protocol='openai-compatible', service_capabilities=['text'], parameters={}, auth=dict(mode=mode))
            platform = SimpleNamespace(profiles=SimpleNamespace(connection_profile=lambda identity: profile,
                                                                  connection_key=lambda identity: key))
            with patch('seampact.capability_generation._generic_text_llm_slot', return_value=True):
                bindings, gaps, _ = resolve_resources(platform, manifest, selections, inspector)
            self.assertEqual(gaps, [])
            self.assertEqual(bindings['checker']['auth_mode'], expected)
            self.assertEqual(bool(bindings['checker'].get('api_key_env')), bool(key))


if __name__ == '__main__':
    unittest.main()
