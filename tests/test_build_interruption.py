"""A restarted coordinator must not strand or certify old image work."""
import copy
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from seampact.hosting.deployment import Deployments
from seampact.store import Store


class BuildInterruptionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.store = Store(self.home / "state.sqlite3")
        self.addCleanup(self.store.db.close)
        self.platform = SimpleNamespace(home=self.home, store=self.store, catalog=SimpleNamespace(),
                                        source_lock=threading.RLock())
        self.group = "group"

    def coordinator(self):
        with patch("seampact.hosting.deployment.threading.Thread"):
            return Deployments(self.platform)

    def test_old_running_attempt_is_unknown_and_old_operation_is_not_replayed(self):
        attempt = dict(status="RUNNING", started_at="old", platform_session="previous")
        artifact = dict(id=self.group, image="worker:old", installation_id="original",
                        build=attempt, publish=dict(status="NOT_RUN"))
        original_result = copy.deepcopy(artifact)
        operation = dict(kind="build:" + self.group, at="old", result=original_result)
        self.store.save("deployment", self.group, artifact)
        self.store.save("runtime_operation", "old-operation", operation)
        controller = self.coordinator()
        current = self.store.get("deployment", self.group)
        self.assertEqual(current["build"]["status"], "INTERRUPTED")
        self.assertEqual(current["build"]["execution_outcome"], "UNKNOWN")
        self.assertEqual(current["interrupted_operations"][0]["previous"], attempt)
        self.assertEqual(self.store.get("runtime_operation", "old-operation"), operation)
        with patch.object(controller, "_guard"), patch.object(controller, "_instances", return_value=[]):
            controller._artifact_guard(self.group)
        with patch.object(controller, "_artifact_guard") as guard:
            result = controller.build(self.group, dict(operation_id="old-operation"))
        guard.assert_not_called()
        self.assertTrue(result["repeated_operation"])
        self.assertEqual(result["build"], attempt)
        self.assertNotEqual(current["build"]["status"], "BUILT")

    def test_terminal_build_and_pending_publish_are_distinguished(self):
        built = dict(status="BUILT", image_id="sha256:" + "a" * 64, finished_at="done")
        terminal = dict(id="terminal", build=built, publish=dict(status="FAILED", reason="original"))
        pending = dict(id="pending", build=built, publish=dict(status="RUNNING", started_at="old"))
        self.store.save("deployment", "terminal", terminal)
        self.store.save("deployment", "pending", pending)
        self.coordinator()
        self.assertEqual(self.store.get("deployment", "terminal"), terminal)
        current = self.store.get("deployment", "pending")
        self.assertEqual(current["build"], built)
        self.assertEqual(current["publish"]["status"], "INTERRUPTED")
        self.assertEqual(current["publish"]["execution_outcome"], "UNKNOWN")
        self.coordinator()
        self.assertEqual(self.store.get("deployment", "pending"), current)

    def test_new_build_requires_a_new_operation_and_its_own_verified_result(self):
        self.store.save("deployment", self.group,
                        dict(id=self.group, build=dict(status="RUNNING"), publish=dict(status="NOT_RUN")))
        controller = self.coordinator()
        targets = []

        def deferred_thread(*, target, **unused):
            targets.append(target)
            return SimpleNamespace(start=lambda: None)

        current_artifact = lambda unused: self.store.get("deployment", self.group)
        with patch.object(controller, "_artifact", side_effect=current_artifact), \
                patch("seampact.hosting.deployment.threading.Thread", side_effect=deferred_thread):
            started = controller._background(self.group, "build",
                lambda artifact: dict(status="BUILT", image_id="verified-new-image"))
            self.assertEqual(started["build"]["status"], "RUNNING")
            self.assertEqual(started["build"]["platform_session"], controller.session)
            with self.assertRaisesRegex(ValueError, "仍在进行"):
                controller._background(self.group, "build", lambda artifact: self.fail("duplicate work"))
        self.assertEqual(len(targets), 1)
        targets[0]()
        current = self.store.get("deployment", self.group)
        self.assertEqual(current["build"]["status"], "BUILT")
        self.assertEqual(current["build"]["image_id"], "verified-new-image")
        self.assertEqual(len(current["interrupted_operations"]), 1)


if __name__ == "__main__":
    unittest.main()
