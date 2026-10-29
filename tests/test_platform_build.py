"""Exercise platform dependency reuse and lock restoration without Docker or pip."""

import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy"))
import build_platform as builder
import install_environment as installer

BASE = "sha256:" + "a" * 64
OTHER_BASE = "sha256:" + "b" * 64
LOCK = "pip==24.0\nsample==1.0\nsetuptools==75.0\nwheel==0.44\n"


class FakeDocker:
    def __init__(self):
        self.base = dict(Id=BASE, RepoDigests=["python@sha256:" + "d" * 64])
        self.images = {}
        self.observed = {}
        self.commands = []
        self.environment_builds = []

    def resolve_base(self, reference):
        return dict(self.base)

    def inspect(self, reference):
        return self.images.get(reference)

    def run(self, args, *, check=True, visible=False, data=None):
        self.commands.append(list(args))
        if args[0] == "tag":
            self.images[args[2]] = self.base if args[1] == self.base["Id"] else self.images[args[1]]
            return SimpleNamespace(returncode=0, stdout="")
        if args[:2] == ["buildx", "build"]:
            arguments = dict(args[i + 1].split("=", 1) for i, item in enumerate(args) if item == "--build-arg")
            for name in ("BASE_IMAGE", "DEPENDENCY_IMAGE"):
                if name in arguments:
                    if arguments[name].startswith("sha256:") or arguments[name] not in self.images:
                        raise AssertionError("BuildKit FROM needs a verified local image reference")
            if "Dockerfile.dependencies" in Path(args[args.index("-f") + 1]).name:
                context = Path(args[-1])
                request = json.loads((context / "request.json").read_text())
                supplied = (context / "input.lock").read_text()
                self.environment_builds.append(dict(request=request, lock=supplied,
                    files={path.name for path in context.iterdir()}))
                arguments = dict(args[i + 1].split("=", 1) for i, item in enumerate(args) if item == "--build-arg")
                lock = supplied or LOCK
                image_id = "sha256:" + builder.digest([arguments["ENVIRONMENT_KEY"], lock])
                image = dict(Id=image_id, Config=dict(Labels={
                    "io.seampact.platform-environment": arguments["ENVIRONMENT_KEY"],
                    "io.seampact.environment.base": arguments["RESOLVED_BASE"]}))
                self.images[args[args.index("-t") + 1]] = self.images[image_id] = image
                self.observed[image_id] = dict(lock=lock, installed=dict(python="3.11", torch=None, cuda=None))
            return SimpleNamespace(returncode=0, stdout="")
        if args[0] == "run":
            self_test = {"--network": "none", "--entrypoint": "python"}
            for option, value in self_test.items():
                if args[args.index(option) + 1] != value:
                    raise AssertionError("Environment inspection must be isolated")
            image_id = args[args.index("--entrypoint") + 2]
            return SimpleNamespace(returncode=0, stdout=json.dumps(self.observed[image_id]))
        raise AssertionError("Unexpected Docker command: " + repr(args))


class PlatformBuildTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.code = self.root / "code"
        (self.code / "deploy").mkdir(parents=True)
        (self.code / "src").mkdir()
        (self.code / "src/tool.py").write_text("VALUE = 1\n")
        for name in ("Dockerfile.dependencies", "install_environment.py", "Dockerfile"):
            shutil.copyfile(ROOT / "deploy" / name, self.code / "deploy" / name)
        self.project(version="1.0")
        self.state = self.root / "private-state"

    def project(self, version="1.0", dependency="sample>=1"):
        (self.code / "pyproject.toml").write_text(
            '[project]\nname="platform"\nversion="' + version + '"\ndependencies=["' + dependency + '"]\n'
            '[project.optional-dependencies]\nrl=["torch>=2.2"]\n'
            '[build-system]\nrequires=["setuptools>=68"]\nbuild-backend="setuptools.build_meta"\n')

    def prepare(self, docker, **options):
        return builder.prepare(self.code, self.state, docker, **options)

    def test_source_and_release_only_change_reuse_installed_id_without_environment_build(self):
        docker = FakeDocker()
        first = self.prepare(docker)
        (self.code / "src/tool.py").write_text("VALUE = 2\n")
        self.project(version="1.1")
        second = self.prepare(docker)
        self.assertEqual(second["status"], "REUSED")
        self.assertEqual(first["key"], second["key"])
        self.assertEqual(first["image_id"], second["image_id"])
        self.assertEqual(second["version"], "1.1")
        self.assertEqual(len(docker.environment_builds), 1)
        self.assertEqual(docker.environment_builds[0]["files"],
            {"request.json", "input.lock", "Dockerfile.dependencies", "install_environment.py"})
        self.assertTrue(all(args[args.index("--network") + 1] == "none" for args in docker.commands if args[0] == "run"))

    def test_missing_environment_rebuild_uses_saved_exact_lock(self):
        docker = FakeDocker()
        first = self.prepare(docker)
        docker.images.clear()
        rebuilt = self.prepare(docker)
        self.assertEqual(rebuilt["status"], "BUILT")
        self.assertEqual(docker.environment_builds[-1]["lock"], first["lock"])
        self.assertEqual(rebuilt["lock_sha256"], first["lock_sha256"])

    def test_actual_base_requirement_and_channels_change_environment_key(self):
        docker = FakeDocker()
        first = self.prepare(docker)
        docker.base["Id"] = OTHER_BASE
        rebased = self.prepare(docker)
        self.assertNotEqual(first["key"], rebased["key"])
        self.project(dependency="sample>=2")
        changed = self.prepare(docker)
        self.assertNotEqual(changed["key"], rebased["key"])
        channel = self.prepare(docker, pip_index_url="https://packages.example/simple")
        self.assertNotEqual(channel["key"], changed["key"])
        rl = self.prepare(docker, extras="rl", torch_version="2.5.1+cpu", torch_index_url="https://wheels.example/cpu")
        self.assertEqual(rl["request"]["torch_requirement"], "torch==2.5.1+cpu")
        self.assertNotEqual(rl["key"], channel["key"])

    def test_existing_mutable_tag_cannot_override_saved_image_id(self):
        docker = FakeDocker()
        first = self.prepare(docker)
        docker.images[first["image"]] = dict(Id="sha256:" + "e" * 64, Config=dict(Labels={}))
        reused = self.prepare(docker)
        self.assertEqual(reused["status"], "REUSED")
        self.assertEqual(reused["image_id"], first["image_id"])
        self.assertEqual(len(docker.environment_builds), 1)

    def test_mismatched_tag_without_saved_record_is_rebuilt(self):
        docker = FakeDocker()
        first = self.prepare(docker)
        other_state = self.root / "new-private-state"
        docker.images[first["image"]] = dict(Id="sha256:" + "e" * 64, Config=dict(Labels={}))
        rebuilt = builder.prepare(self.code, other_state, docker)
        self.assertEqual(rebuilt["status"], "BUILT")
        self.assertEqual(len(docker.environment_builds), 2)

    def test_changed_observed_lock_is_rejected_before_source_build(self):
        docker = FakeDocker()
        first = self.prepare(docker)
        docker.observed[first["image_id"]]["lock"] += "unexpected==1\n"
        with self.assertRaisesRegex(ValueError, "saved dependency lock"):
            self.prepare(docker)

    def test_platform_final_build_uses_exact_environment_id(self):
        docker = FakeDocker()
        environment = self.prepare(docker)
        builder.build_platform(docker, self.code, environment, "platform:test", revision="release")
        command = docker.commands[-1]
        arguments = dict(command[i + 1].split("=", 1) for i, item in enumerate(command) if item == "--build-arg")
        self.assertEqual(docker.images[arguments["DEPENDENCY_IMAGE"]]["Id"], environment["image_id"])
        self.assertEqual(arguments["SEAMPACT_VERSION"], "1.0")
        dockerfile = (self.code / "deploy/Dockerfile").read_text()
        self.assertIn("--network=none", dockerfile)
        self.assertIn("--no-build-isolation", dockerfile)
        self.assertIn("--no-deps", dockerfile)

    def test_refresh_skips_previous_lock_and_forces_resolution(self):
        docker = FakeDocker()
        first = self.prepare(docker)
        refreshed = self.prepare(docker, refresh=True)
        self.assertEqual(refreshed["status"], "BUILT")
        self.assertEqual(docker.environment_builds[-1]["lock"], "")
        self.assertTrue(any("RESOLVE_NONCE=" in item for args in docker.commands for item in args))
        self.assertEqual(first["key"], refreshed["key"])

    def test_compose_file_uses_a_verified_buildkit_reference(self):
        docker = FakeDocker()
        output = self.root / "compose.env"
        with patch.object(builder, "Docker", return_value=docker):
            builder.main(["--code-dir", str(self.code), "--state-dir", str(self.state),
                          "--compose-env", str(output), "--revision", "example"])
        values = dict(line.split("=", 1) for line in output.read_text().splitlines())
        reference = values["SEAMPACT_DEPENDENCY_IMAGE"]
        self.assertFalse(reference.startswith("sha256:"))
        self.assertIn(docker.images[reference]["Id"], docker.observed)
        self.assertEqual(values["SEAMPACT_VERSION"], "1.0")
        self.assertEqual(values["SEAMPACT_REVISION"], "example")


class PlatformLockTests(unittest.TestCase):
    def test_explicit_torch_channel_ignores_inherited_package_sources(self):
        with patch.dict(os.environ, PIP_EXTRA_INDEX_URL="https://unwanted.example/simple", PIP_FIND_LINKS="/wheels"), \
                patch.object(installer.subprocess, "run") as execute:
            installer.pip("download", "--index-url", "https://wheels.example/cpu", "torch==2.5.1+cpu", channel_only=True)
        env = execute.call_args.kwargs["env"]
        self.assertNotIn("PIP_EXTRA_INDEX_URL", env)
        self.assertNotIn("PIP_FIND_LINKS", env)
        self.assertEqual(env["PIP_CONFIG_FILE"], os.devnull)

    def test_lock_rejects_aliases_for_same_distribution(self):
        for text in ("foo.bar==1\nfoo-bar==2\n", "foo__bar==1\nfoo-bar==2\n"):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, "duplicate"):
                installer.locked_lines(text)

    def test_locked_torch_uses_its_channel_without_exposing_other_packages_to_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            request = builder.dependency_request(
                dict(project={"optional-dependencies": {"rl": ["torch>=2"]}},
                     **{"build-system": {"build-backend": "setuptools.build_meta", "requires": ["setuptools>=68"]}}),
                extras="rl", torch_index_url="https://wheels.example/cpu", pip_index_url="https://packages.example/simple")
            lock = "pip==24.0\nsetuptools==75.0\ntorch==2.5.1+cpu\nwheel==0.44\n"
            (root / "request.json").write_text(json.dumps(request))
            (root / "input.lock").write_text(lock)
            commands = []

            def fake_pip(*args, channel_only=False):
                commands.append(args)
                if args[0] == "download":
                    self.assertTrue(channel_only)
                    (Path(args[args.index("--dest") + 1]) / "torch-2.5.1+cpu.whl").touch()

            distributions = [SimpleNamespace(metadata={"Name": row.split("==")[0]}, version=row.split("==")[1])
                             for row in lock.splitlines()]
            paths = SimpleNamespace(Path=lambda value: root if value == "/opt/seampact-environment" else Path(value))
            torch = SimpleNamespace(__version__="2.5.1+cpu", version=SimpleNamespace(cuda=None))
            with patch.object(installer, "pathlib", paths), patch.object(installer, "pip", side_effect=fake_pip), \
                    patch.object(importlib.metadata, "distributions", return_value=distributions), \
                    patch.object(importlib.metadata, "version", side_effect=dict(row.split("==") for row in lock.splitlines()).__getitem__), \
                    patch.dict(sys.modules, torch=torch):
                installer.main()
            downloads = [args for args in commands if args[0] == "download"]
            self.assertEqual(len(downloads), 1)
            self.assertEqual(downloads[0][downloads[0].index("--index-url") + 1], "https://wheels.example/cpu")
            self.assertIn("torch==2.5.1+cpu", downloads[0])
            for args in commands:
                if args[0] == "install":
                    self.assertNotIn("https://wheels.example/cpu", args)
                    self.assertIn("--no-deps", args)


if __name__ == "__main__":
    unittest.main()
