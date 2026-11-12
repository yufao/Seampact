"""Contract checks for Python executor isolation and retained leases."""
import io
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from seampact.agent_runtime import RunControl
from seampact.isolated_execution import DockerPythonExecutor
from seampact.providers import RequestCancelled
from seampact.workspace_tools import WorkspaceTools


class Projects:
    lock = threading.RLock()

    def task_scope(self, task_id):
        return dict(task_id=task_id, project_id="p", conversation_id="c", status="ACTIVE")

    def container_mounts(self, task_id):
        return [dict(source="/host/data", target="/data", read_only=True),
                dict(source="/host/work", target="/workspace", read_only=False),
                dict(source="/host/scratch", target="/scratch", read_only=False)]


class FakeProcess:
    def __init__(self, *args, **kwargs):
        self.stdin = io.BytesIO()
        self.stdout = io.BytesIO(b"42\n")
        self.stderr = io.BytesIO()
        self.returncode = 0

    def poll(self):
        return self.returncode


class IsolatedExecutionTests(unittest.TestCase):
    def setUp(self):
        self.executor = DockerPythonExecutor("sha256:" + "a" * 64)
        self.scope = dict(project_id="p", conversation_id="c", task_id="t")

    def test_image_and_arguments_are_bounded(self):
        with self.assertRaises(ValueError):
            DockerPythonExecutor("python:latest")
        for timeout in (0, 121, True, 1.5):
            with self.assertRaises(ValueError):
                self.executor.run(Projects(), self.scope, "print(1)", timeout,
                                  control=RunControl(), task={}, emit=lambda *a, **k: None)
        self.assertNotIn("run_python", WorkspaceTools.names())
        self.assertIn("run_python", WorkspaceTools.names(True))

    def test_container_command_restricts_environment_and_returns_output(self):
        commands, task = [], {}
        def command(*args, **kwargs):
            commands.append(args)
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        self.executor._command = command
        with patch("seampact.isolated_execution.subprocess.Popen", FakeProcess):
            result = self.executor.run(Projects(), self.scope, "print(42)", 5,
                control=RunControl(), task=task, emit=lambda *a, **k: None)
        create = next(args for args in commands if args[0] == "create")
        for item in ("--read-only", "--network", "none", "--cap-drop", "ALL", "--pids-limit"):
            self.assertIn(item, create)
        self.assertIn("type=bind,src=/host/data,dst=/data,readonly", create)
        self.assertIn("type=bind,src=/host/work,dst=/workspace", create)
        self.assertNotIn("print(42)", create)
        self.assertEqual(result["stdout"], "42\n")
        self.assertTrue(task["python_calls"][0]["stopped"])

    def test_unknown_container_state_retains_lease(self):
        task = dict(python_calls=[dict(container="seampact-python-" + "a" * 32, stopped=False)])
        self.executor._running = lambda _: None
        self.assertFalse(self.executor.is_drained(task))
        self.assertFalse(task["python_calls"][0]["stopped"])

    def test_cancellation_during_create_never_starts_the_container(self):
        control, task = RunControl(), {}
        def command(*args, **kwargs):
            if args[0] == "create":
                control.cancelled.set()
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        self.executor._command = command
        with patch("seampact.isolated_execution.subprocess.Popen") as start:
            with self.assertRaises(RequestCancelled):
                self.executor.run(Projects(), self.scope, "print(1)", 5,
                    control=control, task=task, emit=lambda *a, **k: None)
        start.assert_not_called()
        self.assertTrue(task["python_calls"][0]["stopped"])


if __name__ == "__main__":
    unittest.main()
