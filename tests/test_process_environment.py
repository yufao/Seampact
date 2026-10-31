"""Reached frozen subprocesses inherit only unchanged original resource keys."""

import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from seampact.inspection import Inspector
from seampact.dependencies import Dependencies, DependencyBindings
from seampact.process_environment import process_environment_flow


CHILD = """import os
import urllib.request
request = urllib.request.Request('data:text/plain,child-ok', headers={'Authorization': f"Bearer {os.getenv('RESOURCE')}"})
with urllib.request.urlopen(request) as response:
    print(response.read().decode())
"""


class ProcessEnvironmentChecks(unittest.TestCase):
    def fixture(self, parent, child=CHILD, extra=None, evidence=None):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        files = {'main.py': parent}
        if child is not None:
            files['child.py'] = child
        files.update(extra or {})
        for path, source in files.items():
            (root / path).parent.mkdir(parents=True, exist_ok=True)
            (root / path).write_text(source, encoding='utf-8')
        inspector = Inspector.freeze(root, root / 'snapshots', 'request', list(files), 'goal', 'original result')
        entry = dict(id='host', kind='cli', path='main.py', symbol='__main__', command=['python', 'main.py'])
        refs = evidence if evidence is not None else [dict(path=p, start=1, end=len(s.splitlines())) for p, s in files.items()]
        return inspector, entry, refs

    def proof(self, parent, **kwargs):
        inspector, entry, refs = self.fixture(parent, **kwargs)
        return process_environment_flow(inspector, entry, 'RESOURCE', 'credential', refs)

    def test_default_none_and_finite_full_copies(self):
        for expression in ('', ', env=None', ', env=os.environ.copy()', ', env=dict(os.environ)',
                           ", env={**os.environ, 'OUTPUT_DIR': 'outputs'}"):
            with self.subTest(expression=expression):
                result = self.proof("import os\nimport sys\nimport subprocess\nsubprocess.run([sys.executable, 'child.py']" + expression + ")\n")
                self.assertTrue(result['proved'], result)
                self.assertEqual({r['path'] for r in result['trace']}, {'main.py', 'child.py'})
                self.assertTrue(any(f['kind'] == 'subprocess_environment_inheritance' for f in result['facts']))

    def test_original_called_helper_and_second_process_hop(self):
        parent = "import os\nimport sys\nimport subprocess\ndef launch(argv, env):\n    subprocess.run(argv, env=env)\nlaunch([sys.executable, 'middle.py'], os.environ.copy())\n"
        middle = "import subprocess\nsubprocess.run(['python', 'child.py'])\n"
        result = self.proof(parent, extra={'middle.py': middle})
        self.assertTrue(result['proved'], result)
        self.assertEqual(len([f for f in result['facts'] if f['kind'] == 'subprocess_environment_inheritance']), 2)

    def test_helper_conditional_mutation_keeps_alias_identity(self):
        for key, expected in [('RESOURCE', False), ('OUTPUT_DIR', True)]:
            source = "import os\nimport subprocess\ndef alter(mapping):\n    if condition:\n        mapping['" + key + "'] = 'other'\nenv = os.environ.copy()\nalter(env)\nsubprocess.run(['python', 'child.py'], env=env)\n"
            self.assertEqual(self.proof(source)['proved'], expected)

    def test_mutated_argv_never_uses_earlier_child_witness(self):
        for operation in [
            "argv[1] = 'other.py'", "del argv[1]", "argv.pop(1)",
            "unknown(argv)", "unknown({'arguments': argv})", "argv += ['extra']",
            "alias = argv\nalias[1] = 'other.py'",
            "alias = argv\nalias.pop(1)", "alias = argv\nunknown(alias)",
            "mutate = argv.pop\nmutate(1)",
            "holder = {'arguments': argv}\nunknown(holder)",
            "holder = [argv]\nholder[0][1] = 'other.py'",
            "holder = {'arguments': argv}\nholder['arguments'].pop(1)",
            "if condition:\n    argv[1] = 'other.py'",
        ]:
            with self.subTest(operation=operation):
                result = self.proof("import subprocess\nargv = ['python', 'child.py']\n" + operation + '\nsubprocess.run(argv)\n')
                self.assertFalse(result['proved'], result)
                self.assertFalse(any(f['kind'] == 'subprocess_environment_inheritance' for f in result['facts']), result)

    def test_helper_argv_mutations_invalidate_caller_aliases(self):
        for operation in ["argv[1] = 'other.py'", "argv.pop(1)", "unknown(argv)",
                          "if condition:\n        argv[1] = 'other.py'", "argv += ['extra']"]:
            with self.subTest(operation=operation):
                source = "import subprocess\ndef alter(argv):\n    " + operation + "\nargv = ['python', 'child.py']\nalter(argv)\nsubprocess.run(argv)\n"
                self.assertFalse(self.proof(source)['proved'])
        source = "import subprocess\ndef launch(argv):\n    alias = argv\n    subprocess.run(alias)\nargv = ['python', 'child.py']\nalias = argv\nlaunch(alias)\n"
        self.assertTrue(self.proof(source)['proved'])
        for helper, call in [
            ("@unknown\ndef alter(argv):\n    argv.pop(1)\n", 'alter(argv)'),
            ("def alter(*arguments):\n    arguments[0].pop(1)\n", 'alter(argv)'),
            ("def alter(argv):\n    argv.pop(1)\n", "alter(**{'argv': argv})"),
        ]:
            with self.subTest(call=call, helper=helper):
                self.assertFalse(self.proof("import subprocess\n" + helper + "argv = ['python', 'child.py']\n" + call + '\nsubprocess.run(argv)\n')['proved'])

    def test_augmented_environment_edits_follow_shared_identity(self):
        for key, expected in [('RESOURCE', False), ('OUTPUT_DIR', True)]:
            for destination in ['env', 'alias']:
                with self.subTest(key=key, destination=destination):
                    source = "import os\nimport subprocess\nenv = os.environ.copy()\nalias = env\n" + destination + " |= {'" + key + "': 'other'}\nsubprocess.run(['python', 'child.py'], env=env)\n"
                    self.assertEqual(self.proof(source)['proved'], expected)
            source = "import os\nimport subprocess\ndef alter(mapping):\n    alias = mapping\n    alias |= {'" + key + "': 'other'}\nenv = os.environ.copy()\nalter(env)\nsubprocess.run(['python', 'child.py'], env=env)\n"
            self.assertEqual(self.proof(source)['proved'], expected)
            source = "import os\nimport subprocess\ndef alter(mapping):\n    mapping |= {'" + key + "': 'other'}\nenv = os.environ.copy()\nalter(env)\nsubprocess.run(['python', 'child.py'], env=env)\n"
            self.assertEqual(self.proof(source)['proved'], expected)
        for operation in ["alias |= unknown", "alias += unknown", "mutate = alias.update\nmutate({'RESOURCE': 'other'})",
                          "holder = {'mapping': env}\nunknown(holder)",
                          "holder = {'mapping': env}\nholder['mapping'].clear()"]:
            with self.subTest(operation=operation):
                self.assertFalse(self.proof("import os\nimport subprocess\nenv = os.environ.copy()\nalias = env\n" + operation + "\nsubprocess.run(['python', 'child.py'], env=env)\n")['proved'])

    def test_selected_key_mutation_rejected_other_directory_allowed(self):
        operations = ["env['KEY'] = 'other'", "del env['KEY']", "env.pop('KEY')", "env.update({'KEY': 'other'})"]
        for operation in operations:
            for key, expected in [('RESOURCE', False), ('OUTPUT_DIR', True)]:
                with self.subTest(operation=operation, key=key):
                    parent = "import os\nimport subprocess\nenv = os.environ.copy()\n" + operation.replace('KEY', key) + "\nsubprocess.run(['python', 'child.py'], env=env)\n"
                    result = self.proof(parent)
                    self.assertEqual(result['proved'], expected, result)

    def test_clear_dynamic_key_empty_dict_and_shadowing_rejected(self):
        for setup, expression in [
            ("env = os.environ.copy()\nenv.clear()", 'env'),
            ("env = os.environ.copy()\nenv[unknown_key] = 'other'", 'env'),
            ("env = os.environ.copy()\nenv.update(dynamic_map)", 'env'),
            ("env = {}", 'env'),
            ("env = os.environ.copy()\nalias = env\nalias['RESOURCE'] = 'other'", 'env'),
            ("env = os.environ.copy()\nenv = {}", 'env'),
            ("subprocess = unknown", 'None'),
            ("dict = unknown", 'dict(os.environ)'),
            ("unknown({'mapping': os.environ})", 'None'),
            ("env = os.environ.copy()\nunknown({'mapping': env})", 'env'),
            ("exec(unknown_code)", 'None'),
            ("runner = exec\nrunner(unknown_code)", 'None'),
            ("setattr(subprocess, 'run', unknown)", 'None'),
        ]:
            with self.subTest(setup=setup):
                result = self.proof('import os\nimport subprocess\n' + setup + "\nsubprocess.run(['python', 'child.py'], env=" + expression + ')\n')
                self.assertFalse(result['proved'], result)

    def test_process_global_writes_and_snapshot_time(self):
        for operation in ["os.environ['RESOURCE'] = 'other'", "os.putenv('RESOURCE', 'other')",
                          "os.environ.update({'RESOURCE': 'other'})", "os.environ.clear()",
                          "os.environ[unknown_key] = 'other'"]:
            with self.subTest(operation=operation):
                source = "import os\nimport subprocess\n" + operation + "\nsubprocess.run(['python', 'child.py'])\n"
                self.assertFalse(self.proof(source)['proved'])
        source = "import os\nimport subprocess\nenv = os.environ.copy()\nos.environ['RESOURCE'] = 'other'\nsubprocess.run(['python', 'child.py'], env=env)\n"
        self.assertTrue(self.proof(source)['proved'])

    def test_keyword_evaluation_order_and_child_own_overwrite(self):
        prefix = "import os\nimport subprocess\ndef overwrite():\n    os.environ['RESOURCE'] = 'other'\n    return 3\n"
        for options, expected in [('timeout=overwrite()', False),
                                  ('env=os.environ.copy(), timeout=overwrite()', True),
                                  ('timeout=overwrite(), env=os.environ.copy()', False)]:
            with self.subTest(options=options):
                self.assertEqual(self.proof(prefix + "subprocess.run(['python', 'child.py'], " + options + ')\n')['proved'], expected)
        child = CHILD.replace('import urllib.request', "import urllib.request\nos.environ['RESOURCE'] = 'replacement'")
        self.assertFalse(self.proof("import subprocess\nsubprocess.run(['python', 'child.py'])\n", child=child)['proved'])

    def test_original_imported_helper_and_module_side_effects(self):
        helper = "import subprocess\ndef launch():\n    subprocess.run(['python', 'child.py'])\n"
        self.assertTrue(self.proof('from helper import launch\nlaunch()\n', extra={'helper.py': helper})['proved'])
        helper = "import os\nos.environ['RESOURCE'] = 'replacement'\n" + helper
        self.assertFalse(self.proof('from helper import launch\nlaunch()\n', extra={'helper.py': helper})['proved'])
        unused = "import subprocess\nif __name__ == '__main__':\n    subprocess.run(['python', 'child.py'])\n"
        self.assertFalse(self.proof('import helper\n', extra={'helper.py': unused})['proved'])

    def test_shell_executable_callback_and_untrusted_cwd_are_unsupported(self):
        for options in ['shell=True', 'executable="other"', 'preexec_fn=overwrite', 'cwd=unknown', 'cwd="../outside"']:
            with self.subTest(options=options):
                self.assertFalse(self.proof("import subprocess\nsubprocess.run(['python', 'child.py'], " + options + ')\n')['proved'])

    def test_unreached_nested_and_false_calls_are_not_edges(self):
        for source in ["def unused():\n    subprocess.run(['python', 'child.py'])\n",
                       "if False:\n    subprocess.run(['python', 'child.py'])\n"]:
            result = self.proof('import subprocess\n' + source)
            self.assertFalse(result['proved'])
            self.assertFalse(any(f['kind'] == 'subprocess_environment_inheritance' for f in result['facts']))

    def test_missing_exact_child_and_child_consumer_are_not_replaced(self):
        source = "import subprocess\nsubprocess.run(['python', 'child.py'])\n"
        result = self.proof(source, child=None, extra={'other/child.py': CHILD})
        self.assertFalse(result['proved'])
        self.assertEqual(result['issue_code'], 'configuration_source_unavailable')
        self.assertEqual(result['facts'][0]['target_path'], 'child.py')
        self.assertFalse(self.proof(source, child='import os\nvalue = os.getenv("RESOURCE")\n')['proved'])

    def test_evidence_for_parent_import_copy_call_and_child_read_required(self):
        parent = "import os\nimport subprocess\nenv = os.environ.copy()\nsubprocess.run(['python', 'child.py'], env=env)\n"
        for path, line in [('main.py', 1), ('main.py', 2), ('main.py', 3), ('main.py', 4), ('child.py', 1), ('child.py', 2), ('child.py', 3)]:
            refs = []
            for name, source in [('main.py', parent), ('child.py', CHILD)]:
                count = len(source.splitlines())
                if name != path:
                    refs.append(dict(path=name, start=1, end=count))
                else:
                    if line > 1:
                        refs.append(dict(path=name, start=1, end=line - 1))
                    if line < count:
                        refs.append(dict(path=name, start=line + 1, end=count))
            with self.subTest(path=path, line=line):
                result = self.proof(parent, evidence=refs)
                self.assertFalse(result['proved'], result)
                self.assertEqual(result['issue_code'], 'evidence_incomplete')

    def test_actual_child_data_url_is_network_free_and_parent_environment_unchanged(self):
        parent = "import os\nimport sys\nimport subprocess\nenv = dict(os.environ)\nenv['OUTPUT_DIR'] = 'outputs'\nsubprocess.run([sys.executable, 'child.py'], env=env, check=True)\n"
        child = CHILD + "\nassert request.get_header('Authorization') == 'Bearer fixture-only-resource'\n"
        inspector, entry, refs = self.fixture(parent, child=child)
        reference = {'$dependency': dict(id='service', field='credential')}
        entry['environment'] = {'RESOURCE': reference}
        candidate = dict(entries=[entry], dependencies=[dict(id='service', kind='service', configurable=True,
            required=True, entry_ids=['host'], read_time='process_start', owner_scope='original CLI process',
            evidence=refs, mappings=[dict(entry_id='host', location='environment', parameter='RESOURCE', field='credential')])])
        inspector.request['source_id'] = 'fixture-source'
        records = {'resource_dependency': {}, 'dependency_binding': {}}
        store = SimpleNamespace(get=lambda kind, identity: records[kind].get(identity),
            list=lambda kind: list(records[kind].values()),
            save=lambda kind, identity, value: records[kind].update({identity: value}))
        connection = dict(id='fixture-connection', config_digest='fixture-connection-version',
                          auth=dict(mode='bearer'), key_usable=True)
        profiles = SimpleNamespace(public_connections=lambda: [connection], lock=threading.RLock(),
            connection_profile=lambda identity: connection, connection_key=lambda identity: 'fixture-only-resource')
        registry = Dependencies(store, profiles, None)
        checked = registry.record(inspector, candidate)[0]
        self.assertEqual(checked['status'], 'MAPPING_CHECKED', checked)
        registry.select(dict(dependency_id='fixture-source:service', harness_id='fixture-source',
                             connection_id='fixture-connection'))
        inspector.request['dependency_bindings'] = registry.selections('fixture-source')
        bindings = DependencyBindings(registry, inspector, candidate)
        bindings.check_entry('host')
        before = dict(os.environ)
        self.assertEqual(bindings.values(entry['environment'], 'host', 'environment', runtime=False), {'RESOURCE': ''})
        environment = dict(before)
        environment.update(bindings.values(entry['environment'], 'host', 'environment', runtime=True))
        completed = subprocess.run([sys.executable, str(inspector.root / 'main.py')], cwd=inspector.root,
                                   env=environment, capture_output=True, text=True, check=True)
        self.assertEqual(completed.stdout.strip(), 'child-ok')
        self.assertEqual(dict(os.environ), before)


if __name__ == '__main__':
    unittest.main()
