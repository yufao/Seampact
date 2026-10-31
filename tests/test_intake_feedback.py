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
from seampact.intake_feedback import entry_readiness, candidate_feedback
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
        entry["task_role"] = dict(role="evidence", purpose="return source value as evidence",
                                  evidence=[dict(path="source.py", start=1, end=2)])
        artifact = dict(schema="source-preparation/1", goal="return original value", entries=[entry],
            targets=[dict(path="source.py", symbol="target", kind="function", purpose="return value",
                execution_role="online", evidence=[dict(path="source.py", start=1, end=2)],
                task_role=copy.deepcopy(entry["task_role"]))],
            resources=[], missing=[])
        report = check_preprocessing(inspector, artifact, self.catalog.inspect_candidate)
        self.assertEqual(report["status"], "SOURCE_REFERENCED")
        self.assertEqual(report["entry_readiness"][0]["status"], "STATIC_READY")
        self.assertFalse(report["split_permission"])
        self.assertEqual(report["classification"]["profiles"][0]["symbol"], "target")
        self.assertEqual(report["entry_readiness"][0]["task_role"]["role"], "evidence")
        self.assertEqual(report["targets"][0]["task_role"]["semantic_claims"], "NOT_INDEPENDENTLY_PROVED")

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
        issue = report["entry_readiness"][0]["issues"][0]
        self.assertEqual(issue["issue_code"], "candidate_incomplete")
        self.assertEqual(issue["field_paths"], ["entries[0].path"])

    def test_resource_selection_cannot_replace_missing_mapping_for_another_entry(self):
        inspector = SimpleNamespace(request=dict(dependency_bindings={}))
        candidate = dict(entries=[dict(id=identity, path="source.py", symbol="target", kind="function")
                                  for identity in ("a", "b")])
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
        candidate = self.candidate()
        candidate["entries"][0]["boundary_ids"] = []
        row = entry_readiness(inspector, candidate, report)[0]
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

    def test_structured_task_roles_remain_unproved_and_unknown_never_filters_entry(self):
        inspector = self.freeze("raise RuntimeError('must not execute')\ndef target(x: str) -> str:\n    return x\n")
        for role in ("target_defense", "evidence", "host", "support", "display", "mock", "offline", "unknown"):
            with self.subTest(role=role):
                candidate = self.candidate()
                candidate["entries"][0]["task_role"] = dict(role=role, purpose="source-scoped purpose",
                    evidence=[dict(path="source.py", start=2, end=3)])
                report = self.jobs._check_candidate(inspector, candidate)
                claim = report["entry_readiness"][0]["task_role"]
                self.assertEqual(claim["status"], "SOURCE_REFERENCED")
                self.assertEqual(claim["semantic_claims"], "NOT_INDEPENDENTLY_PROVED")
                self.assertEqual(claim["role"], role)
                self.assertEqual(report["entry_readiness"][0]["status"], "STATIC_READY")
                self.assertEqual(report["classification"]["profiles"][0]["task_role"], claim)
                self.assertEqual(report["outcome"], "STATIC_CHECKED_NOT_RUN")

    def test_task_role_evidence_failure_is_actionable_without_granting_semantics(self):
        inspector = self.freeze("# unrelated\ndef target(x):\n    return x\n")
        candidate = self.candidate()
        candidate["entries"][0]["task_role"] = dict(role="target_defense", purpose="claimed defense",
            evidence=[dict(path="source.py", start=1, end=1)])
        report = self.jobs._check_candidate(inspector, candidate)
        issue = report["feedback"]["model_repairs"][0]
        self.assertEqual(issue["issue_code"], "evidence_incomplete")
        self.assertEqual(issue["field_paths"], ["entries[0].task_role"])
        self.assertEqual(report["outcome"], "NEEDS_CANDIDATE_REPAIR")
        self.assertEqual(report["entry_readiness"][0]["task_role"]["semantic_claims"], "NOT_INDEPENDENTLY_PROVED")

    def test_optional_requirement_is_checked_when_entry_actually_references_it(self):
        inspector = SimpleNamespace(request=dict(dependency_bindings={}))
        candidate = self.candidate()
        entry = candidate["entries"][0]
        entry["boundary_ids"] = []
        entry["fixed_arguments"] = dict(model={"$dependency": dict(id="model", field="path")})
        dep = dict(id="model", kind="weight", required=False, configurable=True,
            definition_digest="current", entry_ids=[], entry_checks={"target": dict(status="MAPPING_CHECKED")})
        report = dict(dependencies=[dep], entry_bindings=dict(entries=[dict(id="target", status="READY")]))
        rows = entry_readiness(inspector, candidate, report)
        self.assertEqual(rows[0]["issues"][0]["issue_code"], "resource_not_selected")
        report["entry_readiness"] = rows
        self.assertEqual(candidate_feedback(candidate, report)["outcome"], "AWAITING_EXTERNAL_CONDITIONS")
        entry["fixed_arguments"] = {}
        self.assertEqual(entry_readiness(inspector, candidate, report)[0]["status"], "STATIC_READY")

    def test_feedback_uses_typed_mapping_codes_and_keeps_multiple_repairs(self):
        inspector = SimpleNamespace(request={})
        candidate = self.candidate()
        candidate["entries"][0]["boundary_ids"] = []
        codes = [("candidate_incomplete", "candidate"), ("evidence_incomplete", "evidence"),
                 ("parameter_flow_unsupported", "rule"), ("resource_locator_unsupported", "rule")]
        dep = dict(id="model", required=True, entry_ids=["target"], entry_checks={"target":
            dict(status="MISSING", issues=[dict(issue_code=code, reason="weight service not installed",
                repair_action="specific repair " + code, field_paths=["dependencies[0].mappings"])
                for code, _ in codes])})
        report = dict(dependencies=[dep], entry_bindings=dict(entries=[dict(id="target", status="MISSING")]))
        rows = entry_readiness(inspector, candidate, report)
        self.assertEqual([issue["repair_scope"] for issue in rows[0]["issues"]], [scope for _, scope in codes])
        self.assertEqual([issue["issue_code"] for issue in rows[0]["issues"]], [code for code, _ in codes])
        report["entry_readiness"] = rows
        feedback = candidate_feedback(candidate, report)
        self.assertEqual(len(feedback["model_repairs"]), 2)
        self.assertEqual(len(feedback["external_conditions"]), 2)
        self.assertTrue(all(issue["field_paths"] == ["dependencies[0].mappings"]
                            for issue in feedback["model_repairs"]))

    def test_real_dependency_checker_codes_reach_model_feedback_with_full_paths(self):
        inspector = self.freeze("from openai import OpenAI\ndef target(url):\n    return OpenAI(base_url=url)\n")
        candidate = self.candidate()
        candidate["entries"][0]["fixed_arguments"] = dict(url={"$dependency": dict(id="api", field="base_url")})
        candidate["dependencies"] = [dict(id="api", kind="service", required=True, configurable=True,
            entry_ids=["target"], read_time="call", owner_scope="request",
            evidence=[dict(path="source.py", start=1, end=3)], mappings=[])]
        report = self.jobs._check_candidate(inspector, candidate)
        issue = report["feedback"]["model_repairs"][0]
        self.assertEqual(issue["issue_code"], "candidate_incomplete")
        self.assertEqual(issue["field_paths"], ["dependencies[0].mappings"])
        candidate["dependencies"][0]["mappings"] = [dict(entry_id="target", location="entry.fixed_arguments",
            parameter="url", field="base_url")]
        report = self.jobs._check_candidate(inspector, candidate)
        self.assertEqual(report["feedback"]["model_repairs"], [])
        self.assertEqual(report["feedback"]["external_conditions"][0]["issue_code"], "resource_not_selected")
        candidate["dependencies"][0]["evidence"] = []
        report = self.jobs._check_candidate(inspector, candidate)
        self.assertEqual(report["feedback"]["model_repairs"][0]["issue_code"], "evidence_incomplete")

    def test_unknown_binding_failure_is_not_guessed_as_resource_or_model_repair(self):
        inspector = SimpleNamespace(request={})
        candidate = self.candidate()
        candidate["entries"][0]["boundary_ids"] = []
        report = dict(entry_bindings=dict(entries=[dict(id="target", status="MISSING", reason="weight model missing")]))
        rows = entry_readiness(inspector, candidate, report)
        self.assertEqual(rows[0]["issues"][0]["issue_code"], "initialization_unverified")
        self.assertFalse(rows[0]["issues"][0]["model_repairable"])

    def test_typed_post_constructor_initialization_gap_points_to_original_prefix(self):
        inspector = SimpleNamespace(request={})
        candidate = self.candidate()
        candidate["entries"][0]["boundary_ids"] = []
        gap = dict(reason="original initialization not preserved", construction=dict(path="source.py", start=5, end=5),
                   operations=[dict(path="source.py", start=6, end=6, method="eval")])
        report = dict(entry_bindings=dict(entries=[dict(id="target", status="MISSING",
            code="initialization_incomplete", initialization_gaps=[gap])]))
        row = entry_readiness(inspector, candidate, report)[0]
        issue = row["issues"][0]
        self.assertEqual(issue["issue_code"], "initialization_incomplete")
        self.assertTrue(issue["model_repairable"])
        self.assertIn("entries[0].binding_program", issue["field_paths"])
        self.assertEqual(issue["source_evidence"], gap)

    def test_unreferenced_unsupported_boundary_is_in_final_outcome(self):
        inspector = self.freeze("def target(x: str) -> str:\n    return x\n")
        candidate = self.candidate()
        candidate["entries"][0]["boundary_ids"] = []
        candidate["boundaries"][0]["mode"] = "R"
        report = self.jobs._check_candidate(inspector, candidate)
        self.assertEqual(report["outcome"], "AWAITING_EXTERNAL_CONDITIONS")
        self.assertEqual(report["feedback"]["external_conditions"][0]["repair_scope"], "rule")

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
        self.assertEqual(job["outcome"], "STATIC_CHECKED_NOT_RUN")

    def test_simulated_model_repairs_specific_evidence_field_then_submits(self):
        inspector = self.freeze("# context\ndef target(x: str) -> str:\n    return x\n")
        candidate = self.candidate()
        candidate["entries"][0]["task_role"] = dict(role="unknown", purpose="inspect current request",
            evidence=[dict(path="source.py", start=1, end=1)])
        sent = []
        def send(url, key, body):
            sent.append(copy.deepcopy(body))
            if len(sent) == 1:
                name, args = "check_candidate", dict(candidate=copy.deepcopy(candidate))
            elif len(sent) == 2:
                feedback = json.loads(body["messages"][-1]["content"])["feedback"]
                self.assertEqual(feedback["model_repairs"][0]["field_paths"], ["entries[0].task_role"])
                self.assertEqual(feedback["model_repairs"][0]["issue_code"], "evidence_incomplete")
                name, args = "read_file", dict(path="source.py", start=2, count=2)
            else:
                candidate["entries"][0]["task_role"]["evidence"] = [dict(path="source.py", start=2, end=3)]
                name, args = "submit_candidate", dict(candidate=candidate)
            return dict(choices=[dict(finish_reason="tool_calls", message=dict(role="assistant", content=None,
                tool_calls=[dict(id="repair-" + str(len(sent)), type="function", function=dict(
                    name=name, arguments=json.dumps(args)))]))])
        self.jobs.transport = SimpleNamespace(send=send)
        job = dict(id="repair-test", status="RUNNING", request_id="request", rounds=0, usage=[],
            pipeline_stage="analysis", usage_by_stage=dict(analysis=[], preprocessing=[]),
            messages=[dict(role="system", content="test"), dict(role="user", content="test")])
        self.jobs._run(inspector, dict(provider="test", model="test", base_url="https://example.invalid"),
                       None, job, threading.Event())
        self.assertEqual(job["status"], "COMPLETED", job.get("error"))
        self.assertEqual(len(sent), 3)
        self.assertEqual(job["outcome"], "STATIC_CHECKED_NOT_RUN")
        self.assertEqual(job["submitted"]["report"]["feedback"]["model_repairs"], [])
        self.assertEqual(job["execution"], "NOT_RUN")

    def test_completed_external_deferral_does_not_create_implicit_retry(self):
        inspector = self.freeze("def target(x: str) -> str:\n    return x\n")
        self.jobs.check_entries = lambda *_: dict(entries=[dict(id="target", status="MISSING",
            host_condition=dict(gaps=["worker dependency unavailable"]))])
        sent = []
        def send(url, key, body):
            sent.append(body)
            return dict(choices=[dict(finish_reason="tool_calls", message=dict(role="assistant", content=None,
                tool_calls=[dict(id="deferred", type="function", function=dict(name="submit_candidate",
                    arguments=json.dumps(dict(candidate=self.candidate()))))]))])
        self.jobs.transport = SimpleNamespace(send=send)
        job = dict(id="deferred-test", status="RUNNING", request_id="request", rounds=0, usage=[],
            pipeline_stage="analysis", usage_by_stage=dict(analysis=[], preprocessing=[]),
            messages=[dict(role="system", content="test"), dict(role="user", content="test")])
        self.jobs._run(inspector, dict(provider="test", model="test", base_url="https://example.invalid"),
                       None, job, threading.Event())
        self.assertEqual(job["status"], "COMPLETED", job.get("error"))
        self.assertEqual(job["outcome"], "AWAITING_EXTERNAL_CONDITIONS")
        self.assertEqual(len(sent), 1)
        self.assertEqual(job["execution"], "NOT_RUN")


if __name__ == "__main__":
    unittest.main()
