"""Finite host dependency scope checks, with no source execution."""

import copy
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from seampact.dependency_closure import dependency_closure, entry_dependency_issues
from seampact.inspection import Inspector


class DependencyClosureChecks(unittest.TestCase):
    def inspect(self, files, entries, dependencies=None):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        for path, source in files.items():
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(source, encoding='utf-8')
        inspector = Inspector.freeze(root, root / 'snapshots', 'closure', list(files), 'entry', 'output')
        candidate = dict(entries=entries, dependencies=dependencies if dependencies is not None else
                         [dict(id='service', entry_ids=['child'])])
        original = copy.deepcopy(candidate)
        result = dependency_closure(inspector, candidate)
        self.assertEqual(candidate, original)
        self.inspector, self.candidate = inspector, candidate
        return result['entries']

    @staticmethod
    def function(identity, path='main.py', symbol=None, **kwargs):
        return dict(id=identity, path=path, symbol=symbol or identity, kind='function', **kwargs)

    @staticmethod
    def script(identity, path):
        return dict(id=identity, path=path, symbol='__main__', kind='cli', command=['python', path])

    def test_real_call_adds_dependency_and_evidence(self):
        rows = self.inspect({'main.py': 'def child():\n    pass\ndef host():\n    child()\n'},
                            [self.function('host'), self.function('child')])
        self.assertEqual(rows['host']['dependency_ids'], ['service'])
        self.assertEqual(rows['host']['missing_dependency_ids'], ['service'])
        self.assertEqual(rows['host']['issues'][0]['issue_code'], 'host_dependency_missing')
        self.assertTrue(any(e['start'] == 4 for e in rows['host']['evidence']))

    def test_unused_nested_functions_examples_and_false_paths_are_not_edges(self):
        source = ('def child():\n    pass\ndef example():\n    child()\n'
                  'def host():\n    def nested():\n        child()\n    if False:\n        child()\n'
                  '    return 1\n    child()\n')
        rows = self.inspect({'main.py': source}, [self.function('host'), self.function('child')])
        self.assertEqual(rows['host']['dependency_ids'], [])
        self.assertEqual(rows['host']['issues'], [])

    def test_only_called_same_repository_entry_receives_dependency(self):
        rows = self.inspect({'main.py': 'def child():\n    pass\ndef unrelated():\n    pass\ndef host():\n    child()\n'},
            [self.function('host'), self.function('child'), self.function('unrelated')],
            [dict(id='service', entry_ids=['child']), dict(id='other', entry_ids=['unrelated'])])
        self.assertEqual(rows['host']['dependency_ids'], ['service'])

    def test_unique_import_and_alias_forwarding(self):
        rows = self.inspect({'main.py': 'from tasks import child as run\ndef host():\n    alias = run\n    alias()\n',
                             'tasks.py': 'def child():\n    return 1\n'},
                            [self.function('host'), self.function('child', 'tasks.py')])
        self.assertEqual(rows['host']['dependency_ids'], ['service'])

    def test_rebinding_import_does_not_reach_original(self):
        rows = self.inspect({'main.py': 'from tasks import child\ndef host():\n    child = print\n    child()\n',
                             'tasks.py': 'def child():\n    return 1\n'},
                            [self.function('host'), self.function('child', 'tasks.py')])
        self.assertEqual(rows['host']['dependency_ids'], [])

    def test_custom_path_function_is_not_pathlib(self):
        rows = self.inspect({'main.py': 'import subprocess\ndef Path(value):\n    return "other.py"\n'
                             'def host():\n    subprocess.run(["python", Path("child.py")])\n',
                             'child.py': 'pass\n', 'other.py': 'pass\n'},
            [self.function('host'), self.script('child', 'child.py'), self.script('other', 'other.py')],
            [dict(id='service', entry_ids=['child']), dict(id='other-service', entry_ids=['other'])])
        self.assertEqual(rows['host']['dependency_ids'], ['other-service'])

    def test_static_argv_helper_and_frozen_dictionary_select_one_stage(self):
        rows = self.inspect({'main.py': 'import subprocess\nimport sys\nSCRIPTS = {"selected": "child.py", "unused": "other.py"}\n'
                             'def launch(stage):\n    cmd = [sys.executable, SCRIPTS[stage]]\n    subprocess.run(cmd)\n'
                             'def host():\n    launch("selected")\n',
                             'child.py': 'print("child")\n', 'other.py': 'print("other")\n'},
            [self.function('host'), self.script('child', 'child.py'), self.script('other', 'other.py')],
            [dict(id='service', entry_ids=['child']), dict(id='other-service', entry_ids=['other'])])
        self.assertEqual(rows['host']['dependency_ids'], ['service'])
        self.assertEqual(rows['host']['process_edges'][0]['target_path'], 'child.py')
        self.assertEqual(rows['host']['process_edges'][0]['environment']['mode'], 'inherit')

    def test_dynamic_dictionary_key_is_unknown_not_all_values(self):
        rows = self.inspect({'main.py': 'import subprocess\nSCRIPTS = {"a":"child.py", "b":"other.py"}\n'
                             'def host(stage):\n    subprocess.run(["python", SCRIPTS[stage]])\n',
                             'child.py': 'pass\n', 'other.py': 'pass\n'},
                            [self.function('host'), self.script('child', 'child.py'), self.script('other', 'other.py')])
        self.assertEqual(rows['host']['dependency_ids'], [])
        self.assertEqual(rows['host']['unresolved_targets'][0]['reason'], 'subprocess_target_unresolved')

    def test_conditional_argv_mutation_does_not_join_branch_values(self):
        rows = self.inspect({'main.py': 'import subprocess\ndef host(selected):\n    cmd = ["python"]\n'
                             '    if selected:\n        cmd.append("child.py")\n    else:\n        cmd.append("other.py")\n'
                             '    subprocess.run(cmd)\n', 'child.py': 'pass\n', 'other.py': 'pass\n'},
                            [self.function('host'), self.script('child', 'child.py'), self.script('other', 'other.py')])
        self.assertEqual(rows['host']['dependency_ids'], [])
        self.assertEqual(rows['host']['process_edges'][0]['target_path'], None)

    def test_unknown_dictionary_overlay_or_mutation_does_not_preserve_old_command(self):
        for setup in ('SCRIPTS = {"a":"child.py", **unknown}',
                      'SCRIPTS = {"a":"child.py"}\nSCRIPTS.update(unknown)',
                      'SCRIPTS = {"a":"child.py"}\nif condition:\n    SCRIPTS = unknown',
                      'SCRIPTS = {"a":"child.py"}\nconfigure(SCRIPTS)'):
            with self.subTest(setup=setup):
                rows = self.inspect({'main.py': 'import subprocess\n' + setup + '\n'
                                     'def host():\n    subprocess.run(["python", SCRIPTS["a"]])\n', 'child.py': 'pass\n'},
                                    [self.function('host'), self.script('child', 'child.py')])
                self.assertEqual(rows['host']['dependency_ids'], [])
                self.assertTrue(rows['host']['unresolved_targets'])

    def test_unsupported_method_scope_is_not_reported_as_checked(self):
        rows = self.inspect({'main.py': 'class Host:\n    def run(self):\n        pass\n'},
                            [self.function('host', symbol='Host.run')], [])
        self.assertEqual(rows['host']['status'], 'NOT_COVERED')
        self.assertFalse(rows['host']['scope_covered'])
        self.assertTrue(rows['host']['unresolved_targets'])

    def test_main_guard_script_and_pathlib_original_argv(self):
        rows = self.inspect({'scripts/host.py': 'from pathlib import Path\nimport subprocess\nimport sys\n'
                             'ROOT = Path(__file__).resolve().parents[1]\ndef main():\n'
                             '    subprocess.run([sys.executable, str(ROOT / "child.py")], cwd=ROOT)\n'
                             'if __name__ == "__main__":\n    raise SystemExit(main())\n', 'child.py': 'pass\n'},
                            [self.script('host', 'scripts/host.py'), self.script('child', 'child.py')])
        self.assertEqual(rows['host']['dependency_ids'], ['service'])
        self.assertEqual(rows['host']['process_edges'][0]['cwd'], '.')

    def test_subprocess_uses_proven_entry_directory_and_child_keeps_it(self):
        host = self.script('host', 'app/host.py')
        host['cwd'] = dict(path='app', evidence=[dict(path='app/README.md', start=1, end=1)])
        rows = self.inspect({'app/host.py': 'import subprocess\nsubprocess.run(["python", "middle.py"], cwd=None)\n',
                             'app/middle.py': 'import subprocess\nsubprocess.run(["python", "child.py"])\n',
                             'app/child.py': 'pass\n', 'child.py': 'pass\n',
                             'app/README.md': 'python host.py\n'},
                            [host, self.script('child', 'app/child.py'), self.script('other', 'child.py')])
        self.assertEqual(rows['host']['dependency_ids'], ['service'])
        self.assertEqual([e['cwd'] for e in rows['host']['process_edges']], ['app', 'app'])

    def test_relative_child_directory_is_joined_to_original_process_directory(self):
        host = self.script('host', 'app/host.py')
        host['cwd'] = dict(path='app', evidence=[dict(path='app/README.md', start=1, end=1)])
        rows = self.inspect({'app/host.py': 'import subprocess\nsubprocess.run(["python", "child.py"], cwd="nested")\n',
                             'app/nested/child.py': 'pass\n', 'app/README.md': 'python host.py\n'},
                            [host, self.script('child', 'app/nested/child.py')])
        self.assertEqual(rows['host']['dependency_ids'], ['service'])
        self.assertEqual(rows['host']['process_edges'][0]['cwd'], 'app/nested')

    def test_unknown_subprocess_directory_is_not_defaulted_to_repository_root(self):
        rows = self.inspect({'main.py': 'import subprocess\ndef host(directory):\n'
                             '    subprocess.run(["python", "child.py"], cwd=directory)\n',
                             'child.py': 'pass\n'}, [self.function('host'), self.script('child', 'child.py')])
        self.assertEqual(rows['host']['dependency_ids'], [])
        self.assertIsNone(rows['host']['process_edges'][0]['cwd'])
        self.assertTrue(rows['host']['unresolved_targets'])

    def test_callback_and_custom_decorator_stay_unresolved(self):
        rows = self.inspect({'main.py': 'def host(callback):\n    callback()\n'}, [self.function('host')], [])
        self.assertEqual(rows['host']['unresolved_targets'][0]['reason'], 'dynamic_call_target_unresolved')
        rows = self.inspect({'main.py': 'def decorate(f):\n    return print\n@decorate\ndef child():\n    pass\n'
                             'def host():\n    child()\n'}, [self.function('host'), self.function('child')])
        self.assertEqual(rows['host']['dependency_ids'], [])
        self.assertEqual(rows['host']['unresolved_targets'][0]['reason'], 'decorated_call_target_unresolved')

    def test_frozen_package_root_resolves_absolute_internal_import(self):
        rows = self.inspect({'project/src/__init__.py': '', 'project/src/host.py':
                             'from src.tasks import child\ndef host():\n    child()\n',
                             'project/src/tasks.py': 'def child():\n    pass\n'},
                            [self.function('host', 'project/src/host.py'), self.function('child', 'project/src/tasks.py')])
        self.assertEqual(rows['host']['dependency_ids'], ['service'])

    def test_missing_local_called_source_blocks_but_unused_import_does_not(self):
        files = {'pkg/exists.py': 'pass\n', 'main.py': 'from pkg.missing import run\ndef host():\n    run()\ndef unused():\n    pass\n'}
        rows = self.inspect(files, [self.function('host'), self.function('unused')])
        self.assertEqual(rows['host']['issues'][0]['issue_code'], 'dependency_closure_unresolved')
        self.assertEqual(rows['unused']['issues'], [])

    def test_third_party_calls_are_not_missing_local_source(self):
        rows = self.inspect({'main.py': 'import requests\ndef host():\n    requests.get("https://example.com")\n'},
                            [self.function('host')], [])
        self.assertEqual(rows['host']['issues'], [])

    def test_external_object_export_is_distinct_from_missing_local_source(self):
        rows = self.inspect({'main.py': 'from settings import config, update\ndef host():\n    update(config)\n',
                             'settings.py': 'from yacs.config import CfgNode as CN\nconfig = CN()\n'
                             'config.TEST = CN()\nconfig.TEST.MODEL_FILE = ""\n'
                             'def update(cfg):\n    cfg.defrost()\n    cfg.freeze()\n'},
                            [self.function('host')], [])
        self.assertEqual(rows['host']['issues'], [])
        self.assertTrue(any(edge['target_symbol'] == 'update' for edge in rows['host']['call_edges']))

    def test_external_object_explicit_method_writes_keep_local_calls_and_unknown(self):
        for operation, expected in [('config.run = child', ['service']), ('config.run = unknown', [])]:
            with self.subTest(operation=operation):
                rows = self.inspect({'main.py': 'from external import Object\nconfig = Object()\n'
                                     'def child():\n    pass\n' + operation + '\n'
                                     'def host():\n    alias = config\n    alias.run()\n'},
                                    [self.function('host'), self.function('child')])
                self.assertEqual(rows['host']['dependency_ids'], expected)
                if not expected:
                    self.assertTrue(rows['host']['unresolved_targets'])

    def test_host_membership_never_proves_transfer_and_cache_tracks_candidate(self):
        self.inspect({'main.py': 'def child():\n    pass\ndef host():\n    child()\n'},
                     [self.function('host'), self.function('child')])
        first = entry_dependency_issues(self.inspector, self.candidate, self.candidate['entries'][0])
        self.assertEqual(first[0]['issue_code'], 'host_dependency_missing')
        first.clear()
        self.assertTrue(entry_dependency_issues(self.inspector, self.candidate, self.candidate['entries'][0]))
        self.candidate['dependencies'][0]['entry_ids'].append('host')
        rows = dependency_closure(self.inspector, self.candidate)['entries']
        self.assertEqual(rows['host']['missing_dependency_ids'], [])
        self.assertEqual(rows['host']['dependency_ids'], ['service'])
        # Membership is only a scope check; dependency binding still checks mappings.

    def test_shell_or_overwritten_argv_keeps_unknown(self):
        for command in ('cmd = unknown\n    subprocess.run(cmd)',
                        'subprocess.run(["python", "child.py"], shell=True)'):
            with self.subTest(command=command):
                rows = self.inspect({'main.py': 'import subprocess\ndef host():\n    ' + command + '\n', 'child.py': 'pass\n'},
                                    [self.function('host'), self.script('child', 'child.py')])
                self.assertEqual(rows['host']['dependency_ids'], [])
                self.assertTrue(rows['host']['unresolved_targets'])

    def test_overwritten_subprocess_operation_is_not_original_launch(self):
        rows = self.inspect({'main.py': 'import subprocess\nsubprocess.run = unknown\ndef host():\n'
                             '    subprocess.run(["python", "child.py"])\n', 'child.py': 'pass\n'},
                            [self.function('host'), self.script('child', 'child.py')])
        self.assertEqual(rows['host']['dependency_ids'], [])
        self.assertEqual(rows['host']['unresolved_targets'][0]['reason'], 'dynamic_call_target_unresolved')


if __name__ == '__main__':
    unittest.main()
