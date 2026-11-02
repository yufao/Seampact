"""API/controller stop races cannot overwrite confirmed resource release."""
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from seampact.hosting import backends
from seampact.hosting.deployment import Deployments


class StopConcurrencyTests(unittest.TestCase):
    def fixture(self):
        deployments = Deployments.__new__(Deployments)
        deployments.lock = threading.RLock()
        deployments.stops_in_progress = set()
        deployments.affinity = {}
        deployments.backend = lambda: dict(operation_timeout_s=10)
        deployments.platform = SimpleNamespace(store=SimpleNamespace(get=lambda *args: {}),
            runtime=SimpleNamespace(snapshot=lambda: dict(calls={}, roots={})))
        record = dict(id='instance', group_id='group', state='DRAINING', in_flight=0,
                      pending_stop=dict(reason='operator'), handle=dict(container='seampact-owned-g1'),
                      released=False)
        deployments.instances = {record['id']: record}
        deployments._event = lambda *args, **kwargs: None
        return deployments, record

    def test_controller_and_api_issue_one_physical_stop(self):
        deployments, record = self.fixture()
        entered, finish = threading.Event(), threading.Event()
        errors = []

        def stop(*args):
            entered.set()
            self.assertTrue(finish.wait(3))
            return dict(status='STOPPED')

        def advance():
            try:
                deployments._advance_stops()
            except Exception as error:
                errors.append(error)

        with patch.object(backends, 'stop', side_effect=stop) as stopped:
            owner = threading.Thread(target=advance)
            owner.start()
            self.assertTrue(entered.wait(3))
            contender = threading.Thread(target=advance)
            contender.start()
            contender.join(3)
            self.assertFalse(contender.is_alive())
            finish.set()
            owner.join(3)
            self.assertFalse(owner.is_alive())
            self.assertEqual(stopped.call_count, 1)
        self.assertEqual(errors, [])
        self.assertEqual(record['state'], 'STOPPED')
        self.assertTrue(record['released'])
        self.assertIsNone(record['pending_stop'])
        self.assertEqual(deployments.stops_in_progress, set())

    def test_unconfirmed_ended_instance_remains_held_until_backend_confirmation(self):
        deployments, record = self.fixture()
        record['state'] = 'STOPPED'
        self.assertTrue(deployments._held(record))
        with patch.object(backends, 'stop', return_value=dict(status='STOP_PENDING', reason='unconfirmed')):
            deployments._advance_stops()
        self.assertFalse(record['released'])
        self.assertEqual(record['pending_stop']['blocked'], 'unconfirmed')
        self.assertTrue(deployments._held(record))

    def test_successful_observation_clears_only_controller_error(self):
        deployments, record = self.fixture()
        record.update(state='STOPPED', pending_stop=None)
        deployments.closed = SimpleNamespace(wait=lambda delay: next(waits), is_set=lambda: False)
        waits = iter([False, True])
        deployments.controller_error = dict(error='previous stop race')
        deployments._retire_idle = lambda: None
        deployments._observe_starting = lambda: None
        deployments._controller()
        self.assertIsNone(deployments.controller_error)
        self.assertFalse(record['released'])
        self.assertTrue(deployments._held(record))


class DockerStopObservationTests(unittest.TestCase):
    def step(self, code=0, stdout=''):
        return dict(argv=['docker', 'ps'], returncode=code, stdout=stdout, output=stdout, duration_ms=1)

    def test_absence_is_success_only_after_actual_backend_query(self):
        record = dict(handle=dict(container='seampact-owned-g1'))
        with patch.object(backends, 'docker_available', return_value=None), \
             patch.object(backends, 'run', return_value=self.step()) as run:
            result = backends.stop(record, dict(operation_timeout_s=10))
        self.assertEqual(result['status'], 'STOPPED')
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][1:3], ['ps', '-a'])
        self.assertIn('name=^/seampact\\-owned\\-g1$', run.call_args.args[0])
        with patch.object(backends, 'docker_available', return_value=None), \
             patch.object(backends, 'run', return_value=self.step(1, 'query failed')):
            self.assertEqual(backends.stop(record, dict(operation_timeout_s=10))['status'], 'STOP_PENDING')

    def test_existing_container_still_requires_stop_and_remove(self):
        with patch.object(backends, 'docker_available', return_value=None), \
             patch.object(backends, 'run', side_effect=[self.step(stdout='container-id'), self.step(), self.step()]) as run:
            result = backends.stop(dict(handle=dict(container='seampact-owned-g1')), dict(operation_timeout_s=10))
        self.assertEqual(result['status'], 'STOPPED')
        self.assertEqual([call.args[0][1] for call in run.call_args_list], ['ps', 'stop', 'rm'])


if __name__ == '__main__':
    unittest.main()
