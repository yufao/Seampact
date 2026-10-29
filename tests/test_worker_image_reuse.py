"""Verify generated installation order and Docker identity decisions without downloads."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from seampact import backends
from seampact.image_build import BASE_LABEL, RECIPE_LABEL, dependency_image_reference, worker_dockerfile
from seampact.inspection import digest


BASE = "sha256:" + "a" * 64
OTHER_BASE = "sha256:" + "b" * 64
WORKER = "sha256:" + "c" * 64


class FakeDocker:
    def __init__(self, base=BASE):
        self.images = {"python:3.11-slim": dict(Id=base, Config=dict(Labels={}))} if base else {}
        self.commands = []
        self.dependency_builds = 0
        self.invalid_built_labels = False

    def run(self, argv, timeout, stdin=None):
        self.commands.append(list(argv))
        status, output = 0, ""
        if argv[:3] == ["docker", "image", "inspect"]:
            if argv[-1] in self.images:
                output = json.dumps(self.images[argv[-1]])
            else:
                status, output = 1, "No such image"
        elif argv[:2] == ["docker", "pull"]:
            self.images[argv[-1]] = dict(Id=BASE, Config=dict(Labels={}))
        elif argv[:2] == ["docker", "tag"]:
            self.images[argv[3]] = next(info for info in list(self.images.values()) if info["Id"] == argv[2])
        elif argv[:4] == ["docker", "buildx", "build", "--load"]:
            tag = argv[argv.index("-t") + 1]
            arguments = dict(argv[i + 1].split("=", 1) for i, item in enumerate(argv) if item == "--build-arg")
            for name in ("BASE_IMAGE", "DEPENDENCY_IMAGE"):
                if name in arguments:
                    if arguments[name].startswith("sha256:") or arguments[name] not in self.images:
                        raise AssertionError("BuildKit FROM needs a verified local image reference")
            if "--target" in argv:
                self.dependency_builds += 1
                labels = {RECIPE_LABEL: arguments["SEAMPACT_ENVIRONMENT_RECIPE"],
                          BASE_LABEL: arguments["SEAMPACT_ENVIRONMENT_BASE"]}
                if self.invalid_built_labels:
                    labels[BASE_LABEL] = OTHER_BASE
                identity = "sha256:" + digest(json.dumps(arguments, sort_keys=True).encode())
                self.images[tag] = dict(Id=identity, Config=dict(Labels=labels))
            else:
                self.images[tag] = dict(Id=WORKER, Config=dict(Labels={}))
        else:
            raise AssertionError("Unexpected Docker command: " + repr(argv))
        return dict(argv=list(argv), returncode=status, stdout=output, stderr="", output=output, duration_ms=1)


class WorkerImageReuseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.sequence = 0
        self.config = dict(build_timeout_s=30, operation_timeout_s=5)

    def export(self, files=None, **changes):
        self.sequence += 1
        folder = self.root / str(self.sequence)
        folder.mkdir()
        files = files if files is not None else {"requirements.txt": b"sample==1\n", "tool.py": b"VALUE=1\n"}
        environment = dict(python="3.11", base_image="python:3.11-slim", requirements=["requirements.txt"],
                           project="", packages=[], pip_extra_index_url="")
        environment.update(changes)
        inspector = SimpleNamespace(files=files, data=files.__getitem__)
        plan = worker_dockerfile(folder, inspector, environment, "assert True")
        return folder, environment, plan

    def build(self, docker, artifact):
        folder, environment, plan = artifact
        with patch.object(backends, "docker_available", return_value=None), patch.object(backends, "run", side_effect=docker.run):
            return backends.build(folder, "worker:example", environment, self.config,
                                  dependency_image=plan["dependency_image"])

    def final_arguments(self, docker):
        command = next(argv for argv in reversed(docker.commands) if argv[:3] == ["docker", "buildx", "build"])
        return dict(command[i + 1].split("=", 1) for i, item in enumerate(command) if item == "--build-arg")

    def test_source_only_change_skips_environment_build_and_uses_verified_id(self):
        first = self.export(project=".", packages=["extra==2"])
        second = self.export(files={"requirements.txt": b"sample==1\n", "tool.py": b"VALUE=2\n"},
                             project=".", packages=["extra==2"])
        self.assertEqual(first[2]["recipe_sha256"], second[2]["recipe_sha256"])
        docker = FakeDocker()
        built = self.build(docker, first)
        self.assertEqual(built["dependency_reuse"]["status"], "BUILT")
        reused = self.build(docker, second)
        self.assertEqual(reused["status"], "BUILT")
        self.assertEqual(reused["dependency_reuse"]["status"], "REUSED")
        self.assertEqual(docker.dependency_builds, 1)
        self.assertEqual(docker.images[self.final_arguments(docker)["DEPENDENCY_IMAGE"]]["Id"], built["dependency_reuse"]["dependency_image_id"])
        self.assertEqual(docker.images[self.final_arguments(docker)["BASE_IMAGE"]]["Id"], BASE)
        self.assertTrue(all(argv[argv.index("--builder") + 1] == "default" for argv in docker.commands
                            if argv[:3] == ["docker", "buildx", "build"]))
        self.assertEqual(reused["image_id"], WORKER)
        self.assertFalse(any(argv[:2] == ["docker", "pull"] for argv in docker.commands))
        self.assertEqual(reused["dependency_reuse"]["observed_cache_hit"], "NOT_CHECKED")

    def test_changed_requirements_or_actual_base_get_a_new_environment(self):
        first = self.export()
        changed = self.export(files={"requirements.txt": b"sample==2\n"})
        docker = FakeDocker()
        original = self.build(docker, first)
        updated = self.build(docker, changed)
        self.assertNotEqual(original["dependency_image"], updated["dependency_image"])
        docker.images["python:3.11-slim"]["Id"] = OTHER_BASE
        rebased = self.build(docker, changed)
        self.assertNotEqual(updated["dependency_image"], rebased["dependency_image"])
        self.assertEqual(rebased["resolved_base_id"], OTHER_BASE)
        self.assertEqual(docker.dependency_builds, 3)

    def test_a_tag_with_mismatched_labels_is_rebuilt_before_use(self):
        artifact = self.export()
        tag = dependency_image_reference(artifact[2]["recipe_sha256"], BASE)
        docker = FakeDocker()
        docker.images[tag] = dict(Id="sha256:" + "d" * 64,
            Config=dict(Labels={RECIPE_LABEL: artifact[2]["recipe_sha256"], BASE_LABEL: OTHER_BASE}))
        result = self.build(docker, artifact)
        self.assertEqual(result["dependency_reuse"]["status"], "BUILT")
        self.assertIn("不匹配", result["dependency_reuse"]["rejection_reason"])
        self.assertEqual(docker.dependency_builds, 1)
        self.assertNotEqual(self.final_arguments(docker)["DEPENDENCY_IMAGE"], "sha256:" + "d" * 64)

    def test_mismatched_new_environment_is_never_used_for_worker(self):
        docker = FakeDocker()
        docker.invalid_built_labels = True
        result = self.build(docker, self.export())
        self.assertEqual(result["status"], "FAILED")
        self.assertFalse(any("-t" in argv and argv[argv.index("-t") + 1] == "worker:example" for argv in docker.commands))

    def test_empty_requirements_and_independent_extras_have_reusable_environment(self):
        for files, requirements in (({}, []), ({"requirements.txt": b"# empty\n"}, ["requirements.txt"])):
            artifact = self.export(files=files, requirements=requirements, packages=["extra==2"])
            dockerfile = (artifact[0] / "Dockerfile").read_text(encoding="utf-8")
            early, late = dockerfile.split("FROM ${DEPENDENCY_IMAGE} AS worker", 1)
            self.assertIn("extra==2", early)
            self.assertNotIn("extra==2", late)
            self.assertTrue(artifact[2]["dependency_image"])
            self.assertEqual(self.build(FakeDocker(), artifact)["dependency_reuse"]["status"], "BUILT")

    def test_source_context_keeps_all_requirements_project_and_extras_in_order(self):
        artifact = self.export(files={"first.txt": b"sample==1\n", "late.txt": b"-e .\n"},
                               requirements=["first.txt", "late.txt"], project=".", packages=["extra==2"])
        dockerfile = (artifact[0] / "Dockerfile").read_text(encoding="utf-8")
        early, late = dockerfile.split("FROM ${DEPENDENCY_IMAGE} AS worker", 1)
        self.assertNotIn("pip install", early)
        self.assertLess(late.index("COPY source"), late.index("-r first.txt"))
        self.assertLess(late.index("-r first.txt"), late.index("-r late.txt"))
        self.assertLess(late.index("-r late.txt"), late.index("} ."))
        self.assertLess(late.index("} ."), late.index("extra==2"))
        self.assertNotIn("--no-deps", dockerfile)
        self.assertEqual(artifact[2]["requirements"]["status"], "SOURCE_CONTEXT")
        self.assertEqual([item["kind"] for item in artifact[2]["installation"]["late"]], ["requirements", "project", "packages"])

    def test_external_requirement_include_closure_changes_recipe(self):
        files = {"requirements.txt": b"-r sub/requirements.txt\n", "sub/requirements.txt": b"sample==1\n"}
        first = self.export(files=files)
        files = dict(files, **{"sub/requirements.txt": b"sample==2\n"})
        second = self.export(files=files)
        self.assertNotEqual(first[2]["recipe_sha256"], second[2]["recipe_sha256"])
        self.assertEqual((first[0] / "dependency-inputs/sub/requirements.txt").read_bytes(), b"sample==1\n")

    def test_absent_base_is_pulled_then_inspected_once(self):
        docker = FakeDocker(base=None)
        result = self.build(docker, self.export())
        self.assertEqual(result["status"], "BUILT")
        self.assertEqual(result["base_resolution"], "pull-then-inspect")
        self.assertEqual(sum(argv[:2] == ["docker", "pull"] for argv in docker.commands), 1)

    def test_unavailable_backend_reports_full_manual_build_without_reuse_claim(self):
        folder, environment, plan = self.export()
        with patch.object(backends, "docker_available", return_value="unavailable"), patch.object(backends, "run") as execute:
            result = backends.build(folder, "worker:example", environment, self.config, plan["dependency_image"])
        execute.assert_not_called()
        self.assertEqual(result["status"], "BACKEND_UNAVAILABLE")
        self.assertEqual(result["dependency_reuse"]["status"], "NOT_CHECKED")
        self.assertTrue(any("docker buildx build --load" in item for item in result["manual_commands"]))
        self.assertIn("# syntax=docker/dockerfile:1", (folder / "Dockerfile").read_text(encoding="utf-8"))

    def test_manual_entrypoint_uses_the_same_builder_and_saves_actual_receipt(self):
        folder, environment, plan = self.export()
        (folder / "environment.json").write_text(json.dumps(environment), encoding="utf-8")
        docker = FakeDocker()
        with patch.object(backends, "docker_available", return_value=None), patch.object(backends, "run", side_effect=docker.run):
            backends.main([str(folder), "--image", "worker:manual"])
        receipt = json.loads((folder / "build-result.json").read_text(encoding="utf-8"))
        self.assertEqual(receipt["status"], "BUILT")
        self.assertEqual(receipt["image_id"], WORKER)
        self.assertEqual(receipt["dependency_reuse"]["status"], "BUILT")
        self.assertEqual(docker.dependency_builds, 1)


if __name__ == "__main__":
    unittest.main()
