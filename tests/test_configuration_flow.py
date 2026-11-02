"""Finite configuration witnesses and misleading flows rejected by the checker."""

import copy
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from seampact.dependencies import check_requirements
from seampact.inspection import Inspector
from seampact.intake_feedback import explain_candidate
from seampact.configuration_binding import configuration_file_flow


class ConfigurationFlowChecks(unittest.TestCase):
    def check_flow(self, files, *, field='base_url', evidence=None, kind='cli', read_time='process_start', destination='valid'):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        for path, source in files.items():
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(source, encoding='utf-8')
        inspector = Inspector.freeze(root, root / 'snapshots', 'request', list(files), 'goal', 'original result')
        ref = {'$dependency': dict(id='resource', field=field)}
        entry = dict(id='target', kind=kind, path='main.py', symbol='__main__',
                     command=['python', 'main.py'], environment={'RESOURCE': ref})
        dependency = dict(id='resource', kind='weight' if field == 'path' else 'service', configurable=True,
            required=True, read_time=read_time, entry_ids=['target'], owner_scope='original process',
            evidence=evidence if evidence is not None else [dict(path=p, start=1, end=len(s.splitlines())) for p, s in files.items()],
            mappings=[dict(entry_id='target', location='environment', parameter='RESOURCE', field=field)])
        candidate = dict(entries=[entry], dependencies=[dependency])
        if destination == 'missing':
            entry['environment'] = {}
        elif destination == 'wrong':
            entry['environment']['RESOURCE'] = {'$dependency': dict(id='other', field=field)}
        original = copy.deepcopy(candidate)
        checked = check_requirements(inspector, candidate)[0]
        self.assertEqual(candidate, original)
        self.feedback = explain_candidate(inspector, candidate, dict(dependencies=[checked],
            entry_bindings=dict(entries=[dict(id='target', status='MISSING', reason=checked.get('reason'))])))['feedback']
        return checked

    def test_cli_injection_time_is_repairable_without_relaxing_flow_checks(self):
        source = {'main.py': "import os\nimport requests\nrequests.post(os.getenv('RESOURCE'))\n"}
        for read_time in ('constructor', 'call'):
            with self.subTest(read_time=read_time):
                row = self.check_flow(source, read_time=read_time)
                issue = row['entry_checks']['target']['issues'][0]
                self.assertEqual(issue['issue_code'], 'candidate_incomplete')
                self.assertEqual(issue['field_paths'], ['read_time'])
                repair = next(i for i in self.feedback['model_repairs'] if i.get('dependency_id') == 'resource')
                self.assertEqual(repair['field_paths'], ['dependencies[0].read_time'])
                self.assertTrue(repair['model_repairable'])
        self.assertEqual(self.check_flow(source)['status'], 'MAPPING_CHECKED')
        unsupported = {'main.py': "import os\nvalue = os.getenv('RESOURCE')\nunknown(value)\n"}
        self.assertEqual(self.check_flow(unsupported)['status'], 'MISSING')

    def test_missing_or_wrong_reference_is_candidate_error(self):
        source = {'main.py': "import os\nimport requests\nrequests.post(os.getenv('RESOURCE'))\n"}
        for destination in ('missing', 'wrong'):
            with self.subTest(destination=destination):
                row = self.check_flow(source, destination=destination)
                self.assertEqual(row['status'], 'MISSING')
                self.assertEqual(row['entry_checks']['target']['issues'][0]['issue_code'], 'candidate_incomplete')
                self.assertTrue(any(i.get('dependency_id') == 'resource' and i['model_repairable']
                                    for i in self.feedback['model_repairs']))

    def test_in_process_environment_remains_a_rule_gap(self):
        row = self.check_flow({'main.py': "import os\nimport requests\nrequests.post(os.getenv('RESOURCE'))\n"}, kind='function')
        self.assertEqual(row['entry_checks']['target']['issues'][0]['issue_code'], 'parameter_flow_unsupported')
        self.assertTrue(any(i.get('dependency_id') == 'resource' and not i['model_repairable']
                            for i in self.feedback['external_conditions']))

    def test_module_constant_import_and_original_factory(self):
        files = {'main.py': "from cfg import endpoint\nfrom helper import query\nif __name__ == '__main__':\n    query(endpoint)\n",
                 'cfg.py': "import os\nendpoint = os.getenv('RESOURCE')\n",
                 'helper.py': "import requests\ndef query(url):\n    return requests.post(url)\n"}
        row = self.check_flow(files)
        self.assertEqual(row['status'], 'MAPPING_CHECKED', row['reason'])
        proof = row['entry_checks']['target']['mapping_proofs'][0]['proof']
        self.assertEqual({r['path'] for r in proof['trace']}, set(files))

    def test_dataclass_factory_attributes_and_actual_http_request(self):
        files = self.client_files()
        for field in ('base_url', 'model', 'credential'):
            with self.subTest(field=field):
                row = self.check_flow(files, field=field)
                self.assertEqual(row['status'], 'MAPPING_CHECKED', row['reason'])

    @staticmethod
    def client_files():
        return {
            'main.py': "from cfg import load_settings\nfrom client import Client\ndef main():\n    settings = load_settings()\n    client = Client(settings)\n    return client.query()\nif __name__ == '__main__':\n    main()\n",
            'cfg.py': "import os\nfrom dataclasses import dataclass\n@dataclass\nclass Settings:\n    endpoint: str = ''\n    model: str = ''\n    key: str = ''\ndef load_settings():\n    return Settings(endpoint=os.getenv('RESOURCE'), model=os.getenv('RESOURCE'), key=os.getenv('RESOURCE'))\n",
            'client.py': "import json\nimport urllib.request\nclass Client:\n    def __init__(self, cfg):\n        self.cfg = cfg\n    def query(self):\n        url = self.cfg.endpoint.rstrip('/') + '/chat/completions'\n        payload = {'model': self.cfg.model}\n        data = json.dumps(payload).encode('utf-8')\n        req = urllib.request.Request(url=url, data=data, headers={'Authorization': f'Bearer {self.cfg.key}'})\n        with urllib.request.urlopen(req) as response:\n            return response.read()\n"}

    def test_missing_factory_field_assignment_and_import_evidence(self):
        files = self.client_files()
        for path, line in [('cfg.py', 5), ('cfg.py', 9), ('client.py', 5), ('client.py', 2)]:
            evidence = [dict(path=p, start=1, end=len(s.splitlines())) for p, s in files.items() if p != path]
            if line > 1:
                evidence.append(dict(path=path, start=1, end=line - 1))
            if line < len(files[path].splitlines()):
                evidence.append(dict(path=path, start=line + 1, end=len(files[path].splitlines())))
            row = self.check_flow(files, evidence=evidence)
            self.assertEqual(row['status'], 'MISSING', row)
            self.assertEqual(row['entry_checks']['target']['issues'][0]['issue_code'], 'evidence_incomplete')

    def test_overwrite_dynamic_read_and_shadowed_import_are_not_witnesses(self):
        variants = [
            "import os\nimport requests\nurl = os.getenv('RESOURCE')\nurl = 'other'\nrequests.post(url)\n",
            "import os\nimport requests\nurl = os.getenv(dynamic_key)\nrequests.post(url)\n",
            "import os\nimport requests\nos = unknown\nurl = os.getenv('RESOURCE')\nrequests.post(url)\n",
            "import os\nimport requests\nrequests = unknown\nrequests.post(os.getenv('RESOURCE'))\n",
            "import os\ndef client(**kw): pass\nclient(base_url=os.getenv('RESOURCE'))\n",
            "import os\nimport requests\ndef unused():\n    requests.post(os.getenv('RESOURCE'))\n",
        ]
        for source in variants:
            with self.subTest(source=source):
                row = self.check_flow({'main.py': source})
                self.assertEqual(row['status'], 'MISSING', row)

    def test_other_instance_or_attribute_overwrite_is_rejected(self):
        for replacement in ["settings.endpoint = 'other'\n    client = Client(settings)", "client = Client(None)"]:
            files = self.client_files()
            files['main.py'] = files['main.py'].replace('client = Client(settings)', replacement)
            row = self.check_flow(files)
            self.assertEqual(row['status'], 'MISSING', row)

    def test_cli_process_start_boundary_and_empty_evidence(self):
        source = {'main.py': "import os\nimport requests\nrequests.post(os.getenv('RESOURCE'))\n"}
        for options in [dict(kind='function'), dict(read_time='call'), dict(evidence=[])]:
            with self.subTest(options=options):
                self.assertEqual(self.check_flow(source, **options)['status'], 'MISSING')

    def test_environment_overrides_and_dead_paths_are_rejected(self):
        variants = [
            "os.environ['RESOURCE'] = 'other'\nrequests.post(os.getenv('RESOURCE'))",
            "os.putenv('RESOURCE', 'other')\nrequests.post(os.getenv('RESOURCE'))",
            "os.environ.update({'RESOURCE': 'other'})\nrequests.post(os.getenv('RESOURCE'))",
            "os.environ[dynamic_key] = 'other'\nrequests.post(os.getenv('RESOURCE'))",
            "if False:\n    requests.post(os.getenv('RESOURCE'))",
            "while False:\n    requests.post(os.getenv('RESOURCE'))",
            "for item in []:\n    requests.post(os.getenv('RESOURCE'))",
            "url = 'other' or os.getenv('RESOURCE')\nrequests.post(url)",
        ]
        for body in variants:
            with self.subTest(body=body):
                row = self.check_flow({'main.py': 'import os\nimport requests\n' + body + '\n'})
                self.assertEqual(row['status'], 'MISSING', row)
        files = {'main.py': 'from cfg import endpoint\nimport requests\nrequests.post(endpoint)\n',
                 'cfg.py': "import os\nos.environ['RESOURCE'] = 'other'\nendpoint = os.getenv('RESOURCE')\n"}
        self.assertEqual(self.check_flow(files)['status'], 'MISSING')

    def test_dataclass_decorator_monkeypatch_and_nonconsumer_factory(self):
        files = self.client_files()
        files['cfg.py'] = files['cfg.py'].replace('@dataclass', 'dataclass = unknown\n@dataclass')
        self.assertEqual(self.check_flow(files)['status'], 'MISSING')
        files = self.client_files()
        files['main.py'] = files['main.py'].replace('client = Client(settings)', 'settings.endpoint = unknown\n    client = Client(settings)')
        self.assertEqual(self.check_flow(files)['status'], 'MISSING')
        files = self.client_files()
        files['main.py'] = 'from cfg import load_settings\nsettings = load_settings()\n'
        self.assertEqual(self.check_flow(files)['status'], 'MISSING')

    def test_unavailable_local_consumer_reports_source_gap(self):
        files = self.client_files()
        files['main.py'] = files['main.py'].replace('from client import Client', 'from package.missing import Client')
        files['package/__init__.py'] = 'pass\n'
        files['package/present.py'] = 'pass\n'
        row = self.check_flow(files)
        self.assertEqual(row['status'], 'MISSING')
        self.assertEqual(row['entry_checks']['target']['issues'][0]['issue_code'], 'configuration_source_unavailable')

    def test_fixed_disjoint_environment_normalization_preserves_selected_origin(self):
        for key, expected in [('OUTPUT_ROOT', 'MAPPING_CHECKED'), ('RESOURCE', 'MISSING')]:
            files = {'main.py': "import os\nimport requests\nPATHS = {'" + key + "': 'outputs'}\ndef load():\n    for name, value in PATHS.items():\n        os.environ[name] = value\n    return os.getenv('RESOURCE')\nrequests.post(load())\n"}
            self.assertEqual(self.check_flow(files)['status'], expected)

    def test_environment_file_setdefault_then_cross_module_config_and_real_http_consumer(self):
        files = self.client_files()
        files['main.py'] = "from cfg import load_settings\nfrom client import Client\ndef required(name):\n    import os\n    value = os.getenv(name, '').strip()\n    if not value:\n        raise RuntimeError('required')\n    return value\nsettings = load_settings()\nclient = Client(settings)\nclient.query()\n"
        files['cfg.py'] = files['cfg.py'].replace('    return Settings(',
            "    os.environ.setdefault(dynamic_key, dynamic_value)\n    return Settings(")
        for field in ('base_url', 'model', 'credential'):
            with self.subTest(field=field):
                self.assertEqual(self.check_flow(files, field=field)['status'], 'MAPPING_CHECKED')
        files['main.py'] = files['main.py'].replace('settings = load_settings()',
            "settings = load_settings()\nsettings.endpoint = required('RESOURCE')")
        # A second attribute write is deliberately still outside the single
        # original-field witness, rather than being called safe by setdefault.
        self.assertEqual(self.check_flow(files)['status'], 'MISSING')

    def test_original_environment_name_parameter_can_reach_target_client(self):
        files = self.client_files()
        files['main.py'] = "from cfg import load_settings\nfrom client import Client\nclient = Client(load_settings())\nclient.query()\n"
        files['cfg.py'] = files['cfg.py'].replace('def load_settings():',
            "def required(name):\n    value = os.getenv(name, '').strip()\n    if not value:\n        raise RuntimeError('required')\n    return value\ndef load_settings():")
        files['cfg.py'] = files['cfg.py'].replace("os.getenv('RESOURCE')", "required('RESOURCE')")
        for field in ('base_url', 'model', 'credential'):
            with self.subTest(field=field):
                self.assertEqual(self.check_flow(files, field=field)['status'], 'MAPPING_CHECKED')

    def test_repeated_namespace_imports_keep_one_original_http_module(self):
        files = self.client_files()
        files['client.py'] = files['client.py'].replace('import urllib.request', 'import urllib.request\nimport urllib.error')
        for field in ('base_url', 'model', 'credential'):
            self.assertEqual(self.check_flow(files, field=field)['status'], 'MAPPING_CHECKED')
        files['client.py'] = files['client.py'].replace('import urllib.error', 'import other as urllib')
        self.assertEqual(self.check_flow(files)['status'], 'MISSING')

    def test_conditional_client_retains_original_path_without_selecting_branch(self):
        for condition, expected in [('enabled', 'MAPPING_CHECKED'), ('False', 'MISSING')]:
            files = self.client_files()
            files['main.py'] = files['main.py'].replace('client = Client(settings)',
                'client = Client(settings) if ' + condition + ' else Client(None)')
            row = self.check_flow(files)
            self.assertEqual(row['status'], expected)
            if expected == 'MAPPING_CHECKED':
                proof = row['entry_checks']['target']['mapping_proofs'][0]['proof']
                self.assertTrue(proof['conditional_use'])
                self.assertTrue(any(r['kind'] == 'configuration_conditional_use' for r in proof['trace']))

    def test_unrelated_callable_budget_exhaustion_cannot_hide_unknown_environment_effects(self):
        source = 'import os\nimport requests\ndef setup():\n' + '    0\n' * 5001 + "setup()\nrequests.post(os.getenv('RESOURCE'))\n"
        self.assertEqual(self.check_flow({'main.py': source})['status'], 'MISSING')

    def test_environment_mapping_witness_does_not_claim_later_program_closure(self):
        source = "import os\nimport requests\nrequests.post(os.getenv('RESOURCE'))\nunknown_later_work()\n"
        row = self.check_flow({'main.py': source})
        self.assertEqual(row['status'], 'MAPPING_CHECKED')
        proof = row['entry_checks']['target']['mapping_proofs'][0]['proof']
        self.assertEqual(proof['scope'], 'conditional_original_consumer_reachability')
        self.assertEqual(proof['dependency_closure'], 'UNKNOWN')
        self.assertEqual(proof['subsequent_paths'], 'not_interpreted_after_first_consumer')

    def test_yacs_merges_are_source_signals_with_unresolved_override_order(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        files = {'main.py': "import torch\nfrom cfg import config, update\nupdate(config, args)\npath = config.TEST.MODEL_FILE\nmodel = torch.load(path)\n",
                 'cfg.py': "from yacs.config import CfgNode\nconfig = CfgNode()\ndef update(cfg, args):\n    cfg.merge_from_file('model.yaml')\n    if args.opts:\n        cfg.merge_from_list(args.opts)\n",
                 'model.yaml': "TEST:\n  MODEL_FILE: weights/model.pth\n"}
        for path, source in files.items():
            (root / path).write_text(source, encoding='utf-8')
        inspector = Inspector.freeze(root, root / 'snapshots', 'merge', list(files), 'goal', 'original result')
        evidence = [dict(path=p, start=1, end=len(s.splitlines())) for p, s in files.items()]
        row = check_requirements(inspector, dict(entries=[dict(id='target', kind='cli', path='main.py', symbol='__main__')],
            dependencies=[dict(id='weight', kind='weight', configurable=True, required=True, read_time='process_start',
                owner_scope='original process', entry_ids=['target'], evidence=evidence, mappings=[])]))[0]
        self.assertEqual(row['entry_checks']['target']['issues'][0]['issue_code'], 'configuration_flow_unsupported')
        facts = row['configuration_flow']
        merges = [f for f in facts if f['kind'] == 'configuration_merge']
        self.assertEqual([f['operation'] for f in merges], ['cfg.merge_from_file', 'cfg.merge_from_list'])
        self.assertEqual(merges[1]['conditions'], ['args.opts'])
        self.assertTrue(all(f['status'] == 'SOURCE_SIGNAL' and f['selected_value'] == 'UNRESOLVED' for f in facts))


class ConfigurationFileChecks(unittest.TestCase):
    """Original CLI/YAML shape, including a submitted original callback."""

    @staticmethod
    def cli_files():
        return {'main.py': '''import argparse
from openai import OpenAI
import yaml
from concurrent.futures import ThreadPoolExecutor

def evaluate(config):
    api_key = config['llm']['api_key']
    base_url = config['llm']['base_url'] + '/v1'
    workers = config['max_workers']
    client = OpenAI(base_url=base_url, api_key=api_key)
    def process_row(row):
        return client.chat.completions.create(model=config['llm']['model_path'], messages=row)
    def run(row):
        return process_row(row)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        for row in [None]:
            executor.submit(run, row)

def main(args):
    with open(args.config_path, 'r') as stream:
        config = yaml.safe_load(stream)
    evaluate(config)

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--config_path', type=str, required=True)
    args = parser.parse_args()
    main(args)
''', 'config.yaml': 'max_workers: 1\nllm:\n  base_url: "http://original.invalid"\n  api_key: ""\n  model_path: "original-model"\n  generation:\n    temperature: 0.25\n'}

    def check_file(self, files=None, field='base_url', evidence=None):
        files = files or self.cli_files()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        for path, source in files.items():
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(source, encoding='utf-8')
        inspector = Inspector.freeze(root, root / 'snapshots', 'yaml', list(files), 'goal', 'original result')
        key = {'base_url': 'base_url', 'credential': 'api_key', 'model': 'model_path'}[field]
        mapping = dict(location='configuration', parameter='config_path', configuration_file='config.yaml',
                       parameter_path=['llm', key], field=field)
        entry = dict(id='target', kind='cli', path='main.py', symbol='__main__',
                     command=['python', 'main.py', '--config_path', '{config_path}'],
                     fixed_arguments={'config_path': 'config.yaml'})
        return configuration_file_flow(inspector, entry, mapping, evidence if evidence is not None else
            [dict(path=p, start=1, end=len(s.splitlines())) for p, s in files.items()])

    def test_original_cli_nested_yaml_to_sdk_fields_and_executor_model_call(self):
        for field in ('base_url', 'credential', 'model'):
            with self.subTest(field=field):
                result = self.check_file(field=field)
                self.assertTrue(result['proved'], result)
                self.assertEqual(result['configuration_file'], 'config.yaml')
                self.assertEqual(result['original_url_suffix'], '/v1' if field == 'base_url' else '')
                if field == 'model':
                    self.assertTrue(any(r['kind'] == 'original_executor_callback' for r in result['trace']))

    def test_yaml_field_overwrite_dynamic_alias_and_nonconsumer_rejected(self):
        modifications = [
            ('evaluate(config)', "config['llm']['base_url'] = 'other'\n    evaluate(config)"),
            ("api_key = config['llm']['api_key']", "config['llm'][unknown_key] = 'other'\n    api_key = config['llm']['api_key']"),
            ('config = yaml.safe_load(stream)', 'loader = getattr(yaml, unknown_name)\n        config = loader(stream)'),
            ('    evaluate(config)', '    print(config)'),
            ('    main(args)', "    args.config_path = 'other.yaml'\n    main(args)"),
            ('    args = parser.parse_args()', '    parser.parse_args = unknown\n    args = parser.parse_args()'),
            ('import yaml', "import yaml\nyaml.SafeLoader.add_constructor('tag:yaml.org,2002:str', unknown)"),
        ]
        for old, new in modifications:
            with self.subTest(new=new):
                files = self.cli_files()
                # Replace the call, not the function definition.
                files['main.py'] = files['main.py'].replace(old, new) if old != 'evaluate(config)' else files['main.py'].replace('    evaluate(config)', '    ' + new)
                self.assertFalse(self.check_file(files)['proved'])

    def test_uncalled_callback_is_not_model_consumer_evidence(self):
        files = self.cli_files()
        files['main.py'] = files['main.py'].replace('executor.submit(run, row)', 'pass')
        self.assertFalse(self.check_file(files, field='model')['proved'])

    def test_direct_and_suffixed_consumers_cannot_share_one_url_rewrite(self):
        for insert_before in (True, False):
            files = self.cli_files()
            line = '    client = OpenAI(base_url=base_url, api_key=api_key)'
            second = "    second = OpenAI(base_url=config['llm']['base_url'], api_key=config['llm']['api_key'])"
            files['main.py'] = files['main.py'].replace(line, second + '\n' + line if insert_before else line + '\n' + second)
            result = self.check_file(files)
            self.assertFalse(result['proved'], result)
            self.assertIn('multiple original URL suffixes', result['reason'])

    def test_unused_suffix_does_not_change_the_actual_direct_consumer(self):
        files = self.cli_files()
        files['main.py'] = files['main.py'].replace('base_url=base_url, api_key=api_key', "base_url=config['llm']['base_url'], api_key=api_key")
        result = self.check_file(files)
        self.assertTrue(result['proved'], result)
        self.assertEqual(result['original_url_suffix'], '')

    def test_url_rewrite_rejects_resource_escape_after_known_consumer(self):
        files = self.cli_files()
        files['main.py'] = files['main.py'].replace('    client = OpenAI(base_url=base_url, api_key=api_key)',
            '    client = OpenAI(base_url=base_url, api_key=api_key)\n    unknown(config)')
        result = self.check_file(files)
        self.assertFalse(result['proved'], result)
        self.assertIn('unresolved original resource consumer', result['reason'])

    def test_callback_observes_config_overwrite_before_submission(self):
        for replacement in ["config['llm']['model_path'] = 'other'", "config['llm'][unknown_key] = 'other'"]:
            with self.subTest(replacement=replacement):
                files = self.cli_files()
                files['main.py'] = files['main.py'].replace('            executor.submit(run, row)',
                    '            ' + replacement + '\n            executor.submit(run, row)')
                self.assertFalse(self.check_file(files, field='model')['proved'])

    def test_duplicate_anchor_and_missing_yaml_leaf_are_rejected(self):
        for yaml in ['llm:\n  base_url: first\n  base_url: second\n',
                     'llm: &shared\n  base_url: first\n', 'llm:\n  other: first\n']:
            with self.subTest(yaml=yaml):
                files = self.cli_files()
                files['config.yaml'] = yaml
                self.assertFalse(self.check_file(files)['proved'])

    def test_original_environment_scalar_tag_is_preserved_without_evaluating_it(self):
        files = self.cli_files()
        files['main.py'] = files['main.py'].replace('from concurrent.futures import ThreadPoolExecutor', '''from concurrent.futures import ThreadPoolExecutor
import os
def environment_scalar(loader, node):
    value = loader.construct_scalar(node)
    name = value.strip('${} ')
    return os.getenv(name, value)
yaml.SafeLoader.add_constructor('!env', environment_scalar)''')
        files['config.yaml'] = files['config.yaml'].replace('api_key: ""', 'api_key: !env ${EXAMPLE_KEY}')
        for field in ('base_url', 'credential', 'model'):
            with self.subTest(field=field):
                result = self.check_file(files, field=field)
                self.assertTrue(result['proved'], result)
                self.assertTrue(any(r['kind'] == 'original_yaml_environment_scalar_constructor' for r in result['trace']))
        untouched = files['main.py']
        for mutation in ["yaml.SafeLoader.add_constructor('!env', unknown)",
                         "yaml.SafeLoader.yaml_constructors['tag:yaml.org,2002:str'] = unknown",
                         "yaml.SafeLoader.add_implicit_resolver('!env', unknown, None)"]:
            with self.subTest(mutation=mutation):
                files['main.py'] = untouched.replace("yaml.SafeLoader.add_constructor('!env', environment_scalar)",
                    "yaml.SafeLoader.add_constructor('!env', environment_scalar)\n" + mutation)
                self.assertFalse(self.check_file(files)['proved'])
        files['main.py'] = untouched
        files['main.py'] = files['main.py'].replace('    return os.getenv(name, value)', "    os.environ['RESOURCE'] = value\n    return value")
        self.assertFalse(self.check_file(files)['proved'])

    def test_missing_original_loader_evidence_is_not_substituted_by_trace(self):
        result = self.check_file(evidence=[dict(path='config.yaml', start=1, end=7)])
        self.assertFalse(result['proved'])
        self.assertEqual(result['issue_code'], 'evidence_incomplete')
        self.assertTrue(any(r['kind'] == 'original_configuration_loader' for r in result['missing']))


if __name__ == '__main__':
    unittest.main()
