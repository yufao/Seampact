"""Exercise the CLI's spec construction without contacting Docker or deploying."""

import ast
import copy
import runpy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


class DeploySocketGroupsTests(unittest.TestCase):
    def helper(self, rootless, *, reject_stat=False):
        source = (Path(__file__).resolve().parents[1] / "deploy.sh").read_text(encoding="utf-8")
        embedded = source.split("<<'SEAMPACT_DEPLOY_PY'", 1)[1].rsplit("SEAMPACT_DEPLOY_PY", 1)[0]
        tree = ast.parse(embedded)
        function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "add_tools")

        class Socket:
            def __str__(self):
                return "/run/example/docker.sock"

            def stat(self):
                if reject_stat:
                    raise AssertionError("rootless must not use the host numeric GID")
                return SimpleNamespace(st_gid=991)

        def fail(message):
            raise RuntimeError(message)

        def api(method, path):
            self.assertEqual((method, path), ("GET", "/info"))
            return {"SecurityOptions": ["name=rootless"] if rootless else [], "DockerRootDir": "/var/lib/docker"}

        namespace = dict(SOCKET_PATH=Socket(), OLD=None, Path=Path, fail=fail, api=api, CONFIG=dict(container="platform"),
            inspect_environment=lambda env: dict(row.split("=", 1) for row in env or []))
        exec(compile(ast.Module(body=[function], type_ignores=[]), "deploy.sh:add_tools", "exec"), namespace)
        return namespace["add_tools"]

    def test_rootless_root_does_not_add_host_gid(self):
        spec = dict(User="0:0", Env=[], HostConfig={})
        self.helper(True, reject_stat=True)(spec)
        self.assertNotIn("GroupAdd", spec["HostConfig"])
        self.assertEqual(spec["HostConfig"]["Mounts"][0]["Target"], "/var/run/docker.sock")
        self.assertIn("DOCKER_HOST=unix:///var/run/docker.sock", spec["Env"])

    def test_rootless_preserves_existing_groups(self):
        spec = dict(User="0:0", Env=[], HostConfig=dict(GroupAdd=["12"]))
        self.helper(True, reject_stat=True)(spec)
        self.assertEqual(spec["HostConfig"]["GroupAdd"], ["12"])

    def test_rootful_adds_socket_group_once(self):
        spec = dict(User="1000:1000", Env=[], HostConfig=dict(GroupAdd=["12"]))
        add = self.helper(False)
        add(spec)
        add(spec)
        self.assertEqual(spec["HostConfig"]["GroupAdd"], ["12", "991"])

    def test_adopted_hostname_is_preserved_but_storage_uses_stable_identity(self):
        spec = dict(User="1000:1000", Hostname="old-generated-hostname", Env=["HOSTNAME=old-generated-hostname"], HostConfig={})
        add = self.helper(False)
        self.assertEqual(add(spec), {"/var/run/docker.sock", "/seampact-docker-storage"})
        self.assertEqual(add(spec), set())
        self.assertEqual(spec["Hostname"], "old-generated-hostname")
        self.assertIn("SEAMPACT_CONTAINER_NAME=platform", spec["Env"])
        self.assertIn("SEAMPACT_DOCKER_STORAGE_PATH=/seampact-docker-storage", spec["Env"])
        storage = [m for m in spec["HostConfig"]["Mounts"] if m["Target"] == "/seampact-docker-storage"]
        self.assertEqual(storage, [dict(Type="bind", Source="/var/lib/docker", Target="/seampact-docker-storage", ReadOnly=True)])

    def test_existing_conflicting_storage_mount_is_rejected(self):
        for source, readonly in [("/other-daemon", True), ("/var/lib/docker", False)]:
            spec = dict(User="0:0", Env=[], HostConfig=dict(Mounts=[dict(Type="bind", Source=source,
                Target="/seampact-docker-storage", ReadOnly=readonly)]))
            with self.subTest(source=source, readonly=readonly), self.assertRaisesRegex(RuntimeError, "storage probe mount"):
                self.helper(False)(spec)

    def test_rootless_custom_user_is_not_silently_replaced(self):
        spec = dict(User="1000:1000", Env=[], HostConfig={})
        with self.assertRaisesRegex(RuntimeError, "existing custom user is preserved"):
            self.helper(True, reject_stat=True)(spec)
        self.assertEqual(spec["User"], "1000:1000")


class CreatedRecoveryTests(unittest.TestCase):
    def test_recovery_requires_identity_never_started_and_completed_backup(self):
        source = Path(__file__).resolve().parents[1] / "deploy/recover_created.py"
        validate = runpy.run_path(str(source))["validate"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            data = root / "data"
            data.mkdir()
            (data / "platform.sqlite3").touch()
            backup = root / "backups" / "completed"
            (backup / "data").mkdir(parents=True)
            (backup / "data/platform.sqlite3").touch()
            record = dict(schema="seampact-deploy/1", container="platform", data_dir=str(data), latest_backup=str(backup),
                pending=dict(old_id=None, candidate_id="candidate"))
            info = dict(Id="candidate", Name="/platform", State=dict(Status="created", Running=False, Pid=0,
                StartedAt="0001-01-01T00:00:00Z", Error="setgroups: invalid argument"),
                Mounts=[dict(Destination="/data", Type="bind", RW=True, Source=str(data))])
            self.assertEqual(validate(record, info, root), backup)
            for changed in (dict(Status="running", Running=True), dict(StartedAt="2026-01-01T00:00:00Z")):
                other = copy.deepcopy(info)
                other["State"].update(changed)
                with self.assertRaises(SystemExit):
                    validate(record, other, root)
            other = copy.deepcopy(record)
            other["pending"]["old_id"] = "old-platform"
            with self.assertRaises(SystemExit):
                validate(other, info, root)
            other = copy.deepcopy(info)
            other["Id"] = "different-container"
            with self.assertRaises(SystemExit):
                validate(record, other, root)
            (backup / "data/platform.sqlite3").unlink()
            with self.assertRaises(SystemExit):
                validate(record, info, root)


if __name__ == "__main__":
    unittest.main()
