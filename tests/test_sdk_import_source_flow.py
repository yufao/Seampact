"""Conditional original import leaves must reach normal source plans."""
import os
from pathlib import Path
import random
import sys
import tempfile
import typing
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from seampact.source.inspection import Inspector
from seampact.source.call_value_conditions import Context,contextual
from seampact.source.dependency_closure import resolve_resource_calls
from seampact.source.sdk_import_summaries import RULE,FINDER,asset_obligations


class SDKImportSourceFlowTests(unittest.TestCase):
    def trace(self,source,*,conditional=True):
        temporary=tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root=Path(temporary.name)
        (root/'original.py').write_text(source,encoding='utf-8')
        inspector=Inspector.freeze(root,root/'snapshots','request',['original.py'],'score','result')
        entry=dict(id='score',kind='function',path='original.py',symbol='score')
        before={name:value for name,value in sys.modules.items()
            if name.split('.')[0] in {'nltk','numpy','torch','transformers','tqdm'}}
        if conditional:
            inspector=contextual(inspector,Context(compiling=True,initialization_only=True))
        result=resolve_resource_calls(inspector,entry,dict(entries=[entry],boundaries=[]))
        after={name:value for name,value in sys.modules.items()
            if name.split('.')[0] in {'nltk','numpy','torch','transformers','tqdm'}}
        self.assertEqual(before,after)
        return result

    def test_complete_finite_sdk_prefix_retains_worker_conditions_without_importing(self):
        trace=self.trace('import os\nimport random\nfrom typing import List\n'
            'import nltk\nimport numpy as np\nimport torch\nimport torch.nn as nn\n'
            'import torch.nn.functional as F\nfrom nltk.tokenize import sent_tokenize\n'
            'from transformers import AutoConfig, AutoTokenizer, AutoModelForSeq2SeqLM, AutoModelForSequenceClassification\n'
            'from tqdm import tqdm\ntry:\n    nltk.data.find("tokenizers/punkt")\n'
            'except LookupError:\n    nltk.download("punkt")\n'
            'def score(value: List[str]) -> List[float]:\n    return []\n')
        self.assertEqual(trace['activation_prefix']['status'],'CHECKED',trace['activation_prefix'])
        requirements=[row['target_requirements'] for row in trace['external_protocols']
            if row['rule'] in {RULE,FINDER}]
        self.assertTrue(any(row['schema']==FINDER for row in requirements))
        self.assertEqual(asset_obligations(requirements)['resources'],
            ['tokenizers/punkt','tokenizers/punkt_tab/english'])
        self.assertTrue(all(row['execution_permission'] is False and
            row['target_status']=='REQUIRES_NORMAL_WORKER_CHECK' for row in requirements))
        calls=[row['target'] for row in trace['calls']]
        self.assertIn('nltk.data.find',calls)
        self.assertNotIn('nltk.download',calls)
        events=trace['activation_prefix']['events']
        conditional=[row for row in events if row['kind']=='MODULE_CONTROL']
        self.assertEqual(len(conditional),1)
        self.assertFalse(conditional[0]['conditional_normal_exit']['result_value_available'])

    def test_unknown_lookup_and_different_handler_keep_the_original_frontier(self):
        for lookup,handler in [('unreviewed/resource','LookupError'),('tokenizers/punkt','Exception')]:
            with self.subTest(lookup=lookup,handler=handler):
                trace=self.trace('import nltk\ntry:\n    nltk.data.find('+repr(lookup)+')\n'
                    'except '+handler+':\n    nltk.download("punkt")\n'
                    'def score():\n    return []\n')
                self.assertEqual(trace['activation_prefix']['status'],'UNKNOWN')
                self.assertFalse(any(row['rule']==FINDER for row in trace['external_protocols']))

    def test_modified_finder_or_returned_lookup_is_not_a_synthetic_pointer(self):
        trace=self.trace('import nltk\nnltk.data.find = unknown\ntry:\n'
            '    nltk.data.find("tokenizers/punkt")\nexcept LookupError:\n'
            '    nltk.download("punkt")\ndef score():\n    return []\n')
        self.assertEqual(trace['activation_prefix']['status'],'UNKNOWN')
        self.assertFalse(any(row['rule']==FINDER for row in trace['external_protocols']))
        trace=self.trace('import nltk\nresult = nltk.data.find("tokenizers/punkt")\n'
                         'def score():\n    return result\n')
        self.assertEqual(trace['activation_prefix']['status'],'UNKNOWN')
        self.assertFalse(any(row['rule']==FINDER for row in trace['external_protocols']))

    def test_unconditional_existing_source_does_not_acquire_a_new_sdk_version_requirement(self):
        trace=self.trace('import torch\ndef score():\n    return []\n',conditional=False)
        self.assertEqual(trace['activation_prefix']['status'],'UNKNOWN')
        self.assertFalse(any(row['rule'] in {RULE,FINDER} for row in trace['external_protocols']))


if __name__=='__main__': unittest.main()
