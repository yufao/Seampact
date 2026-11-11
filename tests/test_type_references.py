"""Source type-position facts do not become runtime or SDK permissions."""

import ast
import builtins
import copy
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from seampact.source.external_source_protocols import ExternalSourceProtocols
from seampact.source.dependency_closure import resolve_resource_calls, source_import_scope
from seampact.source.inspection import Inspector
from seampact.source.result_structure import derive_result_structure
from seampact.source.type_references import TypeReferences


class TypeReferenceChecks(unittest.TestCase):
    def freeze(self, files):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        for name, source in files.items():
            path = root/name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(source, encoding='utf-8')
        self.inspector = Inspector.freeze(root, root/'snapshots', 'types', files, 'entry', 'output')
        self.protocols = ExternalSourceProtocols(self.inspector)
        self.helper = self.protocols.type_references
        return self.inspector

    def fact(self, files, kind='isinstance', path='main.py'):
        self.freeze(files)
        tree = self.protocols.tree(path)
        node = next(n for n in ast.walk(tree) if isinstance(n, ast.Call) and
                    ((isinstance(n.func, ast.Name) and n.func.id == kind) or kind=='isinstance' and isinstance(n.func, ast.Attribute) and n.func.attr=='isinstance')) if kind!='except' else next(n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler))
        return self.helper.check(path,node,node.type if kind=='except' else node.args[1])

    def test_unique_pure_frozen_class_is_source_checked_only(self):
        fact = self.fact({'sdk.py':'class Marker:\n    pass\n',
                          'main.py':'from sdk import Marker\ndef host():\n    return isinstance(None, Marker)\n'})
        self.assertEqual(fact['status'],'SOURCE_CHECKED')
        self.assertEqual(fact['metaclass'],'SOURCE_PROVED_BUILTIN_TYPE')
        self.assertEqual(fact['activation_status'],'UNKNOWN')
        self.assertEqual(fact['target_state'],'UNKNOWN')
        self.assertFalse(fact['boolean_guard_permission'])
        self.assertFalse(self.protocols.unmodified('sdk.Marker'))
        self.assertEqual(self.protocols.type_reference_facts[0]['status'],'SOURCE_CHECKED')

    def test_local_pure_class_binding_has_the_same_limited_positive_fact(self):
        fact = self.fact({'main.py':'class Marker:\n    pass\ndef host():\n    return isinstance(None, Marker)\n'})
        self.assertEqual(fact['status'],'SOURCE_CHECKED')
        self.assertIn('original builtin class builder and bases',fact['source_profile_preconditions'])

    def test_pure_source_exception_reference_does_not_check_handler_activation(self):
        fact=self.fact({'main.py':'class Error(Exception):\n    pass\ndef host():\n    try:\n        raise Error()\n    except Error:\n        pass\n'},'except')
        self.assertEqual(fact['status'],'SOURCE_CHECKED')
        self.assertEqual(fact['handler_effects'],'UNKNOWN')
        self.assertEqual(fact['activation_status'],'UNKNOWN')
        self.assertFalse(self.protocols.unmodified('main.Error'))

    def test_shadowed_builtin_and_source_builtin_replacement_are_unknown(self):
        for prefix,signature in [('', 'isinstance'), ('isinstance = unknown\n',''),
                                 ('import builtins\nbuiltins.isinstance = unknown\n','')]:
            with self.subTest(prefix=prefix,signature=signature):
                fact=self.fact({'main.py':prefix+'class Marker:\n    pass\ndef host('+signature+'):\n    return isinstance(None, Marker)\n'})
                self.assertEqual(fact['status'],'UNKNOWN')

    def test_unparseable_frozen_fragment_prevents_full_scope_proof(self):
        self.freeze({'main.py': 'class Marker:\n    pass\n',
                     'elsewhere/fragment.py': '    def fragment(self):\n        pass\n'})
        self.assertFalse(self.helper.builtins_closed())
        self.assertEqual(self.helper.source_scope_gaps, [dict(path='elsewhere/fragment.py',
            status='UNKNOWN', reason='frozen_source_not_parseable_or_available', line=1)])
        self.assertFalse(self.helper.builtins_closed())
        self.assertEqual(len(self.helper.source_scope_gaps), 1)

    def test_source_scan_is_reused_but_loaded_builtin_is_rechecked(self):
        self.freeze({'main.py': 'class Marker:\n    pass\n'})
        with patch.object(self.helper, '_aliases', wraps=self.helper._aliases) as scan:
            self.assertTrue(self.helper.builtins_closed())
            first = scan.call_count
            self.assertTrue(self.helper.builtins_closed())
            self.assertEqual(scan.call_count, first)
            with patch.object(builtins, '__build_class__', lambda *args, **kwargs: None):
                self.assertFalse(self.helper.builtins_closed())
            self.assertTrue(self.helper.builtins_closed())
            self.assertEqual(scan.call_count, first)

    def test_list_comprehension_preserves_outer_name_resolution(self):
        self.freeze({'main.py': 'def host(values):\n    return [isinstance(value, str) for value in values]\n'})
        tree = self.protocols.tree('main.py')
        node = next(n for n in ast.walk(tree) if isinstance(n, ast.Call))
        self.assertEqual(self.helper.resolve('main.py', node.func, node), 'builtins.isinstance')
        self.assertEqual(self.helper.resolve('main.py', node.args[1], node), 'builtins.str')

    def test_list_comprehension_shadow_and_unknown_binding_remain_unknown(self):
        for signature, expression in [
            ('values, isinstance', '[isinstance(value, str) for value in values]'),
            ('values', '[isinstance(value, str) for isinstance in values for value in values]'),
            ('values', '[isinstance(value, str) for str in values for value in values]'),
            ('values', '[isinstance(value, str) for value in values if (other := value)]'),
        ]:
            with self.subTest(signature=signature, expression=expression):
                self.freeze({'main.py': 'def host('+signature+'):\n    return '+expression+'\n'})
                tree = self.protocols.tree('main.py')
                node = next(n for n in ast.walk(tree) if isinstance(n, ast.Call))
                actual = self.helper.resolve('main.py', node.args[1] if 'for str ' in expression else node.func, node)
                self.assertIsNone(actual)

    def test_class_body_comprehension_has_no_assumed_class_closure(self):
        for source in [
            'isinstance = opaque\nclass Holder:\n    from builtins import isinstance\n    flags = [isinstance(value, str) for value in values]\n',
            'class Holder:\n    isinstance = opaque\n    flags = [value for value in isinstance(values, str)]\n',
        ]:
            with self.subTest(source=source):
                self.freeze({'main.py': source})
                tree = self.protocols.tree('main.py')
                node = next(n for n in ast.walk(tree) if isinstance(n, ast.Call))
                self.assertIsNone(self.helper.resolve('main.py', node.func, node))

    def test_class_rebinding_and_class_escape_are_unknown(self):
        for statement in ['Marker = unknown','opaque(Marker)','setattr(Marker, "state", unknown)',
                          'globals()["Marker"] = unknown','opaque()']:
            with self.subTest(statement=statement):
                fact=self.fact({'main.py':'class Marker:\n    pass\ndef host():\n    '+statement+'\n    return isinstance(None, Marker)\n'})
                self.assertEqual(fact['status'],'UNKNOWN')

    def test_builder_mutation_dynamic_code_and_opaque_prefix_are_unknown_for_except(self):
        for prefix in ['import builtins\nbuiltins.__build_class__ = attacker\n',
                       'exec("import builtins; builtins.__build_class__ = attacker")\n',
                       'eval("attacker()")\n','import helper\nhelper.patch()\n']:
            with self.subTest(prefix=prefix):
                fact=self.fact({'main.py':prefix+'class Error(Exception):\n    pass\ndef host():\n    try:\n        pass\n    except Error:\n        pass\n'},'except')
                self.assertEqual(fact['status'],'UNKNOWN')

    def test_parent_package_initializer_and_missing_parent_are_unknown(self):
        leaf='class Marker(Exception):\n    pass\n'
        main='from pkg.leaf import Marker\ndef host():\n    try:\n        pass\n    except Marker:\n        pass\n'
        for init in [None,'exec("opaque()")\n','import helper\nhelper.patch()\n']:
            with self.subTest(init=init):
                files={'main.py':main,'pkg/leaf.py':leaf}
                if init is not None:files['pkg/__init__.py']=init
                self.assertEqual(self.fact(files,'except')['status'],'UNKNOWN')
        self.assertEqual(self.fact({'main.py':main,'pkg/leaf.py':leaf,'pkg/__init__.py':''},'except')['status'],'SOURCE_CHECKED')

    def test_definition_time_decorators_defaults_and_annotations_are_not_pure(self):
        for definition in ['@opaque\ndef other():\n    pass\n','def other(value=opaque()):\n    pass\n',
                           'def other(value: opaque()):\n    pass\n','def other() -> opaque():\n    pass\n']:
            with self.subTest(definition=definition):
                self.assertEqual(self.fact({'main.py':definition+'class Marker:\n    pass\ndef host():\n    return isinstance(None, Marker)\n'})['status'],'UNKNOWN')

    def test_custom_metaclass_and_instancecheck_source_are_unknown_without_execution(self):
        source='class Meta(type):\n    def __instancecheck__(cls, value):\n        raise AssertionError("never execute")\nclass Marker(metaclass=Meta):\n    pass\ndef host():\n    return isinstance(None, Marker)\n'
        self.assertEqual(self.fact({'main.py':source})['status'],'UNKNOWN')

    def test_loaded_sdk_name_and_source_are_not_original_object_identity(self):
        for qualified in ['pathlib.Path','yaml.YAMLError']:
            with self.subTest(qualified=qualified):
                module,leaf=qualified.rsplit('.',1)
                kind='except' if leaf=='YAMLError' else 'isinstance'
                use='try:\n        pass\n    except '+leaf+':\n        pass' if kind=='except' else 'return isinstance(None, '+leaf+')'
                fact=self.fact({'main.py':'from '+module+' import '+leaf+'\ndef host():\n    '+use+'\n'},kind)
                self.assertEqual(fact['status'],'UNKNOWN')
                self.assertEqual(fact['external_class_conditions']['original_class_identity'],'UNKNOWN')
                self.assertEqual(fact['external_class_conditions']['target_state'],'UNKNOWN')

    def test_probe_does_not_import_an_unloaded_sdk(self):
        self.assertNotIn('not_loaded_type_fixture',sys.modules)
        fact=TypeReferences.loaded('not_loaded_type_fixture.Marker')
        self.assertEqual(fact['status'],'OBSERVATION_ONLY')
        self.assertEqual(fact['loaded_binding'],'NOT_OBSERVED')
        self.assertNotIn('not_loaded_type_fixture',sys.modules)

    def test_class_metadata_descriptors_and_custom_meta_are_never_called(self):
        calls=[]
        class Descriptor:
            def __get__(self,*args):
                calls.append('descriptor');return 'private value'
        class Trap:
            __module__=Descriptor()
        module=types.ModuleType('type_probe_fixture');module.Trap=Trap
        with patch.dict(sys.modules,{'type_probe_fixture':module}):
            fact=TypeReferences.loaded('type_probe_fixture.Trap')
        self.assertEqual(calls,[])
        self.assertEqual(fact['status'],'OBSERVATION_ONLY')
        self.assertNotIn('private value',json.dumps(fact))
        class Meta(type):
            def __getattribute__(self,key):
                calls.append('meta');raise AssertionError('must not run')
        class Exotic(metaclass=Meta):pass
        module.Exotic=Exotic
        with patch.dict(sys.modules,{'type_probe_fixture':module}):
            fact=TypeReferences.loaded('type_probe_fixture.Exotic')
        self.assertEqual(calls,[])
        self.assertEqual(fact['loaded_binding'],'NOT_AN_EXACT_ORDINARY_CLASS')

    def test_module_subclass_and_untrusted_file_metadata_are_not_used(self):
        calls=[]
        class Module(types.ModuleType):
            def __getattribute__(self,key):
                calls.append(key);raise AssertionError('must not run')
        module=Module('subclass_type_fixture')
        with patch.dict(sys.modules,{'subclass_type_fixture':module}):
            fact=TypeReferences.loaded('subclass_type_fixture.Marker')
        self.assertEqual(calls,[])
        self.assertEqual(fact['loaded_binding'],'NOT_OBSERVED')
        Marker=type('Marker',(),{'__module__':__name__})
        module=types.ModuleType('file_type_fixture');module.Marker=Marker;module.__file__='/untrusted/fifo.py'
        with patch.dict(sys.modules,{'file_type_fixture':module}),patch.object(Path,'read_bytes',side_effect=AssertionError('no file reads')):
            fact=TypeReferences.loaded('file_type_fixture.Marker')
        self.assertEqual(fact['status'],'OBSERVATION_ONLY')
        self.assertNotIn('/untrusted',json.dumps(fact))
        with patch.dict(sys.modules,{'file_type_fixture':module}),patch.object(types,'ModuleType',Module),patch.object(types,'MappingProxyType',Module):
            fact=TypeReferences.loaded('file_type_fixture.Marker')
        self.assertEqual(fact['loaded_binding'],'OBSERVED_ORDINARY_CLASS_OBJECT')
        class Modules(dict):
            def get(self,*args):
                calls.append('mapping');raise AssertionError('must not run')
        original=sys.modules
        try:
            sys.modules=Modules(original)
            fact=TypeReferences.loaded('file_type_fixture.Marker')
        finally:
            sys.modules=original
        self.assertEqual(calls,[])
        self.assertEqual(fact['loaded_binding'],'NOT_OBSERVED')

    def test_ordinary_non_exception_class_is_not_exception_matching_proof(self):
        Plain=type('Plain',(),{'__module__':__name__})
        module=types.ModuleType('exception_type_fixture');module.Plain=Plain
        with patch.dict(sys.modules,{'exception_type_fixture':module}):
            fact=TypeReferences.loaded('exception_type_fixture.Plain',exception=True)
        self.assertEqual(fact['status'],'OBSERVATION_ONLY')
        self.assertEqual(fact['loaded_binding'],'OBSERVED_NON_EXCEPTION_CLASS')

    def test_source_checked_type_site_does_not_grant_negative_or_return_evidence(self):
        ins=self.freeze({'sdk.py':'class Marker:\n    pass\n','main.py':'from sdk import Marker\ndef host():\n    assert isinstance(None, Marker)\n    return 1\n'})
        entry=dict(id='host',kind='function',path='main.py',symbol='host')
        trace=resolve_resource_calls(ins,entry)
        self.assertTrue(any(f['status']=='SOURCE_CHECKED' for f in trace['type_reference_facts']))
        self.assertEqual(trace['negative_scope_status'],'UNKNOWN')
        self.assertEqual(source_import_scope(ins,entry)['status'],'UNKNOWN')
        self.assertEqual(derive_result_structure(ins,entry)['status'],'UNKNOWN')

    def test_handler_factory_and_handler_body_effects_are_still_traced(self):
        source='def choose():\n    return missing_type()\ndef handle(value):\n    return missing_handler(value)\ndef host():\n    try:\n        pass\n    except choose() as error:\n        handle(error)\n'
        ins=self.freeze({'main.py':source})
        entry=dict(id='host',kind='function',path='main.py',symbol='host')
        trace=resolve_resource_calls(ins,entry)
        targets={u['target'] for u in trace['unresolved_targets']}
        self.assertIn('missing_type',targets)
        self.assertIn('missing_handler',targets)
        self.assertEqual(trace['negative_scope_status'],'UNKNOWN')
        source=source.replace('def choose():\n    return missing_type()',
                              'import consumer\ndef choose():\n    return consumer.factory()')
        ins=self.freeze({'main.py':source})
        trace=resolve_resource_calls(ins,entry)
        factory=next(c for c in trace['calls'] if c['target']=='consumer.factory')
        self.assertEqual(factory['path_condition_status'],'UNKNOWN')

    def test_invalid_source_and_uncovered_except_shapes_do_not_escape_boolean_guard(self):
        self.freeze({'torch.py':'def broken(:\n'})
        self.assertFalse(self.protocols.unmodified('torch.no_grad'))
        self.assertFalse(self.protocols.unmodified('torch.no_grad'))
        variants=[('class Error(Exception):\n    pass\nclass Other(Exception):\n    pass\n','(Error, Other)'),
                  ('class Error(Exception):\n    def __init__(self):\n        pass\n','Error'),
                  ('class Meta(type):\n    pass\nclass Error(Exception, metaclass=Meta):\n    pass\n','Error'),
                  ('class Error(Exception):\n    pass\n','Error()')]
        for definition,reference in variants:
            with self.subTest(reference=reference,definition=definition):
                self.freeze({'main.py':definition+'def host():\n    try:\n        pass\n    except '+reference+':\n        pass\n'})
                self.assertFalse(self.protocols.unmodified('main.Error'))
                self.assertTrue(self.protocols.type_reference_facts)
                self.assertTrue(all(f['status']=='UNKNOWN' for f in self.protocols.type_reference_facts))
        self.freeze({'bad.py':'def broken(:\n','main.py':'from bad import Marker\ndef host():\n    return isinstance(None, Marker)\n'})
        tree=self.protocols.tree('main.py');call=next(n for n in ast.walk(tree) if isinstance(n,ast.Call))
        self.assertEqual(self.helper.check('main.py',call,call.args[1])['status'],'UNKNOWN')

    def test_normal_install_identity_separates_type_and_yaml_process_observations(self):
        from seampact.hosting.deployment import binding_fingerprint, worker_spec
        from seampact.source.frozen_configuration import READER_FILES
        from seampact.store import Store
        from seampact.typed_contract import build_contract, verify_contract
        from seampact.units import UnitCatalog
        source=('from fractions import Fraction\nfrom pathlib import Path\nimport yaml\n'
                'def type_entry(value: str) -> str:\n    isinstance(value, Fraction)\n    return value\n'
                'def config_entry(value: str) -> str:\n'
                '    with open(Path(__file__).parent / "settings.yaml") as stream:\n'
                '        config = yaml.safe_load(stream)\n    return value\n')
        frozen=self.freeze({'main.py':source,'settings.yaml':'engine: ordinary\n'})
        frozen.request['source_id']='source'
        frozen.request['configuration']=dict(ordinary_data=dict(rule='source-finite-type-reference/1',
            check_process_observation=dict(loaded='literal program data'),reader_identity=dict(loaded='literal configuration')))
        candidate=dict(entries=[dict(id=name,path='main.py',symbol=name,kind='function',boundary_ids=[])
                                for name in ('type_entry','config_entry')],boundaries=[])
        results=[]
        for loaded in (False,True):
            inspector=Inspector(frozen.root,frozen.snapshots,copy.deepcopy(frozen.request))
            registry=Store(':memory:');self.addCleanup(registry.close)
            catalog=UnitCatalog(frozen.root,registry,None)
            observation=dict(status='OBSERVATION_ONLY',loaded_binding='OBSERVED_ORDINARY_CLASS_OBJECT' if loaded else 'NOT_OBSERVED',
                             installed_source='NOT_VERIFIED_NO_AUTHENTICATED_SDK_ORIGIN' if loaded else 'UNKNOWN',
                             metaclass='OBSERVED_EXACT_BUILTIN_TYPE' if loaded else 'UNKNOWN')
            reader=dict(required_reader='locked reader',required_files=dict(READER_FILES),installed_source='MATCHED',
                        loaded='PRESENT_UNVERIFIED' if loaded else 'NOT_LOADED',runtime_tables='UNKNOWN',
                        target_runtime='NOT_PROVED',observed_files=dict(READER_FILES))
            with patch.object(TypeReferences,'loaded',return_value=observation),patch(
                    'seampact.source.dependency_closure.reader_identity',return_value=reader):
                units=catalog.install(inspector,candidate,'analysis','installation')
            by_id={unit['entry_id']:unit for unit in units}
            active=by_id['type_entry'];withheld=by_id['config_entry']
            self.assertEqual(withheld['status'],'MISSING')
            self.assertFalse(any(v['allowed'] for v in withheld['typed_contract']['operations'].values()))
            group=registry.get('runtime_group',active['runtime_group'])
            self.assertNotIn('config_entry',group['contract']['root_entries'])
            binding=catalog.bindings.get(active['id']) or catalog.deferred[active['id']][0]
            spec=worker_spec(binding,group,active)
            binding.inspector.request['configuration']['ordinary_data']['check_process_observation']['loaded']='changed ordinary data'
            evidence=catalog._binding_evidence(binding,active,full_source_check=True)
            self.assertFalse(verify_contract(active,group=group,evidence=evidence)['valid'])
            binding.inspector.request['configuration']['ordinary_data']['check_process_observation']['loaded']='literal program data'
            results.append(dict(units=by_id,group=group,fingerprint=binding_fingerprint(spec)))
        first,second=results
        self.assertNotEqual(first['units']['type_entry']['dependency_closure'],second['units']['type_entry']['dependency_closure'])
        self.assertNotEqual(first['units']['config_entry']['dependency_closure'],second['units']['config_entry']['dependency_closure'])
        self.assertEqual(first['group']['contract'],second['group']['contract'])
        self.assertEqual(first['fingerprint'],second['fingerprint'])
        for name in ('type_entry','config_entry'):
            self.assertEqual(first['units'][name]['typed_contract'],second['units'][name]['typed_contract'])
            self.assertTrue(verify_contract(second['units'][name],group=second['group'])['valid'])
        changed=copy.deepcopy(second['units']['config_entry'])
        changed['dependency_closure']['configuration_conditions'][0]['reader_identity']['installed_source']='MISMATCH'
        self.assertFalse(verify_contract(changed,group=second['group'])['valid'])
        changed=copy.deepcopy(second['units']['config_entry'])
        changed['dependency_closure']['configuration_conditions'][0]['reader_identity']['required_files']['reader.py']='changed'
        self.assertFalse(verify_contract(changed,group=second['group'])['valid'])
        changed=copy.deepcopy(second['units']['type_entry'])
        changed['dependency_closure']['type_reference_facts'][0]['status']='SOURCE_CHECKED'
        self.assertFalse(verify_contract(changed,group=second['group'])['valid'])
        for field in ('status','original_class_identity','target_state'):
            changed=copy.deepcopy(second['units']['type_entry'])
            changed['dependency_closure']['type_reference_facts'][0]['external_class_conditions'][field]='changed'
            self.assertFalse(verify_contract(changed,group=second['group'])['valid'])
        for mutation in ('change','remove'):
            changed=copy.deepcopy(second['units']['type_entry'])
            condition=changed['dependency_closure']['type_reference_facts'][0]['external_class_conditions']['conditional_non_escape']
            if mutation=='change':condition['required']=['different premise']
            else:condition.pop('required')
            self.assertFalse(verify_contract(changed,group=second['group'])['valid'])
        changed=copy.deepcopy(second['units']['type_entry'])
        changed['dependency_closure']['type_reference_facts'][0]['check_process_observation']['sdk_class']['new_unknown_field']='retained'
        self.assertFalse(verify_contract(changed,group=second['group'])['valid'])


if __name__=='__main__':unittest.main()
