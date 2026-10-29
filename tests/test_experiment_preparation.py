"""Pre-experiment resource payloads and complete platform source snapshots."""

import hashlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from seampact.resources import ResourceRegistry
from seampact.store import Store
from seampact import training


POINTER = b"version https://git-lfs.github.com/spec/v1\noid sha256:" + b"a" * 64 + b"\nsize 1000\n"


class PreparationChecks(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.store = Store(self.root / "state.sqlite3")
        self.addCleanup(self.store.close)
        self.registry = ResourceRegistry(self.root, self.store, None)

    def check(self, kind, path, **extra):
        body = dict(name="test resource", kind=kind, path=str(path), required_files=[])
        body.update(extra)
        record = self.registry.save_weight(body)
        return self.registry.check_weight(record["id"])["check"]

    def test_matching_hash_does_not_turn_lfs_pointer_into_model(self):
        path = self.root / "model.bin"
        path.write_bytes(POINTER)
        check = self.check("file", path, sha256=hashlib.sha256(POINTER).hexdigest())
        self.assertEqual(check["status"], "invalid_artifact")
        self.assertEqual(check["necessary_files"], "unsatisfied")
        self.assertTrue(check["files"][0]["checksum_matches"])
        self.assertEqual(check["invalid_files"][0]["category"], "git_lfs_pointer")

    def test_index_discovered_shard_must_be_materialized(self):
        path = self.root / "model"
        path.mkdir()
        (path / "model.safetensors.index.json").write_text(json.dumps(
            {"weight_map": {"tensor": "part.safetensors"}}), encoding="utf-8")
        (path / "part.safetensors").write_bytes(POINTER)
        self.assertEqual(self.check("directory", path)["status"], "invalid_artifact")
        (path / "part.safetensors").write_bytes(b"binary payload")
        self.assertEqual(self.check("directory", path)["status"], "present")

    def test_declared_size_rejects_partial_download(self):
        path = self.root / "model"
        path.mkdir()
        (path / "weights.bin").write_bytes(b"partial")
        check = self.check("directory", path, required_files=[dict(path="weights.bin", bytes=1000)])
        self.assertEqual(check["status"], "checksum_or_size_mismatch")

    def test_manifest_archives_execution_and_analysis_modules(self):
        adapter = self.root / "scene.py"
        adapter.write_text("# frozen test scene\n", encoding="utf-8")
        scene = dict(observation_schema=["x"], candidate_schema=["x"], tasks=[dict(id="one")],
                     adapter=dict(files=[dict(path=str(adapter), sha256=training.digest_file(adapter))]))
        jobs = training.TrainingJobs.__new__(training.TrainingJobs)
        jobs.directory = self.root / "runs"
        catalog = SimpleNamespace(public=lambda: [], remote_groups=lambda: {}, snapshot_resources=lambda: {})
        bound = SimpleNamespace(public=lambda: {}, resources=lambda: {})
        criteria = dict(dimensions=[])
        with patch.object(training, "freeze_scene", return_value=scene), \
             patch.object(training, "validate_criteria", return_value=criteria), \
             patch.object(training, "validate_costs", return_value={}), \
             patch.object(training, "feedback_protocol", return_value=dict(reward_timing="test")), \
             patch.object(training, "distributions", return_value=[]):
            manifest = jobs._manifest(dict(scene={}, criteria={}, algorithm="required_only"), bound, catalog)
        files = {item["path"]: item for item in manifest["implementation_files"]}
        self.assertTrue({"target_runtime.py", "callsite.py", "verification.py", "classification.py",
                         "intake_feedback.py", "resources.py", "training.py"} <= files.keys())
        for item in files.values():
            blob = Path(item["source_blob"])
            self.assertEqual(hashlib.sha256(blob.read_bytes()).hexdigest(), item["sha256"])


if __name__ == "__main__":
    unittest.main()
