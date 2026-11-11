"""Source-proved result structure crosses the ordinary typed worker boundary."""

from contextlib import contextmanager
from pathlib import Path
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from seampact.composition import _schema_at, _contains_native, _nonnative_branch_matches
from seampact.hosting.deployment import GROUP_PROFILE, decode_port, encode_port
from seampact.runtime import Coordinator, OwnerRuntime
from seampact.source.inspection import Inspector
from seampact.source.record_ports import FrozenRecordPorts
from seampact.store import Store
from seampact.typed_contract import verify_contract
from seampact.units import UnitCatalog, _matches, _result_matches, _select_result_structure
from seampact.worker import Worker


class SourceResultPortTests(unittest.TestCase):
    @contextmanager
    def original_worker(self, source):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'original.py').write_text(source, encoding='utf-8')
            inspector = Inspector.freeze(root, root / 'snapshots', 'source', ['original.py'], 'score', 'original result')
            inspector.request['source_id'] = 'source'
            store = Store(':memory:')
            runtime = OwnerRuntime(Coordinator(store))
            try:
                catalog = UnitCatalog(root, store, runtime)
                candidate = dict(boundaries=[], entries=[dict(id='score', path='original.py', symbol='score',
                    kind='function', boundary_ids=[])])
                unit = catalog.install(inspector, candidate, 'analysis', 'installation')[0]
                self.assertEqual(unit['status'], 'READY', unit.get('reason'))
                worker = Worker.__new__(Worker)
                worker.catalog, worker.store, worker.runtime = catalog, store, runtime
                worker.profile, worker.units = GROUP_PROFILE['id'], {unit['id']}
                worker.record_ports = FrozenRecordPorts(inspector)
                worker.lock, worker.accepting, worker.in_flight = threading.Lock(), True, 0
                worker._identify, worker.identity = lambda body: None, lambda: {}
                yield worker, unit
            finally:
                runtime.close()
                store.close()

    def test_wrong_list_annotation_is_reported_and_real_tuple_keeps_type(self):
        source = ('def score() -> list[float]:\n'
                  '    return ([1], [.875], [["original"]], [[.875]])\n')
        with self.original_worker(source) as (worker, unit):
            proof, schema = unit['result_structure'], unit['result_schema']
            self.assertEqual(proof['status'], 'CHECKED')
            self.assertEqual(proof['declared_schema'], dict(type='array', items=dict(type='number')))
            self.assertEqual(proof['root_annotation_check'], 'CONFLICT')
            self.assertEqual(proof['selection'], 'SOURCE_PROVED_STRUCTURE')
            self.assertEqual(schema['x-python-container'], 'tuple')
            self.assertEqual(len(schema['prefixItems']), 4)
            response = worker.invoke(dict(unit=unit['id'], arguments=encode_port({})))
            self.assertNotIn('error', response)
            result = decode_port(response['result'])
            self.assertIs(type(result), tuple)
            self.assertEqual(result, ([1], [.875], [['original']], [[.875]]))
            self.assertFalse(_matches(result, dict(type='array')))
            self.assertFalse(_result_matches(list(result), schema))
            self.assertFalse(_result_matches(result[:-1], schema))
            self.assertEqual(len(worker.runtime.snapshot()['calls']), 1)
            changed = dict(unit, result_structure=dict(proof, selection='forged'))
            self.assertFalse(verify_contract(changed)['valid'])

    def test_unknown_structure_does_not_replace_declared_guard(self):
        declared = dict(type='array', items=dict(type='number'))
        schema, report = _select_result_structure(declared, dict(status='UNKNOWN', result_schema={}))
        self.assertEqual(schema, declared)
        self.assertEqual(report['selection'], 'DECLARED_ANNOTATION')
        self.assertFalse(_result_matches(([1], [.5], [['chunk']], [[.5]]), schema))

    def test_partial_compatible_structure_keeps_original_element_constraint(self):
        declared = dict(type='array', items=dict(type='number'))
        proof = dict(status='CHECKED', result_schema=dict(type='array', items={}), fully_typed=False)
        schema, report = _select_result_structure(declared, proof)
        self.assertEqual(schema, declared)
        self.assertEqual(report['selection'], 'DECLARED_ANNOTATION')
        self.assertFalse(_result_matches(['not a number'], schema))

    def test_tuple_prefix_paths_preserve_unknown_and_native_field_boundaries(self):
        native = {'x-native-type': dict(source_version='source', path='original.py', symbol='Record')}
        schema = dict(type='array', **{'x-python-container': 'tuple'},
                      prefixItems=[{}, native], minItems=2, maxItems=2)
        self.assertEqual(_schema_at(schema, [0]), {})
        self.assertEqual(_schema_at(schema, [1]), native)
        self.assertEqual(_schema_at(schema, [2]), {})
        self.assertTrue(_contains_native(schema))
        self.assertFalse(_nonnative_branch_matches(schema, dict(type='array', items={})))

    def test_known_outer_tuple_does_not_authorize_opaque_source_objects(self):
        source = ('class Owner:\n    pass\n'
                  'def score() -> list[float]:\n'
                  '    return (1, Owner())\n')
        with self.original_worker(source) as (worker, unit):
            self.assertEqual(unit['result_schema'].get('x-python-container'), 'tuple')
            response = worker.invoke(dict(unit=unit['id'], arguments=encode_port({})))
            self.assertIn('error', response)
            self.assertEqual(response['error_type'], 'PortGap')
            self.assertEqual(len(worker.runtime.snapshot()['calls']), 1)


if __name__ == '__main__':
    unittest.main()
