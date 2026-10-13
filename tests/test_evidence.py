import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from seampact.evidence import collect_evidence


def report(kind="mmct"):
    if kind == "docker":
        return dict(schema="docker-verification/1", status="PASS",
                    checks=dict(container_build="PASS", core_checks={"status": "PASS"}))
    schemas = {"mmct": "mmct-sdk-probe/1", "crag": "crag-fragment-probe/1",
               "crag-sdk": "crag-sdk-probe/1"}
    sdk = {"mmct": {"autogen-agentchat": "0.7.5", "autogen-core": "0.7.5",
                    "autogen-ext": "0.7.5", "Pillow": "12.3.0"},
           "crag": {}, "crag-sdk": {"llama-index-core": "0.14.18",
                                    "llama-index-workflows": "2.15.0"}}
    return dict(schema=schemas[kind], status="PASS", cases=[{"status": "PASS"}], sdk=sdk[kind])


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(value).encode("utf-8")
    path.write_bytes(raw)
    return raw


class MechanismEvidenceTests(unittest.TestCase):
    def test_api_profile_requires_provenance_all_boundary_coverage_and_negative_control(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            value = dict(schema="api-package-profile/1", status="PASS", cases=[dict(status="PASS")],
                         sdk={"llama-index-core": "0.14.25", "llama-index-workflows": "2.22.2"},
                         profile="crag-real-sdk-controlled-endpoints/2",
                         provenance=dict(status="PASS", origin="API_SOURCE_AGENT"),
                         package=dict(status="PASS", boundaries=2), coverage={"first": 1, "second": 3},
                         negative_control=dict(status="BYPASS_DETECTED"), private="PRIVATE_PROFILE_DATA")
            target = root / "crag-api/crag-api-package-result.json"
            write(target, value)
            index = collect_evidence(root)
            record = next(r for r in index["records"] if r["id"] == "crag-api")
            self.assertEqual((record["status"], record["boundary_count"]), ("PASS", 2))
            self.assertNotIn("PRIVATE_PROFILE_DATA", json.dumps(index))
            self.assertFalse(record["production_admitted"])
            value["coverage"]["first"] = 0
            write(target, value)
            record = next(r for r in collect_evidence(root)["records"] if r["id"] == "crag-api")
            self.assertEqual(record["status"], "INVALID")

    def test_fixed_paths_and_sanitized_projection_never_claim_admission(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            private = "PRIVATE_PROMPT_SECRET_PATH"
            mmct = report()
            mmct.update(scope=private, partition_origin=private, production_admitted=True,
                        logs=[private], controlled_endpoints=[private], live_model_quality="PASS")
            mmct["cases"][0]["native"] = {"prompt": private, "path": private}
            mmct["sdk"][private] = private
            raw = write(root / "mmct/mmct-sdk-result.json", mmct)
            write(root / "crag-fragment-result.json", report("crag"))
            write(root / "crag-sdk/crag-sdk-result.json", report("crag-sdk"))
            write(root / "docker-result.json", report("docker"))
            index = collect_evidence(root)
            self.assertEqual(index["summary"]["PASS"], 4)
            self.assertNotIn(private, json.dumps(index))
            self.assertNotIn(str(root), json.dumps(index))
            self.assertFalse(index["production_admitted"])
            self.assertEqual(index["records"][0]["sha256"], hashlib.sha256(raw).hexdigest())
            for record in index["records"]:
                self.assertFalse(record["production_admitted"])
                self.assertEqual(record["full_source_execution"], "NOT_RUN")
                self.assertEqual(record["live_model_quality"], "NOT_RUN")
            self.assertEqual(index["records"][2]["sdk"]["llama-index-core"], "0.14.18")

    def test_missing_corrupt_and_wrong_schema_reports_cannot_retain_pass(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertEqual(collect_evidence(root)["summary"]["NOT_RUN"], 5)
            target = root / "mmct-sdk-result.json"
            write(target, report())
            self.assertEqual(collect_evidence(root)["records"][0]["status"], "PASS")
            for raw in (b"{broken", b'{"schema":"unknown","status":"PASS"}',
                        b'{"schema":"mmct-sdk-probe/1","status":"PASS","status":"FAIL"}',
                        b'{"schema":"mmct-sdk-probe/1","status":"PASS","private":NaN}'):
                target.write_bytes(raw)
                self.assertEqual(collect_evidence(root)["records"][0]["status"], "INVALID")
            target.unlink()
            self.assertEqual(collect_evidence(root)["records"][0]["status"], "NOT_RUN")

    def test_conflicting_duplicates_and_contradictory_case_status_are_invalid(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write(root / "mmct-sdk-result.json", report())
            write(root / "mmct/mmct-sdk-result.json", report())
            self.assertEqual(collect_evidence(root)["records"][0]["status"], "PASS")
            fail = report()
            fail["status"] = "FAIL"
            write(root / "mmct/mmct-sdk-result.json", fail)
            self.assertEqual(collect_evidence(root)["records"][0]["reason"], "ambiguous_reports")
            (root / "mmct/mmct-sdk-result.json").unlink()
            contradictory = report()
            contradictory["cases"][0]["status"] = "FAIL"
            write(root / "mmct-sdk-result.json", contradictory)
            self.assertEqual(collect_evidence(root)["records"][0]["status"], "INVALID")

    def test_sdk_strings_and_failed_probe_details_are_not_arbitrary_ui_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            private = "PRIVATE_ERROR_PATH"
            write(root / "crag-sdk-result.json", dict(schema="crag-sdk-probe/1", status="FAIL", error=private))
            value = collect_evidence(root)
            self.assertEqual(value["records"][2]["status"], "FAIL")
            self.assertNotIn(private, json.dumps(value))
            invalid = report("crag-sdk")
            invalid["sdk"]["llama-index-core"] = private
            write(root / "crag-sdk-result.json", invalid)
            self.assertEqual(collect_evidence(root)["records"][2]["status"], "INVALID")

    def test_report_or_known_directory_symlink_cannot_escape_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "evidence"
            outside = base / "outside"
            root.mkdir()
            write(outside / "mmct-sdk-result.json", report())
            target = root / "mmct-sdk-result.json"
            try:
                os.symlink(outside / target.name, target)
            except OSError:
                self.skipTest("OS does not permit creating test symlinks")
            self.assertEqual(collect_evidence(root)["records"][0]["reason"], "path_escape")
            target.unlink()
            os.symlink(outside, root / "mmct", target_is_directory=True)
            self.assertEqual(collect_evidence(root)["records"][0]["reason"], "path_escape")
