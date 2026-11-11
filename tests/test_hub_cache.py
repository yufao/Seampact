import hashlib
import os
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from seampact.configuration.hub_cache import check_hub_cache, validate_hub_cache_spec


class HubCacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = Path(self.tmp.name)
        self.repo = self.cache / "models--owner--model"
        self.revision = "a" * 40
        self.snapshot = self.repo / "snapshots" / self.revision
        self.snapshot.mkdir(parents=True)
        (self.repo / "refs").mkdir()
        (self.repo / "refs" / "main").write_text(self.revision, encoding="ascii")
        self.data = b"fixed-model-file"
        (self.snapshot / "model.bin").write_bytes(self.data)
        self.spec = dict(schema="fixed-hub-cache/1", repo_id="owner/model", revision=self.revision,
                         cache_dir=str(self.cache), ref="main", required_files=[dict(path="model.bin",
                         sha256=hashlib.sha256(self.data).hexdigest(), bytes=len(self.data))])

    def _offline_check(self, spec=None):
        with patch.dict(os.environ, {"HF_HUB_OFFLINE": "1"}), patch.dict(
                "sys.modules", {"huggingface_hub.constants": types.SimpleNamespace(HF_HUB_OFFLINE=True),
                                "transformers.utils.hub": types.SimpleNamespace(_is_offline_mode=True)}):
            return check_hub_cache(spec or self.spec)

    def test_regular_file_and_explicit_commit_ref(self):
        self.assertEqual(self._offline_check()["status"], "IDENTITY_VERIFIED")
        self.spec["ref"] = self.revision
        (self.repo / "refs" / "main").write_text("b" * 40, encoding="ascii")
        self.assertEqual(self._offline_check()["status"], "IDENTITY_VERIFIED")

    def test_main_drift_and_missing_ref(self):
        ref = self.repo / "refs" / "main"
        ref.write_text("b" * 40, encoding="ascii")
        self.assertEqual(self._offline_check()["status"], "MISSING")
        ref.unlink()
        self.assertEqual(self._offline_check()["status"], "MISSING")

    def test_wrong_size_hash_and_missing_file(self):
        file = self.snapshot / "model.bin"
        for contents in (b"short", b"X" * len(self.data)):
            file.write_bytes(contents)
            result = self._offline_check()
            self.assertEqual(result["status"], "MISSING")
            self.assertEqual(result["missing_files"][0]["path"], "model.bin")
        file.unlink()
        self.assertEqual(self._offline_check()["status"], "MISSING")

    def test_offline_environment_and_imported_sdk_state(self):
        with patch.dict(os.environ, {"HF_HUB_OFFLINE": "0"}):
            self.assertEqual(check_hub_cache(self.spec)["status"], "MISSING")
            result = check_hub_cache(self.spec, require_offline=False)
            self.assertEqual(result["status"], "IDENTITY_VERIFIED")
            self.assertFalse(result["offline_required"])
        with patch.dict(os.environ, {"HF_HUB_OFFLINE": "1"}), patch.dict(
                "sys.modules", {"huggingface_hub.constants": types.SimpleNamespace(HF_HUB_OFFLINE=False),
                                "transformers.utils.hub": types.SimpleNamespace(_is_offline_mode=False)}):
            self.assertEqual(check_hub_cache(self.spec)["status"], "MISSING")
        with patch.dict(os.environ, {"HF_HUB_OFFLINE": "1"}), patch.dict(
                "sys.modules", {"huggingface_hub.constants": types.SimpleNamespace(HF_HUB_OFFLINE=True),
                                "transformers.utils.hub": types.SimpleNamespace(_is_offline_mode=False)}):
            self.assertEqual(check_hub_cache(self.spec)["status"], "MISSING")

    def test_invalid_spec_rejected(self):
        for bad in ("../outside", "/absolute", "x/../outside", "C:/outside", "x\\outside", "a//b"):
            spec = dict(self.spec, required_files=[dict(path=bad, sha256="f" * 64)])
            with self.subTest(path=bad), self.assertRaises(ValueError):
                validate_hub_cache_spec(spec)
        for repo in ("a--b/c", "a/b.git", "a/../b"):
            with self.subTest(repo=repo), self.assertRaises(ValueError):
                validate_hub_cache_spec(dict(self.spec, repo_id=repo))
        with self.assertRaises(ValueError):
            validate_hub_cache_spec(dict(self.spec, ref="other-branch"))

    def test_allowed_blob_link_and_escaping_link(self):
        blob = self.repo / "blobs" / "blob"
        blob.parent.mkdir()
        blob.write_bytes(self.data)
        file = self.snapshot / "model.bin"
        file.unlink()
        try:
            file.symlink_to(blob)
        except OSError as error:
            self.skipTest("symlinks unavailable: " + str(error))
        self.assertEqual(self._offline_check()["status"], "IDENTITY_VERIFIED")
        file.unlink()
        outside = self.cache / "outside"
        outside.write_bytes(self.data)
        file.symlink_to(outside)
        self.assertEqual(self._offline_check()["status"], "MISSING")
        file.unlink()
        outside.unlink()
        outside.symlink_to(blob)
        file.symlink_to(outside)
        self.assertEqual(self._offline_check()["status"], "MISSING")

    def test_escaping_directory_link_and_snapshot_root_link(self):
        outside = self.cache / "outside"
        outside.mkdir()
        (outside / "model.bin").write_bytes(self.data)
        link = self.snapshot / "nested"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError as error:
            self.skipTest("symlinks unavailable: " + str(error))
        self.spec["required_files"][0]["path"] = "nested/model.bin"
        self.assertEqual(self._offline_check()["status"], "MISSING")
        link.unlink()
        self.snapshot.rename(self.snapshot.with_name("saved"))
        self.snapshot.symlink_to(outside, target_is_directory=True)
        self.assertEqual(self._offline_check()["status"], "MISSING")


if __name__ == "__main__":
    unittest.main()
