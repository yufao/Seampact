import ast
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from seampact.source.owner_constants import owner_constants, reachable_owner_nodes
from seampact.units import NativeBindings, MissingBinding
from seampact.source.inspection import Inspector
from seampact.source.frozen_inheritance import FrozenInheritance


SOURCE = '''class Owner:
    def __init__(self, mode='triplet'):
        self.mode = mode
    def run(self):
        if self.mode == 'triplet':
            return 1
        else:
            raise ValueError(self.missing)
'''


class OwnerConstantTests(unittest.TestCase):
    def check(self, source=SOURCE, arguments=None):
        owner = ast.parse(source).body[0]
        entry = dict(path='owner.py', symbol='Owner.run', constructor=dict(arguments=arguments or {}))
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root/'owner.py').write_text(source, encoding='utf-8')
        inspector = Inspector.freeze(root, root/'snapshots', 'owner', ['owner.py'], 'owner source', 'value')
        self.binding = SimpleNamespace(inheritance=FrozenInheritance(inspector))
        return owner, entry, owner_constants(owner, entry)

    def test_frozen_default_only_prunes_unreachable_owner_reads(self):
        owner, entry, constants = self.check()
        self.assertEqual(constants, dict(mode='triplet'))
        NativeBindings._owner_initialization(self.binding, owner, entry, [])
        with self.assertRaisesRegex(MissingBinding, 'missing'):
            owner, entry, _ = self.check(arguments=dict(mode='other'))
            NativeBindings._owner_initialization(self.binding, owner, entry, [])

    def test_unknown_configuration_does_not_inherit_literal_default(self):
        owner, entry, constants = self.check(arguments=dict(mode={'$dependency': {'id':'x','field':'model'}}))
        self.assertEqual(constants, {})
        with self.assertRaisesRegex(MissingBinding, 'missing'):
            NativeBindings._owner_initialization(self.binding, owner, entry, [])

    def test_module_level_class_rewrite_cannot_use_constant_field_pruning(self):
        owner, entry, _ = self.check(SOURCE + '\nOwner.mode = property(lambda self: "other")\n')
        with self.assertRaisesRegex(MissingBinding, 'missing'):
            NativeBindings._owner_initialization(self.binding, owner, entry, [])

    def test_owner_escape_mutation_and_descriptor_disable_pruning(self):
        additions = ('    def change(self):\n        self.mode = "other"\n',
                     '    def change(self):\n        mutate(self)\n',
                     '    @property\n    def mode(self):\n        return "other"\n',
                     '    def change(self):\n        alias = self\n')
        for addition in additions:
            with self.subTest(addition=addition):
                self.assertEqual(self.check(SOURCE + addition)[2], {})

    def test_indirect_owner_escapes_and_runtime_hooks_disable_pruning(self):
        additions = ('    def change(self):\n        mutate((self,))\n',
                     '    def change(self):\n        return self\n',
                     '    def change(self):\n        mutate({"owner": self})\n',
                     '    def change(self):\n        return self.__setattr__\n',
                     '    def change(self):\n        exec("self.mode = \'other\'")\n',
                     '    def change(self):\n        return super().__setattr__\n',
                     '    def change(self):\n        Other.mode = descriptor()\n')
        for addition in additions:
            with self.subTest(addition=addition):
                self.assertEqual(self.check(SOURCE + addition)[2], {})

    def test_class_level_descriptors_and_unpacking_cannot_retain_defaults(self):
        descriptor = SOURCE.replace('class Owner:\n', 'class Owner:\n    mode = descriptor()\n')
        self.assertEqual(self.check(descriptor)[2], {})
        for assignment in ('mode, other = unknown()', 'other = (mode := unknown())',
                           'import unknown as mode', 'mode: str = "other"'):
            source = SOURCE.replace('        self.mode = mode', '        ' + assignment + '\n        self.mode = mode')
            with self.subTest(assignment=assignment):
                self.assertEqual(self.check(source)[2], {})

    def test_conditional_constructor_and_parameter_reassignment_are_not_defaults(self):
        conditional = SOURCE.replace('        self.mode = mode', '        if mode:\n            return\n        self.mode = mode')
        self.assertEqual(self.check(conditional)[2], {})
        reassigned = SOURCE.replace('        self.mode = mode', '        mode = unknown()\n        self.mode = mode')
        self.assertEqual(self.check(reassigned)[2], {})
        reassigned = SOURCE.replace('        self.mode = mode', '        mode = "other"\n        self.mode = mode')
        self.assertEqual(self.check(reassigned)[2], dict(mode='other'))

    def test_original_super_constructor_propagates_a_frozen_parameter(self):
        tree = ast.parse(SOURCE + '\nclass Child(Owner):\n    def __init__(self, mode="triplet"):\n        super().__init__(mode)\n')
        chain = [SimpleNamespace(node=n) for n in reversed(tree.body)]
        self.assertEqual(owner_constants(tree.body[1], dict(constructor=dict(arguments={})), chain), dict(mode='triplet'))


if __name__ == '__main__':
    unittest.main()
