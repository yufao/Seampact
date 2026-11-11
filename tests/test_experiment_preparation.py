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
from seampact.scheduling import training
from seampact.scenes.data_use import task_digest
from seampact.scenes.scenarios import SceneSession


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
        catalog = SimpleNamespace(public=lambda: [], remote_groups=lambda: {}, snapshot_resources=lambda: {},
            add_role_services=lambda *args: None, service_snapshot=lambda: {}, service_placements={})
        bound = SimpleNamespace(public=lambda: {}, resources=lambda: {})
        criteria = dict(dimensions=[])
        with patch.object(training, "freeze_scene", return_value=scene), \
             patch.object(training, "validate_criteria", return_value=criteria), \
             patch.object(training, "validate_costs", return_value={}), \
             patch.object(training, "feedback_protocol", return_value=dict(reward_timing="test")), \
             patch.object(training, "distributions", return_value=[]):
            manifest = jobs._manifest(dict(scene={}, criteria={}, algorithm="required_only"), bound, catalog)
        self.assertEqual(manifest["data_use"], {"status": "UNDECLARED"})
        files = {item["path"]: item for item in manifest["implementation_files"]}
        self.assertTrue({"target_runtime.py", "source/callsite.py", "source/verification.py",
                         "analysis_support/classification.py", "analysis_support/intake_feedback.py",
                         "resources.py", "scheduling/training.py", "configuration/configuration_files.py",
                         "source/__init__.py", "scenes/__init__.py"} <= files.keys())
        for item in files.values():
            blob = Path(item["source_blob"])
            self.assertEqual(hashlib.sha256(blob.read_bytes()).hexdigest(), item["sha256"])

    def test_research_training_rejects_test_data_and_freeze_preserves_resources(self):
        adapter = self.root / "scene.py"
        adapter.write_text("# test adapter\n", encoding="utf-8")
        data = self.root / "input.txt"
        data.write_text("independent example", encoding="utf-8")
        task = dict(id="one", data_member_id="example", input="input.txt")
        member = dict(id="example", upstream=dict(dataset="fixture", revision="v1", example_id="one"),
            group_ids=["fixture:one"], derived_from=[], split="test", allowed_use=["test"],
            expected_task_sha256=task_digest(task),
            files=[dict(path="input.txt", sha256=training.digest_file(data))])
        source = self.root / "data-manifest.json"
        source.write_text(json.dumps(dict(schema="research-data-manifest/1", members=[member])), encoding="utf-8")
        scene = dict(id="scene", version="1", observation_schema=["x"], candidate_schema=["x"],
            tasks=[task], model_roles=[], adapter=dict(path=str(adapter), symbol="factory"),
            research_protocol="multisource-review/1", research_data=dict(schema="research-data-binding/1",
                manifest_path=str(source), manifest_sha256=training.digest_file(source)))
        jobs = training.TrainingJobs.__new__(training.TrainingJobs)
        jobs.directory = self.root / "runs"
        checkpoint = self.root / "checkpoint.bin"
        checkpoint.write_bytes(b"parent checkpoint")
        jobs.record = lambda _: dict(status="COMPLETED", manifest={},
            checkpoint=dict(path=str(checkpoint), artifact_digest=training.digest_file(checkpoint)), config_digest="parent")
        catalog = SimpleNamespace(public=lambda: [], remote_groups=lambda: {}, snapshot_resources=lambda: {},
            add_role_services=lambda *args: None, service_snapshot=lambda: {}, service_placements={})
        bound = SimpleNamespace(public=lambda: {}, resources=lambda: {})
        config = dict(scene=scene, criteria={}, algorithm="ppo", data_purpose="test")
        with patch.object(training, "validate_criteria", return_value=dict(dimensions=[])), \
             patch.object(training, "validate_costs", return_value={}), \
             patch.object(training, "feedback_protocol", return_value=dict(reward_timing="test")), \
             patch.object(training, "distributions", return_value=[]):
            with self.assertRaisesRegex(ValueError, "仅允许 train"):
                jobs._manifest(dict(config, mode="train"), bound, catalog)
            with self.assertRaisesRegex(ValueError, "not allowed for purpose"):
                jobs._manifest(dict(config, mode="train", data_purpose="train"), bound, catalog)
            frozen = jobs._manifest(dict(config, mode="freeze", parent_run_id="parent"), bound, catalog)
        self.assertEqual(frozen["data_use"]["status"], "DECLARATIONS_VERIFIED")
        self.assertEqual(frozen["data_use"]["purpose"], "test")
        paths = {item["path"] for item in frozen["scene"]["resource_files"]}
        self.assertEqual(paths, {str(source.resolve()), str(data.resolve())})
        SceneSession._verify(frozen["scene"])
        data.write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "资源已改变"):
            SceneSession._verify(frozen["scene"])


if __name__ == "__main__":
    unittest.main()
