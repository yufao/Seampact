"""Real finite call evidence and the normal catalog/worker guard, no models."""
import copy
import json
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from seampact.source.call_value_conditions import ordinary, CallUnknown, Context, contextual, compile_condition
from seampact.source.dependency_closure import resolve_resource_calls
from seampact.source.inspection import Inspector
from seampact.runtime import Coordinator, OwnerRuntime
from seampact.store import Store
from seampact.units import UnitCatalog
from seampact.worker import Worker


class CallValueConditionTests(unittest.TestCase):
    def fixture(self, source):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root/'original.py').write_text(source, encoding='utf-8')
        inspector = Inspector.freeze(root, root/'snapshots', 'request', ['original.py'], 'score', 'original result')
        inspector.request['source_id'] = 'source'
        entry = dict(id='score', kind='function', path='original.py', symbol='score', boundary_ids=['whole'])
        candidate = dict(entries=[entry], boundaries=[dict(id='whole', mode='W', path='original.py', symbol='score')])
        return root, inspector, entry, candidate

    def catalog(self, source):
        root, inspector, entry, candidate = self.fixture(source)
        store = Store(':memory:')
        self.addCleanup(store.close)
        runtime = OwnerRuntime(Coordinator(store))
        self.addCleanup(runtime.close)
        catalog = UnitCatalog(root, store, runtime)
        units = catalog.install(inspector, candidate, 'analysis', 'installation')
        return root, inspector, candidate, catalog, units[0]

    def test_unknown_objects_and_metaclasses_are_rejected_without_hooks(self):
        touched = []
        class Meta(type):
            def __eq__(cls, other): touched.append('eq'); return False
            def __hash__(cls): touched.append('hash'); return 1
            def __getattribute__(cls, name): touched.append('metaattr'); return super().__getattribute__(name)
        class Unknown(metaclass=Meta):
            @property
            def __class__(self): touched.append('class'); return list
            def __repr__(self): touched.append('repr'); return 'unknown'
        for value in (Unknown(), {'nested':[Unknown()]}):
            with self.assertRaises(CallUnknown): ordinary(value)
        self.assertEqual(touched, [])
        class CustomList(list): pass
        with self.assertRaises(CallUnknown): ordinary(CustomList([1]))
        for value in (b'bytes', (1,), {'$ndarray':{}}, float('nan')):
            with self.assertRaises(CallUnknown): ordinary(value)

    def test_generic_unknown_is_preserved_and_actual_values_are_separate(self):
        _, inspector, entry, candidate = self.fixture('def score(value):\n    assert isinstance(value, list)\n    return 1\n')
        generic = resolve_resource_calls(inspector, entry, candidate)
        self.assertEqual(generic['negative_scope_status'], 'UNKNOWN')
        recipe = compile_condition(inspector, entry, candidate)
        self.assertEqual(recipe['status'], 'REQUIRES_ACTUAL_CALL')
        actual = resolve_resource_calls(contextual(inspector, Context(values={'value':[]})), entry, candidate)
        self.assertEqual(actual['negative_scope_status'], 'CHECKED', actual)
        self.assertEqual(resolve_resource_calls(inspector, entry, candidate), generic)

    def test_initialization_does_not_inherit_entry_compiler_assumptions(self):
        _, inspector, entry, candidate = self.fixture('class Source:\n'
            '    def __init__(self, value):\n        assert isinstance(value,list)\n'
            '    def score(self, value):\n        assert isinstance(value,list)\n        return 1\n')
        entry.update(kind='method',symbol='Source.score',constructor=dict(symbol='Source',arguments={'value':[]}))
        recipe=compile_condition(inspector,entry,candidate)
        self.assertEqual(recipe['initialization']['status'],'UNKNOWN')

    def test_compiler_resource_plan_does_not_publish_global_mapping_or_non_use(self):
        from seampact.configuration.dependencies import DependencyBindings
        cache=str(Path(tempfile.gettempdir()).resolve()/'finite-condition-cache')
        _, inspector, entry, candidate = self.fixture('from transformers import AutoModel\n'
            'def score(value):\n    assert isinstance(value,list)\n'
            f"    model=AutoModel.from_pretrained('org/model',cache_dir={cache!r})\n    return 1\n")
        row=dict(id='weights',kind='weight',configurable=False,required=True,entry_ids=['score'],
            mappings=[],read_time='call',owner_scope='original model',evidence=[dict(path='original.py',start=1,end=5)],
            placement=dict(expression="'org/model'",hub_cache=dict(schema='fixed-hub-cache/1',repo_id='org/model',
                revision='a'*40,cache_dir=cache,ref='main',required_files=[dict(path='config.json',sha256='b'*64)])))
        candidate['dependencies']=[row]
        binding=DependencyBindings(SimpleNamespace(),contextual(inspector,Context(compiling=True)),candidate)
        self.assertEqual(binding.rows['weights']['status'],'MAPPING_CHECKED',binding.rows['weights'].get('reason'))
        snapshot=binding.snapshot()
        self.assertEqual(snapshot['requirements'][0]['status'],'MISSING')
        self.assertEqual(snapshot['call_condition_plan']['permission'],'NOT_GRANTED')
        self.assertEqual(candidate['dependencies'][0],row)

    def test_short_circuit_does_not_authorize_an_sdk_type_or_unknown_effect(self):
        _, inspector, entry, candidate = self.fixture('import missing_sdk\ndef score(value):\n    assert isinstance(value, list) or isinstance(value, missing_sdk.Marker)\n    return 1\n')
        positive = resolve_resource_calls(contextual(inspector, Context(values={'value':[]})), entry, candidate)
        negative = resolve_resource_calls(contextual(inspector, Context(values={'value':1})), entry, candidate)
        self.assertEqual(positive['negative_scope_status'], 'CHECKED')
        self.assertEqual(negative['negative_scope_status'], 'UNKNOWN')
        _, inspector, entry, candidate = self.fixture('def score(value):\n    assert isinstance(value,list)\n    eval(value)\n')
        self.assertIsNone(compile_condition(inspector, entry, candidate))

    def test_opaque_mutation_alias_and_unknown_iterable_cannot_be_empty_value_proofs(self):
        _, inspector, entry, candidate = self.fixture('import sdk\ndef score(value):\n'
            '    assert isinstance(value,list)\n    alias=value\n    sdk.change(alias)\n'
            '    assert alias or [1]\n    return 1\n')
        trace = resolve_resource_calls(contextual(inspector,Context(values={'value':[1]})),entry,candidate)
        self.assertEqual(trace['negative_scope_status'],'UNKNOWN')
        self.assertTrue(any('short-circuit' in g['reason'] for g in trace['negative_scope_gaps']))
        _, inspector, entry, candidate = self.fixture('import sdk\ndef score(value):\n'
            '    assert isinstance(value,list)\n    result=[]\n    result.extend(sdk.result())\n    return result\n')
        trace=resolve_resource_calls(contextual(inspector,Context(values={'value':[]})),entry,candidate)
        self.assertTrue(any('extension iterable' in g['reason'] for g in trace['negative_scope_gaps']))

    def test_fresh_source_list_slice_carries_only_outer_type_and_unknown_sdk_loop_stays_unknown(self):
        _, inspector, entry, candidate = self.fixture('def local(value):\n    result=[value]\n    return result[:-1]\n'
            'def score(value):\n    result=local(value) or [1]\n    assert isinstance(result,list)\n    return 1\n')
        trace=resolve_resource_calls(contextual(inspector,Context(values={'value':'text'})),entry,candidate)
        self.assertEqual(trace['negative_scope_status'],'CHECKED',trace)
        self.assertFalse(any(p.get('expression')=='local(value) or [1]' for p in trace['branch_evidence']))
        _, inspector, entry, candidate = self.fixture('import sdk\ndef score(value):\n'
            '    assert isinstance(value,list)\n    for item in sdk.values():\n        pass\n    return 1\n')
        trace=resolve_resource_calls(contextual(inspector,Context(values={'value':[]})),entry,candidate)
        self.assertTrue(any('loop iterable' in g['reason'] for g in trace['negative_scope_gaps']))

    def test_catalog_and_worker_require_actual_same_call_before_source_load(self):
        root, inspector, candidate, catalog, unit = self.catalog('def score(value):\n    assert isinstance(value,list)\n    return 7\n')
        self.assertEqual(unit['status'], 'CALL_CONDITION_DEFERRED', unit.get('reason'))
        self.assertEqual(unit['parameters']['properties']['value'], {})
        binding, entry = catalog.deferred[unit['id']]
        self.assertEqual(binding.modules, {})
        self.assertFalse(catalog.check_call_conditions(unit['id'], {'value':[]})['allowed'])
        identity = dict(session='session', instance_id='instance', generation=1, process_nonce='nonce')
        catalog._worker_call_identity = identity
        worker = Worker.__new__(Worker)
        worker.catalog, worker.store = catalog, catalog.store
        worker.session, worker.instance, worker.generation, worker.process_nonce = identity.values()
        worker.units, worker.accepting, worker.lock = {unit['id']}, True, threading.Lock()
        worker.in_flight, worker.preparing = 0, 0
        body = dict(unit=unit['id'], arguments={'value':[]}, **identity)
        checked = worker.check_call(body)
        self.assertTrue(checked['allowed'], checked)
        self.assertEqual(binding.modules, {})  # pure checker has not imported source
        changed = dict(body, arguments={'value':{'$ndarray':{}}})
        self.assertFalse(worker.check_call(changed)['allowed'])
        self.assertFalse(catalog.check_call_conditions(unit['id'], None)['allowed'])
        self.assertFalse(catalog.check_call_conditions(unit['id'], {'value':[]}, instance_identity=dict(identity,generation=2))['allowed'])

    def test_normal_cold_host_prewarm_controller_http_and_worker_call(self):
        from seampact.hosting.deployment import Deployments, worker_spec, json_request
        from seampact.worker import make_worker_server
        root, inspector, candidate, catalog, unit = self.catalog(
            'def score(value, option=None):\n    assert isinstance(value,list)\n    return 7\n')
        binding, entry = catalog.deferred[unit['id']]
        group = catalog.store.get('runtime_group', unit['runtime_group'])
        self.assertEqual(group['host']['members']['score']['status'], 'HOST_CHECKED')
        self.assertEqual(unit['typed_contract']['operations']['invoke']['status'], 'CONDITIONAL')
        self.assertTrue(unit['typed_contract']['operations']['prepare']['allowed'])
        self.assertFalse(catalog.check_call_conditions(unit['id'], {'value':[]})['allowed'])
        bundle = root/'bundle'
        (bundle/'source').mkdir(parents=True)
        shutil.copyfile(root/'original.py', bundle/'source'/'original.py')
        shutil.copytree(root/'snapshots', bundle/'snapshots')
        spec = worker_spec(binding, group, unit)
        (bundle/'spec.json').write_text(json.dumps(spec), encoding='utf-8')
        from seampact.units import NativeBindings
        with patch('seampact.worker.json_request', return_value={'session':'worker-session'}), \
             patch('seampact.worker.RemoteRuntime', return_value=catalog.runtime), \
             patch.object(NativeBindings, '_namespace', side_effect=AssertionError('startup imported source')):
            worker = Worker(bundle/'spec.json', 'http://unused', 'a'*32, 1)
            cold = worker.check_call(dict(unit=unit['id'], arguments={'value':[]}, **worker.identity()))
            for explicit in (None, {}):
                full = worker.check_call(dict(unit=unit['id'], arguments={'value':[], 'option':explicit}, **worker.identity()))
                self.assertTrue(full['allowed'], full)
                self.assertEqual(full['omitted'], [])
        self.addCleanup(worker.store.close)
        self.assertFalse(cold['allowed'])
        self.assertIn('omitted defaults', cold['reason'])
        self.assertEqual(worker._initialization()[unit['id']], 'NOT_RUN')
        prepared = worker.prewarm(dict(units=[unit['id']], **worker.identity()))
        self.assertEqual(prepared['status'], 'PREPARED', prepared)
        server = make_worker_server(worker, '127.0.0.1', 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        router = Deployments.__new__(Deployments)
        router.lock, router.instances = threading.RLock(), {}
        router.platform = SimpleNamespace(catalog=catalog, store=catalog.store)
        catalog.remote_router = router
        ident = worker.identity()
        router.instances[ident['instance_id']] = dict(id=ident['instance_id'], session=ident['session'],
            generation=ident['generation'], process_nonce=ident['process_nonce'], group_id=group['id'],
            state='READY', accepting=True, units=[unit['id']], endpoint='http://127.0.0.1:'+str(server.server_address[1]))
        evidence = catalog.require_call_conditions(unit['id'], {'value':[]})
        self.assertEqual(evidence['omitted'], ['option'])
        self.assertEqual(evidence['instance_identity'], ident)
        body = dict(unit=unit['id'], arguments={'value':[]}, **ident)
        answer = json_request(router.instances[ident['instance_id']]['endpoint']+'/invoke', body)
        self.assertEqual(answer.get('result'), 7, answer)
        self.assertEqual(catalog.store.get('unit',unit['id'])['status'], 'CALL_CONDITION_DEFERRED')
        self.assertTrue(catalog.require_call_conditions(unit['id'], {'value':[], 'option':None})['allowed'])
        self.assertTrue(catalog.require_call_conditions(unit['id'], {'value':[], 'option':{}})['allowed'])
        self.assertFalse(catalog.check_call_conditions(unit['id'], {'value':[]},
            instance_identity=dict(ident,generation=2))['allowed'])
        other = json_request(router.instances[ident['instance_id']]['endpoint']+'/check_call',
            dict(body, unit='another-group:score'))
        self.assertFalse(other['allowed'])
        wb, _ = worker.catalog.deferred[unit['id']]
        import inspect
        original = inspect.unwrap(wb.entries[unit['id']])
        original.__defaults__ = ('changed',)
        self.assertFalse(catalog.check_call_conditions(unit['id'], {'value':[]})['allowed'])
        self.assertTrue(catalog.require_call_conditions(unit['id'], {'value':[], 'option':None})['allowed'])

    def test_conditional_recipe_and_full_candidate_cannot_be_replayed(self):
        from seampact.hosting.deployment import worker_spec
        _, inspector, candidate, catalog, unit = self.catalog('def score(value):\n    assert isinstance(value,list)\n    return 1\n')
        binding, _ = catalog.deferred[unit['id']]
        group = catalog.store.get('runtime_group',unit['runtime_group'])
        structure = catalog.require_call_condition_structure(unit['id'])
        self.assertEqual(structure['call_permission'], 'REQUIRES_ACTUAL_CALL')
        spec = worker_spec(binding,group,unit)
        self.assertEqual(spec['candidate'], binding.candidate)
        changed = copy.deepcopy(unit)
        changed['call_condition']['candidate_digest'] = 'forged'
        from seampact.source.call_value_conditions import digest,verify_recipe
        changed['call_condition']['digest']=digest({k:v for k,v in changed['call_condition'].items() if k!='digest'})
        self.assertFalse(verify_recipe(inspector,candidate['entries'][0],binding.candidate,changed['call_condition']))
        catalog.store.save('unit',unit['id'],changed)
        with self.assertRaises(ValueError): catalog.require_call_condition_structure(unit['id'])
        with self.assertRaises(ValueError):
            worker_spec(binding,dict(group,entries=['other']),unit)


if __name__ == '__main__':
    unittest.main()
