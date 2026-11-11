"""Original Hub arguments stay intact through source and actual-host checks."""

import copy
import hashlib
import os
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from seampact.configuration.dependencies import check_requirements, DependencyBindings, EnvironmentGap
from seampact.source.inspection import Inspector


class FixedHubDependencyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cache = self.root / 'cache'
        self.revision = 'a' * 40
        self.repo = self.cache / 'models--owner--model'
        self.snapshot = self.repo / 'snapshots' / self.revision
        self.snapshot.mkdir(parents=True)
        (self.repo / 'refs').mkdir()
        (self.repo / 'refs' / 'main').write_text(self.revision)
        self.data = b'mechanical cache fixture; not a model'
        (self.snapshot / 'model.bin').write_bytes(self.data)
        self.spec = dict(schema='fixed-hub-cache/1', repo_id='owner/model', revision=self.revision,
            cache_dir=str(self.cache), ref='main', required_files=[dict(path='model.bin',
            sha256=hashlib.sha256(self.data).hexdigest(), bytes=len(self.data))])

    def fixture(self, source, *, entry=None, expression="'owner/model'"):
        (self.root / 'main.py').write_text(source)
        inspector = Inspector.freeze(self.root, self.root / 'snapshots', 'request', ['main.py'], 'goal', 'result')
        entry = entry or dict(id='target', path='main.py', symbol='run', kind='function')
        dependency = dict(id='weights', kind='weight', configurable=False, required=True,
            entry_ids=['target'], owner_scope='original', read_time='call', mappings=[],
            evidence=[dict(path='main.py', start=1, end=len(source.splitlines()))],
            placement=dict(expression=expression, hub_cache=copy.deepcopy(self.spec)))
        return inspector, dict(entries=[entry], dependencies=[dependency])

    def offline(self):
        modules = {'huggingface_hub.constants': types.SimpleNamespace(HF_HUB_OFFLINE=True),
                   'transformers.utils.hub': types.SimpleNamespace(_is_offline_mode=True)}
        return patch.dict('sys.modules', modules)

    def test_source_proof_does_not_turn_asset_identity_into_execution(self):
        source = ('from transformers import AutoModel\ndef run():\n'
                  f"    return AutoModel.from_pretrained('owner/model', cache_dir={str(self.cache)!r})\n")
        inspector, candidate = self.fixture(source)
        original = copy.deepcopy(candidate)
        row = check_requirements(inspector, candidate)[0]
        self.assertEqual(row['status'], 'MAPPING_CHECKED', row.get('reason'))
        self.assertEqual(candidate, original)
        self.assertEqual(row['placement']['original_argument'], 'PRESERVED')
        self.assertEqual(row['execution'], 'NOT_RUN')
        self.assertEqual(row['placement']['resolved_path'], str(self.snapshot))
        bindings = DependencyBindings(None, inspector, candidate)
        with patch.dict(os.environ, {'HF_HUB_OFFLINE': '0'}):
            with self.assertRaises(EnvironmentGap):
                bindings.check_entry('target')
        with patch.dict(os.environ, {'HF_HUB_OFFLINE': '1'}), self.offline():
            bindings.check_entry('target')
            (self.snapshot / 'model.bin').write_bytes(b'changed')
            with self.assertRaises(EnvironmentGap):
                bindings.check_entry('target')

    def test_proved_unused_branch_preserves_original_declaration(self):
        source = ('from transformers import AutoModel\nclass Review:\n'
                  '    def __init__(self, mode="small"):\n'
                  '        if mode == "small":\n'
                  f"            self.model = AutoModel.from_pretrained('owner/model', cache_dir={str(self.cache)!r})\n"
                  '        else:\n'
                  f"            self.model = AutoModel.from_pretrained('owner/other', cache_dir={str(self.cache)!r})\n"
                  '    def run(self):\n        return 1\n')
        entry = dict(id='target', path='main.py', symbol='Review.run', kind='method',
                     constructor=dict(symbol='Review', arguments=dict(mode='small')))
        inspector, candidate = self.fixture(source, entry=entry, expression="'owner/other'")
        original = copy.deepcopy(candidate)
        row = check_requirements(inspector, candidate)[0]
        self.assertEqual(row['status'], 'MAPPING_CHECKED', row.get('reason'))
        self.assertEqual(row['declared_entry_ids'], ['target'])
        self.assertEqual(row['entry_ids'], [])
        self.assertEqual(row['scope_checks']['target']['status'], 'NOT_USED')
        self.assertEqual(candidate, original)
        DependencyBindings(None, inspector, candidate).check_entry('target')

    def test_unknown_branch_keeps_requirement_and_rejects_mismatched_hub(self):
        source = ('from transformers import AutoModel\ndef run(choice):\n'
                  '    if choice:\n'
                  f"        return AutoModel.from_pretrained('owner/other', cache_dir={str(self.cache)!r})\n"
                  '    return 1\n')
        inspector, candidate = self.fixture(source, expression="'owner/other'")
        row = check_requirements(inspector, candidate)[0]
        self.assertEqual(row['status'], 'MISSING')
        self.assertEqual(row['entry_ids'], ['target'])
        self.assertNotEqual(row['scope_checks']['target']['status'], 'NOT_USED')


if __name__ == '__main__':
    unittest.main()
