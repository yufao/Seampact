"""Real export refreshes checked environment support and builds loadable workers."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from seampact.scenes.scenarios import FrozenCatalog
from seampact.service import Platform
from seampact.source.inspection import Inspector
from seampact.typed_contract import verify_contract
from seampact.worker import Worker


class ContractExportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.platform = Platform(self.root / "platform")
        self.addCleanup(self.platform.close)
        source = self.root / "source"
        source.mkdir()
        (source / "original.py").write_text("def echo(value: str) -> str:\n    return value\n", encoding="utf-8")
        inspector = Inspector.freeze(source, self.root / "snapshots", "request",
                                     ["original.py"], "echo", "original return")
        inspector.request["source_id"] = "source"
        self.unit, = self.platform.catalog.install(inspector, dict(entries=[
            dict(id="echo", kind="function", symbol="echo", path="original.py")]), "analysis", "install")
        self.platform.store.save("harness", "source", dict(id="source", status="CONNECTED",
                                                          installation_id="install"))
        self.gid = self.unit["runtime_group"]

    def export(self):
        return self.platform.deployments.export(self.gid, dict(
            environment=dict(python="3.11", requirements=[], packages=[])))

    def test_export_refreshes_environment_and_worker_rechecks_original(self):
        frozen = FrozenCatalog(self.platform.catalog, [self.unit["id"]])
        artifact = self.export()
        current = self.platform.store.get("unit", self.unit["id"])
        group = self.platform.store.get("runtime_group", self.gid)
        self.assertTrue(verify_contract(current, group=group)["valid"])
        self.assertNotEqual(current["typed_contract"]["digest"], self.unit["typed_contract"]["digest"])
        with self.assertRaises(ValueError):
            frozen.invoke(self.unit["id"], dict(value="old placement"))
        with patch("seampact.worker.json_request", return_value=dict(session="test-session")):
            worker = Worker(Path(artifact["folder"]) / "worker.json", "http://unused.invalid")
        self.addCleanup(worker.store.close)
        hosted = worker.store.get("unit", self.unit["id"])
        host_group = worker.store.get("runtime_group", self.gid)
        self.assertTrue(verify_contract(hosted, group=host_group)["valid"])
        self.assertEqual(worker.units, {self.unit["id"]})
        again = self.export()
        self.assertEqual(self.platform.store.get("unit", self.unit["id"])["typed_contract"], current["typed_contract"])
        self.assertEqual(again["binding_fingerprint"], artifact["binding_fingerprint"])
        self.assertTrue(json.loads((Path(again["folder"]) / "worker.json").read_text(encoding="utf-8"))["platform_core"])

    def test_export_does_not_reissue_a_tampered_contract(self):
        unit = self.platform.store.get("unit", self.unit["id"])
        unit["typed_contract"]["operations"]["scale_out"]["allowed"] = False
        unit["symbol"] = "other"
        self.platform.store.save("unit", unit["id"], unit)
        with self.assertRaises(ValueError):
            self.export()
        self.assertIsNone(self.platform.store.get("deployment", self.gid))


if __name__ == "__main__":
    unittest.main()
