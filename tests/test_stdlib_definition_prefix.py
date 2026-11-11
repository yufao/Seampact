"""Finite definition-time stdlib protocols, without evaluating annotations."""
import ast
import os
from pathlib import Path
import random
import sys
import tempfile
import typing
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from seampact.source.inspection import Inspector
from seampact.source.dependency_closure import resolve_resource_calls
from seampact.source.stdlib_definition_prefix import RULE,import_fact,verify_requirements


class StdlibDefinitionPrefixTests(unittest.TestCase):
    def trace(self,source):
        temporary=tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root=Path(temporary.name)
        (root/'original.py').write_text(source,encoding='utf-8')
        inspector=Inspector.freeze(root,root/'snapshots','request',['original.py'],'score','result')
        entry=dict(id='score',kind='function',path='original.py',symbol='score')
        return resolve_resource_calls(inspector,entry,dict(entries=[entry],boundaries=[]))

    def test_original_existing_imports_and_builtin_list_annotations_close_the_prefix(self):
        trace=self.trace('import os\nimport random\nfrom typing import List\n'
            'def score(value: List[str]) -> List[float]:\n    return []\n')
        self.assertEqual(trace['activation_prefix']['status'],'CHECKED',trace['activation_prefix'])
        facts=[row['target_requirements'] for row in trace['external_protocols'] if row['rule']==RULE]
        self.assertEqual(len(facts),5)
        self.assertTrue(verify_requirements(facts))
        self.assertTrue(all(row['execution_permission'] is False for row in facts))

    def test_unreviewed_type_subscription_keeps_its_original_unknown(self):
        trace=self.trace('from typing import List\nclass Custom: pass\n'
                         'def score(value: List[Custom]):\n    return []\n')
        self.assertEqual(trace['activation_prefix']['status'],'UNKNOWN')

    def test_changed_alias_or_module_spec_does_not_execute_protocol_hooks(self):
        calls=[]
        class Evil:
            def __getitem__(self,key): calls.append('getitem'); return str
            def __getattribute__(self,key):
                if key=='_initializing': calls.append('initializing')
                return object.__getattribute__(self,key)
        with patch.object(typing,'List',Evil()):
            self.assertEqual(import_fact('typing','List')['status'],'UNKNOWN')
        with patch.object(os,'__spec__',Evil()):
            self.assertEqual(import_fact('os','os')['status'],'UNKNOWN')
        self.assertEqual(calls,[])

    def test_forged_metadata_does_not_authorize_an_ordinary_prefix(self):
        valid=import_fact('os','os')
        self.assertFalse(verify_requirements([dict(valid,execution_permission=True)]))
        self.assertFalse(verify_requirements([dict(valid,operation='candidate-import')]))


if __name__=='__main__':
    unittest.main()
