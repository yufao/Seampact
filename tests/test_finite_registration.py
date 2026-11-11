"""Complete native subclass worlds retain original order and passive identity."""
import sys
import unittest
import weakref
import types
import builtins
from unittest.mock import patch

sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parents[1] / 'src'))
from seampact.source.finite_registration import compile_world, capture_world, verify_world, RegistrationUnknown
import test_activation_prefix as fixture


SOURCE = ('class Root:\n    pass\nclass Left(Root):\n    pass\n'
          'class Right(Root):\n    pass\nclass Diamond(Left):\n    pass\n'
          'def score(value):\n    return value\n')


class FiniteRegistrationTests(unittest.TestCase):
    def world(self, source=SOURCE):
        check = fixture.ActivationPrefixTests('test_parent_package_runs_before_child_and_is_not_repeated')
        self.addCleanup(check.doCleanups)
        inspector, entry, candidate = check.fixture({'original.py': source}, 'original.py')
        world = compile_world(inspector, entry, candidate, 'original.py', 'Root')
        module = types.ModuleType('original')
        exec(compile(source, str(inspector.root / 'original.py'), 'exec'), module.__dict__)
        return world, module, inspector, entry, candidate

    def test_complete_creation_order_c3_and_strict_recursion_state(self):
        world, module, _, _, _ = self.world()
        self.assertEqual(world.direct_subclasses(world.root), ('original.py:Left', 'original.py:Right'))
        self.assertEqual(world.direct_subclasses('original.py:Left'), ('original.py:Diamond',))
        self.assertEqual(world.row('original.py:Diamond')['mro'],
                         ('original.py:Diamond', 'original.py:Left', 'original.py:Root', 'builtins.object'))
        self.assertTrue(world.strict_descent((world.root,), ('original.py:Left',)))
        self.assertFalse(world.strict_descent((world.root,), (world.root,)))
        self.assertFalse(world.strict_descent((world.root,), ('missing:Unknown',)))
        self.assertEqual(world.name('original.py:Left'), 'Left')
        receipt = capture_world(world, {'original.py': module})
        self.assertTrue(all(type(value) is weakref.ReferenceType for value in receipt['classes'].values()))
        answer = verify_world(world, receipt, {'original.py': module})
        self.assertEqual(answer['status'], 'TARGET_MEMBERS_CHECKED')
        self.assertFalse(answer['execution_permission'])
        self.assertEqual(answer['read_use_region'], 'REQUIRES_NORMAL_LOCKED_CALL_CONSUMER')

    def test_extra_member_is_not_a_name_whitelist_and_never_reads_unknown_meta(self):
        world, module, _, _, _ = self.world()
        namespace = module.__dict__
        receipt = capture_world(world, {'original.py': module})
        hits = []
        class Meta(type):
            def __eq__(cls, value): hits.append('eq'); return True
            def __getattribute__(cls, name): hits.append('attr'); return type.__getattribute__(cls, name)
        class Extra(namespace['Root'], metaclass=Meta):
            pass
        hits.clear()
        with self.assertRaises(RegistrationUnknown):
            verify_world(world, receipt, {'original.py': module})
        self.assertEqual(hits, [])

    def test_namespace_member_replacement_loss_and_class_name_drift_reject(self):
        for mutation in ('replace', 'delete', 'name', 'bases', 'shadow'):
            with self.subTest(mutation=mutation):
                world, module, _, _, _ = self.world()
                namespace = module.__dict__
                receipt = capture_world(world, {'original.py': module})
                if mutation == 'replace':
                    namespace['Left'] = type('Left', (namespace['Root'],), {})
                elif mutation == 'delete':
                    del namespace['Left']
                elif mutation == 'name':
                    namespace['Left'].__name__ = 'Changed'
                elif mutation == 'bases':
                    namespace['Left'].__bases__ = (namespace['Right'],)
                else:
                    namespace['Root'].__subclasses__ = lambda: []
                with self.assertRaises(RegistrationUnknown):
                    verify_world(world, receipt, {'original.py': module})

    def test_unknown_namespace_keys_and_receipt_protocols_do_not_execute_hooks(self):
        world, module, _, _, _ = self.world()
        namespace = module.__dict__
        receipt = capture_world(world, {'original.py': module})
        hits = []
        class Key(str):
            __hash__ = str.__hash__
            def __eq__(self, other): hits.append('eq'); return str.__eq__(self, other)
        original = namespace.pop('Root')
        key = Key('Root')
        namespace[key] = original
        hits.clear()
        with self.assertRaises(RegistrationUnknown):
            verify_world(world, receipt, {'original.py': module})
        self.assertEqual(hits, [])
        namespace.pop(key)
        namespace['Root'] = original
        class Unknown:
            def __eq__(self, value): hits.append('eq'); return True
            def __call__(self): hits.append('call'); return original
        for field in ('schema', 'classes'):
            changed = dict(receipt)
            changed[field] = Unknown() if field == 'schema' else {world.root: Unknown()}
            hits.clear()
            with self.assertRaises(RegistrationUnknown):
                verify_world(world, changed, {'original.py': module})
            self.assertEqual(hits, [])

    def test_unknown_creation_import_hook_and_rebinding_paths_remain_unknown(self):
        for fragment in (
            'import sdk\n',
            'def create():\n    class Child(Root):\n        pass\n',
            'class Other:\n    def __init_subclass__(cls):\n        pass\n',
            'factory=type\n',
            'Root=Left\n',
            'class Root:\n    pass\n',
            'class Multi(Left,Right):\n    pass\n',
        ):
            with self.subTest(fragment=fragment):
                with self.assertRaises(RegistrationUnknown):
                    self.world(SOURCE + fragment)

    def test_call_creation_prefix_cannot_be_relabelled_as_existing_members(self):
        world, _, _, _, _ = self.world()
        prefix = dict(world.prefix)
        prefix['events'] = [*prefix['events'], dict(kind='CLASS_CREATION', path='original.py', status='SOURCE_CHECKED')]
        self.assertFalse(world.accepts_prefix(prefix))
        self.assertTrue(world.accepts_prefix(world.prefix))

    def test_exported_requirements_cannot_mutate_source_facts(self):
        world, _, _, _, _ = self.world()
        original = world.requirements()
        changed = world.requirements()
        changed['classes'][0]['bases'].append('unknown:Injected')
        changed['classes'][0]['mro'].clear()
        changed['activation_prefix']['events'].clear()
        self.assertEqual(world.requirements(), original)
        with self.assertRaises(TypeError):
            world.row(world.root)['symbol'] = 'Changed'

    def test_receipt_does_not_keep_original_module_alive(self):
        world, module, _, _, _ = self.world()
        receipt = capture_world(world, {'original.py': module})
        original = weakref.ref(module)
        del module
        self.assertIsNone(original())
        with self.assertRaises(RegistrationUnknown):
            verify_world(world, receipt, {'original.py': types.ModuleType('original')})

    def test_native_set_and_original_module_builtin_binding_are_fresh(self):
        world, module, _, _, _ = self.world()
        receipt = capture_world(world, {'original.py': module})
        hits = []
        def changed(*args):
            hits.append('set')
            return set()
        with patch.object(builtins, 'set', changed):
            with self.assertRaises(RegistrationUnknown):
                verify_world(world, receipt, {'original.py': module})
        self.assertEqual(hits, [])
        module.__dict__['set'] = changed
        with self.assertRaises(RegistrationUnknown):
            verify_world(world, receipt, {'original.py': module})
        self.assertEqual(hits, [])


if __name__ == '__main__':
    unittest.main()
