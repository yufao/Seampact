"""Normal deployment materializes H without granting semantic operations."""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from seampact.service import Platform
from seampact.source.inspection import Inspector
from seampact.typed_contract import operation_status


SOURCE = ('def unknown(value):\n    return eval(value)\n'
          'def score(value: str) -> int:\n    if value == "safe":\n        return 7\n'
          '    return unknown(value)\n')


class ValueConditionedMaterializationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.platform = Platform(root / 'platform')
        self.addCleanup(self.platform.close)
        source = root / 'source'
        source.mkdir()
        (source / 'original.py').write_text(SOURCE, encoding='utf-8')
        self.inspector = Inspector.freeze(source, root / 'snapshots', 'request', ['original.py'], 'score', 'result')
        self.inspector.request['source_id'] = 'source'
        self.candidate = dict(entries=[dict(id='score', kind='function', path='original.py', symbol='score',
                                            boundary_ids=['whole'])],
                              boundaries=[dict(id='whole', mode='W', path='original.py', symbol='score')])
        installed, = self.platform.catalog.install(self.inspector, self.candidate, 'analysis', 'install')
        self.uid, self.gid = installed['id'], installed['runtime_group']
        self.platform.store.save('harness', 'source', dict(id='source', status='CONNECTED', installation_id='install'))

    def export(self):
        return self.platform.deployments.export(self.gid, dict(environment=dict(python='3.11', requirements=[], packages=[])))

    def test_normal_export_preserves_complete_recipe_and_never_grants_execution(self):
        unit = self.platform.store.get('unit', self.uid)
        group = self.platform.store.get('runtime_group', self.gid)
        self.assertEqual(unit['status'], 'STRUCTURE_ONLY')
        self.assertEqual(group['host']['status'], 'STRUCTURE_ONLY')
        self.assertEqual(group['host']['host_entries'], [])
        self.assertEqual(group['host']['structure_entries'], ['score'])
        self.assertEqual(group['contract']['root_entries'], [])
        self.assertEqual(group['contract']['structure_entries'], ['score'])
        self.assertEqual(group['contract']['operations']['start']['status'], 'UNKNOWN')
        self.assertEqual(group['host']['members']['score']['prepare']['status'], 'UNKNOWN')
        artifact = self.export()
        self.assertEqual(artifact['materialization'], 'STRUCTURE_ONLY')
        self.assertFalse(artifact['semantic_export'] or artifact['invocation'] or artifact['process_start'])
        spec = json.loads((Path(artifact['folder']) / 'worker.json').read_text(encoding='utf-8'))
        self.assertEqual(spec['candidate'], self.candidate)
        self.assertEqual((Path(artifact['folder']) / 'source' / 'original.py').read_text(encoding='utf-8'), SOURCE)
        self.assertEqual(artifact['build']['status'], 'NOT_RUN')
        current = self.platform.store.get('unit', self.uid)
        group = self.platform.store.get('runtime_group', self.gid)
        binding, _ = self.platform.catalog.structural[self.uid]
        evidence = self.platform.catalog._binding_evidence(binding, current, full_source_check=True)
        self.assertTrue(operation_status(current, 'materialize_guarded_entry', group=group, evidence=evidence)['allowed'])
        for action in ('export_entry', 'invoke', 'dispatch', 'remote', 'prepare', 'prewarm', 'scale_out', 'scale_in'):
            self.assertFalse(operation_status(current, action, group=group, evidence=evidence)['allowed'], action)
        self.assertFalse(any(current['structure_admission']['permissions'].values()))
        self.assertEqual(binding.modules, {})
        self.assertEqual(binding.entries, {})
        again = self.export()
        self.assertEqual(again['binding_fingerprint'], artifact['binding_fingerprint'])

    def test_forged_host_membership_does_not_authorize_materialization(self):
        group = self.platform.store.get('runtime_group', self.gid)
        group['host']['host_entries'] = ['score']
        self.platform.store.save('runtime_group', self.gid, group)
        with self.assertRaises(ValueError):
            self.export()
        self.assertIsNone(self.platform.store.get('deployment', self.gid))

    def test_tampered_unit_or_absent_current_binding_cannot_export(self):
        unit = self.platform.store.get('unit', self.uid)
        unit['structure_admission']['permissions']['invoke'] = True
        self.platform.store.save('unit', self.uid, unit)
        with self.assertRaises(ValueError):
            self.export()
        self.assertIsNone(self.platform.store.get('deployment', self.gid))
        unit['structure_admission']['permissions']['invoke'] = False
        self.platform.store.save('unit', self.uid, unit)
        self.platform.catalog.structural.pop(self.uid)
        with self.assertRaises(ValueError):
            self.export()

    def test_changed_frozen_source_or_candidate_is_rejected(self):
        binding, _ = self.platform.catalog.structural[self.uid]
        original = copy.deepcopy(binding.candidate)
        binding.candidate['entries'][0]['symbol'] = 'unknown'
        with self.assertRaises(ValueError):
            self.export()
        binding.candidate = original
        snapshot = self.inspector.snapshots / self.inspector.files['original.py']['sha256']
        snapshot.write_text(SOURCE + '\n# drift\n', encoding='utf-8')
        with self.assertRaises(ValueError):
            self.export()
        self.assertIsNone(self.platform.store.get('deployment', self.gid))


if __name__ == '__main__':
    unittest.main()
