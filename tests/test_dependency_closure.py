"""Finite host dependency scope checks, with no source execution."""

import ast
import copy
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from seampact.source.dependency_closure import _Closure, dependency_closure, entry_dependency_issues, resolve_resource_calls
from seampact.source.inspection import Inspector


class DependencyClosureChecks(unittest.TestCase):
    def test_replay_tree_preserves_lexical_identity_without_cross_scope_cache(self):
        self.inspect({'main.py': 'def host(isinstance):\n    return isinstance(None, str)\n'},
                     [self.function('host')], [])
        checker = _Closure(self.inspector, self.candidate)
        tree = checker.tree('main.py')
        call = next(node for node in ast.walk(tree) if isinstance(node, ast.Call))
        self.assertIs(tree, checker.tree('main.py'))
        self.assertIs(tree, checker.sdk_source.tree('main.py'))
        self.assertIsNone(checker.sdk_source.type_references.resolve('main.py', call.func, call))
        self.assertEqual(checker.sdk_source.type_references.builtin('main.py', call)['status'], 'UNKNOWN')
        other = _Closure(self.inspector, self.candidate)
        self.assertIsNot(tree, other.tree('main.py'))

    def test_public_optional_parameter_is_not_frozen_to_its_none_default(self):
        for annotation in ('', ': str', ': str | None'):
            with self.subTest(annotation=annotation):
                rows = self.inspect({'main.py': 'def host(value' + annotation + '=None):\n'
                    '    if value is not None:\n        eval(value)\n'}, [self.function('host')], [])
                self.assertTrue(any(t['target']=='eval' for t in rows['host']['unresolved_targets']))

    def test_public_keyword_only_and_scalar_defaults_remain_overridable(self):
        for parameter, condition in (('*, value: str = None', 'value is not None'),
                                     ('value: str = "safe"', 'value == "unsafe"')):
            with self.subTest(parameter=parameter):
                rows = self.inspect({'main.py': 'def host(' + parameter + '):\n'
                    '    if ' + condition + ':\n        eval(value)\n'}, [self.function('host')], [])
                self.assertTrue(any(t['target']=='eval' for t in rows['host']['unresolved_targets']))

    def test_explicit_fixed_none_is_kept_while_internal_omission_uses_default(self):
        rows = self.inspect({'main.py': 'def host(value: str=None):\n'
            '    if value is not None:\n        eval(value)\n'}, [self.function('host',fixed_arguments=dict(value=None))], [])
        self.assertEqual(rows['host']['unresolved_targets'], [])
        rows = self.inspect({'main.py': 'def helper(value: str=None):\n'
            '    if value is not None:\n        eval(value)\ndef host():\n    return helper()\n'},
            [self.function('host'),self.function('helper')], [])
        self.assertEqual(rows['host']['unresolved_targets'], [])
        self.assertTrue(any(t['target']=='eval' for t in rows['helper']['unresolved_targets']))

    def test_open_public_kwargs_cannot_become_omitted_helper_arguments(self):
        rows = self.inspect({'main.py': 'def helper(value: str=None):\n'
            '    if value is not None:\n        eval(value)\ndef host(**kwargs):\n    return helper(**kwargs)\n'},
            [self.function('host')], [])
        self.assertTrue(any(t['target']=='eval' for t in rows['host']['unresolved_targets']))

    def test_keyword_expansion_preserves_known_internal_none_and_not_unknown_mutation(self):
        source = ('def helper(value: str=None):\n    if value is not None:\n        eval(value)\n'
                  'def host():\n    options={"value": None}\n    {mutation}\n    return helper(**options)\n')
        rows = self.inspect({'main.py':source.replace('{mutation}', 'pass')}, [self.function('host')], [])
        self.assertEqual(rows['host']['unresolved_targets'], [])
        rows = self.inspect({'main.py':source.replace('{mutation}', 'external(options)')}, [self.function('host')], [])
        self.assertTrue(any(t['target']=='eval' for t in rows['host']['unresolved_targets']))

    def test_open_public_positionals_cannot_become_omitted_helper_arguments(self):
        rows = self.inspect({'main.py': 'def helper(first=None, value: str=None):\n'
            '    if value is not None:\n        eval(value)\ndef host(*args):\n    return helper(*args)\n'},
            [self.function('host')], [])
        self.assertTrue(any(t['target']=='eval' for t in rows['host']['unresolved_targets']))

    def test_unresolved_dependency_reference_is_not_a_literal_dictionary(self):
        rows = self.inspect({'main.py': 'def host(value: str=None):\n'
            '    if value is None:\n        eval("dynamic")\n'}, [self.function('host',
            fixed_arguments=dict(value={'$dependency':dict(id='resource',field='parameters.value')}))], [])
        self.assertTrue(any(t['target']=='eval' for t in rows['host']['unresolved_targets']))

    def test_literal_none_optional_callback_is_not_a_reachable_call(self):
        rows = self.inspect({'main.py': 'def helper(callback=None):\n    if callback is not None:\n        callback()\n    return 1\ndef host():\n    return helper()\n'},
                            [self.function('host')], [])
        self.assertEqual(rows['host']['unresolved_targets'], [])

    def test_unknown_callback_is_not_confused_with_literal_none(self):
        rows = self.inspect({'main.py': 'def host(callback):\n    if callback is not None:\n        callback()\n'},
                            [self.function('host')], [])
        self.assertTrue(any(t['target'] == 'callback' for t in rows['host']['unresolved_targets']))

    def test_real_optional_callback_keeps_its_dependency(self):
        rows = self.inspect({'main.py': 'def child():\n    return 2\ndef helper(callback=None):\n    if callback is not None:\n        return callback()\ndef host():\n    return helper(child)\n'},
                            [self.function('host'), self.function('child')])
        self.assertEqual(rows['host']['dependency_ids'], ['service'])

    def test_branch_union_with_none_retains_possible_callback(self):
        rows = self.inspect({'main.py': 'def child():\n    return 2\ndef host(choice):\n    if choice:\n        callback = child\n    else:\n        callback = None\n    if callback is not None:\n        callback()\n'},
                            [self.function('host'), self.function('child')])
        # A mixed branch may not be erased merely because one branch is None.
        self.assertTrue(rows['host']['dependency_ids'] or rows['host']['unresolved_targets'])

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

    def test_conditional_imported_call_checks_both_possible_targets(self):
        rows = self.inspect({'main.py': 'def host(choice):\n    if choice:\n        from a import run as parser\n    else:\n        from b import run as parser\n    parser()\n',
                             'a.py': 'def run():\n    pass\n', 'b.py': 'def run():\n    pass\n'},
                            [self.function('host'), self.function('a', 'a.py', 'run'), self.function('b', 'b.py', 'run')],
                            [dict(id='a-service', entry_ids=['a']), dict(id='b-service', entry_ids=['b'])])
        self.assertEqual(set(rows['host']['dependency_ids']), {'a-service', 'b-service'})
        self.assertFalse(rows['host']['unresolved_targets'])

    def test_conditional_missing_binding_remains_unknown(self):
        rows = self.inspect({'main.py': 'def child():\n    pass\ndef host(choice):\n    if choice:\n        parser = child\n    parser()\n'},
                            [self.function('host'), self.function('child')])
        self.assertEqual(rows['host']['dependency_ids'], ['service'])
        self.assertTrue(any(x['reason'] == 'dynamic_call_target_unresolved' for x in rows['host']['unresolved_targets']))

    def test_nested_callable_survives_unknown_branch(self):
        rows = self.inspect({'main.py': 'def child():\n    pass\ndef host(choice):\n    def attempt():\n        child()\n    if choice:\n        marker = 1\n    else:\n        marker = 2\n    attempt()\n'},
                            [self.function('host'), self.function('child')])
        self.assertEqual(rows['host']['dependency_ids'], ['service'])
        self.assertFalse(rows['host']['unresolved_targets'])

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

    def test_known_external_factory_lazy_singleton_keeps_original_call_chain(self):
        for guard in ('not pipeline', 'pipeline is None'):
            with self.subTest(guard=guard):
                source = ('import spacy\npipeline = None\ndef sentences(text):\n    global pipeline\n'
                          f'    if {guard}:\n        pipeline = spacy.load("test_pipeline")\n'
                          '    return pipeline(text)\ndef host():\n    sentences("first")\n    sentences("second")\n')
                rows = self.inspect({'main.py': source}, [self.function('host')], [])
                self.assertEqual(rows['host']['status'], 'CHECKED', rows['host'])
                protocols = rows['host']['external_protocols']
                self.assertEqual({row['rule'] for row in protocols} - {'source-known-branch/1'},
                    {'module-lazy-external-singleton/1', 'external-callable-factory/1', 'external-factory-result-call/1'})
                factory = next(row for row in protocols if row['rule'] == 'external-callable-factory/1')
                self.assertEqual(factory['resource']['name'], 'test_pipeline')
                self.assertEqual(factory['resource']['status'], 'ENVIRONMENT_NOT_CHECKED')

    def test_lazy_global_protocol_rejects_other_writers_factories_and_module_patches(self):
        source = ('import spacy\npipeline = None\ndef sentences(text):\n    global pipeline\n'
                  '    if not pipeline:\n        pipeline = spacy.load("test_pipeline")\n'
                  '    return pipeline(text)\ndef host():\n    sentences("text")\n')
        cases = [
            ({'main.py': source.replace('spacy.load(', 'spacy.other(')}),
            ({'main.py': source.replace('"test_pipeline"', 'unknown_model')}),
            ({'main.py': source + '\ndef reset():\n    global pipeline\n    pipeline = unknown\n'}),
            ({'main.py': source, 'patch.py': 'import spacy as sdk\nsdk.load = replacement\n'}),
            ({'main.py': source, 'patch.py': 'import main as original\noriginal.pipeline = unknown\n'}),
            ({'main.py': source, 'patch.py': 'import main as original\nsetattr(original, "pipeline", unknown)\n'}),
            ({'main.py': source.replace('    if not pipeline:', '    before = pipeline(text)\n    if not pipeline:')}),
            ({'main.py': source + '\ndef expose():\n    return pipeline\n'}),
            ({'main.py': source + '\ndef configure():\n    pipeline.add_pipe(callback)\n'}),
        ]
        for files in cases:
            with self.subTest(files=files):
                row = self.inspect(files, [self.function('host')], [])['host']
                self.assertEqual(row['status'], 'MISSING', row)
                self.assertFalse(any(protocol['rule'] == 'module-lazy-external-singleton/1'
                                     for protocol in row['external_protocols']))

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

    def test_lambda_callbacks_and_branch_bindings_keep_all_targets(self):
        prefix = ('def child():\n    pass\ndef other():\n    pass\n'
                  'def host(choice):\n    def attempt(callback):\n        callback()\n')
        source = prefix + ('    if choice:\n        run = lambda: child()\n'
                           '    else:\n        run = lambda: other()\n    attempt(run)\n')
        rows = self.inspect({'main.py': source}, [self.function('host'), self.function('child'), self.function('other')],
                            [dict(id='service', entry_ids=['child']), dict(id='other-service', entry_ids=['other'])])
        self.assertEqual(rows['host']['dependency_ids'], ['service', 'other-service'])
        self.assertEqual(rows['host']['unresolved_targets'], [])
        source = source.replace('run = lambda: other()', 'run = unknown')
        rows = self.inspect({'main.py': source}, [self.function('host'), self.function('child')])
        self.assertEqual(rows['host']['dependency_ids'], ['service'])
        self.assertTrue(any(u['reason'] == 'dynamic_call_target_unresolved' for u in rows['host']['unresolved_targets']))

    def test_attribute_call_evaluates_its_factory_once(self):
        source = ('items = []\ndef child():\n    pass\nclass Job:\n    def run(self):\n        pass\n'
                  'def factory():\n    items.append(child)\n    return Job()\n'
                  'def host():\n    factory().run()\n    items[1]()\n')
        rows = self.inspect({'main.py': source}, [self.function('host'), self.function('child')])
        self.assertEqual(rows['host']['dependency_ids'], [])
        self.assertTrue(rows['host']['unresolved_targets'])

    def test_call_summary_reuse_preserves_changed_callbacks_and_mutable_returns(self):
        source = ('def child():\n    pass\ndef other():\n    pass\n'
                  'def apply(callback):\n    callback()\n    return 1\n'
                  'def host():\n    apply(child)\n    apply(child)\n    apply(other)\n')
        rows = self.inspect({'main.py': source}, [self.function('host'), self.function('child'), self.function('other')],
                            [dict(id='service', entry_ids=['child']), dict(id='other-service', entry_ids=['other'])])
        self.assertGreater(rows['host']['reused_call_summaries'], 0)
        self.assertEqual(rows['host']['dependency_ids'], ['service', 'other-service'])
        source = ('def child():\n    pass\ndef make():\n    return []\n'
                  'def host():\n    first = make()\n    first.append(child)\n    second = make()\n    second[0]()\n')
        rows = self.inspect({'main.py': source}, [self.function('host'), self.function('child')])
        self.assertEqual(rows['host']['dependency_ids'], [])
        self.assertTrue(rows['host']['unresolved_targets'])

    def test_call_summary_observes_instance_mutation(self):
        source = ('def child():\n    pass\ndef other():\n    pass\n'
                  'class Job:\n    def __init__(self):\n        self.callback = child\n'
                  '    def run(self):\n        self.callback()\n'
                  'def host():\n    job = Job()\n    job.run()\n    job.callback = other\n    job.run()\n')
        rows = self.inspect({'main.py': source}, [self.function('host'), self.function('child'), self.function('other')],
                            [dict(id='service', entry_ids=['child']), dict(id='other-service', entry_ids=['other'])])
        self.assertEqual(rows['host']['dependency_ids'], ['service', 'other-service'])

    def test_global_path_choice_requires_explicit_path_or_none(self):
        prefix = 'from pathlib import Path\nROOT = Path("default")\ndef configure(raw):\n    global ROOT\n'
        for expression, unknown in (('optional or Path("default")', False), ('raw or Path("default")', True)):
            source = prefix + ('    optional = Path(raw) if raw else None\n    ROOT = ' + expression + '\n'
                               'def host(raw):\n    configure(raw)\n    ROOT.read_text()\n')
            rows = self.inspect({'main.py': source}, [self.function('host')], [])
            self.assertEqual(bool(rows['host']['unresolved_targets']), unknown)

    def test_known_constructor_branch_retains_original_receiver_and_nested_owner(self):
        source = ('def child():\n    pass\nclass Engine:\n    def run(self):\n        child()\n'
                  'class Host:\n    def __init__(self, mode="other"):\n'
                  '        if mode in ["selected", "alternate"]:\n            self.engine = Engine()\n'
                  '        else:\n            unavailable()\n'
                  '    def run(self):\n        if isinstance(self.engine, Engine):\n            self.engine.run()\n'
                  '        else:\n            unavailable()\n')
        entry = dict(id='host', path='main.py', symbol='Host.run', kind='method',
                     constructor=dict(symbol='Host', arguments=dict(mode='selected')))
        rows = self.inspect({'main.py': source}, [entry, self.function('child')])
        self.assertEqual(rows['host']['dependency_ids'], ['service'])
        self.assertEqual(rows['host']['unresolved_targets'], [])
        self.assertIn('source-exact-local-instance/1', {p['rule'] for p in rows['host']['external_protocols']})

    def test_unknown_field_write_preserves_stable_nested_receiver_and_aliases(self):
        source = ('def child():\n    pass\nclass Engine:\n    def run(self):\n        child()\n'
                  'class Host:\n    def __init__(self):\n        self.engine = Engine()\n'
                  '    def configure(self, choice):\n        alias = self.engine\n'
                  '        if choice:\n            alias.chunk = 1\n        else:\n            self.engine.chunk = 2\n'
                  '    def run(self, choice):\n        self.configure(choice)\n        self.engine.run()\n')
        entry = dict(id='host', path='main.py', symbol='Host.run', kind='method',
                     constructor=dict(symbol='Host', arguments={}))
        rows = self.inspect({'main.py': source}, [entry, self.function('child')])
        self.assertEqual(rows['host']['dependency_ids'], ['service'])
        self.assertEqual(rows['host']['unresolved_targets'], [])

    def test_distinct_branch_allocations_never_merge_merely_by_class_name(self):
        source = ('def child():\n    pass\nclass Engine:\n    def __init__(self, callback):\n'
                  '        self.callback = callback\n    def run(self):\n        self.callback()\n'
                  'def host(choice):\n    if choice:\n        engine = Engine(child)\n'
                  '    else:\n        engine = Engine(unknown)\n    engine.run()\n')
        rows = self.inspect({'main.py': source}, [self.function('host'), self.function('child')])
        self.assertTrue(rows['host']['unresolved_targets'])
        self.assertNotEqual(rows['host']['status'], 'CHECKED')

    def test_public_unknown_selector_does_not_prune_constructor_branches(self):
        source = ('def child():\n    pass\ndef other():\n    pass\n'
                  'def host(mode):\n    if mode in ["selected"]:\n        child()\n    else:\n        other()\n')
        rows = self.inspect({'main.py': source}, [self.function('host'), self.function('child'), self.function('other')],
                            [dict(id='service', entry_ids=['child']), dict(id='other-service', entry_ids=['other'])])
        self.assertEqual(rows['host']['dependency_ids'], ['service', 'other-service'])

    def test_isinstance_external_base_shadow_and_class_mutation_remain_unknown(self):
        prefix = ('def child():\n    pass\ndef other():\n    pass\n'
                  'class Engine{base}:\n    pass\ndef host():\n    engine = Engine()\n'
                  '    {change}\n    if isinstance(engine, Engine):\n        child()\n    else:\n        other()\n')
        for base, change in (('(ExternalBase)', 'pass'), ('', 'engine.__class__ = unknown'),
                             ('', 'external(engine)'), ('', 'isinstance = external')):
            with self.subTest(base=base, change=change):
                source = 'from sdk import ExternalBase, external\n' + prefix.format(base=base, change=change)
                rows = self.inspect({'main.py': source}, [self.function('host'), self.function('child'), self.function('other')],
                    [dict(id='service', entry_ids=['child']), dict(id='other-service', entry_ids=['other'])])
                self.assertEqual(rows['host']['dependency_ids'], ['service', 'other-service'])
                self.assertFalse(any(p['rule'] == 'source-exact-local-instance/1' for p in rows['host']['external_protocols']))

    def test_resource_calls_keep_selected_original_values_none_and_unknown_separate(self):
        source = ('from sdk import Loader\nclass Host:\n    def __init__(self, mode, cache=None):\n'
                  '        if mode in ["selected"]:\n            self.model = Loader.from_pretrained("org/selected", cache_dir=cache)\n'
                  '        else:\n            self.model = Loader.from_pretrained("org/other", cache_dir=cache)\n'
                  '    def run(self):\n        return 1\n')
        entry = dict(id='host', path='main.py', symbol='Host.run', kind='method',
                     constructor=dict(symbol='Host', arguments=dict(mode='selected')))
        self.inspect({'main.py': source}, [entry], [])
        result = resolve_resource_calls(self.inspector, entry, self.candidate)
        calls = [c for c in result['calls'] if c['target'] == 'sdk.Loader.from_pretrained']
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['args'][0], dict(status='CHECKED', value='org/selected'))
        self.assertEqual(calls[0]['kwargs']['cache_dir'], dict(status='CHECKED', value=None))
        self.assertEqual(calls[0]['path_condition_status'], 'CHECKED')
        entry = copy.deepcopy(entry)
        entry['constructor']['arguments']['cache'] = {'$dependency': dict(id='cache', field='path')}
        calls = resolve_resource_calls(self.inspector, entry)['calls']
        self.assertEqual(calls[0]['kwargs']['cache_dir'], dict(status='UNKNOWN'))

    def test_resource_calls_in_unknown_branches_and_loops_keep_potential_scope(self):
        source = ('from sdk import Loader\ndef host(choice):\n'
                  '    if choice:\n        Loader.from_pretrained("org/first")\n'
                  '    else:\n        Loader.from_pretrained("org/second")\n'
                  '    for value in choice:\n        Loader.from_pretrained("org/loop")\n')
        entry = self.function('host')
        self.inspect({'main.py': source}, [entry], [])
        calls = resolve_resource_calls(self.inspector, entry)['calls']
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(c['path_condition_status'] == 'UNKNOWN' for c in calls))

    def test_class_descriptor_cannot_supply_an_exact_isinstance_proof(self):
        source = ('def child():\n    pass\ndef other():\n    pass\nclass Expected:\n    pass\n'
                  'class Actual:\n    @property\n    def __class__(self):\n        return Expected\n'
                  'def host():\n    value = Actual()\n    if isinstance(value, Expected):\n        child()\n'
                  '    else:\n        other()\n')
        rows = self.inspect({'main.py': source}, [self.function('host'), self.function('child'), self.function('other')],
                            [dict(id='service', entry_ids=['child']), dict(id='other-service', entry_ids=['other'])])
        self.assertEqual(rows['host']['dependency_ids'], ['service', 'other-service'])
        self.assertFalse(any(p['rule'] == 'source-exact-local-instance/1' for p in rows['host']['external_protocols']))

    def test_container_receiver_alias_gap_cannot_become_empty_checked_scope(self):
        source = ('def child():\n    pass\nclass Engine:\n    def run(self):\n        child()\n'
                  'def host(choice):\n    box = [Engine()]\n    if choice:\n        box.append(Engine())\n'
                  '    else:\n        box.append(unknown)\n    box[0].run()\n')
        rows = self.inspect({'main.py': source}, [self.function('host'), self.function('child')])
        self.assertTrue(rows['host']['dependency_ids'] or rows['host']['unresolved_targets'])
        self.assertNotEqual(rows['host']['status'], 'CHECKED')

    def test_equal_shaped_bound_receivers_do_not_hide_a_later_alias_write(self):
        source = ('def child():\n    pass\ndef other():\n    pass\nclass Engine:\n'
                  '    def __init__(self, callback):\n        self.callback = callback\n'
                  '    def run(self):\n        self.callback()\n'
                  'def host(choice):\n    first = Engine(other)\n    second = Engine(other)\n'
                  '    if choice:\n        callback = first.run\n    else:\n        callback = second.run\n'
                  '    second.callback = child\n    callback()\n')
        rows = self.inspect({'main.py': source}, [self.function('host'), self.function('child'), self.function('other')],
                            [dict(id='service', entry_ids=['child']), dict(id='other-service', entry_ids=['other'])])
        self.assertTrue('service' in rows['host']['dependency_ids'] or rows['host']['unresolved_targets'])
        self.assertNotEqual(rows['host']['status'], 'CHECKED')


if __name__ == '__main__':
    unittest.main()
