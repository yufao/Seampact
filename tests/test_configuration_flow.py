"""Finite configuration witnesses and misleading flows rejected by the checker."""

import copy
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from seampact.dependencies import check_requirements
from seampact.inspection import Inspector


class ConfigurationFlowChecks(unittest.TestCase):
    def check_flow(self, files, *, field='base_url', evidence=None, kind='cli', read_time='process_start'):
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
        original = copy.deepcopy(candidate)
        checked = check_requirements(inspector, candidate)[0]
        self.assertEqual(candidate, original)
        return checked

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


if __name__ == '__main__':
    unittest.main()
