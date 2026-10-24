"""Platform Git clones are removed with their source; user directories are not."""

import stat
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from seampact.dependencies import Dependencies
from seampact.intake import RepositoryIntake, managed_checkout, remove_checkout
from seampact.resources import ResourceRegistry
from seampact.service import Platform
from seampact.store import Store


class SourceCleanupChecks(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.home = Path(self.directory.name)
        self.platform = Platform.__new__(Platform)
        self.platform.home = self.home
        self.platform.intake = RepositoryIntake(self.home / "sources")
        self.platform.store = Store(self.home / "platform.sqlite3")
        self.platform.source_lock = threading.RLock()
        self.platform.training = SimpleNamespace(lock=threading.RLock())
        self.platform.analysis = SimpleNamespace(cancel=lambda identity: None)
        self.platform.catalog = SimpleNamespace(source_running=lambda identity: False, detach=lambda identity: 0)
        self.platform.deployments = SimpleNamespace(lock=threading.RLock(), mark_source_detached=lambda identity: None,
            _guard=lambda *args: None, _instances=lambda identity: [], _held=lambda item: False)
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(self.platform.store.close)

    def clone(self, name, filename="README.md"):
        path = self.platform.intake.home / "repositories" / name
        path.mkdir(parents=True)
        (path / filename).write_text("checkout", encoding="utf-8")
        return path

    def source(self, identity, root, **extra):
        record = dict(id=identity, root=str(root), address="https://example.test/" + identity + ".git",
                      status="CONNECTED", managed_clones=[str(root.resolve())])
        record.update(extra)
        self.platform.store.save("harness", identity, record)
        return record

    def test_managed_checkout_rejects_user_and_parent_paths(self):
        clone = self.clone("abc")
        user = self.home / "mine"
        user.mkdir()
        base = self.platform.intake.home / "repositories"
        self.assertEqual(managed_checkout(self.platform.intake.home, str(clone)), clone.resolve())
        self.assertIsNone(managed_checkout(self.platform.intake.home, str(user)))
        self.assertIsNone(managed_checkout(self.platform.intake.home, str(base)))
        self.assertIsNone(managed_checkout(self.platform.intake.home, str(clone / "README.md")))
        self.assertIsNone(managed_checkout(self.platform.intake.home, str(self.home)))

    def test_delete_removes_platform_clone_and_keeps_user_directory(self):
        clone = self.clone("remote")
        nested = clone / ".git" / "objects"
        nested.mkdir(parents=True)
        locked = nested / "pack"
        locked.write_text("readonly", encoding="utf-8")
        locked.chmod(stat.S_IREAD)
        user = self.home / "mine"
        user.mkdir()
        (user / "keep.txt").write_text("keep", encoding="utf-8")
        self.source("remote", clone)
        self.source("local", user, address=str(user), managed_clones=[str(user)])
        removed = self.platform.delete_harness("remote")
        self.assertEqual(removed["source_files"], "REMOVED")
        self.assertFalse(clone.exists())
        self.assertIsNone(self.platform.store.get("harness", "remote"))
        kept = self.platform.delete_harness("local")
        self.assertEqual(kept["source_files"], "RETAINED")
        self.assertEqual(kept["removed_clones"], [])
        self.assertTrue((user / "keep.txt").exists())

    def test_delete_removes_weight_inside_checkout_and_keeps_external_weight(self):
        clone = self.clone("weighted")
        inside = clone / "artifacts" / "gate"
        inside.mkdir(parents=True)
        (inside / "gate_model.json").write_text("{}", encoding="utf-8")
        external = self.home / "models" / "gate"
        external.mkdir(parents=True)
        (external / "gate_model.json").write_text("{}", encoding="utf-8")
        self.platform.store.save("weight_resource", "inside", dict(id="inside", name="same-gate", path=str(inside)))
        self.platform.store.save("weight_resource", "outside", dict(id="outside", name="same-gate", path=str(external)))
        self.platform.store.save("dependency_binding", "weighted:gate", dict(
            dependency_id="weighted:gate", resource_id="inside", resource=dict(id="inside", path=str(inside))))
        self.platform.store.save("dependency_binding", "other:gate", dict(
            dependency_id="other:gate", resource_id="outside", resource=dict(id="outside", path=str(external))))
        self.source("weighted", clone)
        result = self.platform.delete_harness("weighted")
        self.assertEqual(result["source_files"], "REMOVED")
        self.assertEqual(result["removed_weights"][0]["id"], "inside")
        self.assertFalse(clone.exists())
        self.assertIsNone(self.platform.store.get("weight_resource", "inside"))
        self.assertIsNone(self.platform.store.get("dependency_binding", "weighted:gate"))
        self.assertEqual(self.platform.store.get("weight_resource", "outside")["path"], str(external))
        self.assertTrue((external / "gate_model.json").exists())
        self.assertIsNotNone(self.platform.store.get("dependency_binding", "other:gate"))

    def test_weight_inside_platform_checkout_cannot_be_registered_or_selected(self):
        clone = self.clone("repo")
        inside = clone / "artifacts"
        inside.mkdir()
        registry = ResourceRegistry(self.home, self.platform.store, SimpleNamespace(private_directory=None))
        with self.assertRaises(ValueError):
            registry.save_weight(dict(name="gate", kind="directory", path=str(inside)))
        external = self.home / "models" / "gate"
        external.mkdir(parents=True)
        saved = registry.save_weight(dict(name="gate", kind="directory", path=str(external)))
        self.assertEqual(Path(saved["path"]), external.resolve())
        self.platform.store.save("weight_resource", "inside", dict(id="inside", path=str(inside), name="old-gate"))
        self.platform.store.save("resource_dependency", "source:gate", dict(
            id="source:gate", source_id="source", active=True, configurable=True, kind="weight",
            selection_allowed=True, definition_digest="digest", origin_id="gate"))
        dependencies = Dependencies(self.platform.store, SimpleNamespace(), registry)
        with self.assertRaises(ValueError):
            dependencies.select(dict(dependency_id="source:gate", harness_id="source", resource_id="inside"))

    def test_delete_removes_update_checkout_recorded_on_the_source(self):
        current = self.clone("current")
        previous = self.clone("previous")
        self.source("source", current, managed_clones=[str(current.resolve()), str(previous.resolve())])
        self.platform.store.save("analysis_revision", "draft", dict(id="draft", harness_id="source",
            proposed_source=dict(root=str(previous))))
        result = self.platform.delete_harness("source")
        self.assertEqual(result["source_files"], "REMOVED")
        self.assertFalse(current.exists())
        self.assertFalse(previous.exists())

    def test_active_call_blocks_deletion_and_leaves_the_checkout(self):
        clone = self.clone("busy")
        self.source("busy", clone)
        self.platform.catalog.source_running = lambda identity: True
        with self.assertRaises(ValueError):
            self.platform.delete_harness("busy")
        self.assertTrue((clone / "README.md").exists())
        self.assertIsNotNone(self.platform.store.get("harness", "busy"))

    def test_failed_clone_does_not_leave_a_directory(self):
        intake = self.platform.intake

        def fail(arguments, required=True):
            if arguments and arguments[0] == "clone":
                destination = Path(arguments[-1])
                destination.mkdir(parents=True)
                (destination / "leftover.txt").write_text("partial", encoding="utf-8")
                raise ValueError("Git repository operation failed")
            raise AssertionError(arguments)

        intake._git = fail
        with self.assertRaises(ValueError):
            intake.prepare("https://example.test/demo.git")
        self.assertEqual(list((intake.home / "repositories").glob("*")), [])

    def test_unsaved_connect_removes_only_the_platform_checkout(self):
        clone = self.clone("unsaved")
        user = self.home / "mine"
        user.mkdir()
        (user / "keep.txt").write_text("keep", encoding="utf-8")
        self.platform._discard_unsaved_checkout(dict(id="unsaved", root=str(clone)))
        self.platform._discard_unsaved_checkout(dict(id="local", root=str(user)))
        self.assertFalse(clone.exists())
        self.assertTrue((user / "keep.txt").exists())

    def test_remove_checkout_leaves_a_missing_directory_alone(self):
        missing = self.platform.intake.home / "repositories" / "missing"
        self.assertFalse(remove_checkout(missing))


if __name__ == "__main__":
    unittest.main()
