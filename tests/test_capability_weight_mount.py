"""Registered weights are checked again when deriving a worker mount."""
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from seampact.hosting.capability_hosting import resource_backend
from seampact.resources import ResourceRegistry
from seampact.store import Store


class CapabilityWeightMount(unittest.TestCase):
    def test_registered_file_is_rechecked_and_mounted_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            weight = root / "weight.bin"
            weight.write_bytes(b"weight")
            store = Store(root / "state.sqlite3")
            try:
                resources = ResourceRegistry(root, store, None)
                sha = hashlib.sha256(b"weight").hexdigest()
                record = resources.save_weight(dict(name="test", kind="file", path=str(weight), sha256=sha))
                identity = record["id"]
                group = dict(bindings={"model": dict(form="path", resource_id=identity,
                    path="/work/resources/" + identity, sha256=sha)})
                platform = SimpleNamespace(store=store, resources=resources)
                # Test the host path independently of whether this test process is containerized.
                exists = Path.exists
                with patch.object(Path, "exists", lambda path: False if str(path) == "/.dockerenv" else exists(path)):
                    config = resource_backend(platform, group, dict(kind="docker", mounts=[]))
                    self.assertEqual(config["mounts"], [dict(source=str(weight.resolve()),
                        target="/work/resources/" + identity, read_only=True)])
                    weight.write_bytes(b"changed")
                    with self.assertRaisesRegex(ValueError, "权重当前不可读取"):
                        resource_backend(platform, group, dict(kind="docker", mounts=[]))
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
