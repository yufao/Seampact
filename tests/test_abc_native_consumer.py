"""Normal catalog invocation consumes source/ABC and retained-callable facts."""

import abc
import copy
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from seampact.runtime import Coordinator, OwnerRuntime
from seampact.source.inspection import Inspector
from seampact.store import Store
from seampact.units import UnitCatalog, MissingBinding, _native_function_chain
import test_call_value_conditions as condition_fixtures


SOURCE = ('from abc import ABC\nclass Checker(ABC):\n'
          '    def check(self, value: str) -> str:\n        return value\n')


class ABCNativeConsumerTests(unittest.TestCase):
    def catalog(self, source=SOURCE, symbol='Checker.check'):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root / 'original.py').write_text(source, encoding='utf-8')
        inspector = Inspector.freeze(root, root / 'snapshots', 'abc', ['original.py'], 'check', 'result')
        inspector.request['source_id'] = 'source'
        entry = dict(id='check', kind='method', path='original.py', symbol=symbol,
                     constructor=dict(symbol='Checker', arguments={}), boundary_ids=['whole'])
        owner_symbol = 'Base.check' if 'class Base:' in source else symbol
        candidate = dict(entries=[entry], boundaries=[dict(id='whole', mode='W', path='original.py',
            symbol=owner_symbol, native_wiring='Keep the original class receiver and whole method.')])
        store = Store(':memory:')
        self.addCleanup(store.close)
        runtime = OwnerRuntime(Coordinator(store))
        self.addCleanup(runtime.close)
        catalog = UnitCatalog(root, store, runtime)
        unit = catalog.install(inspector, candidate, 'analysis', 'installation')[0]
        self.assertEqual(unit['status'], 'READY', unit.get('reason'))
        return catalog, unit

    def initialized(self, source=SOURCE):
        catalog, unit = self.catalog(source)
        self.assertEqual(catalog.invoke(unit['id'], {'value': 'first'}), 'first')
        binding = catalog.bindings[unit['id']]
        retained = binding.inherited_calls[unit['id']]
        return catalog, unit, binding, retained

    def test_normal_install_construct_and_repeated_invoke_use_original_abc(self):
        catalog, unit, binding, retained = self.initialized()
        self.assertIs(type(retained['class']), abc.ABCMeta)
        self.assertIs(type(retained['owner']), retained['class'])
        self.assertEqual(catalog.invoke(unit['id'], {'value': 'second'}), 'second')
        self.assertEqual(len(binding.owners), 1)

    def test_cached_call_rechecks_actual_kernel_and_instance_method_shadow(self):
        catalog, unit, _, retained = self.initialized()
        with patch.object(abc, '_abc_init', lambda value: self.fail('must not run changed kernel')):
            with self.assertRaises(MissingBinding):
                catalog.invoke(unit['id'], {'value': 'blocked'})
        catalog, unit, _, retained = self.initialized()
        retained['owner'].check = lambda value: 'shadow'
        with self.assertRaises(MissingBinding):
            catalog.invoke(unit['id'], {'value': 'blocked'})

    def test_cached_old_bound_function_cannot_survive_a_class_same_code_clone(self):
        catalog, unit, _, retained = self.initialized()
        class_ = retained['class']
        old = class_.__dict__['check']
        clone = types.FunctionType(old.__code__, old.__globals__, old.__name__, old.__defaults__, old.__closure__)
        clone.__qualname__, clone.__annotations__ = old.__qualname__, dict(old.__annotations__)
        class_.check = clone
        old.__globals__['HITS'] = []
        namespace = {}
        exec('def replaced(self, value):\n    HITS.append("bad")\n    return value\n', namespace)
        old.__code__ = namespace['replaced'].__code__.replace(co_filename=old.__code__.co_filename,
                                                           co_firstlineno=old.__code__.co_firstlineno)
        with self.assertRaises(MissingBinding):
            catalog.invoke(unit['id'], {'value': 'blocked'})
        self.assertEqual(old.__globals__['HITS'], [])

    def test_cached_code_change_is_rejected_even_with_the_same_source_anchor(self):
        catalog, unit, _, retained = self.initialized()
        original = retained['class'].__dict__['check']
        original.__globals__['HITS'] = []
        namespace = {}
        exec('def replaced(self, value):\n    HITS.append("bad")\n    return value\n', namespace)
        original.__code__ = namespace['replaced'].__code__.replace(co_filename=original.__code__.co_filename,
                                                                co_firstlineno=original.__code__.co_firstlineno)
        with self.assertRaises(MissingBinding):
            catalog.invoke(unit['id'], {'value': 'blocked'})
        self.assertEqual(original.__globals__['HITS'], [])

    def test_ordinary_inheritance_requires_the_original_static_descriptor_kind(self):
        source = ('class Base:\n    @staticmethod\n    def helper():\n        return 1\n'
                  '    def check(self, value: str) -> str:\n        return value\nclass Checker(Base):\n    pass\n')
        catalog, unit, _, retained = self.initialized(source)
        base = retained['class'].__mro__[1]
        base.helper = base.__dict__['helper'].__func__
        with self.assertRaises(MissingBinding):
            catalog.invoke(unit['id'], {'value': 'blocked'})

    def test_ordinary_inheritance_rejects_keyword_default_mapping_content_drift(self):
        source = ('class Base:\n    def check(self, value: str, *, option: str="safe") -> str:\n'
                  '        return option\nclass Checker(Base):\n    pass\n')
        catalog, unit = self.catalog(source)
        self.assertEqual(catalog.invoke(unit['id'], {'value': 'first'}), 'safe')
        binding = catalog.bindings[unit['id']]
        retained = binding.inherited_calls[unit['id']]
        base = retained['class'].__mro__[1]
        original = base.__dict__['check']
        original.__kwdefaults__['option'] = 'changed'
        with self.assertRaises(MissingBinding):
            catalog.invoke(unit['id'], {'value': 'blocked'})

    def test_unknown_metaclass_and_wrapped_object_have_zero_hooks(self):
        _, unit, binding, retained = self.initialized()
        calls = []
        class Meta(type):
            def __getattribute__(cls, name):
                calls.append('meta');raise AssertionError('unknown class attribute')
        class Unknown(metaclass=Meta):
            def __getattribute__(self, name):
                calls.append('object');raise AssertionError('unknown object attribute')
        with self.assertRaises(MissingBinding):
            binding._assert_inherited_owner(Unknown, retained['binding'])
        wrapped = lambda: None
        wrapped.__wrapped__ = Unknown()
        with self.assertRaises(MissingBinding):
            _native_function_chain(wrapped)
        class Key(str):
            __hash__ = str.__hash__
            def __eq__(self, other):
                calls.append('key');return str.__eq__(self, other)
        wrapped = lambda: None
        wrapped.__dict__[Key('__wrapped__')] = Unknown()
        with self.assertRaises(MissingBinding):
            _native_function_chain(wrapped)
        self.assertEqual(calls, [])

    def test_same_code_fake_globals_fail_normal_source_identity_check(self):
        _, _, binding, retained = self.initialized()
        old = retained['class'].__dict__['check']
        clone = types.FunctionType(old.__code__, dict(old.__globals__), old.__name__, old.__defaults__, old.__closure__)
        clone.__qualname__, clone.__annotations__ = old.__qualname__, dict(old.__annotations__)
        retained['class'].check = clone
        with binding._context():
            with self.assertRaises(MissingBinding):
                binding._assert_inherited_owner(retained['class'], retained['binding'])

    def test_abc_original_method_rejects_unproved_signature_metadata(self):
        catalog, unit, _, retained = self.initialized()
        calls = []
        class Unknown:
            @property
            def __class__(self):
                calls.append('class');return object
        retained['class'].__dict__['check'].__signature__ = Unknown()
        with self.assertRaises(MissingBinding):
            catalog.invoke(unit['id'], {'value': 'blocked'})
        self.assertEqual(calls, [])


class WorkerActivationRecipeTests(unittest.TestCase):
    fixture = condition_fixtures.CallValueConditionTests.fixture
    catalog = condition_fixtures.CallValueConditionTests.catalog

    def worker_catalog(self):
        _, _, _, catalog, unit = self.catalog('def score(value):\n    assert isinstance(value,list)\n    return 7\n')
        catalog._worker_call_identity = dict(session='session', instance_id='instance', generation=1, process_nonce='nonce')
        return catalog, unit

    def test_normal_worker_recipe_and_actual_catalog_call_remain_positive(self):
        catalog, unit = self.worker_catalog()
        checked = catalog.check_call_conditions(unit['id'], {'value': []})
        self.assertTrue(checked['allowed'], checked)
        self.assertEqual(catalog.invoke(unit['id'], {'value': []}), 7)

    def test_worker_cannot_consume_missing_or_unknown_activation_prefix(self):
        from seampact.source.call_value_conditions import context
        from seampact.source.dependency_closure import resolve_resource_calls
        for missing in (True, False):
            with self.subTest(missing=missing):
                catalog, unit = self.worker_catalog()
                binding, _ = catalog.deferred[unit['id']]
                self.assertTrue(catalog.check_call_conditions(unit['id'], {'value': []})['allowed'])
                def incomplete(inspector, *args, **kwargs):
                    trace = resolve_resource_calls(inspector, *args, **kwargs)
                    ctx = context(inspector)
                    if ctx is not None and not ctx.compiling:
                        trace = copy.deepcopy(trace)
                        if missing:
                            trace.pop('activation_prefix', None)
                        else:
                            trace['activation_prefix'] = dict(status='UNKNOWN', gaps=['unproved prefix'])
                    return trace
                with patch('seampact.source.dependency_closure.resolve_resource_calls', side_effect=incomplete):
                    checked = catalog.check_call_conditions(unit['id'], {'value': []})
                self.assertFalse(checked['allowed'], checked)
                self.assertEqual(binding.modules, {})


if __name__ == '__main__':
    unittest.main()
