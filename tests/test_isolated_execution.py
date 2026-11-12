"""Contract checks for Python executor isolation and retained leases."""
import io
import json
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from seampact.agent_runtime import RunControl
from seampact.isolated_execution import DockerPythonExecutor, _daemon_path
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

    def test_platform_bind_maps_project_and_replay_paths_without_broadening(self):
        mounts = [dict(Type="bind", Source="/srv/pilot/data", Destination="/data", RW=True)]
        self.assertEqual(_daemon_path("/data/projects/p/data", mounts, writable=False),
                         "/srv/pilot/data/projects/p/data")
        self.assertEqual(_daemon_path("/data/projects/p/tasks/t/scratch/replay/solution.py", mounts, writable=False),
                         "/srv/pilot/data/projects/p/tasks/t/scratch/replay/solution.py")
        self.assertEqual(_daemon_path("/data/projects/p/sessions/c/workspace", mounts, writable=True),
                         "/srv/pilot/data/projects/p/sessions/c/workspace")
        with self.assertRaises(ValueError):
            _daemon_path("/outside/project", mounts, writable=True)
        nested = mounts + [dict(Type="tmpfs", Source="", Destination="/data/projects/p", RW=True)]
        with self.assertRaises(ValueError):
            _daemon_path("/data/projects/p/data", nested, writable=False)
        mounts[0]["RW"] = False
        with self.assertRaises(ValueError):
            _daemon_path("/data/projects/p/workspace", mounts, writable=True)

    def test_container_uses_its_inspected_mounts_for_sources(self):
        observed = []
        def command(*args, **kwargs):
            observed.append(args)
            return SimpleNamespace(returncode=0, stdout=json.dumps([
                dict(Type="bind", Source="/srv/pilot/data", Destination="/data", RW=True)
            ]).encode(), stderr=b"")
        def path(value):
            if value == "/.dockerenv":
                return SimpleNamespace(exists=lambda: True)
            return SimpleNamespace(resolve=lambda strict: value)
        self.executor._command = command
        with patch("seampact.isolated_execution.Path", side_effect=path), patch.dict(
                "os.environ", {"SEAMPACT_CONTAINER_NAME": "platform"}):
            mapped = self.executor._mount_sources([
                dict(source="/data/projects/p/data", target="/data", read_only=True),
                dict(source="/data/projects/p/sessions/c/workspace", target="/workspace", read_only=False)])
        self.assertEqual(observed, [("inspect", "--format", "{{json .Mounts}}", "platform")])
        self.assertEqual([x["source"] for x in mapped],
                         ["/srv/pilot/data/projects/p/data", "/srv/pilot/data/projects/p/sessions/c/workspace"])

    def test_create_error_records_safe_docker_reason_without_stderr(self):
        task = {}
        def command(*args, **kwargs):
            if args[0] == "create":
                return SimpleNamespace(returncode=1, stdout=b"", stderr=(
                    b'invalid mount config for type "bind": bind source path does not exist: /private/token'))
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        self.executor._command = command
        with patch.object(self.executor, "_mount_sources", side_effect=lambda rows: rows):
            with self.assertRaisesRegex(ValueError, "无法创建隔离"):
                self.executor.run(Projects(), self.scope, "print(1)", 5,
                    control=RunControl(), task=task, emit=lambda *a, **k: None)
        self.assertEqual(task["python_calls"][0]["create_error"],
                         dict(exit_code=1, reason="Docker bind source path does not exist"))
        self.assertNotIn("/private/token", str(task))


if __name__ == "__main__":
    unittest.main()
