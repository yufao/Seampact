"""Focused checks for revision declarations; no model or running platform is used."""

import copy
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from seampact.analysis import content_digest
from seampact.revisions import candidate_diff, declared_result, intervention
from seampact.service import Platform
from seampact.store import Store


class RevisionFeedbackChecks(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.directory.name) / "isolated.sqlite")
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(self.store.close)
        self.platform = Platform.__new__(Platform)
        self.platform.store = self.store
        self.platform.source_lock = threading.RLock()
        self.platform._revision_stale = lambda revision: dict(stale=False, stale_reason=None)
        self.store.create("harness", "source", dict(id="source"))

    def revision(self, identity="r", status="DRAFT"):
        candidate = dict(entries=[], runtime_groups=[], label="base")
        return dict(id=identity, harness_id="source", base_analysis_id="base", request_id="request",
            source_version="source-v1", status=status, candidate=candidate,
            candidate_digest=content_digest(candidate), base_candidate=copy.deepcopy(candidate),
            candidate_diff=candidate_diff(candidate, candidate), guidance="", reason="", provenance=[],
            intervention=intervention(), result=declared_result(), result_basis=None,
            report=dict(status="READY"), checked_digest=content_digest(candidate), implementation={})

    def save(self, revision):
        self.store.save("analysis_revision", revision["id"], revision)

    def test_incremental_action_diff_does_not_infer_actor(self):
        revision = self.revision()
        edited = dict(revision["candidate"], label="edited")
        self.platform._save_revision(revision, dict(candidate=edited,
            intervention=dict(actor=dict(kind="external_agent", name="Example agent"))))
        first = revision["provenance"][-1]
        self.assertEqual(first["intervention"]["actor"]["kind"], "external_agent")
        self.assertEqual(first["candidate_snapshot"], edited)
        self.assertEqual(first["before_candidate_snapshot"]["label"], "base")
        self.platform._save_revision(revision, dict(guidance="Check the current candidate"))
        second = revision["provenance"][-1]
        self.assertEqual(second["intervention"]["actor"]["kind"], "unknown")
        self.assertFalse(any(second["candidate_diff"]["counts"].values()))
        self.assertTrue(any(revision["candidate_diff"]["counts"].values()))
        self.assertNotIn("candidate_snapshot", second)

    def test_installed_feedback_preserves_candidate_checks_and_source(self):
        revision = self.revision(status="INSTALLED")
        revision["installed_analysis_id"] = "installed"
        self.save(revision)
        source_before = self.store.get("harness", "source")
        response = self.platform.feedback_revision(dict(revision_id="r",
            intervention=dict(actor=dict(kind="user"), cause="checker_defect", generalization=dict(
                disposition="generalizable", pattern_key="missing-state-edge", target="checker",
                status="verified", version="checker-v2", evidence="Shared reproducer verified")),
            result=dict(status="succeeded", evidence="Independent run record")))["revision"]
        for field in ("candidate", "candidate_digest", "checked_digest", "report", "status"):
            self.assertEqual(response[field], revision[field])
        self.assertEqual(source_before, self.store.get("harness", "source"))
        self.assertEqual(response["result_basis"], dict(candidate_digest=revision["candidate_digest"],
            source_version="source-v1", installed_analysis_id="installed"))
        self.assertEqual(response["provenance"][-1]["kind"], "FEEDBACK")

    def test_candidate_change_invalidates_current_result_but_preserves_declaration(self):
        revision = self.revision()
        self.save(revision)
        self.platform.feedback_revision(dict(revision_id="r", result=dict(status="succeeded", evidence="run-1")))
        revision = self.store.get("analysis_revision", "r")
        self.platform._save_revision(revision, dict(candidate=dict(revision["candidate"], label="new")))
        self.assertEqual(revision["result"]["status"], "unknown")
        self.assertIsNone(revision["result_basis"])
        self.assertEqual(revision["provenance"][0]["result"]["status"], "succeeded")
        self.assertEqual(revision["provenance"][0]["result_basis"]["source_version"], "source-v1")

    def test_model_candidate_records_model_and_invalidates_result(self):
        revision = self.revision()
        revision.update(analysis_id="model-1", result=declared_result(dict(status="succeeded")))
        self.save(revision)
        inspector = SimpleNamespace(request=dict(id="request", source_version="source-v1", rules={"id": "rules"}))
        candidate = dict(revision["candidate"], label="model candidate")
        job = dict(id="model-1", revision_id="r", implementation={"verification.py": "hash"},
            submitted=dict(candidate=candidate, report={"status": "CHECKED"}))
        self.platform._complete_revision(inspector, job)
        saved = self.store.get("analysis_revision", "r")
        event = saved["provenance"][-1]
        self.assertEqual(event["intervention"]["actor"]["kind"], "platform_model")
        self.assertEqual(event["candidate_snapshot"], candidate)
        self.assertEqual(event["check_identity"]["implementation"], job["implementation"])
        self.assertEqual(saved["result"]["status"], "unknown")

    def test_legacy_edit_label_has_unknown_actor_without_rewriting_history(self):
        revision = self.revision()
        revision.pop("intervention")
        revision.pop("result")
        revision["provenance"] = [dict(kind="HUMAN_EDIT", at="legacy")]
        public = self.platform._public_revision(revision)
        self.assertEqual(public["provenance"][0]["intervention"]["actor"]["kind"], "unknown")
        self.assertEqual(public["provenance"][0]["snapshot_status"], "NOT_CAPTURED")
        self.assertEqual(public["intervention"]["generalization"]["disposition"], "needs_review")
        self.assertNotIn("intervention", revision["provenance"][0])

    def test_failed_check_keeps_current_checker_identity(self):
        revision = self.revision()
        revision["candidate"] = {"entries": ["missing runtime group"]}
        revision["candidate_digest"] = content_digest(revision["candidate"])
        inspector = SimpleNamespace(request=dict(id="request", source_version="source-v1", rules={"id": "rules"}))
        self.platform._revision_inspector = lambda current: inspector
        self.platform.analysis = SimpleNamespace(freeze_implementation=lambda current: {"verification.py": "current"})
        with self.assertRaises(ValueError):
            self.platform._check_revision(revision, intervention(dict(actor=dict(kind="user"))))
        saved = self.store.get("analysis_revision", "r")
        self.assertIsNone(saved["checked_digest"])
        event = saved["provenance"][-1]
        self.assertEqual(event["kind"], "CHECK_FAILED")
        self.assertEqual(event["intervention"]["actor"]["kind"], "user")
        self.assertEqual(event["check_identity"]["implementation"]["verification.py"], "current")

    def test_feedback_expected_basis_rejects_mismatch_without_saving(self):
        revision = self.revision(status="INSTALLED")
        revision["installed_analysis_id"] = "installed"
        self.save(revision)
        basis = {key: revision.get(key) for key in
                 ("candidate_digest", "source_version", "installed_analysis_id")}
        before = self.store.get("analysis_revision", "r")
        for field in basis:
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    self.platform.feedback_revision(dict(revision_id="r",
                        expected_basis=dict(basis, **{field: "outdated"}),
                        result=dict(status="succeeded", evidence="old run")))
                self.assertEqual(self.store.get("analysis_revision", "r"), before)
        with self.assertRaises(ValueError):
            self.platform.feedback_revision(dict(revision_id="r", expected_basis={}, result={}))
        self.assertEqual(self.store.get("analysis_revision", "r"), before)
        self.platform._revision_stale = lambda current: dict(stale=True, stale_reason="newer source exists")
        result = self.platform.feedback_revision(dict(revision_id="r", expected_basis=basis,
            result=dict(status="succeeded", evidence="run for the recorded installed version")))["revision"]
        self.assertTrue(result["stale"])
        self.assertEqual(result["result_basis"], basis)
        self.assertEqual(result["status"], "INSTALLED")

    def test_invalid_feedback_does_not_save_partial_updates(self):
        revision = self.revision()
        self.save(revision)
        invalid = [dict(actor=dict(kind="robot")), dict(cause=[]), dict(actor="user"),
            dict(summary="x" * 4001), dict(unexpected="field"),
            dict(generalization=dict(disposition="generalizable")),
            dict(generalization=dict(status="implemented", target="checker", version="v1")),
            dict(generalization=dict(status="verified", target="checker", evidence="proof")),
            dict(generalization=dict(status="verified", version="v1", evidence="proof"))]
        before = self.store.get("analysis_revision", "r")
        for supplied in invalid:
            with self.subTest(supplied=list(supplied)):
                with self.assertRaises(ValueError):
                    self.platform.feedback_revision(dict(revision_id="r", intervention=supplied))
                self.assertEqual(self.store.get("analysis_revision", "r"), before)
        with self.assertRaises(ValueError):
            self.platform.feedback_revision(dict(revision_id="r", result=dict(status="READY")))
        self.assertEqual(self.store.get("analysis_revision", "r"), before)

    def test_pattern_links_do_not_share_resolution_and_summaries_are_compact(self):
        for identity, status in (("one", "open"), ("two", "verified")):
            revision = self.revision(identity)
            revision["intervention"] = intervention(dict(evidence="large detail", generalization=dict(
                disposition="generalizable", pattern_key="shared-pattern", target="rule", status=status,
                version="v1", evidence="explicit declaration")))
            self.save(revision)
        summaries = {row["id"]: row for row in self.platform.revision_summaries()}
        self.assertEqual(summaries["one"]["related_revision_ids"], ["two"])
        self.assertEqual(summaries["one"]["intervention"]["generalization"]["status"], "open")
        self.assertEqual(summaries["two"]["intervention"]["generalization"]["status"], "verified")
        self.assertNotIn("evidence", summaries["one"]["intervention"])
        self.assertNotIn("candidate", summaries["one"])


if __name__ == "__main__":
    unittest.main()
