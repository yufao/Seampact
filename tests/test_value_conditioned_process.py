"""Actual normal Worker objects expose partial scope facts, never startup promises."""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from seampact.hosting.deployment import worker_spec
from seampact.source.inspection import Inspector
from seampact.source.value_conditioned_process import (begin_worker_scope, inspect_worker_scope,
                                                        verify_scope, forget_scope, _loader, bind_worker_birth,
                                                        _original_function)
from seampact.store import Store
from seampact.units import UnitCatalog
from seampact.worker import Worker


class ValueConditionedProcessTests(unittest.TestCase):
    def test_core_compilation_cache_still_reads_same_path_changed_source_bytes(self):
        from seampact.source.value_conditioned_process import _original_function
        data = Path(sys.modules[Worker.__module__].__file__).read_bytes()
        changed = data.replace(b'generation=self.generation,', b'generatioX=self.generation,')
        self.assertNotEqual(data, changed)
        self.assertEqual(len(data), len(changed))
        _original_function(Worker.identity)
        with patch.object(Path, 'read_bytes', return_value=changed):
            with self.assertRaises(ValueError):
                _original_function(Worker.identity)

    def normal_worker(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        source = root / 'source'
        source.mkdir()
        (source / 'original.py').write_text('def unknown(value):\n return eval(value)\n'
            'def score(value: str) -> int:\n if value == "safe":\n  return 7\n return unknown(value)\n', encoding='utf-8')
        inspector = Inspector.freeze(source, root / 'snapshots', 'request', ['original.py'], 'score', 'result')
        inspector.request['source_id'] = 'source'
        candidate = dict(entries=[dict(id='score', kind='function', path='original.py', symbol='score',
                                      boundary_ids=['whole'])],
                         boundaries=[dict(id='whole', mode='W', path='original.py', symbol='score')], runtime_groups=[])
        store = Store(':memory:')
        self.addCleanup(store.close)
        catalog = UnitCatalog(root, store, None)
        unit, = catalog.install(inspector, candidate, 'analysis', 'installation')
        self.assertEqual(unit['status'], 'STRUCTURE_ONLY', unit.get('reason'))
        group = store.get('runtime_group', unit['runtime_group'])
        spec = worker_spec(catalog.structural[unit['id']][0], group, unit)
        spec_path = root / 'worker.json'
        spec_path.write_text(json.dumps(spec), encoding='utf-8')
        # Only the transport response is supplied; the real Worker constructor
        # rechecks the source, original catalog, ports, grouping and Core. No
        # endpoint/server is started, and no source prefix or entry executes.
        with patch('seampact.worker.json_request', return_value={'session': 'fixture-session'}):
            worker = Worker(spec_path, 'http://unused.invalid', 'a' * 32, 1)
        self.addCleanup(worker.store.close)
        return worker, unit['id']

    def test_normal_worker_has_actual_roots_ports_and_core_but_no_startup_attestation(self):
        worker, unit_id = self.normal_worker()
        result = inspect_worker_scope(worker, unit_id)
        self.assertEqual(result['root_status'], 'CLOSED_SOURCE_ROOTS_CHECKED', result)
        self.assertEqual(result['port_status'], 'PRIMITIVE_PORTS_CHECKED', result)
        self.assertEqual(result['core_status'], 'ACTUAL_CORE_CHAIN_CHECKED', result)
        self.assertEqual(result['startup_status'], 'UNKNOWN')
        self.assertEqual(result['process_status'], 'UNKNOWN')
        self.assertEqual(result['no_writer'], 'UNKNOWN')
        self.assertIn('NORMAL_MANAGED_BASELINE_BIRTH_QUERY', result['residual'])
        self.assertTrue(all(value is False for value in result['permissions'].values()))
        binding = worker.catalog.structural[unit_id][0]
        self.assertEqual(binding.modules, {})
        self.assertEqual(binding.entries, {})

    def test_exact_normal_worker_cannot_get_a_posthoc_birth_token(self):
        worker, unit_id = self.normal_worker()
        with self.assertRaisesRegex(ValueError, 'normal Worker constructor'):
            begin_worker_scope(worker, unit_id)
        result = verify_scope({'approved': True}, worker.catalog.structural[unit_id][0],
                              worker.store.get('unit', unit_id), {}, worker.identity())
        self.assertEqual(result['process_status'], 'UNKNOWN')
        self.assertIn('token is absent', result['reason'])
        self.assertFalse(forget_scope({'approved': True}))

    def test_fabricated_and_uninitialized_workers_do_not_create_actual_scope(self):
        for worker in (SimpleNamespace(approved=True, no_writer=True), Worker.__new__(Worker)):
            result = inspect_worker_scope(worker, 'unit')
            self.assertEqual(result['root_status'], 'UNKNOWN')
            self.assertTrue(all(value is False for value in result['permissions'].values()))

    def test_extra_root_and_changed_ports_fail_actual_scope(self):
        worker, unit_id = self.normal_worker()
        worker.units.add('other-root')
        self.assertEqual(inspect_worker_scope(worker, unit_id)['root_status'], 'UNKNOWN')
        worker.units.remove('other-root')
        unit = worker.store.get('unit', unit_id)
        changed = copy.deepcopy(unit)
        changed['result_schema'] = {'x-native-type': {'module': 'builtins', 'symbol': 'object'}}
        worker.store.save('unit', unit_id, changed)
        self.assertEqual(inspect_worker_scope(worker, unit_id)['port_status'], 'UNKNOWN')

    def test_core_shadow_is_rejected_without_invoking_it(self):
        worker, unit_id = self.normal_worker()
        called = []
        worker.catalog.invoke = lambda *args: called.append(True)
        result = inspect_worker_scope(worker, unit_id)
        self.assertEqual(result['core_status'], 'UNKNOWN')
        self.assertIn('instance shadow', result['reason'])
        self.assertEqual(called, [])

    def test_same_lock_normal_context_projection_is_checked_without_importing_source(self):
        worker, unit_id = self.normal_worker()
        binding = worker.catalog.structural[unit_id][0]
        basis = dict(base_meta=tuple(sys.meta_path), base_path=tuple(sys.path))
        with binding.lock, binding._context():
            _loader(binding, basis)
            sys.path.append('unproved-additional-source-search-path')
            try:
                with self.assertRaisesRegex(ValueError, 'projection'):
                    _loader(binding, basis)
            finally:
                sys.path.pop()
        self.assertEqual(binding.modules, {})
        self.assertEqual(binding.entries, {})

    def test_code_constants_are_compared_without_custom_equality(self):
        touched = []
        class Unknown:
            def __eq__(self, other):
                touched.append(True)
                return True
        original = Worker._check_guarded_worker
        code = original.__code__
        try:
            original.__code__ = code.replace(co_consts=code.co_consts + (Unknown(),))
            with self.assertRaises(ValueError):
                _original_function(original)
        finally:
            original.__code__ = code
        self.assertEqual(touched, [])

    def test_actual_internal_birth_query_closes_process_scope_but_grants_no_call(self):
        import json
        import test_value_conditioned_birth as fixture_module
        fixture = fixture_module.ValueConditionedBirthTests('test_original_start_command_and_registration_bind_an_internal_birth')
        self.addCleanup(fixture.doCleanups)
        data = fixture.fixture()
        owner, record, _, _, _ = fixture.start(data)
        unit = data[1]
        path = Path(owner.platform.store.get('deployment', data[2]['id'])['folder']) / 'worker.json'
        with patch('seampact.worker.json_request', return_value={'session': owner.session}):
            worker = Worker(path, 'http://coordinator.invalid', record['id'], record['generation'])
        self.addCleanup(worker.store.close)
        endpoint = 'http://seampact-' + record['name'] + ':8890'
        with patch('seampact.hosting.deployment.json_request', return_value=worker.health()):
            owner.attach(endpoint)
        queries = []
        class Reply:
            def __init__(self, result): self.result = result
            def __enter__(self): return self
            def __exit__(self, *args): return None
            def read(self): return json.dumps(self.result).encode('utf-8')
        def transport(request, **kwargs):
            queries.append(request.full_url)
            identity = json.loads(request.data)
            return Reply(owner.managed_birth(identity))
        token = worker._guarded_scopes[unit['id']]
        with patch('seampact.hosting.deployment.urlopen', side_effect=transport):
            bound = bind_worker_birth(token)
        self.assertEqual(bound['status'], 'MANAGED_BIRTH_BOUND', bound)
        self.assertEqual(queries, ['http://coordinator.invalid/api/runtime/birth'])
        binding = worker.catalog.structural[unit['id']][0]
        current = worker.store.get('unit', unit['id'])
        from seampact.units import _native_context_lock
        with binding.lock, _native_context_lock:
            proof = verify_scope(token, binding, current, current['structure_admission'], worker.identity())
        self.assertEqual(proof['process_status'], 'PROCESS_SCOPE_CHECKED', proof)
        self.assertEqual(proof['no_writer'], 'CHECKED')
        self.assertIn('CURRENT_RESOURCE_AND_PERMIT', proof['residual'])
        self.assertTrue(all(value is False for value in proof['permissions'].values()))
        self.assertEqual(worker.units, set())
        self.assertEqual(worker.guarded_units, {unit['id']})
        self.assertEqual(binding.modules, {})


if __name__ == '__main__':
    unittest.main()
