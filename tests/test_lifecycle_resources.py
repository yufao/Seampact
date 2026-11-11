"""Original resource values and asset facts cannot stand in for SDK effects."""

import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from seampact.configuration.dependencies import DependencyBindings, check_requirements
from seampact.source.inspection import Inspector
from seampact.source.lifecycle_resources import resolve_values
from seampact.store import Store
from seampact.units import UnitCatalog
from seampact.worker import host_weight_check


SOURCE = '''import torch
class Owner:
    def __init__(self, model_path: str):
        self.model = torch.load(model_path)
    def predict(self, values: list[float]) -> int:
        return len(values)
'''


class LifecycleResourceTests(unittest.TestCase):
    def fixture(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root / "original.py").write_text(SOURCE, encoding="utf-8")
        weights = root / "weights.bin"
        weights.write_bytes(b"original weight bytes")
        inspector = Inspector.freeze(root, root / "snapshots", "request", ["original.py"], "predict", "original result")
        inspector.request["source_id"] = "source"
        ref = {"$dependency": dict(id="model", field="path")}
        candidate = dict(entries=[dict(id="predict", kind="method", path="original.py", symbol="Owner.predict",
            constructor=dict(symbol="Owner", arguments=dict(model_path=ref)), boundary_ids=[])], boundaries=[],
            dependencies=[dict(id="model", kind="weight", configurable=True, required=True, entry_ids=["predict"],
                read_time="constructor", owner_scope="original owner", evidence=[dict(path="original.py", start=1, end=6)],
                mappings=[dict(entry_id="predict", location="constructor.arguments", parameter="model_path", field="path")])])
        checked = check_requirements(inspector, candidate)[0]
        self.assertEqual(checked["status"], "MAPPING_CHECKED", checked)
        resource = dict(id="asset", path=str(weights), kind="file", required_files=[],
            sha256=hashlib.sha256(weights.read_bytes()).hexdigest(), model_revision="original", execution_environment="local",
            config_digest="frozen-resource", revision=1)
        resource["check"] = host_weight_check(root)(resource)
        inspector.request["dependency_bindings"] = dict(model=dict(definition_digest=checked["definition_digest"], resource=resource))
        registry = SimpleNamespace(profiles=None)
        bindings = DependencyBindings(registry, inspector, candidate)
        bindings.host_check = host_weight_check(root)
        store = Store(":memory:")
        self.addCleanup(store.close)
        catalog = UnitCatalog(root, store, None)
        catalog.resource_registry, catalog.host_check = registry, host_weight_check(root)
        return root, inspector, candidate, bindings, catalog

    def member(self, report):
        group = report["runtime_groups"]["groups"][0]
        self.assertEqual(group["contract"]["lifecycle"]["replicate"]["status"], "UNKNOWN")
        self.assertEqual(group["contract"]["lifecycle"]["reclaim"]["status"], "UNKNOWN")
        return group["contract"]["lifecycle"]["source_evidence"]["members"][0]

    def test_actual_weight_value_is_consumed_and_installed_contract_matches_inspection(self):
        root, inspector, candidate, bindings, catalog = self.fixture()
        values = candidate["entries"][0]["constructor"]["arguments"]
        resolved, proof, observation = resolve_values(values, "predict", "constructor.arguments", bindings)
        self.assertEqual(resolved["model_path"], str(root / "weights.bin"))
        self.assertEqual(proof["status"], "CHECKED")
        self.assertEqual(observation["values"][0]["assets"]["status"], "CHECKED")
        self.assertEqual(proof["values"][0]["asset_requirements"]["completeness"], "UNKNOWN")
        self.assertEqual(proof["effects"]["status"], "UNKNOWN")
        self.assertNotIn(str(root), json.dumps(proof))
        report = catalog.inspect_candidate(inspector, candidate)
        member = self.member(report)
        scope = next(item for item in member["resource_values"] if item["location"] == "constructor.arguments")
        self.assertEqual(scope["status"], "CHECKED")
        self.assertNotIn("finite primitive subset", member["reason"])
        self.assertIn("external import", member["reason"])
        # Installation checks source only; no original model is imported or loaded.
        catalog.install(inspector, candidate, "analysis", "installation")
        group = catalog.store.get("runtime_group", report["runtime_groups"]["groups"][0]["id"])
        self.assertEqual(group["contract"]["digest"], report["runtime_groups"]["groups"][0]["contract"]["digest"])

    def test_actual_asset_failure_is_separate_from_frozen_contract_requirements(self):
        root, inspector, candidate, bindings, catalog = self.fixture()
        before = catalog.inspect_candidate(inspector, candidate)
        self.assertEqual(before, catalog.inspect_candidate(inspector, candidate))
        weights = root / "weights.bin"
        weights.write_bytes(b"X" * weights.stat().st_size)
        after = catalog.inspect_candidate(inspector, candidate)
        member = self.member(after)
        scope = next(item for item in member["resource_values"] if item["location"] == "constructor.arguments")
        self.assertEqual(scope["status"], "CHECKED")
        group = after["runtime_groups"]["groups"][0]
        observation = next(item for item in group["resource_observations"][0]["scopes"] if item["location"] == "constructor.arguments")
        self.assertEqual(observation["values"][0]["asset_check"]["status"], "checksum_or_size_mismatch")
        self.assertEqual(observation["values"][0]["asset_check"]["mismatch_count"], 1)
        self.assertEqual(after["entries"][0]["status"], "MISSING")
        self.assertEqual(before["runtime_groups"]["groups"][0]["contract"]["digest"], group["contract"]["digest"])
        weights.unlink()
        missing = catalog.inspect_candidate(inspector, candidate)
        observation = next(item for item in missing["runtime_groups"]["groups"][0]["resource_observations"][0]["scopes"]
                           if item["location"] == "constructor.arguments")
        self.assertEqual(observation["values"][0]["asset_check"]["status"], "missing")
        self.assertEqual(observation["values"][0]["asset_check"]["missing_count"], 1)

    def test_presence_without_content_hash_remains_partial_asset_evidence(self):
        _, _, candidate, bindings, _ = self.fixture()
        resource = bindings.selected["model"]["resource"]
        resource.pop("sha256")
        values = candidate["entries"][0]["constructor"]["arguments"]
        _, proof, observation = resolve_values(values, "predict", "constructor.arguments", bindings)
        self.assertEqual(proof["status"], "CHECKED")
        self.assertEqual(observation["values"][0]["assets"]["status"], "UNKNOWN")
        self.assertEqual(observation["values"][0]["assets"]["files"][0]["status"], "UNKNOWN")

    def test_wrong_destination_changed_definition_and_absent_resolver_do_not_consume_reference(self):
        _, _, candidate, bindings, _ = self.fixture()
        values = candidate["entries"][0]["constructor"]["arguments"]
        for entry_id, location, current in (("other", "constructor.arguments", bindings),
                                             ("predict", "entry.fixed_arguments", bindings),
                                             ("predict", "constructor.arguments", None)):
            _, proof, _ = resolve_values(values, entry_id, location, current)
            self.assertEqual(proof["status"], "UNKNOWN")
        bindings.selected["model"]["definition_digest"] = "changed"
        _, proof, _ = resolve_values(values, "predict", "constructor.arguments", bindings)
        self.assertEqual(proof["status"], "UNKNOWN")

    def test_service_credential_never_resolves_secret_or_empty_runtime_placeholder(self):
        code = "def request(key):\n    return Client(api_key=key)\n"
        inspector = SimpleNamespace(root=Path.cwd(), request=dict(source_version="frozen", source_id="source"), data=lambda _: code.encode())
        value = {"$dependency": dict(id="service", field="credential")}
        candidate = dict(entries=[dict(id="request", kind="function", path="original.py", symbol="request", fixed_arguments=dict(key=value))],
            dependencies=[dict(id="service", kind="service", configurable=True, required=True, entry_ids=["request"],
                read_time="call", owner_scope="original", evidence=[dict(path="original.py", start=1, end=2)],
                mappings=[dict(entry_id="request", location="entry.fixed_arguments", parameter="key", field="credential")])])
        bindings = DependencyBindings(SimpleNamespace(profiles=None), inspector, candidate)
        bindings.selected["service"] = dict(definition_digest=bindings.rows["service"]["definition_digest"],
            connection=dict(id="connection", auth=dict(mode="saved")))
        def forbidden(*args, **kwargs):
            self.fail("credential resolution must not occur")
        bindings.value = forbidden
        resolved, proof, _ = resolve_values(dict(key=value), "request", "entry.fixed_arguments", bindings)
        self.assertIsNone(resolved["key"])
        self.assertEqual(proof["status"], "UNKNOWN")
        self.assertNotIn("value_digest", proof["values"][0])

    def test_stale_cached_asset_path_is_not_current_content_evidence(self):
        root, _, candidate, bindings, _ = self.fixture()
        bindings.host_check = None
        resource = bindings.selected["model"]["resource"]
        resource["path"] = str(root / "different.bin")
        values = candidate["entries"][0]["constructor"]["arguments"]
        resolved, proof, observation = resolve_values(values, "predict", "constructor.arguments", bindings)
        self.assertEqual(resolved["model_path"], resource["path"])
        self.assertEqual(proof["status"], "CHECKED")  # Original binding value, not asset availability.
        self.assertFalse(observation["values"][0]["assets"]["path_matches"])
        self.assertEqual(observation["values"][0]["assets"]["status"], "UNKNOWN")

    def test_target_asset_availability_does_not_change_frozen_contract(self):
        root, inspector, candidate, _, catalog = self.fixture()
        inspector.request["dependency_bindings"]["model"]["resource"]["check"].update(status="missing", files=[])
        catalog.host_check = None
        deferred = catalog.inspect_candidate(inspector, candidate)
        self.assertEqual(deferred["entries"][0]["status"], "MISSING")
        catalog.host_check = host_weight_check(root)
        actual = catalog.inspect_candidate(inspector, candidate)
        self.assertEqual(deferred["runtime_groups"]["groups"][0]["contract"]["digest"],
                         actual["runtime_groups"]["groups"][0]["contract"]["digest"])
        for report, basis in ((deferred, "FROZEN_SELECTION_CHECK"), (actual, "ACTUAL_HOST_RECHECK")):
            scopes = report["runtime_groups"]["groups"][0]["resource_observations"][0]["scopes"]
            observation = next(item for item in scopes if item["location"] == "constructor.arguments")
            self.assertEqual(observation["basis"], basis)

    def test_open_request_domain_does_not_hide_independent_constructor_facts(self):
        _, inspector, candidate, _, catalog = self.fixture()
        # The supplied check is deliberately untyped; construction remains independently checked.
        from seampact.scheduling.lifecycle import build_lifecycle_evidence
        report = catalog.inspect_candidate(inspector, candidate)
        check = copy.deepcopy(report["entries"][0])
        check["input_schema"]["properties"]["values"] = {}
        native, _, _ = catalog._candidate_checks(inspector, candidate)
        evidence = build_lifecycle_evidence(inspector, [dict(entry_id="predict", entry=candidate["entries"][0], check=check)],
            ["predict"], request_codec="group-port/3", resource_bindings=native.resource_binding)
        member = evidence["members"][0]
        self.assertEqual(member["request_domain"]["status"], "UNKNOWN")
        self.assertEqual(next(item for item in member["resource_values"] if item["location"] == "constructor.arguments")["status"], "CHECKED")

    def test_undeclared_directory_is_the_same_frozen_gap_when_present_or_missing(self):
        root, _, candidate, bindings, _ = self.fixture()
        resource = bindings.selected["model"]["resource"]
        resource.update(path=str(root / "directory"), kind="directory", required_files=[])
        resource.pop("sha256", None)
        values = candidate["entries"][0]["constructor"]["arguments"]
        _, missing, _ = resolve_values(values, "predict", "constructor.arguments", bindings)
        (root / "directory").mkdir()
        _, present, _ = resolve_values(values, "predict", "constructor.arguments", bindings)
        self.assertEqual(missing, present)
        self.assertEqual(present["status"], "UNKNOWN")
        self.assertEqual(present["values"][0]["gap_code"], "ASSET_REQUIREMENTS_UNDECLARED")

    def test_candidate_permission_and_effect_flags_do_not_supply_a_summary(self):
        _, inspector, candidate, _, catalog = self.fixture()
        candidate = copy.deepcopy(candidate)
        candidate["entries"][0].update(lifecycle=dict(cleanup=True, replicate=True), sdk_effects=dict(status="SUPPORTED"))
        self.member(catalog.inspect_candidate(inspector, candidate))


if __name__ == "__main__":
    unittest.main()
