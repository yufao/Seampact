"""Import-plan leaves do not execute SDKs or turn file identities into effects."""
import ast
import base64
import copy
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from seampact.source.call_value_conditions import CallUnknown
from seampact.source.sdk_import_summaries import (
    RULE,FINDER,import_fact,attribute_fact,import_requirement,finder_requirement,
    asset_obligations,_verify_site,_bundle,torch_backend_domain,
    transformers_cache_domain,verify_before_sdk_import,_verify_reader_kernel,
)


class _Inspector:
    def __init__(self,text):
        self.text=text.encode()
        self.request={'source_version':'original-source'}
        self.files={'source.py':{'sha256':hashlib.sha256(self.text).hexdigest()}}
    def data(self,path): return self.text


class _Checker:
    def __init__(self,text):
        self.inspector=_Inspector(text)
        self.sdk_source=self
        self.protocols=[]
    def protocol(self,*args,**kwargs): self.protocols.append((args,kwargs))
    def unmodified(self,target): return True


class SDKImportSummaryTests(unittest.TestCase):
    def temporary(self):
        temporary=tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return Path(temporary.name)

    def test_source_plan_is_sdk_installation_independent_and_finite(self):
        sdk=lambda:{name:module for name,module in sys.modules.items()
            if name.split('.')[0] in {'nltk','numpy','torch','transformers','tqdm'}}
        before=sdk()
        with patch('seampact.source.lifecycle_resources.sdk_identity',side_effect=AssertionError('controller SDK probe')):
            for module,export in [('nltk',None),('nltk.tokenize','sent_tokenize'),('numpy',None),
                ('torch',None),('torch.nn',None),('torch.nn.functional',None),
                *[('transformers',name) for name in ('AutoConfig','AutoTokenizer',
                    'AutoModelForSeq2SeqLM','AutoModelForSequenceClassification')],('tqdm','tqdm')]:
                fact=import_fact(module,export)
                self.assertEqual(fact['source_status'],'STRUCTURE_CHECKED')
                self.assertFalse(fact['execution_permission'])
                self.assertEqual(fact['summary']['effect_boundary']['omitted_effects'],[])
        self.assertEqual(sdk(),before)
        self.assertIsNone(import_fact('transformers','pipeline'))
        self.assertIsNone(import_fact('torchvision'))
        self.assertIsNotNone(attribute_fact('nltk.data.find'))
        self.assertIsNone(attribute_fact('nltk.download'))

    def test_normal_source_site_rejects_forged_operation_and_changed_bytes(self):
        checker=_Checker('from transformers import AutoTokenizer\n')
        node=ast.parse(checker.inspector.text).body[0]
        requirement=import_requirement(checker,'source.py',node,'transformers','AutoTokenizer')
        _verify_site(requirement,checker.inspector)
        forged=copy.deepcopy(requirement)
        forged['imported']='AutoConfig'
        with self.assertRaises(CallUnknown): _verify_site(forged,checker.inspector)
        checker.inspector.text=b'from transformers import AutoConfig\n'
        with self.assertRaises(CallUnknown): _verify_site(requirement,checker.inspector)

    def test_finder_exports_both_actual_asset_obligations_without_faking_result(self):
        checker=_Checker("import nltk\nnltk.data.find('tokenizers/punkt')\n")
        node=ast.parse(checker.inspector.text).body[1].value
        fact=finder_requirement(checker,'source.py',node,'nltk.data.find',['tokenizers/punkt'],{})
        self.assertEqual(fact['schema'],FINDER)
        self.assertEqual(asset_obligations([fact]),{'operations':['nltk.sent_tokenize'],
            'resources':['tokenizers/punkt','tokenizers/punkt_tab/english']})
        _verify_site(fact,checker.inspector)
        self.assertEqual(fact['retained_exceptions'],'all other original exceptions propagate in original order')
        self.assertIsNone(finder_requirement(checker,'source.py',node,'nltk.data.find',['other'],{}))

    def test_reader_kernel_rejects_replaced_fs_function_before_invoking_it(self):
        _verify_reader_kernel()
        called=[]
        with patch('os.path.isdir',lambda value:called.append(value)):
            with self.assertRaisesRegex(CallUnknown,'reader changed'):
                _verify_reader_kernel()
        self.assertEqual(called,[])

    def test_lookup_error_and_finder_function_identity_are_independent(self):
        with patch('builtins.LookupError',RuntimeError):
            with self.assertRaisesRegex(CallUnknown,'LookupError identity'):
                _verify_reader_kernel()
        checker=_Checker("import nltk as original\noriginal.data.find('tokenizers/punkt')\n")
        node=ast.parse(checker.inspector.text).body[1].value
        fact=finder_requirement(checker,'source.py',node,'nltk.data.find',['tokenizers/punkt'],{})
        _verify_site(fact,checker.inspector)
        other=_Checker("import nltk\nnltk.download('tokenizers/punkt')\n")
        other_node=ast.parse(other.inspector.text).body[1].value
        forged=finder_requirement(other,'source.py',other_node,'nltk.data.find',['tokenizers/punkt'],{})
        with self.assertRaisesRegex(CallUnknown,'original finite call'):
            _verify_site(forged,other.inspector)

    def test_native_environment_backing_mapping_cannot_execute_a_hook(self):
        import os
        called=[]
        class UnknownMapping:
            def keys(self): called.append('keys'); raise AssertionError('executed')
        fields=object.__getattribute__(os.environ,'__dict__')
        original=fields['_data']
        fields['_data']=UnknownMapping()
        try:
            with self.assertRaisesRegex(CallUnknown,'backing data is nonordinary'):
                _verify_reader_kernel()
        finally:
            fields['_data']=original
        self.assertEqual(called,[])

    def test_record_matches_do_not_accept_unrecorded_vendor_or_changed_member(self):
        root=self.temporary()
        package=root/'example'
        package.mkdir()
        source=package/'__init__.py'
        source.write_bytes(b'original = 1\n')
        metadata=root/'example-1.dist-info'
        metadata.mkdir()
        digest=hashlib.sha256(source.read_bytes()).digest()
        record=('example/__init__.py,sha256='+base64.urlsafe_b64encode(digest).decode().rstrip('=')+
                ','+str(source.stat().st_size)+'\nexample-1.dist-info/RECORD,,\n').encode()
        (metadata/'RECORD').write_bytes(record)
        definition={'distribution':'example','version':'1','record_sha256':hashlib.sha256(record).hexdigest(),
            'sdk':[{'path':'example/__init__.py','sha256':digest.hex()}]}
        self.assertEqual(_bundle(definition,[root])['member_count'],2)
        (package/'_distributor_init_local.py').write_bytes(b'raise RuntimeError\n')
        with self.assertRaisesRegex(CallUnknown,'unrecorded'): _bundle(definition,[root])
        (package/'_distributor_init_local.py').unlink()
        source.write_bytes(b'original = 2\n')
        with self.assertRaisesRegex(CallUnknown,'differs'): _bundle(definition,[root])

    def test_complete_native_backend_domain_never_loads_entrypoint(self):
        root=self.temporary()
        metadata=root/'some-1.dist-info'
        metadata.mkdir()
        point=metadata/'entry_points.txt'
        point.write_text('[other]\noperation = never_import:operation\n')
        self.assertEqual(torch_backend_domain([root])['callbacks'],[])
        point.write_text('[torch.backends]\noperation = never_import:operation\n')
        with self.assertRaisesRegex(CallUnknown,'callback'): torch_backend_domain([root])
        self.assertNotIn('never_import',sys.modules)

    def test_original_transformers_migration_not_bypassed_by_offline_flag(self):
        root=self.temporary()
        cache=root/'hub'
        environment={'HOME':str(root),'USERPROFILE':str(root),'HF_HOME':str(root),
                     'HF_HUB_CACHE':str(cache),'TRANSFORMERS_OFFLINE':'1'}
        with patch.dict('os.environ',environment,clear=True):
            facts=transformers_cache_domain()
            self.assertEqual(facts['version'],0)
            self.assertFalse(cache.exists())
            cache.mkdir()
            (cache/'original-model.bin').write_bytes(b'x')
            with self.assertRaisesRegex(CallUnknown,'migration'): transformers_cache_domain()
            (cache/'version.txt').write_text('1')
            self.assertEqual(transformers_cache_domain()['original_migration'],
                             'not selected by original predicates')

    def test_untrusted_context_and_status_cannot_grant_actual_import(self):
        sdk=lambda:{name:module for name,module in sys.modules.items()
            if name.split('.')[0] in {'nltk','numpy','torch','transformers','tqdm'}}
        before=sdk()
        result=verify_before_sdk_import([dict(schema=RULE,status='CHECKED')],None,object(),
            entry={},unit={},plan={})
        self.assertFalse(result['allowed'])
        self.assertEqual(result['lifecycle_permission'],'NOT_GRANTED')
        self.assertEqual(sdk(),before)


if __name__=='__main__': unittest.main()
