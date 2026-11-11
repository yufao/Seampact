"""Matching source files cannot authenticate different loaded bytecode."""
import hashlib
import importlib.util
import marshal
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from types import ModuleType
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from seampact.source.call_value_conditions import CallUnknown
from seampact.source import sdk_code_identity as identity


class SDKPythonCodeIdentityTests(unittest.TestCase):
    def fixture(self,source):
        temporary=tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path=Path(temporary.name)/'sdk.py'
        path.write_bytes(source)
        class OriginalFile:
            def __str__(self): return 'sdk.py'
            def locate(self): return path
        distribution=SimpleNamespace(files=[OriginalFile()])
        requirement=dict(distribution='sdk',version='1',path='sdk.py',sha256=hashlib.sha256(source).hexdigest())
        namespace=dict(__name__='sdk')
        exec(compile(source,str(path),'exec',dont_inherit=True),namespace)
        module=ModuleType('sdk')
        module.__dict__.update(namespace)
        # Rebuild fixture functions against the actual module dictionary.
        exec(compile(source,str(path),'exec',dont_inherit=True),module.__dict__)
        original_modules=patch.dict(sys.modules,{'sdk':module})
        original_modules.start()
        self.addCleanup(original_modules.stop)
        namespace=module.__dict__
        return namespace,requirement,distribution

    def test_original_qualified_source_matches_without_importing_the_sdk(self):
        namespace,requirement,distribution=self.fixture(b'def original(value=1):\n    return value+1\n')
        with patch.object(identity,'sdk_identity',return_value=dict(status='CHECKED')),patch.object(identity.metadata,'distribution',return_value=distribution):
            result=identity.verify_function_source(namespace['original'],[requirement])
        self.assertEqual(result['status'],'CHECKED')

    def test_valid_source_bytes_do_not_authenticate_altered_loaded_cache_code(self):
        namespace,requirement,distribution=self.fixture(b'def original(value=1):\n    return value+1\n')
        changed=dict(__name__='sdk')
        exec(compile('def original(value=1):\n    return value-1\n','unchecked-cache.py','exec',dont_inherit=True),changed)
        namespace['original'].__code__=changed['original'].__code__
        with patch.object(identity,'sdk_identity',return_value=dict(status='CHECKED')),patch.object(identity.metadata,'distribution',return_value=distribution):
            with self.assertRaises(CallUnknown): identity.verify_function_source(namespace['original'],[requirement])

    def test_original_stdlib_context_wrapper_and_wrapped_sdk_body_both_match(self):
        source=b'from contextlib import contextmanager\n@contextmanager\ndef original():\n    yield 1\n'
        namespace,requirement,distribution=self.fixture(source)
        with patch.object(identity,'sdk_identity',return_value=dict(status='CHECKED')),patch.object(identity.metadata,'distribution',return_value=distribution):
            self.assertEqual(identity.verify_function_source(namespace['original'],[requirement])['status'],'CHECKED')

    def test_bytecode_constant_objects_cannot_execute_comparison_hooks(self):
        calls=[]
        class Evil:
            def __eq__(self,other): calls.append('eq'); return True
        def function(): return 1
        changed=function.__code__.replace(co_consts=(None,Evil()))
        with self.assertRaises(CallUnknown): identity.code_shape(changed)
        self.assertEqual(calls,[])

    def test_code_shape_keeps_names_paths_and_float_constant_bits(self):
        def original(): return -0.0
        code=original.__code__
        for field,value in [('co_name','changed'),('co_filename','foreign.py')]:
            with self.subTest(field=field):
                self.assertNotEqual(identity.code_shape(code),identity.code_shape(code.replace(**{field:value})))
        changed=code.replace(co_consts=tuple(0.0 if type(v) is float else v for v in code.co_consts))
        self.assertNotEqual(identity.code_shape(code),identity.code_shape(changed))

    def test_original_code_with_copied_foreign_globals_is_rejected(self):
        namespace,requirement,distribution=self.fixture(b'def original(value=1):\n    return value+1\n')
        from types import FunctionType
        forged=FunctionType(namespace['original'].__code__,dict(namespace),'original',(1,))
        with patch.object(identity,'sdk_identity',return_value=dict(status='CHECKED')),patch.object(identity.metadata,'distribution',return_value=distribution):
            with self.assertRaises(CallUnknown): identity.verify_function_source(forged,[requirement])

    def test_whole_module_cache_detects_initial_class_value_changes_with_same_method_code(self):
        source=b'class Source:\n    setting="original"\n    def original(self):\n        return 1\n'
        _,requirement,distribution=self.fixture(source)
        path=distribution.files[0].locate()
        cache=Path(importlib.util.cache_from_source(str(path)))
        cache.parent.mkdir(parents=True,exist_ok=True)
        malicious=compile(source.replace(b'"original"',b'"changed!"'),str(path),'exec',dont_inherit=True)
        cache.write_bytes(importlib.util.MAGIC_NUMBER+(1).to_bytes(4,'little')+
                          importlib.util.source_hash(source)+marshal.dumps(malicious))
        with patch.object(identity,'sdk_identity',return_value=dict(status='CHECKED')),patch.object(identity.metadata,'distribution',return_value=distribution):
            with self.assertRaises(CallUnknown): identity.verify_original_module_caches([requirement])
            original=compile(source,str(path),'exec',dont_inherit=True)
            cache.write_bytes(importlib.util.MAGIC_NUMBER+(1).to_bytes(4,'little')+
                              importlib.util.source_hash(source)+marshal.dumps(original))
            self.assertEqual(identity.verify_original_module_caches([requirement])['status'],'CHECKED')
        self.assertTrue(cache.exists())


if __name__=='__main__': unittest.main()
