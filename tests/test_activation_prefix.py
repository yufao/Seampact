"""Frozen parent/definition activation is covered without importing source."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from seampact.source.call_value_conditions import call_context, contextual, compile_condition
from seampact.source.dependency_closure import resolve_resource_calls
from seampact.source.inspection import Inspector


class ActivationPrefixTests(unittest.TestCase):
    def fixture(self, files, path='original.py', symbol='score', kind='function'):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        for name, text in files.items():
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding='utf-8')
        inspector = Inspector.freeze(root, root / 'snapshots', 'request', list(files), 'score', 'result')
        entry = dict(id='score', kind=kind, path=path, symbol=symbol)
        if kind == 'method': entry['constructor'] = dict(symbol=symbol.split('.')[0], arguments={})
        candidate = dict(entries=[entry], boundaries=[])
        return inspector, entry, candidate

    def trace(self, files, path='original.py', **kwargs):
        inspector, entry, candidate = self.fixture(files, path, **kwargs)
        context, _ = call_context(dict(value=[]))
        return resolve_resource_calls(contextual(inspector, context), entry, candidate)

    def test_parent_package_runs_before_child_and_is_not_repeated(self):
        trace = self.trace({'pkg/__init__.py': 'import sdk\nsdk.parent()\n',
            'pkg/child.py': 'import sdk\nsdk.child()\ndef score(value):\n    return value\n'}, 'pkg/child.py')
        self.assertEqual([call['target'] for call in trace['calls']], ['sdk.parent','sdk.child'])
        prefix = trace['activation_prefix']
        self.assertEqual([event['path'] for event in prefix['events'] if event['kind'] == 'MODULE_BEGIN'],
                         ['pkg/__init__.py', 'pkg/child.py'])
        self.assertEqual(prefix['status'], 'UNKNOWN')

    def test_parent_importing_child_reuses_partial_parent_namespace(self):
        trace = self.trace({'pkg/__init__.py': 'from . import child\nimport sdk\nsdk.parent()\n',
            'pkg/child.py': 'import sdk\nsdk.child()\ndef score(value):\n    return value\n'}, 'pkg/child.py')
        self.assertEqual([call['target'] for call in trace['calls']], ['sdk.child','sdk.parent'])
        starts = [event['path'] for event in trace['activation_prefix']['events'] if event['kind'] == 'MODULE_BEGIN']
        self.assertEqual(starts, ['pkg/__init__.py', 'pkg/child.py'])

    def test_early_from_import_does_not_read_a_future_parent_assignment(self):
        trace = self.trace({'pkg/__init__.py': "from . import child\nTOKEN='ready'\n",
            'pkg/child.py': 'from . import TOKEN\ndef score(value):\n    return value\n'}, 'pkg/child.py')
        self.assertEqual(trace['activation_prefix']['status'], 'UNKNOWN')
        self.assertTrue(any(gap['kind'] == 'SOURCE_IMPORT_BINDING' for gap in trace['activation_prefix']['gaps']))

    def test_missing_export_does_not_execute_a_package_getattr_for_proof(self):
        trace = self.trace({'pkg/__init__.py': 'import sdk\ndef __getattr__(name):\n    sdk.dynamic()\n    return 1\n',
            'pkg/child.py': 'from . import missing\ndef score(value):\n    return value\n'}, 'pkg/child.py')
        self.assertTrue(any(gap['kind'] == 'MODULE_GETATTR' for gap in trace['activation_prefix']['gaps']))
        self.assertFalse(any(call['target'] == 'sdk.dynamic' for call in trace['calls']))

    def test_dotted_import_binds_original_parent_then_original_child(self):
        trace = self.trace({'pkg/__init__.py': '', 'pkg/child.py': "VALUE='ready'\n",
            'original.py': "import pkg.child\ndef score(value):\n    if pkg.child.VALUE == 'ready':\n        return 1\n    return value\n"})
        self.assertEqual(trace['activation_prefix']['status'], 'CHECKED', trace)
        self.assertTrue(any(item.get('expression') == "pkg.child.VALUE == 'ready'" and item['selected'] == 'body'
                            for item in trace['branch_evidence']))

    def test_parent_try_keeps_resource_find_and_download_as_unknown_original_effects(self):
        files = {'pkg/__init__.py': "import sdk\ntry:\n    sdk.find('resource')\nexcept LookupError:\n    sdk.download('resource')\n",
            'pkg/child.py': 'def score(value):\n    assert isinstance(value,list)\n    return value\n'}
        inspector, entry, candidate = self.fixture(files, 'pkg/child.py')
        recipe = compile_condition(inspector, entry, candidate)
        self.assertIsNotNone(recipe)
        self.assertEqual(recipe['initialization']['status'], 'UNKNOWN')
        trace = self.trace(files, 'pkg/child.py')
        self.assertEqual([call['target'] for call in trace['calls']], ['sdk.find', 'sdk.download'])
        self.assertTrue(all(call['path_condition_status'] == 'UNKNOWN' for call in trace['calls']))

    def test_eager_annotations_of_unused_functions_and_methods_are_original_prefix_work(self):
        trace = self.trace({'original.py': 'import sdk\ndef unused(value:sdk.function_annotation()):\n    pass\n'
            'class Unused:\n    def method(self,value:sdk.method_annotation()):\n        pass\n'
            'def score(value):\n    return value\n'})
        self.assertEqual([call['target'] for call in trace['calls']], ['sdk.function_annotation', 'sdk.method_annotation'])
        self.assertTrue(any(gap['kind'] == 'ANNOTATION_EAGER' for gap in trace['activation_prefix']['gaps']))

    def test_future_annotations_are_recorded_without_executing_the_expression(self):
        trace = self.trace({'original.py': 'from __future__ import annotations\nimport sdk\n'
            'def unused(value:sdk.not_called()):\n    pass\ndef score(value):\n    return value\n'})
        self.assertFalse(any(call['target'] == 'sdk.not_called' for call in trace['calls']))
        self.assertTrue(any(item['kind'] == 'ANNOTATION_DEFERRED' for item in trace['activation_prefix']['events']))

    def test_variable_annotation_observes_the_original_preceding_assignment(self):
        trace = self.trace({'original.py': 'import sdk\nnumber:sdk.annotation(number)=3\n'
            'class Unused:\n    number:sdk.class_annotation(number)=4\ndef score(value):\n    return value\n'})
        self.assertEqual([(call['target'],call['args'][0]) for call in trace['calls']],
            [('sdk.annotation',dict(status='CHECKED',value=3)),('sdk.class_annotation',dict(status='CHECKED',value=4))])

    def test_real_captured_abc_kernel_is_consumed_without_owner_construction(self):
        trace = self.trace({'original.py': 'from abc import ABC\nclass Checker(ABC):\n'
            '    def check(self,value:str)->str:\n        return value\n'}, symbol='Checker.check', kind='method')
        self.assertEqual(trace['activation_prefix']['status'], 'CHECKED', trace)
        imports = [event for event in trace['activation_prefix']['events'] if event['kind'] == 'EXTERNAL_IMPORT']
        self.assertEqual(imports[0]['kernel']['status'], 'CHECKED')
        self.assertFalse(imports[0]['kernel']['owner_construction'])

    def test_namespace_child_still_activates_its_original_outer_package(self):
        trace = self.trace({'pkg/__init__.py': 'import sdk\nsdk.parent()\n',
            'pkg/ns/child.py': 'def score(value):\n    return value\n'}, 'pkg/ns/child.py')
        self.assertEqual([call['target'] for call in trace['calls']], ['sdk.parent'])
        self.assertEqual(trace['activation_prefix']['status'], 'UNKNOWN')
        self.assertEqual([event['path'] for event in trace['activation_prefix']['events'] if event['kind'] == 'MODULE_BEGIN'],
                         ['pkg/__init__.py','pkg/ns/child.py'])

    def test_repository_root_initializer_is_not_an_invented_package_parent(self):
        trace = self.trace({'__init__.py': 'import sdk\nsdk.not_imported()\n',
            'original.py': 'def score(value):\n    return value\n'})
        self.assertEqual(trace['calls'], [])
        self.assertEqual(trace['activation_prefix']['status'], 'CHECKED')

    def test_non_name_annotation_evaluates_receiver_and_index_without_reading_target(self):
        for expression, targets in (
            ('sdk.receiver().attribute:int', ['sdk.receiver']),
            ('sdk.receiver()[sdk.index()]:int', ['sdk.receiver','sdk.index']),
            ('sdk.receiver()[sdk.index()]:sdk.annotation()=sdk.rhs()',
             ['sdk.rhs','sdk.receiver','sdk.index','sdk.annotation']),
        ):
            with self.subTest(expression=expression):
                trace = self.trace({'original.py':'import sdk\n'+expression+'\ndef score(value):\n    return value\n'})
                self.assertEqual([call['target'] for call in trace['calls']], targets)
                self.assertEqual(trace['activation_prefix']['status'], 'UNKNOWN')

    def test_function_local_annotation_target_evaluation_is_not_skipped(self):
        trace = self.trace({'original.py':'state=[]\ndef touch():\n    state.append(1)\n    return 0\n'
            'def score(value):\n    touch().attribute:int\n    if len(state)==0:\n        return 1\n'
            '    import sdk\n    sdk.danger()\n    return 1\n'})
        self.assertTrue(any(call['target'] == 'sdk.danger' for call in trace['calls']))

    def test_annotated_subscript_index_is_evaluated_once_after_rhs(self):
        trace = self.trace({'original.py':"state=[]\nstore={}\ndef index():\n    state.append(1)\n    return 'key'\n"
            'def score(value):\n    store[index()]:int=5\n    if len(state)==1:\n        return 1\n'
            '    import sdk\n    sdk.danger()\n    return 1\n'})
        self.assertFalse(any(call['target'] == 'sdk.danger' for call in trace['calls']))
        self.assertEqual(trace['negative_scope_status'], 'CHECKED', trace)

    def test_non_call_module_expressions_cannot_hide_original_setup_calls(self):
        for expression in ('[touch()]', '(touch(),)', "f'{touch()}'"):
            with self.subTest(expression=expression):
                trace = self.trace({'original.py':'state=[]\ndef touch():\n    state.append(1)\n    return 0\n'
                    +expression+'\ndef score(value):\n    return value\n'})
                self.assertEqual(trace['activation_prefix']['status'], 'UNKNOWN')
                self.assertTrue(any(gap['kind'] == 'MODULE_EXPRESSION' for gap in trace['activation_prefix']['gaps']))

    def test_direct_missing_module_attributes_and_external_stores_stay_unknown(self):
        trace = self.trace({'pkg/__init__.py':'def __getattr__(name):\n    import sdk\n    return sdk.model()\n',
            'original.py':'import pkg\nhidden=pkg.missing\ndef score(value):\n    return value\n'})
        self.assertTrue(any(gap['kind'] == 'MODULE_GETATTR' for gap in trace['activation_prefix']['gaps']))
        trace = self.trace({'original.py':'from abc import ABC\nABC.changed=1\ndef score(value):\n    return value\n'})
        self.assertTrue(any(gap['kind'] == 'SETUP_TARGET_STORE' for gap in trace['activation_prefix']['gaps']))


if __name__ == '__main__':
    unittest.main()
