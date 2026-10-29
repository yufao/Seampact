"""Executable source identities stay scoped without weakening worker checks."""

import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from seampact import target_runtime
from seampact.deployment import Deployments, worker_spec
from seampact.worker import Worker


class BuildIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "seampact"
        shutil.copytree(Path(target_runtime.__file__).parent, self.root,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "ui"))
        self.scope = patch.object(target_runtime, "__file__", str(self.root / "target_runtime.py"))
        self.scope.start()
        self.addCleanup(self.scope.stop)
        self.environment = dict(python=".".join(map(str, sys.version_info[:2])), base_image="python:3.13-slim")

    def identities(self):
        return (target_runtime.core_identity("worker"), target_runtime.checker_identity(self.environment))

    def change(self, name, suffix="\n# identity test change\n"):
        path = self.root / name
        path.write_bytes(path.read_bytes() + suffix.encode())

    def test_unrelated_server_training_scene_and_ui_changes_do_not_invalidate(self):
        before = self.identities()
        for name in ("server.py", "service.py", "training.py", "learners.py", "scenarios.py"):
            self.change(name)
        (self.root / "ui").mkdir()
        (self.root / "ui" / "index.html").write_text("changed UI", encoding="utf-8")
        (self.root / "unrelated.py").write_text("this need not even parse", encoding="utf-8")
        self.assertEqual(self.identities(), before)

    def test_literal_package_version_does_not_invalidate(self):
        before = self.identities()
        path = self.root / "__init__.py"
        path.write_text('"""SeamPact Harness intake and two-layer scheduling prototype."""\n'
                        '__version__ = "999.0.0"\n', encoding="utf-8")
        self.assertEqual(self.identities(), before)

    def test_initializer_behavior_and_consumed_version_are_hashed(self):
        before = self.identities()
        self.change("__init__.py", "\nINITIALIZED = True\n")
        self.assertNotEqual(self.identities(), before)
        self.change("__init__.py", '\n__version__ = "999.0.0"\n')
        with_behavior = self.identities()
        self.change("__init__.py", '\n__version__ = "999.0.1"\n')
        self.assertNotEqual(self.identities(), with_behavior)
        (self.root / "__init__.py").write_text('__version__ = "1"\n', encoding="utf-8")
        self.change("worker.py", "\nfrom . import __version__\n")
        consumed = self.identities()
        (self.root / "__init__.py").write_text('__version__ = "2"\n', encoding="utf-8")
        self.assertNotEqual(self.identities(), consumed)

    def test_entrypoint_and_shared_checker_execution_changes_invalidate(self):
        for name in ("worker.py", "target_runtime.py", "runtime.py", "units.py", "verification.py",
                     "inspection.py", "lowering.py", "deployment.py", "dependencies.py", "lifecycle.py"):
            with self.subTest(module=name):
                before = self.identities()
                self.change(name)
                after = self.identities()
                self.assertNotEqual(after[0], before[0])
                self.assertNotEqual(after[1], before[1])

    def test_nested_import_dependencies_are_included_without_importing_source(self):
        self.change("worker.py", "\ndef unused_branch():\n    from .extra_check import check\n")
        (self.root / "extra_check.py").write_text(
            "from .verification import check_candidate\nraise AssertionError('never import to hash')\n",
            encoding="utf-8")
        before = self.identities()
        self.assertIn("extra_check.py", before[0]["files"])
        self.change("extra_check.py")
        self.assertNotEqual(self.identities(), before)

    def test_other_dynamic_imports_in_native_loader_module_are_rejected(self):
        self.identities()
        self.change("units.py", "\ndef unrelated_loader(name):\n    return importlib.import_module(name)\n")
        with self.assertRaisesRegex(ValueError, "dynamic platform import"):
            self.identities()

    def test_initializer_and_subpackage_imports_follow_python_package_precedence(self):
        package = self.root / "shared_check"
        package.mkdir()
        (package / "__init__.py").write_text("from . import leaf\n", encoding="utf-8")
        (package / "leaf.py").write_text("from ..verification import check_candidate\n", encoding="utf-8")
        (self.root / "shared_check.py").write_text("unused competing module\n", encoding="utf-8")
        self.change("__init__.py", "\nfrom . import shared_check\n")
        before = self.identities()
        self.assertIn("shared_check/__init__.py", before[0]["files"])
        self.assertIn("shared_check/leaf.py", before[0]["files"])
        self.assertNotIn("shared_check.py", before[0]["files"])
        self.change("shared_check/leaf.py")
        self.assertNotEqual(self.identities(), before)

    def test_content_cache_does_not_hide_same_size_and_mtime_edits(self):
        self.change("verification.py", "\n# content-A\n")
        before = self.identities()
        path = self.root / "verification.py"
        stat = path.stat()
        path.write_bytes(path.read_bytes().replace(b"# content-A", b"# content-B"))
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        self.assertEqual(path.stat().st_size, stat.st_size)
        self.assertNotEqual(self.identities(), before)

    def test_image_bundle_contains_exactly_the_checked_closure(self):
        implementation = target_runtime.core_identity("worker")
        destination = Path(self.temporary.name) / "bundle" / "seampact"
        target_runtime.copy_core(destination, implementation)
        copied = {path.relative_to(destination).as_posix() for path in destination.rglob("*") if path.is_file()}
        self.assertEqual(copied, set(implementation["files"]))
        self.assertNotIn("training.py", copied)
        self.assertNotIn("__main__.py", copied)
        with patch.object(target_runtime, "__file__", str(destination / "target_runtime.py")):
            self.assertEqual(target_runtime.core_identity("worker"), implementation)

    def test_missing_or_unknown_dynamic_platform_dependencies_fail_closed(self):
        self.change("worker.py", "\nfrom .missing_check import check\n")
        with self.assertRaisesRegex(ValueError, "no source file"):
            self.identities()
        (self.root / "missing_check.py").write_text("def check(): pass\n", encoding="utf-8")
        self.change("worker.py", "\nimport importlib\ndef dynamic(name): return importlib.import_module(name)\n")
        with self.assertRaisesRegex(ValueError, "explicit source dependency"):
            self.identities()

    def test_worker_artifact_and_host_continue_to_check_complete_identity(self):
        group = dict(entries=["entry"], contract=dict(digest="contract"), installation_id="installation")
        binding = SimpleNamespace(candidate=dict(entries=[dict(id="entry")]),
                                  inspector=SimpleNamespace(request=dict(runtime_environment=self.environment)))
        spec = worker_spec(binding, group, dict(analysis_id="analysis", installation_id="installation"))
        deployments = object.__new__(Deployments)
        self.assertIsNone(deployments._artifact_gap(group, spec))
        self.change("training.py")
        self.assertIsNone(deployments._artifact_gap(group, spec))
        self.change("verification.py")
        self.assertIsNotNone(deployments._artifact_gap(group, spec))
        spec_path = Path(self.temporary.name) / "worker.json"
        spec_path.write_text(json.dumps(spec), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "平台核对代码与冻结工件不一致"):
            Worker(spec_path, "http://unused.invalid")
        legacy = copy.deepcopy(spec)
        legacy["platform_core"] = {"verification.py": "old blanket identity"}
        self.assertIsNotNone(deployments._artifact_gap(group, legacy))

    def test_checker_still_freezes_interpreter_and_base_image(self):
        before = target_runtime.checker_identity(self.environment)
        other_python = "3.12" if self.environment["python"] != "3.12" else "3.11"
        for field, value in (("python", other_python), ("base_image", "python:3.13")):
            changed = dict(self.environment, **{field: value})
            self.assertNotEqual(target_runtime.checker_identity(changed), before)


class WorkerEntrypointTests(unittest.TestCase):
    def test_standalone_worker_loads_only_checked_platform_modules(self):
        root = Path(__file__).resolve().parents[1]
        program = (
            "import runpy,sys; sys.argv=['seampact.worker','--help']; "
            "\ntry: runpy.run_module('seampact.worker',run_name='__main__')"
            "\nexcept SystemExit as e: assert e.code == 0"
            "\nfrom seampact.target_runtime import core_identity"
            "\nchecked=core_identity('worker')['files']"
            "\nloaded={name.split('.',1)[1]+'.py' for name in sys.modules if name.startswith('seampact.')}"
            "\nassert loaded <= set(checked), loaded-set(checked)"
            "\nassert not {'seampact.service','seampact.server','seampact.training'} & set(sys.modules)"
        )
        env = dict(os.environ, PYTHONPATH=str(root / "src"))
        result = subprocess.run([sys.executable, "-c", program], cwd=root, env=env, capture_output=True,
                                text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
