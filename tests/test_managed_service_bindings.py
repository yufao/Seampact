"""Checked original binding -> frozen service identity; no model inference."""
import copy
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from seampact.service import Platform
from seampact import __version__
from seampact.inspection import Inspector
from seampact.scenarios import FrozenCatalog
import test_managed_services as fixtures


class ManagedBindingChecks(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name)
        self.platform = Platform(self.home / "platform")
        self.addCleanup(self.platform.close)
        self.manager = self.platform.managed_services
        self.docker = fixtures.FakeDocker()
        self.manager.runner = self.docker
        self.manager.http_get = lambda url, headers, timeout: (200, {"data": [{"id": "test"}]})
        self.platform.store.save("deployment_backend", "current", {"kind": "docker", "start_timeout_s": 2})
        self.spec = dict(id="test", image="engine:local", model="test", model_identity="test@revision",
            weight_identity="weights@revision", argv=["serve"], endpoint="http://test:8000/v1",
            docker=dict(network="test-network", network_alias="test", container_port=8000),
            contract=dict(exclusive_control=True, request_isolation=True, rebuildable=True,
                          background_work=False, control_owner="fixture", max_concurrency=1),
            resource_envelope=dict(host_memory_mb=16, devices={}, basis="fixture"))
        self.manager.configure(self.spec)
        self.connection = self.platform.profiles.save_connection(dict(name="owned", resource_kind="managed",
            managed_service_id="test", base_url=self.spec["endpoint"], model="test", auth=dict(mode="none")))

    def install(self):
        root = self.home / "source"
        root.mkdir()
        code = ("class Client:\n    def __init__(self, base_url):\n        self.url = base_url\n"
                "    def request(self, value):\n        return self.url + str(value)\n"
                "def run(url: str, value: int) -> str:\n    return Client(base_url=url).request(value)\n")
        (root / "original.py").write_text(code, encoding="utf-8")
        inspector = Inspector.freeze(root, root / "snapshots", "fixture", ["original.py"], "run", "response")
        inspector.request["source_id"] = "fixture"
        candidate = dict(entries=[dict(id="run", path="original.py", symbol="run", kind="function",
            boundary_ids=["W"], fixed_arguments={"url": {"$dependency": dict(id="service", field="base_url")}})],
            boundaries=[dict(id="W", path="original.py", symbol="run", mode="W")],
            dependencies=[dict(id="service", kind="service", configurable=True, required=True,
                entry_ids=["run"], read_time="call", owner_scope="request",
                evidence=[dict(path="original.py", start=6, end=7)],
                mappings=[dict(entry_id="run", location="entry.fixed_arguments", parameter="url", field="base_url")])])
        rows = self.platform.dependencies.record(inspector, candidate)
        self.assertEqual(rows[0]["status"], "MAPPING_CHECKED", rows[0].get("reason"))
        self.platform.dependencies.select(dict(harness_id="fixture", dependency_id="fixture:service",
                                                connection_id=self.connection["id"]))
        inspector.request["dependency_bindings"] = self.platform.dependencies.selections("fixture")
        unit = self.platform.catalog.install(inspector, candidate, "fixture", "fixture")[0]
        self.assertEqual(unit["status"], "READY", unit.get("reason"))
        return unit["id"]

    def test_original_binding_freezes_owned_service_and_rejects_changed_spec(self):
        uid = self.install()
        frozen = FrozenCatalog(self.platform.catalog, [uid])
        self.assertEqual(frozen.placement[uid]["resource_dependencies"], ["service:test"])
        self.assertEqual(set(frozen.remote_groups()), {"service:test"})
        self.assertEqual(frozen.dispatch_status(uid)["status"], "COLD_STARTABLE")
        snapshot = frozen.service_snapshot()
        self.assertEqual(snapshot["allocations"]["service:test"]["image_id"], fixtures.IMAGE)
        self.assertFalse(any(c[:2] == ["docker", "run"] for c in self.docker.commands))
        changed = copy.deepcopy(self.spec)
        changed["weight_identity"] = "weights@new-revision"
        self.manager.configure(changed)
        self.assertEqual(frozen.service_snapshot(), snapshot)
        with self.assertRaisesRegex(ValueError, "规格已变化"):
            FrozenCatalog(self.platform.catalog, [uid])

    def test_connection_model_mismatch_and_checks_never_start_model(self):
        with self.assertRaisesRegex(ValueError, "不符"):
            self.platform.profiles.save_connection(dict(name="wrong", resource_kind="managed",
                managed_service_id="test", base_url=self.spec["endpoint"], model="other"))
        result = self.platform.resources.check_connection(self.connection["id"])
        self.assertFalse(result["accessible"])
        self.assertFalse(any(c[:2] == ["docker", "run"] for c in self.docker.commands))
        self.assertEqual(self.platform.state()["version"], __version__)


if __name__ == "__main__":
    unittest.main()
