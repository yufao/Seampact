"""Import-only checks the actual controlled loader before executing source."""
import builtins
import contextlib
import importlib
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parents[1] / 'src'))
import seampact.units as native
import test_import_only_control as fixture


@contextlib.contextmanager
def changed_code(function):
    original = function.__code__
    try:
        function.__code__ = original.replace(co_consts=original.co_consts + ('changed',))
        yield
    finally:
        function.__code__ = original


class ImportLoaderKernelTests(unittest.TestCase):
    def binding(self):
        control = fixture.ImportOnlyControlTests(
            'test_function_endpoint_requires_real_full_claim_then_checks_actual_default')
        self.addCleanup(control.doCleanups)
        catalog, unit, worker = control.catalog(
            'def score(value,option=None):\n assert isinstance(value,list)\n return 7\n')
        binding, entry = catalog.deferred[unit['id']]
        plan = catalog.require_import_only_plan(unit['id'])
        return catalog, unit, worker, binding, entry, plan

    def test_replaced_loader_finder_and_instance_methods_do_not_execute(self):
        for field in ('loader', 'finder', 'context', 'namespace', 'inject', 'reader', 'source'):
            with self.subTest(field=field):
                _, unit, worker, binding, _, plan = self.binding()
                hits = []
                def changed(*args, **kwargs):
                    hits.append(field)
                    raise AssertionError('unproved loading code executed')
                target, name = {
                    'loader': (native._FrozenLoader, 'exec_module'),
                    'finder': (native._FrozenFinder, 'find_spec'),
                    'context': (binding, '_context'),
                    'namespace': (binding, '_namespace'),
                    'inject': (binding, '_inject_callsites'),
                    'reader': (binding.inspector, 'data'),
                    'source': (native, 'source_unit'),
                }[field]
                with patch.object(target, name, changed):
                    result = worker.import_only(dict(unit=unit['id'], plan_digest=plan['plan_digest'], **fixture.IDENTITY))
                self.assertEqual(result['status'], 'FAILED', result)
                self.assertEqual(hits, [])
                self.assertEqual(binding.modules, {})
                self.assertEqual(binding.owners, {})

    def test_same_function_code_and_globals_drift_are_rejected_before_import(self):
        for field in ('code', 'global', 'inherited_protocol', 'spec_code'):
            with self.subTest(field=field):
                _, unit, worker, binding, _, plan = self.binding()
                if field == 'code':
                    function = native._FrozenLoader.exec_module
                    alteration = changed_code(function)
                elif field == 'spec_code':
                    function = importlib.machinery.ModuleSpec.__init__
                    alteration = changed_code(function)
                elif field == 'global':
                    alteration = patch.dict(native._FrozenLoader.exec_module.__globals__, {'source_unit': object()})
                else:
                    alteration = patch.object(importlib.abc.Loader, '__getattribute__',
                                              lambda *args: (_ for _ in ()).throw(AssertionError('hook')),
                                              create=True)
                with alteration:
                    result = worker.import_only(dict(unit=unit['id'], plan_digest=plan['plan_digest'], **fixture.IDENTITY))
                self.assertEqual(result['status'], 'FAILED', result)
                self.assertEqual(binding.modules, {})
                self.assertEqual(binding.owners, {})

    def test_builtin_and_importlib_replacements_are_rejected_without_calling_them(self):
        for target, name in ((builtins, '__import__'), (builtins, 'compile'), (builtins, 'exec'),
                             (importlib, 'import_module'), (importlib.util, 'spec_from_loader'),
                             (importlib._bootstrap, '_find_and_load'),
                             (importlib._bootstrap._imp, 'acquire_lock')):
            with self.subTest(name=name):
                _, unit, worker, binding, _, plan = self.binding()
                hits = []
                def changed(*args, **kwargs):
                    hits.append(name)
                    raise AssertionError('unproved import mechanism executed')
                with patch.object(target, name, changed):
                    result = worker.import_only(dict(unit=unit['id'], plan_digest=plan['plan_digest'], **fixture.IDENTITY))
                self.assertEqual(result['status'], 'FAILED', result)
                self.assertEqual(hits, [])
                self.assertEqual(binding.modules, {})
                self.assertEqual(binding.owners, {})

    def test_loader_module_unknown_string_keys_are_rejected_without_comparison(self):
        _, unit, worker, binding, _, plan = self.binding()
        namespace = importlib.util.__dict__
        function = namespace.pop('spec_from_loader')
        hits = []
        class Key(str):
            __hash__ = str.__hash__
            def __eq__(self, value):
                hits.append('eq')
                return str.__eq__(self, value)
        key = Key('spec_from_loader')
        namespace[key] = function
        try:
            hits.clear()
            result = worker.import_only(dict(unit=unit['id'], plan_digest=plan['plan_digest'], **fixture.IDENTITY))
            self.assertEqual(result['status'], 'FAILED', result)
            self.assertEqual(hits, [])
            self.assertEqual(binding.modules, {})
        finally:
            namespace.pop(key)
            namespace['spec_from_loader'] = function

    def test_function_metadata_subclass_is_rejected_without_bool_or_key_hooks(self):
        _, unit, worker, binding, entry, plan = self.binding()
        self.assertEqual(worker.import_only(dict(unit=unit['id'], plan_digest=plan['plan_digest'],
                                                **fixture.IDENTITY))['status'], 'IMPORTED')
        receipt = binding.import_only_entries[unit['id']]
        function = receipt['function']
        hits = []
        class Metadata(dict):
            def __bool__(self):
                hits.append('bool')
                return False
            def __iter__(self):
                hits.append('iter')
                return super().__iter__()
        class Key(str):
            __hash__ = str.__hash__
            def __eq__(self, value):
                hits.append('eq')
                return str.__eq__(self, value)
        for metadata in (Metadata(), {Key('__wrapped__'): object()}):
            with self.subTest(kind=type(metadata).__name__):
                function.__dict__ = metadata
                hits.clear()
                with binding._context():
                    with self.assertRaises(native.MissingBinding):
                        binding._capture_import_only_target(entry, receipt['namespace'])
                    with self.assertRaises(native.MissingBinding):
                        binding._capture_imported_helpers(plan)
                self.assertEqual(hits, [])
        function.__dict__ = {}


if __name__ == '__main__':
    unittest.main()
