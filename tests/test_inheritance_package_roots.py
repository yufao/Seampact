"""Local inheritance keeps the original frozen package import context."""

from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from seampact.source.dependency_closure import dependency_closure
from seampact.source.frozen_inheritance import FrozenInheritance, InheritanceIssue
from seampact.source.inspection import Inspector


class InheritancePackageRootTests(unittest.TestCase):
    prefix = 'components/source-project'
    entry_path = prefix + '/pkg/engine.py'

    def freeze(self, *, base=None, engine=None, extras=None):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        files = {
            self.prefix + '/pkg/__init__.py': '',
            self.prefix + '/pkg/core/__init__.py': 'from .base import Base\n',
            self.prefix + '/pkg/core/base.py': base or 'class Base:\n    def check(self, value):\n        return value\n',
            self.entry_path: engine or 'from pkg.core import Base\nclass Engine(Base):\n    pass\n',
            **(extras or {}),
        }
        for name, source in files.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(source, encoding='utf-8')
        self.inspector = Inspector.freeze(root, root / 'snapshots', 'request', list(files), 'entry', 'result')
        return FrozenInheritance(self.inspector)

    def test_absolute_package_reexport_under_non_python_checkout_name(self):
        resolver = self.freeze()
        chain, method = resolver.resolve(self.entry_path, 'Engine.check')
        self.assertEqual([item.node.name for item in chain], ['Engine', 'Base'])
        self.assertEqual(method.path, self.prefix + '/pkg/core/base.py')
        entry = dict(id='check', path=self.entry_path, symbol='Engine.check', kind='method',
                     constructor=dict(symbol='Engine', arguments={}))
        result = dependency_closure(self.inspector, dict(entries=[entry]))['entries']['check']
        self.assertEqual(result['status'], 'CHECKED', result['unresolved_targets'])
        self.assertEqual(result['inheritance_binding']['definition']['path'], method.path)

    def test_relative_package_import_uses_the_same_original_root(self):
        resolver = self.freeze(engine='from .core import Base\nclass Engine(Base):\n    pass\n')
        chain, method = resolver.resolve(self.entry_path, 'Engine.check')
        self.assertEqual([item.node.name for item in chain], ['Engine', 'Base'])
        self.assertEqual(method.symbol, 'Base.check')

    def test_multiple_frozen_module_candidates_remain_unknown(self):
        resolver = self.freeze(extras={self.prefix + '/pkg/core.py': 'class Base:\n    pass\n'})
        with self.assertRaisesRegex(InheritanceIssue, 'multiple frozen import candidates'):
            resolver.resolve(self.entry_path, 'Engine.check')

    def test_unrelated_checkout_is_not_an_invented_import_root(self):
        resolver = self.freeze(engine='from unrelated import Base\nclass Engine(Base):\n    pass\n',
                               extras={'other-project/unrelated.py': 'class Base:\n    def check(self, value):\n        return value\n'})
        with self.assertRaisesRegex(InheritanceIssue, 'external or outside frozen source'):
            resolver.resolve(self.entry_path, 'Engine.check')

    def test_local_parent_does_not_authorize_external_abc_endpoint(self):
        resolver = self.freeze(base='from abc import ABC\nclass Base(ABC):\n    def check(self, value):\n        return value\n')
        with self.assertRaisesRegex(InheritanceIssue, 'abc.ABC'):
            resolver.resolve(self.entry_path, 'Engine.check')

    def test_multiple_bases_and_dynamic_receiver_storage_stay_unknown(self):
        cases = [
            'from pkg.core import Base\nclass Engine(Base, object):\n    pass\n',
            'from pkg.core import Base\nclass Engine(Base):\n    def other(self):\n        return self.__class__\n',
        ]
        for source in cases:
            with self.subTest(source=source):
                resolver = self.freeze(engine=source)
                with self.assertRaises(InheritanceIssue):
                    resolver.resolve(self.entry_path, 'Engine.check')

    def test_class_rewrite_in_original_non_python_checkout_is_still_rejected(self):
        resolver = self.freeze(engine='from pkg.core import Base\nclass Engine(Base):\n    pass\nBase.check = replacement\n')
        with self.assertRaisesRegex(InheritanceIssue, 'monkey-patched'):
            resolver.resolve(self.entry_path, 'Engine.check')

    def test_unreadable_rewrite_scope_is_unknown_without_a_syntax_crash(self):
        resolver = self.freeze(extras={'other-project/broken.py': 'def broken(:\n'})
        with self.assertRaisesRegex(InheritanceIssue, 'unavailable.*rewrite check'):
            resolver.resolve(self.entry_path, 'Engine.check')

    def test_ambiguous_rewrite_reference_is_not_silently_excluded(self):
        resolver = self.freeze(extras={
            self.prefix + '/pkg/patch.py': 'from alias import Base\nBase.check = replacement\n',
            self.prefix + '/pkg/alias.py': 'from pkg.core import Base\n',
            'alias.py': 'class Base:\n    pass\n',
        })
        with self.assertRaisesRegex(InheritanceIssue, 'multiple frozen import candidates'):
            resolver.resolve(self.entry_path, 'Engine.check')


if __name__ == '__main__':
    unittest.main()
