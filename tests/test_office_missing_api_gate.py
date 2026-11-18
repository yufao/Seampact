import io
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import run_office_pilot as runner


class MissingApiGateTests(unittest.TestCase):
    def run_case(self, gaps=(), flag=True, weight=1, attach=False, calibration=False,
                 native_scores=False, block_size=2, grader_fails=False, deadline_after_start=False):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cases = ["case%d" % i for i in range(4)]
            ids = ["task%d" % i for i in range(4)]
            plan = {"schema": "seampact.office-comparison/1", "training_calibration": {},
                    "continuous_training": True, "development_evidence": {"present": True, "pool": {}},
                    "cases": {"training": cases}, "policies": {"app_config": {}},
                    "scheduler": {"objective": {"weights": {"api": weight}}}}
            if calibration:
                plan.update(training_calibration={"case_ids": cases},
                    cases={"training": cases, "development": [], "evaluation": []},
                    objective_weights={"api": weight}, resource_source="measured", deadline_s=None)
                plan["scheduler"]["objective"] = None
                if native_scores:
                    plan["source_manifests"] = {"spreadsheet": {}}
            protocol, output = root / "protocol.json", root / "out"
            protocol.write_text(json.dumps(plan), encoding="utf-8")
            row = {"id": "run", "case_ids": cases, "task_ids": ids, "admitted": [],
                   "status": "RUNNING", "start_index": 0,
                   "arrival_blocks": [{"indices": list(range(i, i+block_size)), "offsets_ms": [0]*block_size, "profile": "test"}
                                      for i in range(0, 4, block_size)]}
            if attach:
                row.update(protocol_sha256=runner.digest(protocol), mode="training", batch=0,
                           admitted=ids[:2], workload={"id": "run", "task_ids": ids,
                           "training": True, "continuous": True, "session": "session",
                           "policy": "CandidatePPO", "policy_config_id": "policy", "pool": {}})
            tasks = [{"id": ids[i], "status": "COMPLETED", "workspace_status": "FINISHED",
                      "objective_result": {"quality": 1, "session": "session", "config_id": "config",
                                           "metrics": {"api_tokens": None if i in gaps else 0}},
                      "api_usage": {"observed_tokens": 10, "unknown_requests": int(i in gaps),
                                    "unreported_capability_calls": 0}} for i in range(4)]
            if native_scores:
                for task in tasks:
                    task["objective_result"]["quality"] = None
                    task["evaluation"] = {"source_revision": "fixture", "outputs": []}
            calls, lock = [], threading.Lock()

            def request(req, timeout):
                path = req.full_url.split("http://127.0.0.1:8880", 1)[1]
                body = json.loads(req.data) if req.data else {}
                with lock:
                    if path == "/api/state":
                        result = {"models": {}, "capability_packages": [], "code_execution": {},
                                  "runtime_groups": [], "scheduler": {"execution": {
                                  "session": "session", "config": {}, "config_id": "config",
                                  "policy": "CandidatePPO", "policy_config_id": "policy",
                                  "objective": {"denominator": 2, "raw": {"finished": 2, "quality_known": 2}}}}}
                    elif path.startswith("/api/tasks/"):
                        result = tasks[ids.index(path.rsplit("/", 1)[1])]
                    elif path == "/api/experiments/office/spreadsheet/score":
                        index = ids.index(body["task_id"])
                        calls.append("score" + str(index))
                        tasks[index]["objective_result"]["quality"] = 0
                        result = {"quality": 0}
                    else:
                        op = body["op"]
                        calls.append(op)
                        if op in ("start", "resume", "record"):
                            result = row
                        elif op == "admit":
                            index = len(row["admitted"])
                            row["admitted"].append(ids[index])
                            result = {"task_id": ids[index], "case_id": cases[index]}
                        elif op == "end_prefix":
                            result = dict(row, status="PARTIAL" if calibration else "UNSETTLED", error="missing API")
                        elif op == "abort":
                            calls.append(body["reason"])
                            result = dict(row, status="PARTIAL" if deadline_after_start else "UNSETTLED")
                        elif op == "drain":
                            result = dict(row, status="AWAITING_SCORING")
                        elif op == "finish":
                            summary = {"denominator": 4, "raw": {"arrivals": 4, "latency_s": 4,
                                "api_tokens": None if gaps else 40}, "resources": {
                                    kind: {"ram_byte_s": 4, "vram_byte_s": 4} for kind in ("measured", "reserved")}}
                            result = dict(row, status="UNSETTLED" if gaps and not calibration else "COMPLETE", error="missing API",
                                          previous_segments=[], checkpoint={}, application={}, workload={"summary": summary, "pool": {}})
                        else:
                            raise AssertionError(op)
                return io.BytesIO(json.dumps(result).encode())

            def grade(command, **kwargs):
                folder = Path(command[-1])
                task = json.loads((folder/"task.json").read_text(encoding="utf-8"))
                index = ids.index(task["id"])
                calls.append("grade" + str(index))
                if grader_fails:
                    raise RuntimeError("fixture grader failed")
                (folder/"native-score.json").write_text(json.dumps({"quality": 0}), encoding="utf-8")
            def feedback(folder):
                task = json.loads((Path(folder)/"task.json").read_text(encoding="utf-8"))
                return {"task_id": task["id"]}
            argv = ["runner", str(protocol), str(output)]
            if calibration:
                argv.append("--calibration-only")
            else:
                argv += ["--training-only"]
                argv += ["--attach-training", "run"] if attach else ["--resume-training"]
            if native_scores:
                argv += ["--grader-command", json.dumps(["fixture-grader", "{collection}"])]
            if flag:
                argv.append("--stop-on-missing-api-usage")
            if deadline_after_start:
                argv += ["--admission-deadline", "100"]
            def wall_time():
                return 101 if deadline_after_start and "start" in calls else 1
            with patch.object(sys, "argv", argv), patch.object(runner, "urlopen", request), \
                    patch("seampact.scheduling.capability_scheduler.freeze_config", return_value={}), \
                    patch.object(runner.subprocess, "run", side_effect=grade), \
                    patch("run_office_spreadsheet.collect"), \
                    patch("run_office_spreadsheet.build_feedback", side_effect=feedback), patch("builtins.print"), \
                    patch.object(runner.time, "time", side_effect=wall_time):
                try:
                    runner.main()
                    error = None
                except (RuntimeError, SystemExit) as exc:
                    error = str(exc)
            receipt_paths = list(output.glob("**/*-0-missing-api-usage.json"))
            receipt_path = receipt_paths[0] if receipt_paths else output / "absent"
            receipt = json.loads(receipt_path.read_text(encoding="utf-8")) if receipt_path.exists() else None
            return calls, error, receipt

    def test_prefix_stops_only_after_whole_block(self):
        calls, error, receipt = self.run_case(gaps=(0,))
        self.assertEqual(calls.count("admit"), 2)
        self.assertEqual(calls[-1], "end_prefix")
        self.assertIn("accounting is missing", error)
        self.assertEqual((receipt["status"], receipt["next_index"]), ("UNSETTLED", 2))
        self.assertEqual([c["case_id"] for c in receipt["affected_cases"]], ["case0"])

    def test_final_block_finishes_with_concrete_gap(self):
        calls, error, receipt = self.run_case(gaps=(3,))
        self.assertEqual(calls[-2:], ["drain", "finish"])
        self.assertEqual(receipt["next_index"], 4)
        self.assertEqual(receipt["affected_cases"][0]["case_id"], "case3")
        self.assertIn("accounting is missing", error)

    def test_opt_in_and_positive_weight_required(self):
        for flag, weight in ((False, 1), (True, 0)):
            with self.subTest(flag=flag, weight=weight):
                calls, _, receipt = self.run_case(gaps=(0,), flag=flag, weight=weight)
                self.assertEqual(calls.count("admit"), 4)
                self.assertIsNone(receipt)

    def test_complete_accounting_allows_full_run(self):
        calls, error, receipt = self.run_case()
        self.assertEqual(calls.count("admit"), 4)
        self.assertIsNone(error)
        self.assertIsNone(receipt)

    def test_unfinalized_quality_is_not_misreported_as_missing_api(self):
        with self.assertRaisesRegex(RuntimeError, "no finalized objective ledger"):
            runner.missing_api_usage([{"id": "t", "status": "COMPLETED", "workspace_status": "FINISHED",
                "objective_result": {"quality": None, "metrics": {"api_tokens": None}}}], ["c"])

    def test_attach_checks_settled_prefix_before_new_admissions(self):
        calls, error, receipt = self.run_case(gaps=(0,), attach=True)
        self.assertNotIn("admit", calls)
        self.assertEqual(calls[-1], "end_prefix")
        self.assertEqual((receipt["status"], receipt["next_index"]), ("UNSETTLED", 2))
        self.assertIn("accounting is missing", error)

    def test_calibration_null_objective_uses_planned_api_weight_and_stops_at_complete_block(self):
        calls, error, receipt = self.run_case(gaps=(0,), calibration=True, native_scores=True, weight=.2)
        self.assertEqual(calls.count("admit"), 2)
        self.assertEqual(calls[-5:], ["grade0", "score0", "grade1", "score1", "end_prefix"])
        self.assertEqual((receipt["status"], receipt["next_index"]), ("PARTIAL", 2))
        self.assertIn("Calibration stopped", error)

    def test_serial_calibration_grades_first_task_before_rejecting_next_admission(self):
        calls, error, receipt = self.run_case(gaps=(0,), calibration=True, native_scores=True, block_size=1)
        self.assertEqual(calls, ["start", "admit", "grade0", "score0", "end_prefix"])
        self.assertEqual(receipt["next_index"], 1)
        self.assertIn("accounting is missing", error)

    def test_complete_calibration_grades_each_block_before_next_arrival_and_final_accounting(self):
        calls, error, receipt = self.run_case(calibration=True, native_scores=True)
        self.assertEqual(calls, ["start", "admit", "admit", "grade0", "score0", "grade1", "score1",
                                 "admit", "admit", "grade2", "score2", "grade3", "score3", "drain", "finish"])
        self.assertIsNone(error)
        self.assertIsNone(receipt)

    def test_calibration_gate_remains_opt_in_and_requires_positive_weight(self):
        for flag, weight in ((False, .2), (True, 0)):
            with self.subTest(flag=flag, weight=weight):
                calls, _, receipt = self.run_case(gaps=(0,), calibration=True, native_scores=True, flag=flag, weight=weight)
                self.assertEqual(calls[:5], ["start", "admit", "admit", "admit", "admit"])
                self.assertIsNone(receipt)

    def test_final_calibration_block_retains_gap_without_claiming_a_partial_training_run(self):
        calls, error, receipt = self.run_case(gaps=(3,), calibration=True, native_scores=True)
        self.assertEqual(calls.count("admit"), 4)
        self.assertEqual(calls[-2:], ["drain", "finish"])
        self.assertEqual((receipt["status"], receipt["next_index"]), ("COMPLETE", 4))
        self.assertEqual(receipt["affected_cases"][0]["case_id"], "case3")
        self.assertIn("Calibration stopped", error)

    def test_grading_failure_records_abort_before_more_calibration_admissions(self):
        calls, error, _ = self.run_case(calibration=True, native_scores=True, grader_fails=True)
        self.assertEqual(calls, ["start", "admit", "admit", "grade0", "abort", "driver_error"])
        self.assertEqual(error, "fixture grader failed")

    def test_deadline_after_platform_start_closes_the_empty_calibration(self):
        calls, error, _ = self.run_case(calibration=True, deadline_after_start=True)
        self.assertEqual(calls, ["start", "abort", "admission_deadline"])
        self.assertEqual(error, "75")


if __name__ == "__main__":
    unittest.main()
