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

    def test_imported_class_constructor_bound_method_and_attribute_callback(self):
        rows = self.inspect({'main.py': 'from tasks import Worker\ndef host():\n    worker = Worker(child)\n    worker.run()\ndef child():\n    pass\n',
                             'tasks.py': 'from external import Base\nclass Worker(Base):\n    def __init__(self, callback):\n        self.callback = callback\n    def run(self):\n        self.callback()\n    def unused(self):\n        missing()\n'},
                            [self.function('host'), self.function('child')])
        self.assertEqual(rows['host']['dependency_ids'], ['service'])
        self.assertEqual([u for u in rows['host']['unresolved_targets']], [])
        self.assertEqual({e['target_symbol'] for e in rows['host']['call_edges']}, {'Worker', 'Worker.__init__', 'Worker.run', 'host', 'child'})
        method = next(e for e in rows['host']['call_edges'] if e['target_symbol'] == 'Worker.run')
        self.assertEqual(method['evidence'][0]['start'], 4)

    def test_local_inheritance_super_static_and_class_methods(self):
        source = ('def child():\n    pass\nclass Base:\n    def __init__(self):\n        child()\n'
                  '    def run(self):\n        child()\nclass Worker(Base):\n'
                  '    def __init__(self):\n        super().__init__()\n'
                  '    @staticmethod\n    def static():\n        child()\n'
                  '    @classmethod\n    def create(cls):\n        return cls()\n'
                  'def host():\n    worker = Worker.create()\n    worker.run()\n    Worker.static()\n')
        rows = self.inspect({'main.py': source}, [self.function('host'), self.function('child')])
        self.assertEqual(rows['host']['dependency_ids'], ['service'])
        self.assertEqual(rows['host']['unresolved_targets'], [])
        symbols = {e['target_symbol'] for e in rows['host']['call_edges']}
        self.assertTrue({'Worker.create', 'Worker.__init__', 'Base.__init__', 'Base.run', 'Worker.static'} <= symbols)

    def test_class_and_instance_rebinding_do_not_reach_old_method(self):
        prefix = 'def child():\n    pass\nclass Worker:\n    def run(self):\n        child()\n'
        for body in ('Worker = unknown\n    Worker()', 'worker = Worker()\n    worker.run = unknown\n    worker.run()',
                     'Worker.run = unknown\n    Worker().run()'):
            with self.subTest(body=body):
                rows = self.inspect({'main.py': prefix + 'def host():\n    ' + body + '\n'},
                                    [self.function('host'), self.function('child')])
                self.assertEqual(rows['host']['dependency_ids'], [])
                self.assertIn('dependency_closure_unsupported', {i['issue_code'] for i in rows['host']['issues']})

    def test_dynamic_class_protocols_and_decorators_are_rule_gaps(self):
        for definition in ('class Worker(dynamic):\n    pass',
                           '@unknown\nclass Worker:\n    pass',
                           'class Worker:\n    def __new__(cls):\n        return child()',
                           'class Worker:\n    @unknown\n    @staticmethod\n    def run():\n        child()'):
            with self.subTest(definition=definition):
                call = 'Worker().run()' if 'def run' in definition else 'Worker()'
                rows = self.inspect({'main.py': 'def child():\n    pass\n' + definition + '\ndef host():\n    ' + call + '\n'},
                                    [self.function('host'), self.function('child')])
                self.assertEqual(rows['host']['dependency_ids'], [])
                self.assertTrue(rows['host']['unresolved_targets'])
                self.assertTrue(all(i['issue_code'] == 'dependency_closure_unsupported' for i in rows['host']['issues']))
                self.assertTrue(all(i['repair_scope'] == 'rule' and i['model_repairable'] is False for i in rows['host']['issues']))

    def test_dataclass_fields_post_init_and_factory_follow_actual_calls(self):
        source = ('from dataclasses import dataclass, field\ndef child():\n    pass\n'
                  'def factory():\n    child()\n    return child\n'
                  '@dataclass(frozen=True)\nclass Job:\n    callback: object\n'
                  '    generated: object = field(default_factory=factory)\n'
                  '    def __post_init__(self):\n        self.callback()\n'
                  '    def run(self):\n        self.generated()\n'
                  'def host():\n    job = Job(child)\n    job.run()\n')
        rows = self.inspect({'main.py': source}, [self.function('host'), self.function('child')])
        self.assertEqual(rows['host']['dependency_ids'], ['service'])
        self.assertEqual(rows['host']['unresolved_targets'], [])
        symbols = {e['target_symbol'] for e in rows['host']['call_edges']}
        self.assertTrue({'Job', 'Job.__post_init__', 'factory', 'Job.run'} <= symbols)
        # Overridden factory is not a call edge; unconstructed dataclasses do not run factories.
        source = source.replace('job = Job(child)', 'job = Job(child, generated=print)')
        rows = self.inspect({'main.py': source}, [self.function('host'), self.function('child')])
        self.assertNotIn('factory', {e['target_symbol'] for e in rows['host']['call_edges']})

    def test_executor_nested_callback_keeps_lexical_binding_and_late_rebinding(self):
        source = ('from concurrent.futures import ThreadPoolExecutor\ndef child():\n    pass\n'
                  'def other():\n    pass\ndef host():\n    callback = other\n'
                  '    def process_row():\n        callback()\n'
                  '    def run_and_release():\n        process_row()\n'
                  '    callback = child\n    with ThreadPoolExecutor() as executor:\n'
                  '        executor.submit(run_and_release)\n')
        rows = self.inspect({'main.py': source}, [self.function('host'), self.function('child'), self.function('other')])
        self.assertEqual(rows['host']['dependency_ids'], ['service'])
        self.assertEqual(rows['host']['unresolved_targets'], [])
        symbols = {e['target_symbol'] for e in rows['host']['call_edges']}
        self.assertTrue({'host.<locals>.process_row', 'host.<locals>.run_and_release', 'child'} <= symbols)
        self.assertNotIn('other', symbols)

    def test_returned_closure_default_and_local_shadowing_use_python_bindings(self):
        prefix = 'def child():\n    pass\ndef other():\n    pass\n'
        source = prefix + ('def make():\n    callback = child\n    def nested(captured=callback):\n        captured()\n'
                           '    callback = other\n    return nested\ndef host():\n    make()()\n')
        rows = self.inspect({'main.py': source}, [self.function('host'), self.function('child'), self.function('other')])
        self.assertEqual(rows['host']['dependency_ids'], ['service'])
        self.assertNotIn('other', {e['target_symbol'] for e in rows['host']['call_edges']})
        source = prefix + 'def host():\n    child()\n    child = other\n'
        rows = self.inspect({'main.py': source}, [self.function('host'), self.function('child')])
        self.assertEqual(rows['host']['dependency_ids'], [])
        self.assertEqual(rows['host']['unresolved_targets'][0]['reason'], 'dynamic_call_target_unresolved')

    def test_called_nonlocal_rebinding_is_unsupported_and_invalidates_old_capture(self):
        source = ('def child():\n    pass\ndef host():\n    callback = child\n'
                  '    def mutate(value):\n        nonlocal callback\n        callback = value\n'
                  '    def run():\n        callback()\n    mutate(unknown)\n    run()\n')
        rows = self.inspect({'main.py': source}, [self.function('host'), self.function('child')])
        self.assertEqual(rows['host']['dependency_ids'], [])
        self.assertTrue(any(u['reason'] == 'lexical_binding_mutation_unsupported' and u['evidence'][0]['start'] == 6 for u in rows['host']['unresolved_targets']))
        self.assertTrue(all(i['issue_code'] == 'dependency_closure_unsupported' for i in rows['host']['issues']))

    def test_uncalled_classes_and_closures_do_not_run_bodies_or_factories(self):
        source = ('from dataclasses import dataclass, field\ndef child():\n    pass\n'
                  '@dataclass\nclass Job:\n    x: object = field(default_factory=child)\n'
                  '    def run(self):\n        child()\ndef host():\n'
                  '    callback = child\n    def unused():\n        nonlocal callback\n        callback = unknown\n        child()\n'
                  '    class Local:\n        def __init__(self):\n            child()\n    return 1\n')
        rows = self.inspect({'main.py': source}, [self.function('host'), self.function('child')])
        self.assertEqual(rows['host']['dependency_ids'], [])
        self.assertEqual(rows['host']['issues'], [])

    def test_recursive_and_step_limits_are_not_missing_source(self):
        for source in ('def host():\n    host()\n', 'def host():\n' + '    value = 1\n' * 16010):
            with self.subTest(size=len(source)):
                rows = self.inspect({'main.py': source}, [self.function('host')], [])
                self.assertEqual(rows['host']['status'], 'MISSING')
                self.assertEqual({i['issue_code'] for i in rows['host']['issues']}, {'dependency_closure_limit'})
                self.assertTrue(rows['host']['issues'][0]['evidence'])
                self.assertEqual(rows['host']['issues'][0]['repair_scope'], 'rule')

    def test_available_unmodelled_export_is_rule_gap_but_absent_source_stays_source_gap(self):
        rows = self.inspect({'main.py': 'from pkg.module import Runner\ndef host():\n    Runner()\n',
                             'pkg/module.py': 'if dynamic:\n    Runner = unknown\n'}, [self.function('host')], [])
        self.assertEqual({i['issue_code'] for i in rows['host']['issues']}, {'dependency_closure_unsupported'})
        rows = self.inspect({'main.py': 'from pkg.missing import Runner\ndef host():\n    Runner()\n',
                             'pkg/exists.py': ''}, [self.function('host')], [])
        self.assertEqual({i['issue_code'] for i in rows['host']['issues']}, {'dependency_closure_unresolved'})

    def test_opaque_external_comparisons_do_not_prune_original_calls(self):
        prefix = ('from argparse import ArgumentParser\nfrom external import Config\n'
                  'args = ArgumentParser().parse_args()\nconfig = Config()\n'
                  'def child():\n    pass\n')
        for expression in ('args.command == "evaluate"', 'config.MODEL.NAME == "detconfcmx"',
                           'args.command != "evaluate"', 'config.MODEL.NAME != "detconfcmx"'):
            with self.subTest(expression=expression):
                rows = self.inspect({'main.py': prefix + 'def host():\n    if ' + expression + ':\n        child()\n'},
                                    [self.function('host'), self.function('child')])
                self.assertEqual(rows['host']['dependency_ids'], ['service'])

    def test_known_scalar_comparisons_select_only_the_original_branch(self):
        prefix = 'def child():\n    pass\ndef other():\n    pass\n'
        for expression, expected in (('"evaluate" == "evaluate"', 'child'), ('1 != 2', 'child'),
                                     ('1.5 == 2.0', 'other'), ('True != True', 'other')):
            with self.subTest(expression=expression):
                source = prefix + 'def host():\n    if ' + expression + ':\n        child()\n    else:\n        other()\n'
                rows = self.inspect({'main.py': source}, [self.function('host'), self.function('child'), self.function('other')],
                                    [dict(id='service', entry_ids=['child']), dict(id='other-service', entry_ids=['other'])])
                self.assertEqual(rows['host']['dependency_ids'], ['service' if expected == 'child' else 'other-service'])


if __name__ == '__main__':
    unittest.main()
