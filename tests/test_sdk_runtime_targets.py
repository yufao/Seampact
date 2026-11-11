"""Actual target checks never obtain provenance from bytes or warm objects."""
import copy
import hashlib
from pathlib import Path
import sys
import tempfile
from types import ModuleType
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from seampact.source.call_value_conditions import CallUnknown,digest
from seampact.source.sdk_runtime_targets import (
    _function_snapshot,_verify_function,_resources,_class_state,verify_actual_target,
    _verify_enum_kernel,
    _lru_function,
)
from seampact.source.sdk_value_summaries import _NLTK,RULE


class ActualSDKTargetTests(unittest.TestCase):
    def requirement(self):
        return dict(schema=RULE,operation='nltk.sent_tokenize',summary=copy.deepcopy(_NLTK),
                    summary_digest=digest(_NLTK),input_status='CHECKED',execution_permission=False)

    def test_matching_name_and_summary_never_create_original_target_provenance(self):
        result=verify_actual_target(copy.deepcopy(_NLTK),self.requirement())
        self.assertFalse(result['allowed'])
        self.assertIn('original SDK import occurrence',result['remaining'][0])
        self.assertEqual(result['lifecycle_permission'],'NOT_GRANTED')

    def test_code_defaults_and_global_rebinding_are_independent_actual_checks(self):
        namespace=dict(__name__='synthetic_original',limit=3)
        exec('def original(value=1):\n    return value+limit\n',namespace)
        function=namespace['original']
        snapshot=_function_snapshot(function)
        _verify_function(snapshot)
        function.__defaults__=(2,)
        with self.assertRaises(CallUnknown): _verify_function(snapshot)
        function.__defaults__=(1,)
        namespace['limit']=4
        with self.assertRaises(CallUnknown): _verify_function(snapshot)
        namespace['limit']=3
        exec('def replacement(value=1):\n    return value-limit\n',namespace)
        function.__code__=namespace['replacement'].__code__
        with self.assertRaises(CallUnknown): _verify_function(snapshot)

    def test_resource_content_and_original_first_effective_root_are_checked_without_sdk(self):
        temporary=tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root=Path(temporary.name)/'data'
        language=root/'tokenizers/punkt_tab/english'
        language.mkdir(parents=True)
        rows=[]
        for name in ('collocations.tab','sent_starters.txt','abbrev_types.txt','ortho_context.tab'):
            path=language/name
            path.write_bytes(b'ordinary frozen model bytes\n')
            rows.append(dict(path=path.relative_to(root).as_posix(),sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
        module=ModuleType('nltk.data')
        module.path=[str(Path(temporary.name)/'absent'),str(root)]
        from seampact.source.sdk_assets import _tree
        import stat
        manifest=_tree(root)
        binding=dict(kind='directory',path=str(root),required_files=rows,full_tree_manifest=manifest,
                     manifest_digest=digest(manifest),root_identity=dict(mode=stat.S_IMODE(root.lstat().st_mode),
                         owner=getattr(root.lstat(),'st_uid',None)))
        with patch.dict(sys.modules,{'nltk.data':module}):
            self.assertEqual(_resources(binding)['status'],'CHECKED')
            (language/'collocations.tab').write_bytes(b'changed')
            with self.assertRaises(CallUnknown): _resources(binding)

    def test_non_native_requirement_values_do_not_execute_comparison_hooks(self):
        calls=[]
        class Evil(str):
            def __eq__(self,other): calls.append('eq'); return True
        requirement=self.requirement()
        requirement['summary']['id']=Evil('not-original')
        result=verify_actual_target(_NLTK,requirement)
        self.assertFalse(result['allowed'])
        self.assertEqual(calls,[])

    def test_in_place_global_and_external_module_attribute_changes_are_detected(self):
        module=ModuleType('synthetic-original-kernel')
        module.compile=lambda value:value
        namespace=dict(__name__='synthetic_original',options={'enabled':False},kernel=module)
        exec('def original(value):\n    return kernel.compile(value) if options["enabled"] else value\n',namespace)
        snapshot=_function_snapshot(namespace['original'])
        namespace['options']['enabled']=True
        with self.assertRaises(CallUnknown): _verify_function(snapshot)
        namespace['options']['enabled']=False
        module.compile=lambda value:None
        with self.assertRaises(CallUnknown): _verify_function(snapshot)

    def test_full_original_class_namespace_changes_are_detected(self):
        class Original:
            pattern='original'
            options={'case_sensitive':True}
            def original(self,value): return value
        snapshot=_class_state(Original)
        Original.pattern='changed'
        self.assertNotEqual(_class_state(Original),snapshot)
        Original.pattern='original'
        Original.options['case_sensitive']=False
        self.assertNotEqual(_class_state(Original),snapshot)
        Original.options['case_sensitive']=True
        Original.injected='new'
        self.assertNotEqual(_class_state(Original),snapshot)

    def test_original_flag_protocol_and_function_metadata_changes_are_rejected(self):
        import enum
        _verify_enum_kernel()
        with patch.object(enum.IntFlag,'__and__',lambda self,other:0):
            with self.assertRaises(CallUnknown): _verify_enum_kernel()
        def original(value): return value
        snapshot=_function_snapshot(original)
        original.__isabstractmethod__=True
        with self.assertRaises(CallUnknown): _verify_function(snapshot)

    def test_native_lru_actual_function_is_not_authenticated_from_wrapped_metadata(self):
        import functools
        def original(value): return value
        def other(value): return None
        wrapper=functools.lru_cache()(original)
        self.assertIs(_lru_function(wrapper),original)
        wrapper.__wrapped__=other
        with self.assertRaises(CallUnknown): _lru_function(wrapper)

    def test_copied_function_code_does_not_authenticate_custom_implicit_builtins(self):
        from types import FunctionType
        def original(value): return len(value)
        forged=FunctionType(original.__code__,dict(__name__='synthetic_original',__builtins__={'len':lambda value:0}))
        with self.assertRaises(CallUnknown): _function_snapshot(forged)

    def test_aborted_and_finished_proof_handles_drop_only_evidence_references(self):
        from seampact.source import sdk_runtime_targets as targets,sdk_native_kernels as kernels
        token=targets._OriginalPrefix()
        targets._PENDING[token]=dict(marker='source evidence')
        self.assertTrue(targets.abort_original_import(token))
        self.assertFalse(targets.abort_original_import(token))
        context=targets.RuntimeSDKContext()
        native=kernels.capture_native_kernels()
        targets._CONTEXTS[context]=dict(native_kernels=native)
        self.assertTrue(targets.forget_original_import(context))
        self.assertNotIn(context,targets._CONTEXTS)
        self.assertNotIn(native,kernels._CONTEXTS)
        self.assertFalse(targets.forget_original_import(context))

    def test_global_proof_registries_do_not_keep_original_binding_receipt_alive(self):
        import weakref,gc
        from seampact.source import sdk_runtime_targets as targets,sdk_native_kernels as kernels
        class Binding:
            pass
        binding=Binding()
        context=targets.RuntimeSDKContext()
        native=kernels.capture_native_kernels()
        binding.receipt=dict(context=context)
        targets._CONTEXTS[context]=dict(binding=weakref.ref(binding),native_kernels=native)
        owner_ref,context_ref,native_ref=weakref.ref(binding),weakref.ref(context),weakref.ref(native)
        del binding,context,native
        gc.collect()
        self.assertIsNone(owner_ref())
        self.assertIsNone(context_ref())
        self.assertIsNone(native_ref())


if __name__=='__main__':
    unittest.main()
