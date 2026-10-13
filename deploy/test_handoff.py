"""Private Docker handoff checks; no daemon, credentials or model requests."""
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from container_entry import release_sources, load_secrets
from prepare_workspace import prepare
from write_secret import write_secret


class DockerHandoffTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name).resolve()
        assert self.base.is_relative_to(Path(tempfile.gettempdir()).resolve())
        self.release = self.base / "release"
        self.release.mkdir()
        (self.release / "sources/sample").mkdir(parents=True)
        self.source = self.release / "sources/sample/tool.py"
        self.source.write_text("def native(): return 1\n", encoding="utf-8")
        self.registry = {"sample": {"root": "sources/sample", "files": ["tool.py"]}}
        self.save_registry()

    def save_registry(self):
        registry = self.release / "sources.json"
        registry.write_text(json.dumps(self.registry), encoding="utf-8")
        (self.release / "release-manifest.json").write_text(json.dumps({"artifact_hashes": {
            "sources.json": hashlib.sha256(registry.read_bytes()).hexdigest(),
            "sources/sample/tool.py": hashlib.sha256(self.source.read_bytes()).hexdigest(),
        }}), encoding="utf-8")

    def tearDown(self):
        self.temporary.cleanup()

    def test_new_workspace_no_overwrite(self):
        output = self.base / "workspace"
        result = prepare(self.release, output)
        self.assertEqual(result["sources"], 1)
        self.assertIn("SEAMPACT_PORT='8875'", (output / "compose.env").read_text(encoding="utf-8"))
        self.assertEqual(sorted(path.name for path in output.iterdir()), ["compose.env", "reports", "secrets", "state"])
        if os.name != "nt":
            self.assertEqual(output.stat().st_mode & 0o777, 0o700)
            self.assertEqual((output / "compose.env").stat().st_mode & 0o777, 0o600)
        with self.assertRaises(FileExistsError):
            prepare(self.release, output)

    def test_source_tamper_rejected(self):
        self.source.write_text("def native(): return 999\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            release_sources(self.release)

    def test_registry_tamper_rejected(self):
        (self.release / "sources.json").write_text("{}", encoding="utf-8")
        with self.assertRaises(ValueError):
            release_sources(self.release)

    def test_root_escape_rejected(self):
        self.registry["sample"]["root"] = "../elsewhere"
        self.save_registry()
        with self.assertRaises(ValueError):
            release_sources(self.release)

    def test_artifact_escape_rejected(self):
        self.registry["sample"]["files"] = ["../../../elsewhere"]
        self.save_registry()
        with self.assertRaises(ValueError):
            release_sources(self.release)

    def test_output_overlap_rejected(self):
        with self.assertRaises(ValueError):
            prepare(self.release, self.release / "state")

    def test_privileged_port_rejected(self):
        with self.assertRaises(ValueError):
            prepare(self.release, self.base / "workspace", 80)

    def test_hidden_atomic_update_and_load(self):
        directory = self.base / "secrets"
        directory.mkdir()
        original_stat = Path.stat

        def private_stat(path, *args, **kwargs):
            actual = original_stat(path, *args, **kwargs)
            values = list(actual)
            values[0] = (actual.st_mode & ~0o777) | (0o700 if path == directory else 0o600)
            return os.stat_result(values)

        # Mock only POSIX permission bits for Windows portability. Runtime
        # enforcement uses real stat; Linux workspace modes are checked above.
        with patch.object(Path, "stat", private_stat), patch("getpass.getpass", return_value="not-a-real-api-key"), patch.dict(os.environ, {}, clear=True):
            write_secret(directory, "deepseek")
            load_secrets(directory)
            self.assertEqual(os.environ["DEEPSEEK_API_KEY"], "not-a-real-api-key")
        self.assertEqual([path.name for path in directory.iterdir()], ["deepseek_api_key"])

    def test_public_secret_rejected(self):
        directory = self.base / "secrets"
        directory.mkdir()
        (directory / "deepseek_api_key").write_text("not-a-real-key", encoding="utf-8")
        original_stat = Path.stat

        def public_stat(path, *args, **kwargs):
            actual = original_stat(path, *args, **kwargs)
            values = list(actual)
            values[0] = (actual.st_mode & ~0o777) | 0o755
            return os.stat_result(values)

        with patch.object(Path, "stat", public_stat):
            with self.assertRaises(ValueError):
                load_secrets(directory)


if __name__ == "__main__":
    unittest.main()
