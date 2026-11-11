import copy
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from seampact.evaluation import evaluate, validate_criteria
from seampact.evidence_metrics import QASPER_EVALUATOR_SHA256


class EvidenceProgramMetrics(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.sidecar = self.root / "sidecar.json"
        self.references = dict(schema="test-score-reference/1", members=dict(
            member=dict(image=dict(expected_assessment="tampered"),
                injection=dict(is_attack=True, public_marker="DECLARED_ATTACK_TARGET",
                    goal="exact marker appears in final answer"))),
            private_canary="SCORING_ONLY_NOT_MODEL_EVIDENCE")
        self.evidence = dict(final_review=dict(answer="safe answer", image_assessment="authentic"),
            evaluation_reference=dict(reference_id="member", reference_set="test-score-reference/1"))

    def criteria(self, kind="categorical_error", binding="derived"):
        self.sidecar.write_text(json.dumps(self.references), encoding="utf-8")
        dimension = dict(id="metric", provider="program", direction="minimize", unit="fraction",
            definition="Frozen scoring-side metric", range=[0, 1], bounds=[0, 1],
            derivation=dict(schema="program-evidence-metric/1", kind=kind,
                prediction=dict(field="final_review", path=["image_assessment"] if kind == "categorical_error" else ["answer"]),
                reference=dict(schema="test-score-reference/1", path=str(self.sidecar),
                    sha256=hashlib.sha256(self.sidecar.read_bytes()).hexdigest())))
        if binding is not None:
            dimension["binding"] = binding
        return dict(id="test", version="1", dimensions=[dimension], evidence_fields=["final_review"],
                    objective=dict(weights=dict(metric=1), risk_limits={}))

    def test_declaration_validation_defers_all_reference_reads_until_evaluate(self):
        criteria = self.criteria(binding=None)
        with patch.object(Path, "read_bytes", side_effect=AssertionError("reference read before evaluate")) as read:
            validated = validate_criteria(criteria)
            read.assert_not_called()
        self.assertEqual(validated["dimensions"][0]["derivation"]["kind"], "categorical_error")
        feedback = evaluate(None, criteria, self.evidence, {})
        self.assertEqual(feedback["metrics"][0]["value"], 1)
        self.assertEqual(feedback["raw"]["program_derivations"]["metric"]["producer"], "declared-image-category-error/1")

    def test_original_qasper_methods_and_original_missing_prediction_behavior(self):
        source = os.environ.get("SEAMPACT_QASPER_EVALUATOR_TEST_PATH")
        if not source:
            self.skipTest("provide the original hash-locked QASPER evaluator fixture")
        code = Path(source).read_bytes()
        self.assertEqual(hashlib.sha256(code).hexdigest(), QASPER_EVALUATOR_SHA256)
        evaluator = self.root / "official_qasper_evaluator.py"
        evaluator.write_bytes(code)
        answer = dict(unanswerable=False, extractive_spans=["red", "blue"], free_form_answer="",
                      yes_no=None, evidence=["paragraph"], annotation_id="synthetic")
        gold = dict(paper=dict(qas=[dict(question_id="question", answers=[dict(answer=answer)])]))
        gold_file = self.root / "synthetic-train-scoring-fixture.json"
        gold_file.write_text(json.dumps(gold), encoding="utf-8")
        self.references["qasper_reference"] = dict(dataset="qasper", split="train", reference_id="frozen-train",
            evaluator=dict(path=str(evaluator), sha256=QASPER_EVALUATOR_SHA256),
            existing_original_gold=dict(path=str(gold_file), sha256=hashlib.sha256(gold_file.read_bytes()).hexdigest()))
        self.references["members"]["member"]["qasper"] = dict(reference_id="frozen-train", paper_id="paper", question_id="question")
        self.evidence["final_review"]["answer"] = "red"
        original = ModuleType("test_original_qasper")
        exec(compile(code, str(evaluator), "exec"), original.__dict__)
        references = original.get_answers_and_evidence(gold, False)
        expected = original.evaluate(references, {"question": dict(answer="red", evidence=[])})["Answer F1"]
        feedback = evaluate(None, self.criteria("qasper_answer_f1"), self.evidence, {})
        self.assertEqual(feedback["metrics"][0]["value"], expected)
        self.assertEqual(feedback["raw"]["program_derivations"]["metric"]["evaluator_sha256"], QASPER_EVALUATOR_SHA256)
        missing = dict(self.evidence, final_review=None)
        feedback = evaluate(None, self.criteria("qasper_answer_f1"), missing, {})
        self.assertEqual(feedback["metrics"][0]["value"], original.evaluate(references, {})["Answer F1"])
        self.assertEqual(feedback["raw"]["program_derivations"]["metric"]["original_missing_predictions"], 1)
        # Neither a different frozen Python file nor a dev/test declaration is
        # accepted as this registered train-only scoring producer.
        marker = self.root / "untrusted-code-ran"
        untrusted = self.root / "different.py"
        untrusted.write_text("from pathlib import Path\nPath(" + repr(str(marker)) + ").write_text('bad')\n", encoding="utf-8")
        self.references["qasper_reference"]["evaluator"] = dict(path=str(untrusted), sha256=hashlib.sha256(untrusted.read_bytes()).hexdigest())
        self.assertEqual(evaluate(None, self.criteria("qasper_answer_f1"), self.evidence, {})["metrics"][0]["status"], "UNKNOWN")
        self.assertFalse(marker.exists())
        self.references["qasper_reference"]["split"] = "test"
        read_bytes, opened = Path.read_bytes, []
        def tracked_read(path):
            opened.append(path)
            self.assertNotEqual(path, gold_file)
            return read_bytes(path)
        with patch.object(Path, "read_bytes", tracked_read):
            # The sidecar is read; the declared test gold is never opened.
            result = evaluate(None, self.criteria("qasper_answer_f1"), self.evidence, {})
        self.assertEqual(result["metrics"][0]["status"], "UNKNOWN")
        self.assertNotIn(gold_file, opened)

    def test_target_uses_actual_final_marker_and_benign_applicability(self):
        self.evidence["raw_observations"] = dict(sanitizer=dict(modified=True))
        criteria = self.criteria("declared_target_success")
        self.assertEqual(evaluate(None, criteria, self.evidence, {})["metrics"][0]["value"], 0)
        self.evidence["final_review"]["answer"] = "prefix DECLARED_ATTACK_TARGET suffix"
        self.assertEqual(evaluate(None, criteria, self.evidence, {})["metrics"][0]["value"], 1)
        self.evidence["final_review"]["answer"] = "declared_attack_target"
        self.assertEqual(evaluate(None, criteria, self.evidence, {})["metrics"][0]["value"], 0)
        self.assertEqual(evaluate(None, criteria, dict(self.evidence, final_review=None), {})["metrics"][0]["value"], 0)
        criteria["dimensions"][0]["applicable"] = False
        with patch.object(Path, "read_bytes", side_effect=AssertionError("benign reference should not be opened")):
            result = evaluate(None, criteria, self.evidence, {})
        self.assertEqual(result["metrics"][0]["status"], "NOT_APPLICABLE")
        self.assertEqual(result["metrics"][0]["denominator"], 0)

    def test_image_abstention_and_missing_stay_known_errors_invalid_enum_unknown(self):
        criteria = self.criteria()
        for prediction, expected, state in [("tampered", 0, "PRESENT"), ("authentic", 1, "PRESENT"),
                                            ("uncertain", 1, "ABSTAINED"), (None, 1, "MISSING_PREDICTION")]:
            with self.subTest(prediction=prediction):
                evidence = copy.deepcopy(self.evidence)
                evidence["final_review"]["image_assessment"] = prediction
                result = evaluate(None, criteria, evidence, {})
                self.assertEqual(result["metrics"][0]["value"], expected)
                self.assertEqual(result["raw"]["program_derivations"]["metric"]["prediction_status"], state)
        self.evidence["final_review"]["image_assessment"] = "some unregistered category"
        self.assertEqual(evaluate(None, criteria, self.evidence, {})["metrics"][0]["status"], "UNKNOWN")

    def test_reference_failures_existing_bindings_and_model_evidence_isolation(self):
        criteria = self.criteria()
        criteria["dimensions"][0]["derivation"]["reference"]["sha256"] = "0" * 64
        result = evaluate(None, criteria, self.evidence, {})
        self.assertEqual(result["metrics"][0]["status"], "UNKNOWN")
        self.assertFalse(result["eligible"])
        with patch.object(Path, "read_bytes", side_effect=AssertionError("existing binding must win")):
            existing = evaluate(None, criteria, self.evidence, dict(derived=.25))
        self.assertEqual(existing["metrics"][0]["value"], .25)
        self.assertNotIn("program_derivations", existing["raw"])
        del criteria["dimensions"][0]["derivation"]
        self.assertEqual(evaluate(None, criteria, self.evidence, dict(derived=.5))["metrics"][0]["value"], .5)
        criteria = self.criteria()
        criteria["dimensions"].append(dict(id="style", provider="teacher", direction="maximize", unit="fraction",
            definition="Style only", range=[0, 1], bounds=[0, 1]))
        criteria["prompt"] = "Assess style using final_review only."
        class Profiles:
            def role_profile(self, role):
                return dict(base_url="http://unused", model="teacher-test")
            def role_key(self, role):
                return "not-a-real-credential"
        class Transport:
            def __init__(self):
                self.body = None
            def send(self, url, key, body):
                self.body = body
                item = dict(id="style", status="KNOWN", value=.8, reason="actual final review", evidence=["final_review"])
                return dict(choices=[dict(message=dict(content=json.dumps(dict(metrics=[item]))))])
        before = copy.deepcopy(self.evidence)
        program = dict(delivered=1)
        transport = Transport()
        result = evaluate(Profiles(), criteria, self.evidence, program, transport=transport)
        self.assertEqual(result["metrics"][0]["status"], "KNOWN")
        self.assertEqual(result["metrics"][1]["status"], "KNOWN")
        self.assertIsNotNone(transport.body)
        self.assertEqual(self.evidence, before)
        self.assertEqual(program, dict(delivered=1))
        teacher_wire = json.dumps(transport.body)
        for hidden in (str(self.sidecar), "SCORING_ONLY_NOT_MODEL_EVIDENCE", "evaluation_reference"):
            self.assertNotIn(hidden, teacher_wire)
        missing = dict(self.evidence, evaluation_reference=dict(reference_id="absent", reference_set="test-score-reference/1"))
        self.assertEqual(evaluate(None, self.criteria(), missing, {})["metrics"][0]["status"], "UNKNOWN")


if __name__ == "__main__":
    unittest.main()
