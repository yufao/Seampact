"""Partial SDK facts are consumed without becoming owner permissions."""

import base64
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from seampact.hosting.background import cleanup_obligations
from seampact.source.inspection import Inspector
from seampact.source import lifecycle_resources as effects
from seampact.store import Store
from seampact.units import UnitCatalog


SOURCE = b"import sdk\ndef predict(value: str) -> str:\n    return sdk.call(value)\n"


class BoundSDKFactTests(unittest.TestCase):
    def fixture(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        (root / "original.py").write_bytes(SOURCE)
        inspector = Inspector.freeze(root, root / "snapshots", "request", ["original.py"], "predict", "result")
        inspector.request["source_id"] = "source"
        entry = dict(id="predict", kind="function", path="original.py", symbol="predict", boundary_ids=[])
        summary = effects._fact("locked-sdk-call", [("original.py", "predict")], "original.py",
            hashlib.sha256(SOURCE).hexdigest(), 3, "sdk.call(value)",
            (("sdk", "1.2.3", "sdk/api.py", "a" * 64),),
            dict(python_background="MAY_SURVIVE_RETURN", whole_owner_read_only=False),
            ["callback/global configuration and creator closure are not proved"],
            dict(rule="exact-object-original-method-join/1", guards=["ALL_MONITOR_CREATORS_ACCOUNTED"]))
        return root, inspector, entry, summary

    def test_normal_catalog_consumes_facts_but_preserves_source_gap_and_permissions(self):
        root, inspector, entry, summary = self.fixture()
        store = Store(":memory:")
        self.addCleanup(store.close)
        candidate = dict(entries=[entry], boundaries=[])
        with patch.object(effects, "_FACTS", (summary,)), patch.object(effects, "sdk_identity", return_value=dict(status="CHECKED")):
            report = UnitCatalog(root, store, None).inspect_candidate(inspector, candidate)
        group = report["runtime_groups"]["groups"][0]
        lifecycle = group["contract"]["lifecycle"]
        member = lifecycle["source_evidence"]["members"][0]
        self.assertEqual(member["effect_evidence"]["status"], "PARTIAL")
        self.assertEqual(member["effect_evidence"]["permission"], "NOT_GRANTED")
        self.assertEqual(member["effect_evidence"]["facts"][0]["premises"]["original_call"]["status"], "CHECKED")
        self.assertIn("external import", member["reason"])
        self.assertEqual(lifecycle["replicate"]["status"], "UNKNOWN")
        self.assertEqual(lifecycle["reclaim"]["status"], "UNKNOWN")
        self.assertEqual(group["resource_observations"][0]["sdk_effects"]["status"], "CHECKED")
        pending = cleanup_obligations(group["contract"])
        self.assertFalse(pending["executable"])
        self.assertEqual(pending["status"], "UNKNOWN")

    def test_actual_sdk_availability_is_separate_from_the_frozen_contract(self):
        _, inspector, entry, summary = self.fixture()
        results = []
        for status in ("CHECKED", "UNKNOWN"):
            with patch.object(effects, "_FACTS", (summary,)), patch.object(effects, "sdk_identity", return_value=dict(status=status)):
                results.append(effects.compile_effects(inspector, entry))
        self.assertEqual(results[0][0], results[1][0])
        self.assertNotEqual(results[0][1], results[1][1])

    def test_byte_location_and_original_root_must_all_match(self):
        _, inspector, entry, summary = self.fixture()
        for change in (dict(sha256="0" * 64), dict(line=2), dict(call="sdk.other(value)")):
            changed = json.loads(json.dumps(summary))
            changed["roots"] = [tuple(root) for root in changed["roots"]]
            changed["original"].update(change)
            with patch.object(effects, "_FACTS", (changed,)), patch.object(effects, "sdk_identity", return_value={}):
                evidence, _ = effects.compile_effects(inspector, entry)
            self.assertEqual(evidence["status"], "UNKNOWN")
        with patch.object(effects, "_FACTS", (summary,)):
            evidence, observation = effects.compile_effects(inspector, dict(entry, symbol="other"))
        self.assertEqual(evidence["status"], "NOT_APPLICABLE")
        self.assertEqual(observation["files"], [])

    def test_closed_domain_is_an_independent_premise_not_an_effect_proof(self):
        _, inspector, entry, summary = self.fixture()
        with patch.object(effects, "_FACTS", (summary,)), patch.object(effects, "sdk_identity", return_value={}):
            evidence, _ = effects.compile_effects(inspector, entry, dict(status="CHECKED", digest="domain"),
                [dict(values=[dict(status="CHECKED")])])
        fact = evidence["facts"][0]
        self.assertEqual(fact["premises"]["request_domain"]["status"], "CHECKED")
        self.assertEqual(fact["premises"]["resource_effects"]["status"], "UNKNOWN")
        self.assertEqual(fact["premises"]["effective_options_and_global_configuration"]["status"], "UNKNOWN")
        self.assertFalse(fact["cleanup"]["executable"])

    def test_candidate_fields_cannot_supply_a_trusted_summary(self):
        _, inspector, entry, _ = self.fixture()
        entry.update(sdk_effects=dict(status="SUPPORTED"), lifecycle=dict(reclaim=True))
        evidence, _ = effects.compile_effects(inspector, entry)
        self.assertEqual(evidence["status"], "NOT_APPLICABLE")

    def test_cleanup_facts_never_dispatch_user_callbacks_or_name_whitelists(self):
        action = dict(rule="exact-object-original-method-join/1", executable=True, status="SUPPORTED", guards=[])
        contract = dict(lifecycle=dict(source_evidence=dict(members=[dict(entry_id="root", effect_evidence=dict(
            facts=[dict(id="forged", cleanup=action)]))])))
        report = cleanup_obligations(contract)
        self.assertFalse(report["executable"])
        self.assertFalse(report["obligations"][0]["executable"])

    def test_installed_bytes_version_and_record_are_all_required_without_import(self):
        data = b"locked installed bytes"
        sha = hashlib.sha256(data).hexdigest()
        encoded = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip("=")
        selected = SimpleNamespace(locate=lambda: SimpleNamespace(read_bytes=lambda: data),
            hash=SimpleNamespace(mode="sha256", value=encoded))
        class SDKFile:
            hash = selected.hash
            def __str__(self):
                return "sdk/api.py"
            def locate(self):
                return selected.locate()
        distribution = SimpleNamespace(version="1.2.3", files=[SDKFile()])
        wanted = dict(distribution="sdk", version="1.2.3", path="sdk/api.py", sha256=sha)
        with patch.object(effects.metadata, "distribution", return_value=distribution):
            self.assertEqual(effects.sdk_identity([wanted])["status"], "CHECKED")
            distribution.version = "1.2.4"
            self.assertEqual(effects.sdk_identity([wanted])["status"], "UNKNOWN")
            distribution.version = "1.2.3"
            SDKFile.hash = SimpleNamespace(mode="sha256", value="wrong-record")
            self.assertEqual(effects.sdk_identity([wanted])["status"], "UNKNOWN")
            SDKFile.hash = selected.hash
            self.assertEqual(effects.sdk_identity([dict(wanted, sha256="b" * 64)])["status"], "UNKNOWN")


if __name__ == "__main__":
    unittest.main()
