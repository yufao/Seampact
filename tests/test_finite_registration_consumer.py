"""Normal finite registry calls consume original targets inside source locks."""
import contextlib
import sys
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parents[1] / 'src'))
from seampact.units import MissingBinding
import test_import_only_control as control
from test_finite_registration_closure import SOURCE


FIXED = SOURCE.replace('Selector.choose(value)', 'Selector.choose("Leaf")')


class FiniteRegistrationConsumerTests(unittest.TestCase):
    def fixture(self, source=FIXED):
        test = control.ImportOnlyControlTests(
            'test_function_endpoint_requires_real_full_claim_then_checks_actual_default')
        self.addCleanup(test.doCleanups)
        catalog, unit, worker = test.catalog(source, selected_boundary=True)
        binding, entry = catalog.deferred[unit['id']]
        return catalog, unit, worker, binding, entry

    def prepare(self, source=FIXED):
        catalog, unit, worker, binding, entry = self.fixture(source)
        plan = catalog.require_import_only_plan(unit['id'])
        with patch.object(binding, '_load', side_effect=AssertionError('owner load is forbidden')):
            answer = worker.import_only(dict(unit=unit['id'], plan_digest=plan['plan_digest'], **control.IDENTITY))
        self.assertEqual(answer['status'], 'IMPORTED', answer)
        self.assertEqual(binding.owners, {})
        self.assertEqual(binding.entries, {})
        return catalog, unit, worker, binding, entry

    def check(self, worker, unit):
        return worker.check_call(dict(unit=unit['id'], arguments={'value': []}, **control.IDENTITY))

    def test_normal_install_import_original_invoke_and_repeated_proxy(self):
        catalog, unit, worker, binding, _ = self.fixture()
        self.assertEqual(unit['status'], 'CALL_CONDITION_DEFERRED')
        self.assertTrue(unit['call_condition']['runtime_protocol_requirements'])
        cold = self.check(worker, unit)
        self.assertFalse(cold['allowed'])
        self.assertEqual(cold['reason_code'], 'REGISTRATION_BINDING_UNKNOWN')
        self.assertEqual(binding.modules, {})
        plan = catalog.require_import_only_plan(unit['id'])
        self.assertEqual(worker.import_only(dict(unit=unit['id'], plan_digest=plan['plan_digest'], **control.IDENTITY))['status'], 'IMPORTED')
        self.assertTrue(self.check(worker, unit)['allowed'])
        self.assertEqual(catalog.invoke(unit['id'], {'value': []}), 'Leaf')
        self.assertEqual(catalog.invoke(unit['id'], {'value': []}), 'Leaf')
        self.assertEqual(binding.owners, {})

    def test_member_loss_replacement_and_unknown_metaclass_run_no_hooks(self):
        for change in ('extra', 'lost', 'replaced'):
            with self.subTest(change=change):
                catalog, unit, worker, binding, _ = self.prepare()
                namespace = binding.import_only_entries[unit['id']]['namespace']
                hits = []
                class Meta(type):
                    def __eq__(cls, value): hits.append('eq'); return True
                    def __getattribute__(cls, name): hits.append('attr'); return type.__getattribute__(cls, name)
                if change == 'extra':
                    class Extra(namespace['Root'], metaclass=Meta): pass
                elif change == 'lost':
                    del namespace['Leaf']
                else:
                    namespace['Leaf'] = type('Leaf', (namespace['Left'],), {})
                hits.clear()
                self.assertFalse(self.check(worker, unit)['allowed'])
                with self.assertRaises(ValueError):
                    catalog.invoke(unit['id'], {'value': []})
                self.assertEqual(hits, [])

    def test_actual_global_class_alias_binding_is_rechecked(self):
        source = FIXED.replace('class Selector:', 'Alias=Root\nclass Selector:').replace('cls = Root', 'cls = Alias')
        catalog, unit, worker, binding, _ = self.prepare(source)
        self.assertTrue(self.check(worker, unit)['allowed'])
        namespace = binding.import_only_entries[unit['id']]['namespace']
        namespace['Alias'] = namespace['Right']  # Original Root graph is unchanged.
        answer = self.check(worker, unit)
        self.assertFalse(answer['allowed'])
        self.assertIn('reference changed', answer['reason'])
        with self.assertRaises(ValueError):
            catalog.invoke(unit['id'], {'value': []})

    def test_static_descriptor_function_code_clone_and_default_drift_reject(self):
        for change in ('descriptor', 'clone', 'code', 'default', 'kwdefault'):
            with self.subTest(change=change):
                source = FIXED.replace('def members(cls=None):', 'def members(cls=None, *, tag="safe"):') if change == 'kwdefault' else FIXED
                catalog, unit, worker, binding, _ = self.prepare(source)
                class_ = binding.import_only_entries[unit['id']]['namespace']['Selector']
                original = class_.__dict__['members'].__func__
                if change == 'descriptor':
                    class_.members = original
                elif change == 'clone':
                    clone = types.FunctionType(original.__code__, original.__globals__, original.__name__, original.__defaults__)
                    clone.__qualname__ = original.__qualname__
                    class_.members = staticmethod(clone)
                elif change == 'code':
                    original.__code__ = original.__code__.replace(co_consts=original.__code__.co_consts + (99,))
                elif change == 'default':
                    original.__defaults__ = ('changed',)
                else:
                    original.__kwdefaults__['tag'] = 'changed'
                self.assertFalse(self.check(worker, unit)['allowed'])
                with self.assertRaises(ValueError):
                    catalog.invoke(unit['id'], {'value': []})

    def test_read_use_target_is_checked_after_original_load_inside_context(self):
        catalog, unit, worker, binding, _ = self.prepare()
        self.assertTrue(self.check(worker, unit)['allowed'])
        original_load = binding._load
        created = []
        async def changed(entry, installed, construction=None):
            result = await original_load(entry, installed, construction)
            namespace = binding.import_only_entries[unit['id']]['namespace']
            created.append(type('AddedDuringBinding', (namespace['Root'],), {}))
            return result
        with patch.object(binding, '_load', changed):
            with self.assertRaisesRegex(MissingBinding, 'subclass membership/order changed'):
                catalog.invoke(unit['id'], {'value': []})
        self.assertEqual(len(created), 1)

    def test_unproved_builtin_io_is_not_hidden_as_cold_target_absence(self):
        for source in (FIXED.replace('if not cls:', 'print("unproved")\n        if not cls:'),
                       FIXED.replace('[s for c in cls.__subclasses__()', '[s for c in (print("unproved"), cls.__subclasses__())[1]')):
            with self.subTest(source=source):
                catalog, unit, worker, binding, _ = self.fixture(source)
                answer = self.check(worker, unit)
                self.assertFalse(answer['allowed'])
                self.assertNotEqual(answer['reason_code'], 'REGISTRATION_BINDING_UNKNOWN')
                self.assertEqual(binding.modules, {})
                self.assertEqual(binding.owners, {})

    def test_selected_normal_import_intent_uses_existing_full_claim_path(self):
        test = control.ImportOnlyControlTests('test_normal_selected_intent_reaches_real_worker_import_and_recheck')
        self.addCleanup(test.doCleanups)
        original_catalog = test.catalog
        test.catalog = lambda source, **kwargs: original_catalog(FIXED, **kwargs)
        test.test_normal_selected_intent_reaches_real_worker_import_and_recheck()


if __name__ == '__main__':
    unittest.main()
