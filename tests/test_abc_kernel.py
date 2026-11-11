"""Actual narrow ABC source/target consumers, without owner construction."""

import abc
import ast
import builtins
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from seampact.source.abc_kernel import check_abc_kernel, check_abc_target
from seampact.source.dependency_closure import dependency_closure
from seampact.source.frozen_inheritance import FrozenInheritance, InheritanceIssue
from seampact.source.inspection import Inspector


SOURCE = 'from abc import ABC\nclass Checker(ABC):\n    label="original"\n    def check(self, value: str) -> str:\n        return value\n'


class ABCKernelChecks(unittest.TestCase):
    def freeze(self, source=SOURCE):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root / 'main.py').write_text(source, encoding='utf-8')
        self.inspector = Inspector.freeze(root, root / 'snapshots', 'abc', ['main.py'], 'entry', 'result')
        return FrozenInheritance(self.inspector)

    def actual(self, source=SOURCE):
        resolver = self.freeze(source)
        chain = resolver.chain('main.py', 'Checker')
        method = resolver.method(chain, 'check')
        namespace = {}
        exec(compile(source, str(self.inspector.root / 'main.py'), 'exec'), namespace)
        return chain, namespace, method

    def test_actual_kernel_and_normal_closure_have_a_finite_positive_abc_case(self):
        resolver = self.freeze()
        self.assertEqual(check_abc_kernel()['status'], 'CHECKED')
        chain, method = resolver.resolve('main.py', 'Checker.check')
        self.assertEqual(chain[-1].external_endpoint, 'abc.ABC')
        self.assertEqual(resolver.facts(chain, method)['external_endpoint'], 'abc.ABC')
        entry = dict(id='check', kind='method', path='main.py', symbol='Checker.check',
                     constructor=dict(symbol='Checker', arguments={}))
        trace = dependency_closure(self.inspector, dict(entries=[entry]))['entries']['check']
        self.assertEqual(trace['status'], 'CHECKED', trace['unresolved_targets'])

    def test_existing_target_is_checked_without_constructing_an_owner(self):
        source = SOURCE.replace('    label=', '    def __init__(self):\n        raise AssertionError("no construction in target check")\n    label=')
        chain, namespace, _ = self.actual(source)
        class_ = namespace['Checker']
        result = check_abc_target(class_, [class_], chain)
        self.assertEqual(result['status'], 'CHECKED', result)
        self.assertFalse(result['owner_construction'])

    def test_changed_kernel_native_global_or_python_code_is_unknown(self):
        with patch.object(abc, '_abc_init', lambda value: self.fail('not called')):
            self.assertEqual(check_abc_kernel()['status'], 'UNKNOWN')
        function = abc.ABCMeta.__new__
        original = function.__defaults__
        try:
            function.__defaults__ = ()
            self.assertEqual(check_abc_kernel()['status'], 'UNKNOWN')
        finally:
            function.__defaults__ = original
        cell = function.__closure__[0]
        original = cell.cell_contents
        try:
            cell.cell_contents = type
            self.assertEqual(check_abc_kernel()['status'], 'UNKNOWN')
        finally:
            cell.cell_contents = original
        self.assertEqual(check_abc_kernel()['status'], 'CHECKED')

    def test_unknown_class_state_and_protocol_changes_are_rejected(self):
        for change in ['Checker.label="changed"', 'Checker.extra=1',
                       'Checker.check.__defaults__=("changed",)',
                       'Checker.check.__annotations__={"value": int, "return": str}',
                       'Checker.__getattr__=lambda self,name: None']:
            with self.subTest(change=change):
                chain, namespace, _ = self.actual()
                exec(change, namespace)
                self.assertEqual(check_abc_target(namespace['Checker'], [namespace['Checker']], chain)['status'], 'UNKNOWN')

    def test_registry_registration_and_abstract_decorators_remain_outside_source_subset(self):
        source = SOURCE.replace('from abc import ABC', 'from abc import ABC, abstractmethod').replace(
            '    def check', '    @abstractmethod\n    def check')
        with self.assertRaises(InheritanceIssue):
            self.freeze(source).resolve('main.py', 'Checker.check')
        chain, namespace, _ = self.actual()
        class Virtual:
            pass
        namespace['Checker'].register(Virtual)
        self.assertEqual(check_abc_target(namespace['Checker'], [namespace['Checker']], chain)['status'], 'UNKNOWN')

    def test_external_prefix_and_constructor_call_effects_do_not_gain_closure(self):
        with self.assertRaises(InheritanceIssue):
            self.freeze('import opaque\n' + SOURCE).resolve('main.py', 'Checker.check')
        resolver = self.freeze(SOURCE.replace('    label=', '    def __init__(self):\n        missing_call()\n    label='))
        resolver.resolve('main.py', 'Checker.check')
        entry = dict(id='check', kind='method', path='main.py', symbol='Checker.check',
                     constructor=dict(symbol='Checker', arguments={}))
        trace = dependency_closure(self.inspector, dict(entries=[entry]))['entries']['check']
        self.assertEqual(trace['status'], 'MISSING')
        self.assertTrue(any(row['target'] == 'missing_call' for row in trace['unresolved_targets']))

    def test_untrusted_tuple_and_default_elements_are_not_called(self):
        chain, namespace, _ = self.actual(SOURCE.replace('value: str', 'value: str=("original",)'))
        calls = []
        class Trap:
            def __eq__(self, other):
                calls.append('equality')
                return True
        namespace['Checker'].check.__defaults__ = ((Trap(),),)
        self.assertEqual(check_abc_target(namespace['Checker'], [namespace['Checker']], chain)['status'], 'UNKNOWN')
        self.assertEqual(calls, [])
        class Sequence(list):
            def __iter__(self):
                raise AssertionError('must not iterate unknown sequence')
        self.assertEqual(check_abc_target(namespace['Checker'], Sequence([namespace['Checker']]), chain)['status'], 'UNKNOWN')

    def test_unknown_types_do_not_run_metaclass_equality_or_attribute_hooks(self):
        chain, namespace, _ = self.actual()
        calls = []
        class Meta(type):
            def __eq__(cls, other):
                calls.append('metaclass equality')
                return True
        class Unknown(metaclass=Meta):
            def __getattribute__(self, name):
                calls.append('attribute access')
                raise AssertionError('unknown protocol')
        value = Unknown()
        namespace['Checker'].check = value
        self.assertEqual(check_abc_target(namespace['Checker'], [namespace['Checker']], chain)['status'], 'UNKNOWN')
        self.assertEqual(check_abc_target(None, value, chain)['status'], 'UNKNOWN')
        self.assertEqual(calls, [])

    def test_nonordinary_stored_dictionary_keys_are_rejected_before_lookup(self):
        calls = []
        class Key(str):
            __hash__ = str.__hash__
            def __eq__(self, other):
                calls.append('key equality')
                return str.__eq__(self, other)
        for mapping, name in [(abc.__dict__, '__doc__'), (builtins.__dict__, '__doc__'), (sys.modules, 'abc')]:
            with self.subTest(name=name):
                original = mapping.pop(name)
                key = Key(name)
                mapping[key] = original
                calls.clear()
                try:
                    self.assertEqual(check_abc_kernel()['status'], 'UNKNOWN')
                    self.assertEqual(calls, [])
                finally:
                    mapping.pop(key)
                    mapping[name] = original
        chain, namespace, _ = self.actual()
        original = namespace.pop('__builtins__')
        key = Key('__builtins__')
        namespace[key] = original
        calls.clear()
        self.assertEqual(check_abc_target(namespace['Checker'], [namespace['Checker']], chain)['status'], 'UNKNOWN')
        self.assertEqual(calls, [])

    def test_actual_descriptor_kind_must_match_the_source_decorator(self):
        source = SOURCE.replace('    def check(self, value: str)', '    @staticmethod\n    def check(value: str)')
        chain, namespace, _ = self.actual(source)
        class_ = namespace['Checker']
        self.assertEqual(check_abc_target(class_, [class_], chain)['status'], 'CHECKED')
        class_.check = class_.__dict__['check'].__func__
        self.assertEqual(check_abc_target(class_, [class_], chain)['status'], 'UNKNOWN')


if __name__ == '__main__':
    unittest.main()
