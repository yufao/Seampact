"""Credential preparation follows checked host roots without trimming their closure."""

import copy
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from seampact.configuration.dependencies import DependencyBindings
from seampact.hosting import backends, deployment
from seampact.hosting.deployment import Deployments, GROUP_PROFILE
from seampact.source.inspection import Inspector
from seampact.store import Store
from seampact.worker import Worker


class HostResourceScopeTests(unittest.TestCase):
    def fixture(self):
        code = ("from openai import OpenAI\n"
                "def child(key):\n    return OpenAI(api_key=key)\n"
                "def host(key):\n    return child(key)\n"
                "def unavailable(key):\n    return OpenAI(api_key=key)\n")
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root / "original.py").write_text(code, encoding="utf-8")
        inspector = Inspector.freeze(root, root / "snapshots", "request", ["original.py"], "host", "result")
        inspector.request["source_id"] = "source"
        entries, dependencies = [], []
        for entry_id, dependency_id in (("host", "service"), ("unavailable", "unresolved")):
            entries.append(dict(id=entry_id, path="original.py", kind="function", symbol=entry_id,
                fixed_arguments=dict(key={"$dependency": dict(id=dependency_id, field="credential")})))
            dependencies.append(dict(id=dependency_id, kind="service", configurable=True, required=True,
                entry_ids=[entry_id], read_time="call", owner_scope="original request",
                evidence=[dict(path="original.py", start=1, end=7)],
                mappings=[dict(entry_id=entry_id, location="entry.fixed_arguments", parameter="key", field="credential")]))
        candidate = dict(entries=entries, dependencies=dependencies)
        connection = dict(id="checked-connection", config_digest="connection-version",
                          auth=dict(mode="saved", credential_version="v1"))
        self.key_reads = []
        def read_key(_):
            self.key_reads.append(True)
            return "private-scope-test-value"
        profiles = SimpleNamespace(lock=threading.RLock(), connection_profile=lambda _: copy.deepcopy(connection),
                                   connection_key=read_key)
        resources = DependencyBindings(SimpleNamespace(profiles=profiles), inspector, candidate)
        self.assertEqual(resources.rows["service"]["entry_checks"]["host"]["status"], "MAPPING_CHECKED")
        resources.selected["service"] = dict(definition_digest=resources.rows["service"]["definition_digest"],
                                             connection=connection)
        group = dict(id="group", entries=["host", "unavailable"], units=["installation:host", "installation:unavailable"],
            installation_id="installation", host=dict(status="HOST_CHECKED", host_entries=["host"]),
            contract=dict(digest="checked-contract", host_profile=GROUP_PROFILE["id"], root_entries=["host"],
                          operations=dict(start=dict(status="SUPPORTED")), replicas=dict(replicable=False)))
        store = Store(":memory:")
        self.addCleanup(store.close)
        store.save("deployment", "group", dict(contract_digest="checked-contract", installation_id="installation",
            platform_core={"fixture": "core"}, image="fixture-image", folder=".", binding_fingerprint="frozen",
            build=dict(status="BUILT"), publish=dict(status="NOT_REQUIRED")))
        owner = object.__new__(Deployments)
        owner.platform = SimpleNamespace(source_lock=threading.RLock(), store=store)
        owner.lock, owner.instances, owner.run_locks, owner.session = threading.RLock(), {}, {}, "session"
        owner._guard = lambda *_: None
        owner._group = lambda _: group
        owner._instances = lambda _: []
        owner._binding = lambda _: (SimpleNamespace(resource_binding=resources), None, None)
        owner.backend = lambda: dict(kind="docker")
        owner.gpu_allocation = lambda *_, **__: dict(status="VERIFIED", devices=[])
        owner._next_generation = lambda _: 1
        owner._event = lambda *_, **__: None
        owner._view = lambda record: copy.deepcopy(record)
        return owner, group, resources

    def start(self, owner, launch):
        with patch.object(deployment, "core_identity", return_value={"fixture": "core"}), \
             patch.object(backends, "start", launch):
            return owner._start_unprotected("group", None, False)

    def test_unavailable_root_does_not_expand_credentials_but_host_helper_dependency_remains(self):
        owner, group, resources = self.fixture()
        def launch(*args, **kwargs):
            self.assertEqual(kwargs["credential_env"],
                             {"SEAMPACT_CREDENTIAL_CHECKED_CONNECTION": "private-scope-test-value"})
            return dict(status="APPLIED", steps=[])
        self.start(owner, launch)
        self.assertEqual(len(self.key_reads), 1)
        self.assertEqual(group["entries"], ["host", "unavailable"])
        self.assertIn("unresolved", resources.rows)

    def test_host_transitive_dependency_still_requires_selection(self):
        owner, _, resources = self.fixture()
        resources.selected.clear()
        with self.assertRaisesRegex(ValueError, "unselected"):
            self.start(owner, lambda *_, **__: self.fail("backend must not start"))
        self.assertEqual(owner.instances, {})

    def test_changed_host_contract_cannot_reuse_old_artifact(self):
        owner, group, _ = self.fixture()
        group["host"]["host_entries"].append("unavailable")
        group["contract"]["root_entries"].append("unavailable")
        group["contract"]["digest"] = "changed-contract"
        with self.assertRaisesRegex(ValueError, "契约/接入版本不一致"):
            self.start(owner, lambda *_, **__: self.fail("backend must not start"))
        self.assertEqual(self.key_reads, [])
        self.assertEqual(owner.instances, {})

    def test_unavailable_root_is_still_rejected_by_worker(self):
        worker = object.__new__(Worker)
        worker.units = {"installation:host"}
        worker.identity = lambda: {}
        result = worker.invoke(dict(unit="installation:unavailable"))
        self.assertIn("rejected", result)

    def test_empty_missing_or_inconsistent_host_mask_never_falls_back_to_group(self):
        for host in ({"host_entries": []}, {}, {"host_entries": ["unavailable"]}):
            with self.subTest(host=host):
                owner, group, _ = self.fixture()
                group["host"] = dict(host, status="HOST_CHECKED")
                with self.assertRaisesRegex(ValueError, "宿主可执行入口与冻结运行组契约不一致"):
                    self.start(owner, lambda *_, **__: self.fail("backend must not start"))
                self.assertEqual(self.key_reads, [])
                self.assertEqual(owner.instances, {})


if __name__ == "__main__":
    unittest.main()
