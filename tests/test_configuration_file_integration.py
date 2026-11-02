"""Original CLI consumes checked service selections in a private YAML copy."""

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import test_configuration_flow as fixtures

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from seampact.configuration.dependencies import Dependencies
from seampact.source.inspection import Inspector
from seampact.runtime import Coordinator, OwnerRuntime
from seampact.store import Store
from seampact.units import UnitCatalog


class ConfigurationFileIntegration(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        files = fixtures.ConfigurationFileChecks.cli_files()
        for path, contents in files.items():
            (self.root / path).write_text(contents, encoding='utf-8')
        self.original = (self.root / 'config.yaml').read_bytes()
        self.inspector = Inspector.freeze(self.root, self.root / 'snapshots', 'request', list(files), 'goal', 'output')
        self.inspector.request.update(source_id='source', configuration={'entries': {'target': {
            'fixed_arguments': {'config_path': 'config.yaml'}}}})
        evidence = [dict(path=p, unit='file', start=1, end=len(s.splitlines())) for p, s in files.items()]
        refs = {key: {'$dependency': {'id': 'service', 'field': field}} for key, field in
                [('base_url', 'base_url'), ('api_key', 'credential'), ('model_path', 'model')]}
        entry = dict(id='target', kind='cli', path='main.py', symbol='__main__',
            command=['python', 'main.py', '--config_path', '{config_path}'],
            fixed_arguments={'config_path': 'config.yaml'}, configuration={'config_path': {'llm': refs}})
        mappings = [dict(entry_id='target', location='configuration', parameter='config_path',
            configuration_file='config.yaml', parameter_path=['llm', key], field=field)
            for key, field in [('base_url', 'base_url'), ('api_key', 'credential'), ('model_path', 'model')]]
        dep = dict(id='service', kind='service', configurable=True, required=True, read_time='process_start',
                   owner_scope='original process', entry_ids=['target'], evidence=evidence, mappings=mappings)
        self.candidate = dict(entries=[entry], dependencies=[dep], boundaries=[], runtime_groups=[])
        stub = self.root / 'fixture-dependencies'
        stub.mkdir()
        (stub / 'yaml.py').write_text('''import json
def safe_load(stream):
    print('CONFIG_PATH=' + stream.name)
    result = {}; stack = [(-1, result)]
    for line in stream:
        indent = len(line) - len(line.lstrip()); key, value = line.strip().split(':', 1)
        while stack[-1][0] >= indent: stack.pop()
        current = stack[-1][1]
        if not value.strip():
            current[key] = {}; stack.append((indent, current[key]))
        else:
            current[key] = json.loads(value.strip())
    print('GENERATION=' + str(result['llm']['generation']['temperature']))
    print('WORKERS=' + str(result['max_workers']))
    return result
''', encoding='utf-8')
        (stub / 'openai.py').write_text('''class OpenAI:
    def __init__(self, base_url, api_key):
        print('BASE=' + base_url)
        print('CREDENTIAL_MATCH=' + str(api_key == 'fixture-secret'))
        print('CREDENTIAL=' + api_key)
        self.chat = self; self.completions = self
    def create(self, model, messages):
        print('MODEL=' + model)
''', encoding='utf-8')
        self.store = Store(':memory:')
        self.runtime = OwnerRuntime(Coordinator(self.store))
        self.addCleanup(self.store.close)
        self.addCleanup(self.runtime.close)
        self.connection = dict(id='connection', base_url='https://example.invalid/v1', model='fixture-model',
                               auth={'mode': 'api_key'}, config_digest='digest', key_usable=True)
        profiles = SimpleNamespace(lock=threading.RLock(), public_connections=lambda: [self.connection],
            connection_profile=lambda _: self.connection, connection_key=lambda _: 'fixture-secret')
        self.registry = Dependencies(self.store, profiles, None)
        self.catalog = UnitCatalog(self.root, self.store, self.runtime)
        self.catalog.resource_registry = self.registry
        self.stub = stub

    def install(self):
        row = self.registry.record(self.inspector, self.candidate)[0]
        self.assertEqual(row['status'], 'MAPPING_CHECKED', row.get('reason'))
        self.registry.select(dict(dependency_id='source:service', harness_id='source', connection_id='connection'))
        self.inspector.request['dependency_bindings'] = self.registry.selections('source')
        unit = self.catalog.install(self.inspector, self.candidate, 'analysis', 'installation')[0]
        self.assertEqual(unit['status'], 'READY', unit.get('reason'))
        return unit

    def test_checked_yaml_reaches_original_process_and_is_cleaned(self):
        worker_path = os.pathsep.join([str(self.stub), os.environ.get('PYTHONPATH', '')])
        calls = []
        run = subprocess.run
        def capture(command, **kwargs):
            calls.append(command)
            return run(command, **kwargs)
        with patch.object(sys, 'path', [str(self.stub)] + sys.path), patch.dict(os.environ, PYTHONPATH=worker_path):
            unit = self.install()
            with patch('seampact.units.subprocess.run', side_effect=capture):
                result = self.catalog.invoke(unit['id'], {})
        self.assertEqual(result.returncode, 0, result.stderr)
        output = result.stdout.decode()
        self.assertIn('BASE=https://example.invalid/v1', output.splitlines())
        self.assertIn('MODEL=fixture-model', output)
        self.assertIn('CREDENTIAL_MATCH=True', output)
        self.assertIn('GENERATION=0.25', output)
        self.assertIn('WORKERS=1', output)
        self.assertNotIn('fixture-secret', output + result.stderr.decode())
        self.assertNotIn('fixture-secret', json.dumps(calls))
        config_path = next(line.split('=', 1)[1] for line in output.splitlines() if line.startswith('CONFIG_PATH='))
        self.assertFalse(Path(config_path).exists())
        self.assertEqual((self.root / 'config.yaml').read_bytes(), self.original)

    def test_task_cannot_override_configuration_path(self):
        worker_path = os.pathsep.join([str(self.stub), os.environ.get('PYTHONPATH', '')])
        with patch.object(sys, 'path', [str(self.stub)] + sys.path), patch.dict(os.environ, PYTHONPATH=worker_path):
            unit = self.install()
            with self.assertRaises((ValueError, TypeError)):
                self.catalog.invoke(unit['id'], {'config_path': 'other.yaml'})

    def test_process_failure_removes_private_configuration(self):
        worker_path = os.pathsep.join([str(self.stub), os.environ.get('PYTHONPATH', '')])
        paths = []
        def fail(command, **kwargs):
            path = Path(command[-1])
            self.assertTrue(path.is_file())
            self.assertIn('fixture-secret', path.read_text(encoding='utf-8'))
            paths.append(path)
            raise OSError('fixture process launch failed')
        with patch.object(sys, 'path', [str(self.stub)] + sys.path), patch.dict(os.environ, PYTHONPATH=worker_path):
            unit = self.install()
            with patch('seampact.units.subprocess.run', side_effect=fail):
                with self.assertRaisesRegex(OSError, 'fixture process launch failed'):
                    self.catalog.invoke(unit['id'], {})
        self.assertEqual(len(paths), 1)
        self.assertFalse(paths[0].exists())
        self.assertEqual((self.root / 'config.yaml').read_bytes(), self.original)

    def test_source_proved_configuration_path_is_not_a_fabricated_business_literal(self):
        self.inspector.request.pop('configuration')
        worker_path = os.pathsep.join([str(self.stub), os.environ.get('PYTHONPATH', '')])
        with patch.object(sys, 'path', [str(self.stub)] + sys.path), patch.dict(os.environ, PYTHONPATH=worker_path):
            unit = self.install()
        self.assertNotIn('config_path', unit['parameters'].get('properties', {}))

    def test_undeclared_resource_reference_is_rejected(self):
        self.candidate['entries'][0]['configuration']['config_path']['llm']['base_url']['$dependency']['id'] = 'other'
        worker_path = os.pathsep.join([str(self.stub), os.environ.get('PYTHONPATH', '')])
        with patch.object(sys, 'path', [str(self.stub)] + sys.path), patch.dict(os.environ, PYTHONPATH=worker_path):
            row = self.registry.record(self.inspector, self.candidate)[0]
            self.assertNotEqual(row['status'], 'MAPPING_CHECKED')


if __name__ == '__main__':
    unittest.main()
