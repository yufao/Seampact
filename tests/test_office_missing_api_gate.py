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
    def run_case(self, gaps=(), flag=True, weight=1, attach=False):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cases = ["case%d" % i for i in range(4)]
            ids = ["task%d" % i for i in range(4)]
            plan = {"schema": "seampact.office-comparison/1", "training_calibration": {},
                    "continuous_training": True, "development_evidence": {"present": True, "pool": {}},
                    "cases": {"training": cases}, "policies": {"app_config": {}},
                    "scheduler": {"objective": {"weights": {"api": weight}}}}
            protocol, output = root / "protocol.json", root / "out"
            protocol.write_text(json.dumps(plan), encoding="utf-8")
            row = {"id": "run", "case_ids": cases, "task_ids": ids, "admitted": [],
                   "status": "RUNNING", "start_index": 0,
                   "arrival_blocks": [{"indices": list(b), "offsets_ms": [0, 0], "profile": "test"}
                                      for b in ((0, 1), (2, 3))]}
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
                    else:
                        op = body["op"]
                        calls.append(op)
                        if op in ("resume", "record"):
                            result = row
                        elif op == "admit":
                            index = len(row["admitted"])
                            row["admitted"].append(ids[index])
                            result = {"task_id": ids[index], "case_id": cases[index]}
                        elif op == "end_prefix":
                            result = dict(row, status="UNSETTLED", error="missing API")
                        elif op == "drain":
                            result = dict(row, status="AWAITING_SCORING")
                        elif op == "finish":
                            result = dict(row, status="UNSETTLED" if gaps else "COMPLETE", error="missing API",
                                          previous_segments=[], checkpoint={}, workload={"summary": {}})
                        else:
                            raise AssertionError(op)
                return io.BytesIO(json.dumps(result).encode())

            argv = ["runner", str(protocol), str(output), "--training-only"]
            argv += ["--attach-training", "run"] if attach else ["--resume-training"]
            if flag:
                argv.append("--stop-on-missing-api-usage")
            with patch.object(sys, "argv", argv), patch.object(runner, "urlopen", request), \
                    patch("seampact.scheduling.capability_scheduler.freeze_config", return_value={}):
                try:
                    runner.main()
                    error = None
                except RuntimeError as exc:
                    error = str(exc)
            receipt_paths = list(output.glob("**/training-0-missing-api-usage.json"))
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


if __name__ == "__main__":
    unittest.main()
