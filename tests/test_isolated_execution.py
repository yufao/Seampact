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
from seampact.isolated_execution import DockerPythonExecutor, _daemon_path, mount_source_identity
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
        with patch("seampact.isolated_execution.subprocess.Popen", FakeProcess), \
             patch.object(self.executor, "_created_receipt", return_value=dict(image=self.executor.image)):
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
        self.assertEqual(task["python_calls"][0]["isolation_receipt"]["scope"], self.scope)
        self.assertEqual(task["python_calls"][0]["isolation_receipt"]["requested_mounts"], Projects().container_mounts('t'))

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

    def test_timed_out_create_only_recovers_exact_unstarted_container(self):
        image = "sha256:" + "a" * 64
        mounts = Projects().container_mounts("t")
        receipt = dict(Id="d" * 64, Name="/seampact-python-" + "b" * 32, Image=image,
            State=dict(Status="created", Running=False, Pid=0, StartedAt="0001-01-01T00:00:00Z"),
            Config=dict(Cmd=["python", "-I", "-"], OpenStdin=True, WorkingDir="/workspace"),
            HostConfig=dict(NetworkMode="none", ReadonlyRootfs=True, Privileged=False, CapDrop=["ALL"],
                SecurityOpt=["no-new-privileges"], NanoCpus=1000000000, Memory=512 * 1024 * 1024,
                PidsLimit=64, LogConfig=dict(Type="none"),
                Tmpfs={"/tmp": "rw,nosuid,nodev,noexec,size=64m"}),
            Mounts=[dict(Type="bind", Source=m["source"], Destination=m["target"], RW=not m["read_only"])
                    for m in mounts])
        def command(*args, **kwargs):
            raw = image.encode() if args[:2] == ("image", "inspect") else json.dumps([receipt]).encode()
            return SimpleNamespace(returncode=0, stdout=raw, stderr=b"")
        self.executor._command = command
        name = receipt["Name"][1:]
        self.assertTrue(self.executor._verified_created(name, mounts))
        receipt['Config']['Env'] = ['PRIVATE_TEST_CREDENTIAL=do-not-retain']
        verified = self.executor._created_receipt(name, mounts)
        self.assertNotIn('PRIVATE_TEST_CREDENTIAL', json.dumps(verified))
        self.assertEqual(verified['image'], image)
        self.assertEqual(verified['container_id'], 'd' * 64)
        for change in (
                lambda x: x["State"].update(Status="running"),
                lambda x: x["State"].update(StartedAt="2026-01-01T00:00:00Z"),
                lambda x: x["Mounts"][1].update(Source="/another/session"),
                lambda x: x["HostConfig"].update(NetworkMode="bridge"),
                lambda x: x.update(Image="sha256:" + "b" * 64)):
            saved = json.loads(json.dumps(receipt))
            change(receipt)
            self.assertFalse(self.executor._verified_created(name, mounts))
            receipt.clear()
            receipt.update(saved)

    def test_create_timeout_verified_receipt_continues_and_unknown_never_starts(self):
        for verified in (True, False):
            with self.subTest(verified=verified):
                task, commands = {}, []
                def command(*args, **kwargs):
                    commands.append(args)
                    if args[0] == "create":
                        raise ValueError("create timed out")
                    return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
                self.executor._command = command
                with patch.object(self.executor, "_mount_sources", side_effect=lambda rows: rows), \
                     patch.object(self.executor, "_verified_created", return_value=verified), \
                     patch.object(self.executor, "_created_receipt", return_value=dict(image=self.executor.image) if verified else None), \
                     patch("seampact.isolated_execution.subprocess.Popen", return_value=FakeProcess()) as start:
                    if verified:
                        self.executor.run(Projects(), self.scope, "print(1)", 5,
                            control=RunControl(), task=task, emit=lambda *a, **k: None)
                        self.assertTrue(task["python_calls"][0]["stopped"])
                        self.assertEqual(task["python_calls"][0]["create_recovered"], "verified_unstarted_container")
                        start.assert_called_once()
                    else:
                        with self.assertRaisesRegex(RuntimeError, "状态未确认"):
                            self.executor.run(Projects(), self.scope, "print(1)", 5,
                                control=RunControl(), task=task, emit=lambda *a, **k: None)
                        start.assert_not_called()
                        self.assertTrue(task["python_calls"][0]["create_uncertain"])
                        self.assertFalse(any(args[0] in {"kill", "rm"} for args in commands))

    def test_normal_create_never_starts_without_verified_actual_isolation(self):
        task = {}
        self.executor._command = lambda *a, **k: SimpleNamespace(returncode=0, stdout=b'', stderr=b'')
        with patch.object(self.executor, '_created_receipt', return_value=None), \
             patch('seampact.isolated_execution.subprocess.Popen') as start:
            with self.assertRaisesRegex(ValueError, '无法核实隔离'):
                self.executor.run(Projects(), self.scope, 'print(1)', 5,
                                  control=RunControl(), task=task, emit=lambda *a, **k: None)
        start.assert_not_called()
        self.assertNotIn('isolation_receipt', task['python_calls'][0])
        self.assertTrue(task['python_calls'][0]['stopped'])

    def test_windows_docker_desktop_mount_identity_is_exact(self):
        expected = mount_source_identity('C:\\office\\projects\\p\\data')
        self.assertEqual(expected, mount_source_identity('/run/desktop/mnt/host/c/office/projects/p/data'))
        self.assertEqual(expected, mount_source_identity('/host_mnt/c/office/projects/p/data'))
        self.assertNotEqual(expected, mount_source_identity('/run/desktop/mnt/host/d/office/projects/p/data'))
        self.assertNotEqual(expected, mount_source_identity('/unrelated/office/projects/p/data'))
        for value in ('relative/path', 'C:\\office\\..\\grading', '/office/../grading'):
            with self.assertRaises(ValueError):
                mount_source_identity(value)


if __name__ == "__main__":
    unittest.main()
