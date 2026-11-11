"""Normal guarded worker routes never grant source execution permission."""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from seampact.hosting.deployment import worker_spec
from seampact.source.inspection import Inspector
from seampact.source.value_conditioned_process import forget_scope
from seampact.store import Store
from seampact.units import UnitCatalog
from seampact.worker import Worker
from seampact.runtime import ProtocolError


SOURCE = ('def unknown(value):\n    return eval(value)\n'
          'def score(value: str="safe") -> int:\n'
          '    if value == "safe":\n        return 7\n'
          '    return unknown(value)\n')


class GuardedWorkerRouteTests(unittest.TestCase):
    def normal_worker(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        source = root / 'source'
        source.mkdir()
        (source / 'original.py').write_text(SOURCE, encoding='utf-8')
        inspector = Inspector.freeze(source, root / 'snapshots', 'request', ['original.py'], 'score', 'result')
        inspector.request['source_id'] = 'source'
        candidate = dict(entries=[dict(id='score', kind='function', path='original.py', symbol='score',
                                      boundary_ids=['whole'])],
                         boundaries=[dict(id='whole', mode='W', path='original.py', symbol='score')])
        store = Store(':memory:')
        self.addCleanup(store.close)
        catalog = UnitCatalog(root, store, None)
        unit, = catalog.install(inspector, candidate, 'analysis', 'installation')
        self.assertEqual(unit['status'], 'STRUCTURE_ONLY', unit.get('reason'))
        group = store.get('runtime_group', unit['runtime_group'])
        self.assertEqual(group['host']['status'], 'STRUCTURE_ONLY')
        binding, _ = catalog.structural[unit['id']]
        spec = worker_spec(binding, group, unit)
        spec['runtime_environment'] = dict(python=f'{sys.version_info.major}.{sys.version_info.minor}',
                                           python_requirements=[])
        path = root / 'worker.json'
        path.write_text(json.dumps(spec), encoding='utf-8')
        # Only the coordinator's HTTP response is substituted. The actual
        # Worker, its catalog, proof checks and frozen source remain intact.
        with patch('seampact.worker.json_request', return_value={'session': 'fixture-session'}):
            worker = Worker(path, 'http://unused.invalid', 'a' * 32, 1)
        self.addCleanup(worker.store.close)
        for token in worker._guarded_scopes.values():
            self.addCleanup(forget_scope, token)
        self.addCleanup(worker.catalog.structural[unit['id']][0].modules.clear)
        return worker, unit['id'], inspector

    def test_guarded_health_and_safe_or_unsafe_check_do_not_create_a_ready_root(self):
        worker, unit_id, _ = self.normal_worker()
        binding, _ = worker.catalog.structural[unit_id]
        health = worker.health()
        self.assertEqual(health['units'], [])
        self.assertEqual(health['guarded_units'], [unit_id])
        self.assertEqual(worker.units, set())
        self.assertEqual(worker.guarded_units, {unit_id})
        for arguments, expected in (({'value': 'safe'}, 'CONTROLLER_SOURCE_CHECKED'),
                                    ({}, 'CONTROLLER_SOURCE_CHECKED'),
                                    ({'value': 'unsafe'}, 'UNKNOWN')):
            with self.subTest(arguments=arguments):
                result = worker.check_call(dict(unit=unit_id, arguments=arguments, **worker.identity()))
                self.assertFalse(result['allowed'], result)
                self.assertFalse(result.get('can_start', False))
                if expected == 'CONTROLLER_SOURCE_CHECKED':
                    self.assertEqual(result['source_proof']['status'], expected, result)
                    self.assertEqual(result['process_proof']['process_status'], 'UNKNOWN')
                    self.assertEqual(result['target_proof']['target_status'], 'UNKNOWN')
                else:
                    self.assertEqual(result['status'], expected, result)
        self.assertEqual(binding.modules, {})
        self.assertEqual(binding.entries, {})
        self.assertEqual(binding.import_only_entries, {})

    def test_guarded_busy_is_not_missing_or_a_cold_worker(self):
        worker, unit_id, _ = self.normal_worker()
        binding, _ = worker.catalog.structural[unit_id]
        worker.in_flight = 1
        try:
            result = worker.check_call(dict(unit=unit_id, arguments={'value': 'safe'}, **worker.identity()))
        finally:
            worker.in_flight = 0
        self.assertFalse(result['allowed'])
        self.assertEqual(result['reason_code'], 'WORKER_BUSY')
        self.assertEqual(binding.modules, {})

    def test_forged_worker_membership_and_instance_do_not_run_original_source(self):
        worker, unit_id, _ = self.normal_worker()
        binding, _ = worker.catalog.structural[unit_id]
        identity = worker.identity()
        with self.assertRaises(ProtocolError):
            worker.check_call(dict(unit=unit_id, arguments={'value': 'safe'}, **dict(identity, generation=2)))
        # This is intentionally not a legitimate worker registration. Even a
        # corrupted in-memory membership must hit the catalog's real gates.
        worker.units.add(unit_id)
        try:
            response = worker.invoke(dict(unit=unit_id, arguments={'value': 'safe'}, **identity))
            self.assertTrue('error' in response or 'rejected' in response, response)
            response = worker.import_only(dict(unit=unit_id, plan_digest='not-a-permit', **identity))
            self.assertEqual(response['status'], 'FAILED')
        finally:
            worker.units.remove(unit_id)
        self.assertEqual(binding.modules, {})
        self.assertEqual(binding.entries, {})
        self.assertEqual(binding.import_only_entries, {})

    def test_business_and_preparation_routes_refuse_guarded_member(self):
        worker, unit_id, _ = self.normal_worker()
        binding, _ = worker.catalog.structural[unit_id]
        identity = worker.identity()
        response = worker.invoke(dict(unit=unit_id, arguments={'value': 'safe'}, **identity))
        self.assertIn('rejected', response)
        response = worker.import_only(dict(unit=unit_id, plan_digest='not-a-permit', **identity))
        self.assertEqual(response['status'], 'FAILED')
        response = worker.prewarm(dict(units=[unit_id], **identity))
        self.assertEqual(response['units'][unit_id]['status'], 'NOT_PREPARED')
        self.assertFalse(response['initialization_ready'])
        self.assertEqual(binding.modules, {})
        self.assertEqual(binding.entries, {})
        self.assertEqual(binding.import_only_entries, {})

    def test_forged_member_or_changed_default_and_frozen_source_remain_unknown(self):
        worker, unit_id, inspector = self.normal_worker()
        binding, _ = worker.catalog.structural[unit_id]
        identity = worker.identity()
        forged = worker.check_call(dict(unit='forged-root', arguments={'value': 'safe'}, **identity))
        self.assertFalse(forged['allowed'])
        self.assertEqual(forged['reason_code'], 'MEMBER_MISSING')
        self.assertIn('rejected', worker.invoke(dict(unit='forged-root', arguments={'value': 'safe'}, **identity)))
        self.assertEqual(worker.import_only(dict(unit='forged-root', plan_digest='forged', **identity))['status'], 'FAILED')
        unit = worker.store.get('unit', unit_id)
        changed = copy.deepcopy(unit)
        changed['structure_admission']['original_defaults']['value'] = 'unsafe'
        worker.store.save('unit', unit_id, changed)
        invalid = worker.check_call(dict(unit=unit_id, arguments={}, **identity))
        self.assertFalse(invalid['allowed'], invalid)
        self.assertNotEqual(invalid.get('status'), 'CONTROLLER_SOURCE_CHECKED')
        worker.store.save('unit', unit_id, unit)
        snapshot = inspector.snapshots / inspector.files['original.py']['sha256']
        snapshot.write_text(SOURCE + '\n# changed\n', encoding='utf-8')
        invalid = worker.check_call(dict(unit=unit_id, arguments={'value': 'safe'}, **identity))
        self.assertFalse(invalid['allowed'], invalid)
        self.assertNotEqual(invalid.get('status'), 'CONTROLLER_SOURCE_CHECKED')
        self.assertEqual(binding.modules, {})
        self.assertEqual(binding.entries, {})
        self.assertEqual(binding.import_only_entries, {})


if __name__ == '__main__':
    unittest.main()
