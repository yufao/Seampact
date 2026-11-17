"""Single-task workspace reconciliation keeps project neighbors untouched."""
import copy
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from seampact.chat import ChatJobs


class WorkspaceReconcileTests(unittest.TestCase):
    def setUp(self):
        self.body = dict(task_id='target', project_id='project', conversation_id='conversation')
        self.task = dict(id='target', project_id='project', conversation_id='conversation',
                         workspace=dict(self.body), status='COMPLETED', workspace_status='UNKNOWN')
        self.lease = dict(self.body, status='UNKNOWN')
        self.chat = object.__new__(ChatJobs)
        self.chat._workspace_reconcile_lock = threading.Lock()
        self.chat._threads = {}
        self.chat.store = SimpleNamespace(get=lambda kind, identity: copy.deepcopy(self.task))
        self.chat.projects = SimpleNamespace(lock=threading.RLock(),
            task_scope=lambda identity: copy.deepcopy(self.lease),
            session=lambda identity: dict(project_id='project'))
        self.chat.catalog = self.chat.profiles = None
        self.called = []
        def finish(task, catalog, profiles):
            self.called.append(task['id'])
            task['workspace_status'] = 'FINISHED'
            return True
        self.chat._finish_project_scope = finish

    def test_only_requested_task_is_reconciled(self):
        result = self.chat.reconcile_task_workspace(self.body)
        self.assertEqual(result['workspace_status'], 'FINISHED')
        self.assertEqual(self.called, ['target'])

    def test_terminal_scope_and_worker_are_checked(self):
        for kind in ('active', 'scope', 'thread'):
            with self.subTest(kind=kind):
                self.setUp()
                if kind == 'active':
                    self.task['status'] = 'RUNNING'
                elif kind == 'scope':
                    self.lease['conversation_id'] = 'another'
                else:
                    self.chat._threads['target'] = SimpleNamespace(is_alive=lambda: True)
                with self.assertRaises(ValueError):
                    self.chat.reconcile_task_workspace(self.body)
                self.assertEqual(self.called, [])


if __name__ == '__main__':
    unittest.main()
