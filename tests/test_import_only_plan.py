"""Import-only source plans are replayed, never candidate permissions."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from seampact.source.import_only import compile_import_only_plan, verify_import_only_plan
from seampact.source.inspection import Inspector


class ImportOnlyPlanTests(unittest.TestCase):
    resources = dict(requirements=[], bindings={})

    def fixture(self, files, path='pkg/entry.py', kind='function', symbol='score'):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        for name, text in files.items():
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding='utf-8')
        inspector = Inspector.freeze(root, root / 'snapshots', 'request', list(files), 'entry', 'result')
        entry = dict(id='score', kind=kind, path=path, symbol=symbol)
        if kind == 'method': entry['constructor'] = dict(symbol=symbol.split('.')[0], arguments={})
        return inspector, entry, dict(entries=[entry], boundaries=[])

    def test_complete_local_parent_child_plan_has_a_stable_replayable_positive(self):
        inspector, entry, candidate = self.fixture({'pkg/__init__.py': "CONFIG='fixed'\n",
            'pkg/entry.py': 'def score(value):\n    return value\n'})
        plan = compile_import_only_plan(inspector, entry, candidate, self.resources)
        self.assertEqual(plan['status'], 'CHECKED', plan)
        self.assertEqual(plan['modules_order'], ['pkg/__init__.py', 'pkg/entry.py'])
        self.assertTrue(verify_import_only_plan(inspector, entry, candidate, self.resources, plan))
        self.assertNotIn('pkg.entry', sys.modules)

    def test_import_plan_does_not_enter_owner_constructor_initializers_or_body(self):
        inspector, entry, candidate = self.fixture({'pkg/__init__.py':'', 'pkg/entry.py':
            'class Source:\n    def __init__(self):\n        import sdk\n        sdk.model()\n'
            '    def prepare(self):\n        import sdk\n        sdk.initializer()\n'
            '    def score(self,value):\n        import sdk\n        return sdk.detect(value)\n'},
            kind='method', symbol='Source.score')
        entry['initializers'] = [dict(symbol='prepare', arguments={})]
        plan = compile_import_only_plan(inspector, entry, candidate, self.resources)
        self.assertEqual(plan['status'], 'CHECKED', plan)
        self.assertEqual(plan['top_calls'], [])

    def test_sdk_find_download_and_candidate_flags_do_not_make_an_unknown_plan_checked(self):
        inspector, entry, candidate = self.fixture({'pkg/__init__.py':
            "import sdk\ntry:\n    sdk.find('data')\nexcept LookupError:\n    sdk.download('data')\n",
            'pkg/entry.py': 'def score(value):\n    return value\n'})
        entry['import_permission'] = True
        candidate['import_summary'] = dict(status='CHECKED', no_model=True)
        plan = compile_import_only_plan(inspector, entry, candidate, self.resources)
        self.assertEqual(plan['status'], 'UNKNOWN')
        self.assertEqual([row['target'] for row in plan['top_calls']], ['sdk.find','sdk.download'])
        forged = dict(plan, status='CHECKED')
        self.assertFalse(verify_import_only_plan(inspector, entry, candidate, self.resources, forged))

    def test_entry_candidate_and_frozen_resource_changes_refuse_the_same_plan(self):
        inspector, entry, candidate = self.fixture({'pkg/__init__.py':'',
            'pkg/entry.py':'def score(value):\n    return value\n'})
        plan = compile_import_only_plan(inspector, entry, candidate, self.resources)
        for resources, changed in ((dict(requirements=[dict(id='changed')],bindings={}), candidate),
                                    (self.resources, dict(candidate, configuration='changed'))):
            self.assertFalse(verify_import_only_plan(inspector, entry, changed, resources, plan))
        altered_entry = dict(entry, fixed_arguments=dict(value=[]))
        self.assertFalse(verify_import_only_plan(inspector, altered_entry, dict(entries=[altered_entry]), self.resources, plan))

    def test_unknown_metadata_keys_are_rejected_without_protocol_hooks(self):
        touched = []
        class Key(str):
            def __hash__(self): touched.append('hash'); return super().__hash__()
            def __eq__(self, other): touched.append('eq'); return super().__eq__(other)
        inspector, entry, candidate = self.fixture({'pkg/__init__.py':'',
            'pkg/entry.py':'def score(value):\n    return value\n'})
        resources = {Key('bindings'): {}}
        touched.clear()
        plan = compile_import_only_plan(inspector, entry, candidate, resources)
        self.assertEqual(plan['status'], 'UNKNOWN')
        self.assertEqual(touched, [])


if __name__ == '__main__':
    unittest.main()
