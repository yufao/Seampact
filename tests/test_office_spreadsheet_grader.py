"""Native-host grading requires retained, task-owned execution isolation evidence."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'scripts')]
from seampact.isolated_execution import ISOLATION_RECEIPT
from seampact.office_spreadsheet import SOURCE_REVISION
from seampact.store import Store
from run_office_spreadsheet_grader import main, verify_business_home


class NativeHostGraderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.home, self.grading = self.root / 'business', self.root / 'grading'
        self.home.mkdir()
        self.grading.mkdir()
        self.manifest = self.grading / 'manifest.json'
        self.manifest.write_text('{}', encoding='utf-8')
        self.manifest_sha = hashlib.sha256(self.manifest.read_bytes()).hexdigest()
        self.scope = dict(project_id='project', conversation_id='session', task_id='task')
        self.image = 'sha256:' + 'a' * 64
        self.project = self.home / 'projects' / 'project'
        self.scratch = self.project / 'tasks' / 'task' / 'scratch'
        self.mounts = [dict(source=str(path), target=target, read_only=readonly) for path, target, readonly in (
            (self.project / 'tasks' / 'task' / 'inputs', '/data', True),
            (self.project / 'sessions' / 'session' / 'workspace', '/workspace', False),
            (self.scratch, '/scratch', False))]
        for mount in self.mounts:
            Path(mount['source']).mkdir(parents=True)
        self.task = dict(id='task', project_id='project', conversation_id='session', status='COMPLETED',
            workspace=dict(self.scope), workspace_status='FINISHED',
            experiment=dict(case='case', manifest_sha256='b' * 64, original_manifest_sha256=self.manifest_sha),
            evaluation=dict(outputs=[dict(index=i, sha256=None) for i in (1, 2, 3)]),
            spreadsheet_replay=dict(status='REPLAYED'), files=[], python_calls=[self.call('one', self.mounts)])
        self.store = Store(self.home / 'platform.sqlite3')
        self.addCleanup(self.store.close)
        self.store.save('project', 'project', dict(id='project'))
        self.store.save('project_session', 'session', dict(id='session', project_id='project'))
        self.lease = dict(self.scope, status='FINISHED', private_inputs=True)
        self.persist()

    def call(self, identity, mounts):
        return dict(call_id=identity, container='seampact-python-' + identity, stopped=True,
            create_uncertain=False, status='COMPLETED', isolation_receipt=dict(
                schema=ISOLATION_RECEIPT, verified_before_start=True, container_id='c' * 64,
                container_name='seampact-python-' + identity, image=self.image, scope=dict(self.scope),
                command=['python', '-I', '-'], working_directory='/workspace',
                requested_mounts=copy.deepcopy(mounts), daemon_mounts=copy.deepcopy(mounts),
                mounts=[dict(Type='bind', Source=m['source'], Destination=m['target'], RW=not m['read_only']) for m in mounts],
                sandbox=dict(NetworkMode='none', ReadonlyRootfs=True, Privileged=False, CapAdd=None, CapDrop=['ALL'],
                    SecurityOpt=['no-new-privileges'], NanoCpus=1000000000, Memory=512 * 1024 * 1024,
                    PidsLimit=64, LogConfig=dict(Type='none'), Tmpfs={'/tmp': 'rw,nosuid,nodev,noexec,size=64m'})))

    def persist(self):
        self.store.save('task', 'task', self.task)
        self.store.save('project_lease', 'task', self.lease)

    def verify(self):
        self.persist()
        return verify_business_home(self.home, self.task, self.grading)

    def test_task_owned_development_and_all_frozen_replay_mounts_are_accepted(self):
        for i in (1, 2, 3):
            base = self.scratch / ('spreadsheet-replay-' + 'd' * 32) / str(i)
            mounts = [dict(source=str(base / path), target=target, read_only=readonly) for path, target, readonly in (
                ('data', '/data', True), ('workspace', '/workspace', False), ('scratch', '/scratch', False),
                ('solution.py', '/solution.py', True))]
            self.task['python_calls'].append(self.call('replay-' + str(i), mounts))
        result = self.verify()
        self.assertEqual(result['verified_python_calls'], 4)
        self.assertEqual(result['images'], [self.image])

    def test_missing_receipt_unknown_stop_or_foreign_scope_is_rejected(self):
        original = copy.deepcopy(self.task)
        changes = (lambda c: c.pop('isolation_receipt'), lambda c: c.update(stopped=False),
                   lambda c: c.update(create_uncertain=True),
                   lambda c: c['isolation_receipt']['scope'].update(conversation_id='other'))
        for change in changes:
            with self.subTest(change=change):
                self.task = copy.deepcopy(original)
                change(self.task['python_calls'][0])
                with self.assertRaisesRegex(ValueError, 'isolation receipt'):
                    self.verify()

    def test_changed_actual_mount_extra_mount_or_writable_inputs_are_rejected(self):
        original = copy.deepcopy(self.task)
        for change in (
            lambda r: r['mounts'][0].update(Source=str(self.grading)),
            lambda r: r['mounts'].append(dict(Type='bind', Source=str(self.root), Destination='/extra', RW=False)),
            lambda r: r['mounts'][0].update(RW=True),
            lambda r: r['requested_mounts'][1].update(source=str(self.home / 'projects' / 'other'))):
            with self.subTest(change=change):
                self.task = copy.deepcopy(original)
                change(self.task['python_calls'][0]['isolation_receipt'])
                with self.assertRaisesRegex(ValueError, 'mounts differ'):
                    self.verify()

    def test_network_privilege_rootfs_and_unbounded_scratch_are_rejected(self):
        original = copy.deepcopy(self.task)
        for change in (dict(NetworkMode='bridge'), dict(ReadonlyRootfs=False), dict(Privileged=True),
                       dict(CapAdd=['SYS_ADMIN']), dict(Tmpfs={'/tmp': 'rw,size=1g'})):
            with self.subTest(change=change):
                self.task = copy.deepcopy(original)
                self.task['python_calls'][0]['isolation_receipt']['sandbox'].update(change)
                with self.assertRaisesRegex(ValueError, 'isolation settings'):
                    self.verify()

    def test_unsettled_or_wrong_project_lease_is_rejected(self):
        for change in (dict(status='UNKNOWN'), dict(project_id='other'), dict(conversation_id='other')):
            with self.subTest(change=change):
                self.lease = dict(self.scope, status='FINISHED', private_inputs=True)
                self.lease.update(change)
                with self.assertRaisesRegex(ValueError, 'ownership'):
                    self.verify()

    def test_collected_receipt_cannot_replace_the_authoritative_task(self):
        collected = copy.deepcopy(self.task)
        collected['python_calls'][0]['isolation_receipt']['mounts'] = []
        with self.assertRaisesRegex(ValueError, 'Collected task differs'):
            verify_business_home(self.home, collected, self.grading)

    def test_grading_cannot_be_inside_or_above_the_business_home(self):
        for grading in (self.project, self.root):
            with self.subTest(grading=grading), self.assertRaisesRegex(ValueError, 'overlap'):
                verify_business_home(self.home, self.task, grading)

    def test_host_cli_scores_only_after_database_and_execution_receipts_match(self):
        self._run_cli(host=True)

    def test_existing_business_container_cli_remains_available(self):
        self._run_cli(host=False)

    def test_utf8_task_json_is_read_independently_of_the_windows_default_encoding(self):
        self.task['encoding_probe'] = '原始任务 –'
        self.persist()
        self._run_cli(host=True)

    def test_blocked_before_python_with_missing_or_empty_calls_scores_false_without_claiming_isolation(self):
        self.task.update(status='BLOCKED', spreadsheet_replay=None)
        self.task['python_calls'] = []
        self.assertEqual(self.verify()['status'], 'NO_EXECUTION')
        self.task.pop('python_calls')
        self.persist()
        result = self._run_cli(host=True, expected_calls=0)
        self.assertEqual(result['cases'], [False] * 3)
        self.assertFalse(result['business_isolation']['isolation_verified'])

    def test_no_python_cannot_claim_a_solution_replay_or_delivered_outputs(self):
        original = copy.deepcopy(self.task)
        original['python_calls'] = []
        original.pop('spreadsheet_replay')
        for change in (lambda t: t.update(spreadsheet_replay=dict(status='REPLAYED')),
                       lambda t: t.update(files=[dict(sha256='d' * 64)]),
                       lambda t: t['evaluation'].update(solution_sha256='d' * 64),
                       lambda t: t['evaluation']['outputs'][0].update(sha256='d' * 64)):
            with self.subTest(change=change):
                self.task = copy.deepcopy(original)
                change(self.task)
                with self.assertRaisesRegex(ValueError, 'cannot claim'):
                    self.verify()

    def test_create_failure_without_receipt_remains_unknown_infrastructure(self):
        self.task.update(status='FAILED', spreadsheet_replay=None, workspace_status='UNKNOWN',
            python_calls=[dict(call_id='create-failed', status='FAILED', stopped=False, create_uncertain=True)])
        self.task['evaluation']['execution_infrastructure_error'] = 'ValueError'
        self.lease['status'] = 'UNKNOWN'
        self.persist()
        result = self._run_cli(host=True, expected_calls=None, expected_status='UNKNOWN_INFRASTRUCTURE')
        self.assertEqual(result['cases'], [None] * 3)
        self.assertIsNone(result['quality'])
        self.assertFalse(result['business_isolation']['isolation_verified'])

    def test_grader_timeout_requests_removal_of_the_named_scoring_container(self):
        result = self._run_cli(host=True, expected_status='UNKNOWN_INFRASTRUCTURE', timeout=True)
        self.assertEqual(result['grader_cleanup'], 'REMOVED')
        self.assertEqual(result['cases'], [None] * 3)

    def _run_cli(self, *, host, expected_calls=1, expected_status='EVALUATED', timeout=False):
        directory = self.root / 'collection'
        directory.mkdir()
        (directory / 'delivered').mkdir()
        (directory / 'task.json').write_text(json.dumps(self.task, ensure_ascii=False), encoding='utf-8')
        collection = dict(task_id='task', case_id='case', status='COMPLETED',
            records=[dict(index=i, sha256=None, status='MISSING_DELIVERY') for i in (1, 2, 3)])
        (directory / 'collection.json').write_text(json.dumps(collection), encoding='utf-8')
        receipt = self.root / 'grader-receipt.json'
        receipt.write_text(json.dumps(dict(status='READY_SYNTHETIC_VALIDATED', image=self.image,
                           grading_only=dict(manifest_sha256=self.manifest_sha))), encoding='utf-8')
        args = ['grader', str(directory), '--allowed-root', str(self.root), '--grading-root', str(self.grading),
                '--receipt', str(receipt), *(['--business-home', str(self.home)] if host else ['--business-container', 'existing-platform'])]
        commands = []
        def output(command, **kwargs):
            commands.append(command)
            return self.image if command[:3] == ['docker', 'image', 'inspect'] else '[]'
        def run_container(command, **kwargs):
            commands.append(command)
            if timeout and command[:2] == ['docker', 'run']:
                raise subprocess.TimeoutExpired(command, 600)
            return SimpleNamespace(returncode=0, stdout=json.dumps(
                dict(case_id='case', source_revision=SOURCE_REVISION, cases=[False] * 3, hard=False, soft=0)))
        with patch.object(sys, 'argv', args), patch('run_office_spreadsheet_grader.subprocess.check_output', side_effect=output), \
             patch('run_office_spreadsheet_grader.subprocess.run', side_effect=run_container), \
             patch('builtins.print'):
            main()
        result = json.loads((directory / 'native-score.json').read_text(encoding='utf-8'))
        self.assertEqual(result['status'], expected_status)
        self.assertEqual(result['quality'], False if expected_status == 'EVALUATED' else None)
        self.assertEqual(any(command[:2] == ['docker', 'inspect'] for command in commands), not host)
        if host and expected_calls is not None:
            self.assertEqual(result['business_isolation']['verified_python_calls'], expected_calls)
        if expected_status == 'EVALUATED':
            self.assertTrue(any(command[:2] == ['docker', 'run'] for command in commands))
        if timeout:
            self.assertEqual(commands[-1], ['docker', 'rm', '-f', result['grader_container']])
        return result


if __name__ == '__main__':
    unittest.main()
