"""A checker runs only through its retained and freshly checked image identity."""

import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from seampact.hosting import backends
from seampact.source.inspection import Inspector
from seampact.target_runtime import TargetRuntime, checker_identity


IMAGE = 'sha256:' + 'a' * 64
OTHER = 'sha256:' + 'b' * 64


class TargetRuntimeImageTests(unittest.TestCase):
    def fixture(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        (root / 'main.py').write_text('def target():\n    return 1\n', encoding='utf-8')
        inspector = Inspector.freeze(root, root / 'snapshots', 'request', ['main.py'], 'target', 'result')
        environment = dict(python='3.11', base_image='python:3.11-slim')
        inspector.request['runtime_environment'] = environment
        record = dict(status='READY', identity=checker_identity(environment), image_id=IMAGE,
                      image_reference=backends.content_image_reference(IMAGE))
        inspector.request['target_runtime'] = copy.deepcopy(record)
        return inspector, record

    def test_run_uses_the_rechecked_retained_reference_with_the_same_frozen_core(self):
        inspector, record = self.fixture()
        answer = dict(schema='target-source-tools-result/1', source_version=inspector.request['source_version'],
                      result=dict(candidates=[]))
        with patch.object(backends, 'docker_available', return_value=None), \
                patch.object(backends, '_inspect_image', return_value=(dict(returncode=0), dict(Id=IMAGE))) as inspect, \
                patch.object(backends, 'run', return_value=dict(returncode=0, stdout=json.dumps(answer), output='')) as run:
            result = TargetRuntime(inspector, record).call('entry_candidates', path='')
        self.assertEqual(result, dict(candidates=[]))
        self.assertEqual(inspect.call_args.args[0], record['image_reference'])
        self.assertEqual(run.call_args.args[0][-1], record['image_reference'])
        self.assertNotEqual(run.call_args.args[0][-1], IMAGE)

    def test_missing_or_moved_retained_image_is_rejected_before_execution(self):
        for observed, actual in ((dict(returncode=1), None), (dict(returncode=0), dict(Id=OTHER))):
            inspector, record = self.fixture()
            with patch.object(backends, 'docker_available', return_value=None), \
                    patch.object(backends, '_inspect_image', return_value=(observed, actual)), \
                    patch.object(backends, 'run') as run:
                with self.assertRaisesRegex(ValueError, '内容保留引用'):
                    TargetRuntime(inspector, record).call('entry_candidates', path='')
            run.assert_not_called()

    def test_new_ready_record_requires_the_content_reference_and_original_checker_identity(self):
        inspector, record = self.fixture()
        for change in (dict(image_reference='mutable:tag'), dict(image_reference=None), dict(identity='other-core')):
            with self.subTest(change=change), patch.object(backends, 'run') as run:
                with self.assertRaises(ValueError):
                    TargetRuntime(inspector, dict(record, **change)).call('entry_candidates', path='')
                run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
