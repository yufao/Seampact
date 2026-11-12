"""Capability intake inventories bytes without eagerly discovering legacy entries."""
import tempfile
import unittest
import os
from pathlib import Path
from unittest.mock import patch

from seampact.intake import RepositoryIntake
from seampact.source.inspection import Inspector
from seampact.source.inspection import inventory_repository


class RepositoryIntakeChecks(unittest.TestCase):
    def test_generation_skips_entry_scan_but_preserves_scope_and_credential_guard(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "repository"
            repository.mkdir()
            (repository / "main.py").write_text("def main():\n    return 1\n", encoding="utf-8")
            (repository / "README.md").write_text("Public library usage\n", encoding="utf-8")
            (repository / "private_config.py").write_text("api_key = 'non-placeholder-test-value'\n", encoding="utf-8")
            intake = RepositoryIntake(root / "managed")
            with patch("seampact.source.inspection.source_facts", side_effect=AssertionError("entry scan must be lazy")):
                source = intake.prepare(str(repository), discover_entries=False)
            self.assertEqual(source["entry"]["status"], "NOT_REQUESTED")
            self.assertEqual(source["entry"]["candidates"], [])
            self.assertEqual(set(source["files"]), {"main.py", "README.md"})
            self.assertEqual(next(item for item in source["inventory"] if item["path"] == "private_config.py")["status"], "WITHHELD")
            frozen = Inspector.freeze_repository(source, root / "snapshots", "request", dict(scope="complete-capability"))
            self.assertEqual(set(frozen.files), {"main.py", "README.md"})
            self.assertEqual(frozen.request["entry"]["status"], "NOT_REQUESTED")
            legacy = intake.prepare(str(repository))
            self.assertEqual(legacy["entry"]["status"], "STATIC_CANDIDATES")
            self.assertTrue(any(item.get("symbol") == "main" for item in legacy["entry"]["candidates"]))

    def test_inventory_does_not_follow_directory_links(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository, outside = root / "repository", root / "outside"
            repository.mkdir()
            outside.mkdir()
            (repository / "main.py").write_text("value = 1\n", encoding="utf-8")
            (outside / "outside.py").write_text("outside_value = 2\n", encoding="utf-8")
            try:
                os.symlink(outside, repository / "linked", target_is_directory=True)
            except OSError:
                self.skipTest("directory links unavailable")
            scope = inventory_repository(repository, discover_entries=False)
            self.assertEqual(scope["files"], ["main.py"])
            link = next(item for item in scope["inventory"] if item["path"] == "linked")
            self.assertEqual(link["kind"], "symlink")
            self.assertFalse(link["readable"])


if __name__ == "__main__":
    unittest.main()
