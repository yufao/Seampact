"""Actual original asset lookup facts cannot be supplied by a private path."""
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from seampact.source.sdk_assets import compile_sdk_asset_plan,verify_sdk_asset_plan,original_nltk_paths


class SDKAssetTests(unittest.TestCase):
    def fixture(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        (root/'tokenizers/punkt').mkdir(parents=True)
        english = root/'tokenizers/punkt_tab/english'
        english.mkdir(parents=True)
        for name in ('collocations.tab','sent_starters.txt','abbrev_types.txt','ortho_context.tab'):
            (english/name).write_text('original ordinary data\n')
        return root

    def plan(self):
        return compile_sdk_asset_plan(SimpleNamespace(request=dict(source_version='source')),dict(id='entry'),
            dict(entries=[]),dict(bindings={}),['nltk.tokenize.sent_tokenize'])

    def test_native_first_root_and_complete_tree_are_frozen_and_rechecked(self):
        root = self.fixture()
        with patch.dict(os.environ,{'NLTK_DATA':str(root)}), patch(
                'seampact.source.lifecycle_resources.sdk_identity',return_value=dict(status='CHECKED')):
            plan = self.plan()
            self.assertEqual(plan['status'],'CHECKED',plan)
            self.assertEqual(len(plan['original_lookup']),2)
            self.assertTrue(verify_sdk_asset_plan(SimpleNamespace(request=dict(source_version='source')),
                dict(id='entry'),dict(entries=[]),dict(bindings={}),['nltk.tokenize.sent_tokenize'],plan))
            (root/'tokenizers/punkt_tab/english/abbrev_types.txt').write_text('changed')
            self.assertFalse(verify_sdk_asset_plan(SimpleNamespace(request=dict(source_version='source')),
                dict(id='entry'),dict(entries=[]),dict(bindings={}),['nltk.tokenize.sent_tokenize'],plan))

    def test_installed_bytes_or_earlier_archive_alone_cannot_supply_a_lookup(self):
        root = self.fixture()
        with tempfile.TemporaryDirectory() as earlier:
            (Path(earlier)/'tokenizers.zip').write_bytes(b'not opened')
            with patch.dict(os.environ,{'NLTK_DATA':earlier+os.pathsep+str(root)}), patch(
                    'seampact.source.lifecycle_resources.sdk_identity',return_value=dict(status='CHECKED')):
                self.assertEqual(self.plan()['status'],'UNKNOWN')
        with patch.dict(os.environ,{'NLTK_DATA':str(root)}), patch(
                'seampact.source.lifecycle_resources.sdk_identity',return_value=dict(status='UNKNOWN')):
            self.assertEqual(self.plan()['status'],'UNKNOWN')

    def test_relative_original_root_is_not_silently_rewritten(self):
        with patch.dict(os.environ,{'NLTK_DATA':'relative/resource'}):
            with self.assertRaises(ValueError):
                original_nltk_paths()


if __name__=='__main__':
    unittest.main()
