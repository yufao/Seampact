"""A terminal model job must not leave its source locked against a fresh analysis."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from seampact.service import Platform


class AnalysisRestartChecks(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        original = root / 'original'
        original.mkdir()
        (original / 'main.py').write_text('def target(value: str) -> str:\n    return value\n', encoding='utf-8')
        self.platform = Platform(root / 'platform')
        self.addCleanup(self.platform.close)
        self.source = dict(id='source', name='fixture', address=str(original), root=str(original),
                           files=['main.py'], inventory=[], entry='target', status='ANALYZING',
                           analysis_id='old', request_id='old-request')
        self.platform.store.save('harness', 'source', self.source)
        self.platform.store.save('request', 'old-request', dict(id='old-request', source_id='source'))

    def job(self, status):
        self.platform.store.save('analysis', 'old', dict(id='old', request_id='old-request', harness_id='source',
            status=status, phase='model_request', failure_phase='model_request', rounds=1,
            started_at='2026-01-01T00:00:00+00:00', error='provider HTTP 400', usage=[], messages=[], submitted=None))

    def test_terminal_jobs_can_start_a_fresh_analysis_despite_stale_source_status(self):
        for status in ('FAILED', 'INTERRUPTED', 'CANCELLED'):
            with self.subTest(status=status):
                self.platform.store.save('harness', 'source', self.source)
                self.job(status)
                visible = next(h for h in self.platform.state()['harnesses'] if h['id'] == 'source')
                self.assertEqual(visible['status'], status)
                with patch.object(self.platform.analysis, 'start') as start, \
                        patch.object(self.platform.profiles, 'role_identity', return_value='analysis'):
                    result = self.platform.analyze('source')
                start.assert_called_once()
                self.assertEqual(start.call_args.kwargs['preprocessing'], 'api')
                self.assertNotEqual(result['analysis_id'], 'old')
                self.assertNotEqual(start.call_args.args[0].request['id'], 'old-request')

    def test_live_analysis_or_revision_blocks_restart_even_when_source_says_failed(self):
        source = dict(self.source, status='FAILED')
        self.platform.store.save('harness', 'source', source)
        for status in ('RUNNING', 'CANCELLING'):
            with self.subTest(status=status):
                self.job(status)
                with patch.object(self.platform.analysis, 'start') as start:
                    with self.assertRaisesRegex(ValueError, '正在分析'):
                        self.platform.analyze('source')
                    start.assert_not_called()
                with self.assertRaisesRegex(ValueError, '模型分析或修订'):
                    self.platform._environment_guard(source)

    def test_preparing_environment_still_blocks_without_a_live_analysis(self):
        self.job('FAILED')
        self.platform.store.save('harness', 'source', dict(self.source, status='FAILED',
                                  target_runtime=dict(status='PREPARING')))
        with patch.object(self.platform.analysis, 'start') as start:
            with self.assertRaisesRegex(ValueError, '目标解释器正在准备'):
                self.platform.analyze('source')
            start.assert_not_called()


if __name__ == '__main__':
    unittest.main()
