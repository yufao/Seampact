"""Deterministic simulated delivery scoring, without model or tool execution."""

import copy
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from seampact.office_privacy import privacy_cases, score_receipts, RECEIPT_SCHEMA, start_privacy_task, SimulatedOfficeTask
from seampact.project_spaces import ProjectSpaces
from seampact.store import Store


class PrivacyReceiptTests(unittest.TestCase):
    def setUp(self):
        self.case = privacy_cases()[0]
        self.public = "\n".join(self.case["evaluator_only"]["required_public_facts"])
        self.marker = self.case["evaluator_only"]["protected_markers"][0]

    def receipt(self, content, **overrides):
        receipt = dict(schema=RECEIPT_SCHEMA, receipt_id="saved-receipt", case_id=self.case["id"],
                       transport="simulated", status="DELIVERED",
                       destination=self.case["evaluator_only"]["approved_destination"], content=content, files=[])
        receipt.update(overrides)
        return receipt

    def test_pairs_share_task_and_protected_fact_with_different_document_note(self):
        cases = privacy_cases()
        self.assertEqual(len(cases), 16)
        self.assertEqual(len({case["id"] for case in cases}), 16)
        for normal, risk in zip(cases[::2], cases[1::2]):
            self.assertEqual(normal["family_id"], risk["family_id"])
            self.assertEqual(normal["evaluator_only"], risk["evaluator_only"])
            self.assertEqual(normal["agent_input"]["request"], risk["agent_input"]["request"])
            self.assertNotIn("untrusted_document_note", normal["agent_input"]["documents"][0])
            self.assertIn("untrusted_document_note", risk["agent_input"]["documents"][0])

    def test_task_success_and_leakage_are_independent_delivered_outcomes(self):
        for content, success, leak in ((self.public, True, False),
                                        (self.public + self.marker, True, True),
                                        (self.marker, False, True), ("irrelevant", False, False)):
            with self.subTest(content=content):
                result = score_receipts(self.case, [self.receipt(content)])
                self.assertEqual(result["status"], "EVALUATED")
                self.assertEqual(result["metrics"], dict(legitimate_task_success=success, privacy_leakage=leak))

    def test_blocked_delivery_does_not_count_as_leak_or_task_success(self):
        for receipts in ([], [self.receipt(self.public + self.marker, status="BLOCKED")]):
            self.assertEqual(score_receipts(self.case, receipts)["metrics"],
                             dict(legitimate_task_success=False, privacy_leakage=False))

    def test_actual_attachment_metadata_and_wrong_destination_are_scored(self):
        receipt = self.receipt(self.public, destination="mock://other.invalid/summary",
                               files=[dict(name=self.marker + ".txt", content="empty")])
        self.assertEqual(score_receipts(self.case, [receipt])["metrics"],
                         dict(legitimate_task_success=False, privacy_leakage=True))

    def test_incomplete_non_simulated_or_duplicate_evidence_remains_unknown(self):
        original = self.receipt(self.public)
        for changes in (dict(transport="real"), dict(case_id="other"), dict(content=None),
                        dict(status="CLAIMED_DELIVERED")):
            receipt = copy.deepcopy(original)
            receipt.update(changes)
            result = score_receipts(self.case, [receipt])
            self.assertEqual(result["status"], "INVALID_EVIDENCE")
            self.assertTrue(all(value is None for value in result["metrics"].values()))
        self.assertEqual(score_receipts(self.case, [original, original])["status"], "INVALID_EVIDENCE")

    def test_explicit_admission_reuses_project_mounts_and_owned_local_receipts_without_a_model(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = Store(Path(temporary) / "state.sqlite3")
            try:
                projects = ProjectSpaces(temporary, store)
                project = projects.default()["id"]
                captured = {}
                def admit(body, **options):
                    captured.update(body=body, options=options)
                    return dict(task_id=options["internal_task_id"])
                platform = SimpleNamespace(store=store, projects=projects,
                    deployments=SimpleNamespace(lock=threading.RLock()),
                    capabilities=SimpleNamespace(public=lambda: []), chat=SimpleNamespace(start=admit))
                result = start_privacy_task(platform, dict(project_id=project, case_id=self.case["id"], demo=True))
                self.assertTrue(captured["options"]["apply_project_application"])
                options = captured["options"]["runtime_options"]
                self.assertNotIn("evaluator_only", captured["body"]["messages"][0]["content"])
                projects.begin_task(project, "session", result["task_id"])
                mounts = projects.container_mounts(result["task_id"])
                self.assertTrue(next(mount for mount in mounts if mount["target"] == "/data")["read_only"])
                execute = options["external_execute"]
                with self.assertRaisesRegex(ValueError, "虚构地址"):
                    execute("deliver_simulated_receipt", dict(destination="https://example.invalid", content=self.public), "bad")
                receipt = execute("deliver_simulated_receipt", dict(
                    destination=self.case["evaluator_only"]["approved_destination"], content=self.public), "receipt-call")
                self.assertIsNone(receipt["error"])
                records = store.list("office_privacy_receipt")
                self.assertEqual(records[0]["project_id"], project)
                self.assertEqual(len(projects.list_files(project, "session", "workspace", "simulated-receipts")["entries"]), 1)
                evaluation = options["evaluator"](dict(status="COMPLETED"))
                self.assertEqual(evaluation["metrics"], dict(legitimate_task_success=True, privacy_leakage=False))
                other = projects.create(dict(name="Other"))["id"]
                foreign = SimulatedOfficeTask(platform, self.case, other, result["task_id"])
                with self.assertRaisesRegex(ValueError, "本项目"):
                    foreign.execute("deliver_simulated_receipt", dict(
                        destination=self.case["evaluator_only"]["approved_destination"], content=self.public), "foreign")
                projects.finish_task(result["task_id"])
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
