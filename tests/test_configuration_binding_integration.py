"""A selected file reaches the original CLI merge and loader through a Unit."""

import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import test_configuration_binding as fixtures

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from seampact.dependencies import Dependencies
from seampact.resources import ResourceRegistry
from seampact.runtime import Coordinator, OwnerRuntime
from seampact.store import Store
from seampact.units import UnitCatalog


class ConfigurationBindingIntegration(unittest.TestCase):
    def test_registered_selection_is_used_by_the_original_cli(self):
        try:
            import yacs.config
        except ImportError:
            self.skipTest('optional original worker dependency yacs is unavailable')
        inspector, candidate, root = fixtures.ConfigurationBindingChecks.fixture(self)
        inspector.request['source_id'] = 'source'
        # The external loader is observed without downloading or running a model.
        # It is deliberately outside the frozen original source proof.
        stub = root / 'fixture-dependencies'
        stub.mkdir()
        (stub / 'torch.py').write_text(
            "def load(path):\n    print('ORIGINAL_LOADER=' + path)\n    return {}\n", encoding='utf-8')
        selected = root / 'selected model.bin'
        selected.write_bytes(b'fixture-only-resource')
        yaml_before = (root / 'example.yaml').read_bytes()
        store = Store(':memory:')
        runtime = OwnerRuntime(Coordinator(store))
        self.addCleanup(store.close)
        self.addCleanup(runtime.close)
        profiles = SimpleNamespace(private_directory=root / 'private')
        resources = ResourceRegistry(root / 'platform', store, profiles)
        registry = Dependencies(store, profiles, resources)
        row = registry.record(inspector, candidate)[0]
        self.assertEqual(row['status'], 'MAPPING_CHECKED', row.get('reason'))
        resource = resources.save_weight(dict(name='fixture', kind='file', path=str(selected)))
        self.assertEqual(resources.check_weight(resource['id'])['check']['status'], 'present')
        registry.select(dict(dependency_id='source:weights', harness_id='source', resource_id=resource['id']))
        inspector.request['dependency_bindings'] = registry.selections('source')
        catalog = UnitCatalog(root, store, runtime)
        catalog.resource_registry = registry
        worker_path = os.pathsep.join([str(stub), os.environ.get('PYTHONPATH', '')])
        with patch.object(sys, 'path', [str(stub)] + sys.path), patch.dict(os.environ, PYTHONPATH=worker_path):
            unit = catalog.install(inspector, candidate, 'analysis', 'installation')[0]
            self.assertEqual(unit['status'], 'READY', (unit.get('reason'), unit.get('dependency_closure')))
            self.assertEqual(unit['parameters']['required'], ['image'])
            result = catalog.invoke(unit['id'], dict(image='image with spaces.png'))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(('ORIGINAL_LOADER=' + str(selected)).encode(), result.stdout)
        self.assertEqual((root / 'example.yaml').read_bytes(), yaml_before)
        self.assertEqual(registry.selections('source')['weights']['resource_id'], resource['id'])


if __name__ == '__main__':
    unittest.main()
