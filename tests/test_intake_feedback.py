"""Verify source-only classification, checker feedback and isolated tool routing."""

import base64
import copy
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from seampact.analysis import AnalysisJobs
from seampact.inspection import Inspector
from seampact.intake_feedback import entry_readiness
from seampact.preprocessing import check_preprocessing
from seampact.store import Store
from seampact.service import Platform
from seampact.target_runtime import execute
from seampact.units import UnitCatalog


class IntakeFeedbackChecks(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.catalog = UnitCatalog(self.root, self.store, None)
        self.jobs = AnalysisJobs(self.store, None, check_entries=self.catalog.inspect_candidate)

    def freeze(self, source):
        (self.root / "source.py").write_text(source, encoding="utf-8")
        inspector = Inspector.freeze(self.root, self.root / "snapshots", "request", ["source.py"],
                                     "target", "original result")
        inspector.request.update(source_id="source", entry_profile=dict(
            frame_observation_excluded=True, explicit_lifetime=True))
        return inspector

    def candidate(self, symbol="target", kind="function"):
        return dict(boundaries=[dict(id="whole", path="source.py", symbol=symbol, mode="W")],
            entries=[dict(id="target", path="source.py", symbol=symbol, kind=kind, boundary_ids=["whole"])],
            runtime_groups=[dict(id="original", entries=["target"])])

    def test_check_pipeline_keeps_static_and_runtime_separate_without_executing_source(self):
        inspector = self.freeze("raise RuntimeError('must not run')\ndef target(x: str) -> str:\n    return x\n")
        request = AnalysisJobs.source_request(inspector)
        self.assertEqual(request["classification"]["schema"], "harness-classification/1")
        self.assertFalse(request["classification"]["scope"]["whole_repository_coverage"])
        report = self.jobs._check_candidate(inspector, self.candidate())
        self.assertEqual(report["entry_bindings"]["entries"][0]["status"], "READY")
        row = report["entry_readiness"][0]
        self.assertEqual(row["status"], "STATIC_READY")
        self.assertEqual(row["execution"], "NOT_RUN")
        self.assertEqual(row["initialization"], "NOT_RUN")
        self.assertEqual(row["parameters"]["required"], ["x"])
        self.assertFalse(report["classification"]["admission_granted"])
        self.assertFalse(report["boundaries"][0]["operations"]["parallel"]["admitted"])

    def test_preprocessing_handoff_contains_target_classification_and_real_binding_status(self):
        inspector = self.freeze("def target(x: str) -> str:\n    return x\n")
        entry = self.candidate()["entries"][0]
        entry.pop("boundary_ids")
        artifact = dict(schema="source-preparation/1", goal="return original value", entries=[entry],
            targets=[dict(path="source.py", symbol="target", kind="function", purpose="return value",
                execution_role="online", evidence=[dict(path="source.py", start=1, end=2)])],
            resources=[], missing=[])
        report = check_preprocessing(inspector, artifact, self.catalog.inspect_candidate)
        self.assertEqual(report["status"], "SOURCE_REFERENCED")
        self.assertEqual(report["entry_readiness"][0]["status"], "STATIC_READY")
        self.assertFalse(report["split_permission"])
        self.assertEqual(report["classification"]["profiles"][0]["symbol"], "target")

    def test_rejected_split_produces_rule_action_instead_of_weight_selector(self):
        inspector = self.freeze("def target(x: str) -> str:\n    return x\n")
        candidate = self.candidate()
        candidate["boundaries"][0]["mode"] = "R"
        report = self.jobs._check_candidate(inspector, candidate)
        issues = report["entry_readiness"][0]["issues"]
        self.assertIn("unsupported_rule", {row["category"] for row in issues})
        self.assertTrue(all(row["target"] == "revision" for row in issues))

    def test_malformed_entry_remains_diagnostic_instead_of_breaking_whole_report(self):
        inspector = self.freeze("def target(x):\n    return x\n")
        candidate = self.candidate()
        candidate["entries"][0].pop("path")
        report = self.jobs._check_candidate(inspector, candidate)
        self.assertEqual(report["entry_readiness"][0]["status"], "NEEDS_REPAIR")
        self.assertEqual(report["entry_readiness"][0]["issues"][0]["category"], "binding_unresolved")

    def test_resource_selection_cannot_replace_missing_mapping_for_another_entry(self):
        inspector = SimpleNamespace(request=dict(dependency_bindings={}))
        candidate = dict(entries=[dict(id="a"), dict(id="b")])
        dep = dict(id="model", kind="weight", required=True, configurable=True,
            definition_digest="current", entry_ids=["a", "b"], entry_checks={
                "a": dict(status="MAPPING_CHECKED"), "b": dict(status="MISSING", reason="no original mapping")})
        report = dict(dependencies=[dep], entry_bindings=dict(entries=[
            dict(id="a", status="MISSING"), dict(id="b", status="MISSING")]))
        rows = entry_readiness(inspector, candidate, report)
        self.assertEqual(rows[0]["issues"][0]["category"], "resource_binding")
        self.assertEqual(rows[1]["issues"][0]["category"], "parameter_mapping")
        inspector.request["dependency_bindings"] = {"model": dict(definition_digest="obsolete")}
        self.assertEqual(entry_readiness(inspector, candidate, report)[0]["issues"][0]["category"], "resource_binding")
        inspector.request["dependency_bindings"]["model"]["definition_digest"] = "current"
        report["entry_bindings"]["entries"][0]["status"] = "READY"
        rows = entry_readiness(inspector, candidate, report)
        self.assertEqual(rows[0]["status"], "STATIC_READY")
        self.assertEqual(rows[1]["status"], "NEEDS_REPAIR")

    def test_environment_deferrals_keep_the_original_reason_without_claiming_a_load(self):
        inspector = SimpleNamespace(request={})
        report = dict(entry_bindings=dict(entries=[dict(id="target", status="MISSING",
            host_condition=dict(gaps=["native dependency is not installed in this execution environment: missing_lib"]))]))
        row = entry_readiness(inspector, self.candidate(), report)[0]
        self.assertEqual(row["issues"][0]["category"], "runtime_environment")
        self.assertIn("missing_lib", row["issues"][0]["reason"])
        self.assertEqual(row["execution"], "NOT_RUN")

    def test_target_interpreter_dispatches_new_tools_without_loading_original_module(self):
        inspector = self.freeze("raise RuntimeError('never import')\ndef target(x):\n    return x\n")
        inspector.request["runtime_environment"] = dict(python=".".join(map(str, sys.version_info[:2])))
        payload = dict(schema="target-source-tools/1", request=copy.deepcopy(inspector.request),
            files={path: base64.b64encode(inspector.data(path)).decode("ascii") for path in inspector.files})
        page = execute(dict(payload, method="inspect_harness", arguments=dict(count=1)))
        self.assertEqual(page["schema"], "harness-classification/1")
        target = execute(dict(payload, method="classify_target", arguments=dict(target=self.candidate()["entries"][0])))
        self.assertEqual(target["symbol"], "target")
        self.assertEqual(target["execution"], "NOT_RUN")

    def test_zero_model_reconnection_generates_the_same_feedback_without_inventing_messages(self):
        inspector = self.freeze("def target(x: str) -> str:\n    return x\n")
        platform = Platform.__new__(Platform)
        platform.store, platform.source_lock = self.store, threading.RLock()
        platform.analysis, platform.catalog = self.jobs, self.catalog
        platform.dependencies = SimpleNamespace(record=lambda *_: None)
        self.store.create("harness", "source", dict(id="source", status="ANALYZED", analysis_id="recheck"))
        job = dict(id="recheck", status="RUNNING", model_calls=0, messages=[],
                   submitted=dict(candidate=self.candidate()))
        self.store.create("analysis", job["id"], job)
        installed = platform._connect_units(inspector, job)
        self.assertEqual(len(installed), 1)
        saved = self.store.get("analysis", job["id"])
        self.assertEqual(saved["messages"], [])
        self.assertEqual(saved["model_calls"], 0)
        self.assertEqual(saved["submitted"]["report"]["entry_readiness"][0]["status"], "STATIC_READY")

    def test_agent_consumes_classification_and_persists_tool_feedback_through_submission(self):
        inspector = self.freeze("def target(x: str) -> str:\n    return x\n")
        sent = []
        def send(url, key, body):
            sent.append(copy.deepcopy(body))
            name, args = (("inspect_harness", dict(path="source.py", count=1)) if len(sent) == 1 else
                          ("submit_candidate", dict(candidate=self.candidate())))
            return dict(choices=[dict(finish_reason="tool_calls", message=dict(role="assistant", content=None,
                tool_calls=[dict(id="call-" + str(len(sent)), type="function", function=dict(
                    name=name, arguments=json.dumps(args)))]))])
        self.jobs.transport = SimpleNamespace(send=send)
        job = dict(id="analysis", status="RUNNING", request_id="request", rounds=0, usage=[],
            pipeline_stage="analysis", usage_by_stage=dict(analysis=[], preprocessing=[]),
            messages=[dict(role="system", content="test"), dict(role="user", content=json.dumps(
                AnalysisJobs.source_request(inspector)))])
        self.jobs._run(inspector, dict(provider="test", model="test", base_url="https://example.invalid"),
                       None, job, threading.Event())
        self.assertEqual(job["status"], "COMPLETED", job.get("error"))
        self.assertEqual(len(sent), 2)
        self.assertIn("classification", json.loads(sent[0]["messages"][1]["content"]))
        self.assertIn("inspect_harness", {row["function"]["name"] for row in sent[0]["tools"]})
        reply = sent[1]["messages"][-1]
        self.assertEqual(reply["role"], "tool")
        self.assertEqual(json.loads(reply["content"])["schema"], "harness-classification/1")
        self.assertEqual(job["submitted"]["report"]["entry_readiness"][0]["execution"], "NOT_RUN")


if __name__ == "__main__":
    unittest.main()
