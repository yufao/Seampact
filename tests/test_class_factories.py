"""Finite class facts never become target execution or construction permits."""

import ast
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from seampact.source.class_factories import finite_class_graph, literal_class_factory
from seampact.source.frozen_inheritance import FrozenInheritance, InheritanceIssue
from seampact.source.inspection import Inspector


class ClassFactoryFactsTests(unittest.TestCase):
    def freeze(self, source, extras=None):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        files = {'main.py': source, **(extras or {})}
        for name, text in files.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding='utf-8')
        self.inspector = Inspector.freeze(root, root / 'snapshots', 'classes', files, 'entry', 'result')
        return self.inspector

    def graph(self, source, symbol='Child'):
        return finite_class_graph(self.freeze(source), 'main.py', symbol)

    def factory(self, source, *, selection_key='a', filter_attribute='available'):
        inspector = self.freeze(source)
        node = next(node.value for node in ast.parse(source).body
                    if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'choices' for t in node.targets))
        return literal_class_factory(inspector, 'main.py', node, 'name',
                                     filter_attribute=filter_attribute, selection_key=selection_key)

    def test_local_diamond_uses_c3_and_keeps_construction_separate(self):
        fact = self.graph('class Root:\n    def __init__(self):\n        original_effect()\n'
                          'class Left(Root):\n    pass\nclass Right(Root):\n    pass\n'
                          'class Child(Left, Right):\n    pass\n')
        self.assertEqual(fact['source_status'], 'SOURCE_CHECKED')
        self.assertEqual(fact['status'], 'CONDITIONAL')
        self.assertEqual(fact['mro'], ['main.py:Child', 'main.py:Left', 'main.py:Right', 'main.py:Root', 'builtins.object'])
        self.assertEqual(fact['construction_effects'][0]['status'], 'UNKNOWN')
        self.assertFalse(fact['execution_permission'])
        self.assertEqual(fact['target_status'], 'UNKNOWN')

    def test_abc_is_a_conditional_original_protocol_endpoint(self):
        fact = self.graph('from abc import ABC, abstractmethod\nclass Root(ABC):\n'
                          '    @abstractmethod\n    def run(self):\n        pass\n'
                          'class Child(Root):\n    def run(self):\n        return 1\n')
        self.assertEqual(fact['status'], 'CONDITIONAL')
        self.assertEqual(fact['mro'], ['main.py:Child', 'main.py:Root', 'abc.ABC', 'builtins.object'])
        endpoint = fact['external_endpoints'][0]
        self.assertEqual(endpoint['metaclass'], 'abc.ABCMeta')
        self.assertEqual(endpoint['status'], 'CONDITIONAL')
        self.assertTrue(any('abstract' in requirement for requirement in endpoint['target_requirements']))
        with self.assertRaises(InheritanceIssue) as rejected:
            FrozenInheritance(self.inspector).resolve('main.py', 'Child.run')
        self.assertEqual(rejected.exception.facts['status'], 'CONDITIONAL')
        self.assertFalse(rejected.exception.facts['execution_permission'])

    def test_custom_hooks_metaclasses_descriptors_and_dynamic_bases_stay_unknown(self):
        cases = [
            'class Child:\n    def __new__(cls):\n        return opaque()\n',
            'class Child(metaclass=Unknown):\n    pass\n',
            'class Child:\n    name = property(opaque)\n',
            'class Child:\n    @property\n    def name(self):\n        return "a"\n',
            'class Child(make_base()):\n    pass\n',
        ]
        for source in cases:
            with self.subTest(source=source):
                fact = self.graph(source)
                self.assertEqual(fact['status'], 'UNKNOWN')
                self.assertTrue(fact['unknowns'])
                self.assertFalse(fact['execution_permission'])

    def test_external_non_abc_base_and_dynamic_registry_stay_unknown(self):
        for source in ['from pydantic import BaseModel\nclass Child(BaseModel):\n    pass\n',
                       'class Root:\n    pass\nclass Child(Root.__subclasses__()[0]):\n    pass\n']:
            with self.subTest(source=source):
                self.assertEqual(self.graph(source)['status'], 'UNKNOWN')

    def test_original_builtin_method_descriptors_are_conditional_not_executed(self):
        source = ('class Child:\n    @staticmethod\n    def first():\n        return opaque()\n'
                  '    @classmethod\n    def second(cls):\n        return opaque()\n')
        fact = self.graph(source)
        self.assertEqual(fact['status'], 'CONDITIONAL')
        self.assertFalse(fact['execution_permission'])
        for prefix in ['staticmethod = unknown\n', 'from unknown import staticmethod\n']:
            with self.subTest(prefix=prefix):
                self.assertEqual(self.graph(prefix + source)['status'], 'UNKNOWN')
        self.assertEqual(self.graph(source.replace('class Child:\n', 'class Child:\n    staticmethod = unknown\n'))['status'], 'UNKNOWN')
        self.assertEqual(self.graph(source.replace('class Child:\n', 'class Child:\n    staticmethod = 1\n'))['status'], 'UNKNOWN')

    def test_inconsistent_c3_and_class_rewrites_stay_unknown(self):
        cases = [
            'class A:\n    pass\nclass B:\n    pass\nclass X(A,B):\n    pass\n'
            'class Y(B,A):\n    pass\nclass Child(X,Y):\n    pass\n',
            'class Child:\n    pass\nChild.key = replacement\n',
        ]
        for source in cases:
            with self.subTest(source=source):
                self.assertEqual(self.graph(source)['status'], 'UNKNOWN')

    def test_literal_factory_keeps_original_members_and_filter_order(self):
        result = self.factory('class A:\n    name="a"\n    available=True\n'
                              'class B:\n    name="b"\n    available=False\nchoices=(A,B)\n')
        self.assertEqual(result['status'], 'CONDITIONAL')
        self.assertEqual([row['identity'] for row in result['ordered_members']], ['main.py:A', 'main.py:B'])
        self.assertEqual(result['mapping'], [dict(key='a', identity='main.py:A')])
        self.assertEqual(result['selected'], 'main.py:A')
        self.assertEqual(result['construction_status'], 'UNKNOWN')
        self.assertFalse(result['execution_permission'])

    def test_inherited_literal_attributes_follow_the_checked_mro(self):
        result = self.factory('class Base:\n    name="a"\n    available=True\nclass A(Base):\n    pass\nchoices=[A]\n')
        self.assertEqual(result['status'], 'CONDITIONAL')
        self.assertEqual(result['selected'], 'main.py:A')

    def test_factory_attributes_follow_final_class_bindings_and_method_shadowing(self):
        source = ('class A:\n    name="a"\n    available=True\n    def name(self):\n        return "b"\n'
                  'choices=(A,)\n')
        self.assertEqual(self.factory(source)['status'], 'UNKNOWN')
        inherited = ('class Base:\n    name="a"\n    available=True\nclass A(Base):\n'
                     '    def name(self):\n        return "b"\nchoices=(A,)\n')
        self.assertEqual(self.factory(inherited)['status'], 'UNKNOWN')
        overwritten = ('class A:\n    def name(self):\n        return "b"\n    name="a"\n'
                       '    available=True\nchoices=(A,)\n')
        self.assertEqual(self.factory(overwritten)['selected'], 'main.py:A')

    def test_computed_module_truth_filter_is_not_inferred_from_an_import(self):
        result = self.factory('import not_an_installed_fixture\nclass A:\n    name="a"\n'
                              '    available=bool(not_an_installed_fixture)\nchoices=(A,)\n')
        self.assertEqual(result['status'], 'UNKNOWN')
        self.assertIsNone(result['selected'])
        self.assertEqual(result['ordered_members'][0]['filter'], None)
        self.assertNotIn('not_an_installed_fixture', sys.modules)
        source = ('import not_an_installed_fixture\nclass A:\n    name="a"\n    available=bool(not_an_installed_fixture)\n'
                  'class B:\n    name="b"\n    available=True\nchoices=(A,B)\n')
        mixed = self.factory(source, selection_key='b')
        self.assertEqual(mixed['status'], 'UNKNOWN')
        self.assertIsNone(mixed['selected'])

    def test_missing_actual_key_and_duplicate_keys_do_not_gain_selection(self):
        source = 'class A:\n    name="a"\n    available=True\nchoices=(A,)\n'
        missing = self.factory(source, selection_key='missing')
        self.assertEqual(missing['selection_status'], 'CONDITIONAL_LOOKUP_FAILURE')
        self.assertIsNone(missing['selected'])
        duplicate = self.factory(source.replace('choices=(A,)', 'class B(A):\n    pass\nchoices=(A,B)'))
        self.assertEqual(duplicate['status'], 'UNKNOWN')

    def test_forged_or_dynamic_collections_have_no_original_finite_set(self):
        inspector = self.freeze('class A:\n    name="a"\nchoices=(A,)\n')
        forged = ast.parse('(A,A)', mode='eval').body
        self.assertEqual(literal_class_factory(inspector, 'main.py', forged, 'name')['status'], 'UNKNOWN')
        self.assertEqual(literal_class_factory(inspector, 'main.py', None, 'name')['status'], 'UNKNOWN')

    def test_function_local_class_shadow_is_not_resolved_as_the_global_class(self):
        source = 'class A:\n    name="a"\ndef make(A):\n    return (A,)\n'
        inspector = self.freeze(source)
        node = next(node for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Tuple))
        result = literal_class_factory(inspector, 'main.py', node, 'name', selection_key='a')
        self.assertEqual(result['status'], 'UNKNOWN')
        source = ('class A:\n    name="global"\nclass Owner:\n    class A:\n        name="nested"\n'
                  '    choices=(A,)\n')
        inspector = self.freeze(source)
        node = next(node for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Tuple))
        self.assertEqual(literal_class_factory(inspector, 'main.py', node, 'name')['status'], 'UNKNOWN')
        source = 'class A:\n    name="global"\nchoices=(A,); factory=lambda A:(A,)\n'
        inspector = self.freeze(source)
        node = next(node.body for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Lambda))
        self.assertEqual(literal_class_factory(inspector, 'main.py', node, 'name')['status'], 'UNKNOWN')


if __name__ == '__main__':
    unittest.main()
