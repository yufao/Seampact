import copy
import json
import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from seampact.capabilities import VERSION_2, SCALES_2, freeze_representation, represent
from seampact.scenes.multisource_review_scene import MultiSourceReviewScene

try:
    import numpy as np
except ImportError:
    np = None


class ReviewContext:
    def __init__(self):
        self.calls, self.model_calls = [], []
        self.failure_evidence = None

    def record_failure_evidence(self, evidence, output_artifacts=()):
        from seampact.scenes.evaluation_evidence import passive_value
        self.failure_evidence = passive_value(evidence)

    def invoke(self, unit, arguments):
        self.calls.append(dict(unit_id=unit, arguments=copy.deepcopy(arguments), status="RETURN"))
        if unit == "classifier":
            return dict(original_label="UNTRUSTED", original_score=0.8)
        if unit == "anonymizer":
            return dict(text="A redacted report.", entities=[dict(start=0, end=4)])
        raise AssertionError("foreign Unit")

    def complete(self, messages, tools):
        self.model_calls.append(copy.deepcopy(messages))
        report = dict(answer="Name appears in a draft.", text_assessment="uncertain", image_assessment="not_applicable")
        return dict(choices=[dict(finish_reason="tool_calls", message=dict(tool_calls=[dict(function=dict(
            name="submit_material_review", arguments=json.dumps(report)))]))])


class MultiSourceReviewTests(unittest.TestCase):
    def test_simple_routes_required_material_text_and_image_without_duplicate_verifier(self):
        class Context(ReviewContext):
            def invoke(self, unit, arguments):
                self.calls.append(dict(unit_id=unit, arguments=copy.deepcopy(arguments), status="RETURN"))
                return dict(retrieve="returned evidence", clean="cleaned material", facts=["ENTAILMENT"], image=.8)[unit]
        checks = [
            dict(id="clean", stage="intake", requires=["material"], calls=[dict(unit_id="clean", arguments={})],
                 updates=dict(material={"$result": ["clean", 0]})),
            dict(id="retrieve", stage="intake", required=True, calls=[dict(unit_id="retrieve", arguments={})],
                 updates=dict(evidence=[{"$result": ["retrieve", 0]}])),
            dict(id="consistency", stage="draft_review", calls=[dict(unit_id="consistency", arguments={})],
                 observation=dict(kind="selfcheck_score")),
            dict(id="facts", stage="draft_review", requires=["evidence"], calls=[dict(unit_id="facts", arguments={})],
                 observation=dict(kind="nli")),
            dict(id="image", stage="publication", requires=["image"], calls=[dict(unit_id="image", arguments={})],
                 observation=dict(kind="image_score"))]
        context = Context()
        scene = MultiSourceReviewScene(dict(schema="review-events/1", checks=checks), context)
        event = scene.reset(dict(request="Review", material="external material", image={},
                                evaluation_reference=dict(gold="OFFLINE_GOLD_SENTINEL")), 1)
        for target in ("retrieve", "clean", "continue", "facts", "continue", "image", "publish"):
            self.assertEqual(event["references"]["simple"], target)
            if target == "clean":
                self.assertEqual(event["references"]["required_only"], "continue")
            if target == "facts":
                self.assertEqual(event["references"]["all_applicable"], "consistency")
            event = scene.step(self.choose(event, target))
        self.assertEqual([call["unit_id"] for call in context.calls], ["retrieve", "clean", "facts", "image"])
        self.assertTrue(event["terminated"])
        self.assertNotIn("OFFLINE_GOLD_SENTINEL", json.dumps(context.model_calls))

    def test_simple_uses_visible_text_uncertainty_and_always_checks_present_image(self):
        checks = [
            dict(id="facts", stage="draft_review", requires=["evidence"], calls=[dict(unit_id="facts", arguments={})],
                 observation=dict(kind="minicheck_binary")),
            dict(id="image", stage="publication", requires=["image"], calls=[dict(unit_id="image", arguments={})],
                 observation=dict(kind="image_score"))]
        scene = MultiSourceReviewScene(dict(schema="review-events/1", checks=checks), ReviewContext())
        for assessment, expected in (("uncertain", "facts"), ("contradicted", "facts"),
                                     ("supported", "continue"), ("not_applicable", "continue")):
            with self.subTest(assessment=assessment):
                event = scene.reset(dict(id="unrelated-id", request="Review", evidence=["visible evidence"],
                    image=dict(width=2, height=2), evaluation_reference=dict(gold=assessment)), 1)
                scene.step(self.choose(event, "continue"))
                scene.report["text_assessment"] = assessment
                event = scene._decision()
                self.assertEqual(event["references"]["simple"], expected)
                scene.stage = "publication"
                scene.report["image_assessment"] = "authentic"
                self.assertEqual(scene._decision()["references"]["simple"], "image")

    def test_simple_selfcheck_fallback_requires_no_applicable_evidence_check(self):
        checks = [
            dict(id="facts", stage="draft_review", requires=["question", "evidence"],
                 calls=[dict(unit_id="facts", arguments={})], observation=dict(kind="nli")),
            dict(id="consistency", stage="draft_review", calls=[dict(unit_id="consistency", arguments={})],
                 observation=dict(kind="selfcheck_score"))]
        scene = MultiSourceReviewScene(dict(schema="review-events/1", checks=checks), ReviewContext())
        event = scene.reset(dict(request="Review", evidence=["visible evidence"]), 1)
        event = scene.step(self.choose(event, "continue"))
        self.assertEqual(event["references"]["simple"], "consistency")
        scene.used.add("consistency")
        self.assertEqual(scene._decision()["references"]["simple"], "continue")

    def test_provided_draft_keeps_exact_text_and_uses_no_business_model(self):
        context = ReviewContext()
        original = "Fictional draft: Alice, phone 555-0100."
        scene = MultiSourceReviewScene(dict(schema="review-events/1", draft_source="provided_material", checks=[
            dict(id="privacy", stage="draft_review", required=True, input_version="answer:1",
                 calls=[dict(unit_id="classifier", arguments=dict(text={"$report": ["answer"]}))])]), context)
        event = scene.reset(dict(request="Inspect this publication draft.", material=original), 1)
        event = scene.step(self.choose(event, "continue"))
        event = scene.step(self.choose(event, "privacy"))
        self.assertEqual(context.calls[0]["arguments"]["text"], original)
        self.assertEqual(scene.report["answer"], original)
        self.assertEqual(scene.version, 1)
        self.assertEqual(context.model_calls, [])
        event = scene.step(self.choose(event, "continue"))
        terminal = scene.step(self.choose(event, "publish"))
        self.assertEqual(terminal["response"]["answer"], original)
        self.assertEqual(terminal["evidence"]["original_material"], original)

    def test_provided_draft_allows_only_actual_returned_redaction_without_regeneration(self):
        context = ReviewContext()
        scene = MultiSourceReviewScene(dict(schema="review-events/1", draft_source="provided_material", checks=[
            dict(id="redact", stage="publication", required=True,
                 calls=[dict(unit_id="anonymizer", arguments=dict(text={"$report": ["answer"]}))],
                 updates=dict(answer={"$result": ["redact", 0, "text"]}))]), context)
        event = scene.reset(dict(request="Anonymize.", material="Original fictional name."), 1)
        event = scene.step(self.choose(event, "continue"))
        event = scene.step(self.choose(event, "continue"))
        self.assertNotIn("publish", [item["id"] for item in event["candidates"]])
        event = scene.step(self.choose(event, "redact"))
        self.assertEqual(scene.report["answer"], "A redacted report.")
        self.assertEqual(scene.version, 2)
        self.assertEqual(context.model_calls, [])
        self.assertEqual(scene.step(self.choose(event, "publish"))["response"]["answer"], "A redacted report.")

    def test_provided_draft_rejects_missing_or_empty_text_before_calls(self):
        for material in (None, "", "  ", ["text"]):
            context = ReviewContext()
            scene = MultiSourceReviewScene(dict(schema="review-events/1", draft_source="provided_material"), context)
            with self.assertRaisesRegex(ValueError, "provided publication draft"):
                scene.reset(dict(request="Inspect", material=material), 1)
            self.assertEqual(context.calls + context.model_calls, [])

    def retrieval_scene(self, status="success", text="actual retrieved passage"):
        class RetrievalContext(ReviewContext):
            failure_evidence = None
            def invoke(self, unit, arguments):
                self.calls.append(dict(unit_id=unit, arguments=copy.deepcopy(arguments), status="RETURN"))
                return ["original result string", dict(status=status, formatted_result=text,
                    api_response=dict(secret_document_id="offline-only"))]
            def record_failure_evidence(self, evidence, output_artifacts=()):
                self.failure_evidence = copy.deepcopy(evidence)
        context = RetrievalContext()
        config = dict(schema="review-events/1", checks=[dict(id="retrieve", stage="intake", required=True,
            input_version="question:1", requires=["question"], calls=[dict(unit_id="retriever",
                arguments=dict(query_list=[{"$task": ["question"]}]))],
            result_requirements=[dict(value={"$result": ["retrieve", 0, 1, "status"]}, equals="success"),
                dict(value={"$result": ["retrieve", 0, 1, "formatted_result"]}, nonempty_string=True)],
            observation=dict(kind="other", value=dict(status={"$result": ["retrieve", 0, 1, "status"]})),
            updates=dict(evidence=[{"$result": ["retrieve", 0, 1, "formatted_result"]}]))])
        return MultiSourceReviewScene(config, context), context

    def test_required_retrieval_precedes_generation_and_evidence_is_actual_versioned_return(self):
        scene, context = self.retrieval_scene()
        event = scene.reset(dict(request="Review", question="Original question", evaluation_reference={"gold":"SECRET"}), 1)
        self.assertEqual([c["id"] for c in event["candidates"]], ["retrieve"])
        with self.assertRaisesRegex(ValueError, "required check"):
            scene.step(dict(id="continue"))
        event = scene.step(self.choose(event, "retrieve"))
        self.assertEqual(scene.evidence_version, 1)
        self.assertEqual(scene.evidence_updates[0]["input_version"], "question:1")
        self.assertEqual(scene.evidence_updates[0]["sources"][0]["cost_ref"], "unit_call:0")
        self.assertEqual(context.calls[0]["arguments"], dict(query_list=["Original question"]))
        event = scene.step(self.choose(event, "continue"))
        wire = json.dumps(context.model_calls)
        self.assertIn("actual retrieved passage", wire)
        self.assertNotIn("SECRET", wire)
        self.assertNotIn("offline-only", wire)
        event = scene.step(self.choose(event, "continue"))
        terminal = scene.step(self.choose(event, "publish"))
        self.assertEqual(terminal["evidence"]["current_evidence"], ["actual retrieved passage"])
        self.assertEqual(terminal["evidence"]["original_results"]["retrieve"][0][1]["api_response"]["secret_document_id"], "offline-only")

    def test_required_retrieval_failure_keeps_actual_return_without_business_call_or_retry(self):
        for status, text in (("api_error", "error"), ("success", "")):
            with self.subTest(status=status, text=text):
                scene, context = self.retrieval_scene(status, text)
                event = scene.reset(dict(request="Review", question="Question", evaluation_reference={"gold":"SECRET"}), 1)
                with self.assertRaisesRegex(ValueError, "actual returned result"):
                    scene.step(self.choose(event, "retrieve"))
                self.assertEqual(len(context.calls), 1)
                self.assertEqual(context.model_calls, [])
                self.assertNotIn("retrieve", scene.used)
                self.assertEqual(context.failure_evidence["original_results"]["retrieve"][0][1]["status"], status)
                self.assertNotIn("SECRET", json.dumps(context.failure_evidence))

    def test_result_requirements_reject_future_and_non_result_selectors(self):
        scene, context = self.retrieval_scene()
        scene.checks[0]["result_requirements"][0]["value"] = {"$result": ["future", 0]}
        event = scene.reset(dict(request="Review", question="Question"), 1)
        with self.assertRaisesRegex(ValueError, "actual returned result"):
            scene.step(self.choose(event, "retrieve"))
        self.assertEqual(len(context.calls), 1)
        self.assertEqual(context.model_calls, [])
        invalid = dict(schema="review-events/1", checks=[dict(id="x", stage="intake", calls=[dict(unit_id="u", arguments={})],
            result_requirements=[dict(value={"$task": ["question"]}, equals="value")])])
        with self.assertRaisesRegex(ValueError, "actual results"):
            MultiSourceReviewScene(invalid, context)
        invalid["checks"][0].pop("result_requirements")
        invalid["checks"][0]["updates"] = {"evidence": [{"$result": ["other", 0]}]}
        with self.assertRaisesRegex(ValueError, "actual result selectors"):
            MultiSourceReviewScene(invalid, context)

    def test_future_required_check_cannot_be_skipped_by_abstention_or_forged_publish(self):
        context = ReviewContext()
        scene = MultiSourceReviewScene(dict(schema="review-events/1", abstain_answer="declined", checks=[
            dict(id="review", stage="draft_review", required=True, calls=[dict(unit_id="classifier", arguments={})])]), context)
        event = scene.reset(dict(request="Review"), 1)
        self.assertNotIn("abstain", [candidate["id"] for candidate in event["candidates"]])
        for identity in ("abstain", "publish"):
            with self.assertRaisesRegex(ValueError, "required check"):
                scene.step(dict(id=identity))
        event = scene.step(self.choose(event, "continue"))
        self.assertNotIn("continue", [candidate["id"] for candidate in event["candidates"]])
        with self.assertRaisesRegex(ValueError, "required check"):
            scene.step(dict(id="continue"))
        event = scene.step(self.choose(event, "review"))
        self.assertIn("continue", [candidate["id"] for candidate in event["candidates"]])

    @unittest.skipIf(np is None, "NumPy is not installed")
    def test_original_numpy_results_cross_the_scene_with_paid_source_and_input_identity(self):
        class NumpyContext(ReviewContext):
            mini = ([1], [np.float32(.875)], [["original chunk"]], [np.array([.875], dtype=np.float32)])
            selfcheck = np.array([.125], dtype=np.float32)
            def invoke(self, unit, arguments):
                self.calls.append(dict(unit_id=unit, arguments=copy.deepcopy(arguments), status="RETURN"))
                return self.mini if unit == "minicheck" else self.selfcheck
        context = NumpyContext()
        config = dict(schema="review-events/1", checks=[
            dict(id="mini", stage="intake", calls=[dict(unit_id="minicheck", arguments=dict(
                docs=[{"$task": ["material"]}], claims=["original claim"]))],
                observation=dict(kind="minicheck_binary", label_map={"0": "unsupported", "1": "supported"},
                    value={"$result": ["mini", 0, 0, 0]})),
            dict(id="self", stage="intake", calls=[dict(unit_id="selfcheck", arguments=dict(
                text={"$task": ["material"]}))],
                observation=dict(kind="selfcheck_score", value={"$result": ["self", 0, 0]}))])
        scene = MultiSourceReviewScene(config, context)
        event = scene.reset(dict(request="Review.", material="original material",
            evaluation_reference=dict(hidden="GOLD MUST STAY OFFLINE")), 1)
        event = scene.step(self.choose(event, "mini"))
        event = scene.step(self.choose(event, "self"))
        records = event["scene_observation"]["observation_records"]
        self.assertEqual([record["source"] for record in records], ["minicheck", "selfcheck"])
        self.assertEqual([record["input_version"] for record in records], ["material:1", "material:1"])
        self.assertEqual([record["cost_refs"] for record in records], [["unit_call:0"], ["unit_call:1"]])
        self.assertEqual(records[0]["value"], "supported")
        self.assertEqual(records[1]["value"], .125)
        raw = scene.raw_records[0]
        self.assertEqual(raw["value"], 1)
        self.assertEqual({key: raw[key] for key in ("source", "event", "input_version", "cost_refs")},
                         {key: records[0][key] for key in ("source", "event", "input_version", "cost_refs")})
        context.selfcheck[:] = .5
        self.assertEqual(records[1]["value"], .125)
        representation = dict(version=VERSION_2, scales={name: 1 for name in SCALES_2})
        freeze_representation(dict(unit_ids=["minicheck", "selfcheck"], representation=representation), {})
        represent(event, representation)
        self.assertTrue(all(type(value) in (int, float) and math.isfinite(value) for value in event["observation"]))
        from seampact.capabilities import OBSERVATION_SCHEMA_2
        # The declared Mini meaning reaches the existing support feature; raw
        # codes do not create an additional "other" observation feature.
        self.assertEqual(event["observation"][OBSERVATION_SCHEMA_2.index("result:minicheck_binary:supported")], 1)
        self.assertEqual(event["observation"][OBSERVATION_SCHEMA_2.index("result:other:available")], 0)
        self.assertEqual(context.model_calls, [])
        self.assertNotIn("GOLD MUST STAY OFFLINE", json.dumps(event, allow_nan=False))
        event = scene.step(self.choose(event, "continue"))
        event = scene.step(self.choose(event, "continue"))
        terminal = scene.step(self.choose(event, "publish"))
        self.assertEqual(terminal["evidence"]["raw_observations"][0]["value"], 1)
        self.assertEqual(terminal["evidence"]["original_results"]["mini"][0],
                         [[1], [.875], [["original chunk"]], [[.875]]])

    @unittest.skipIf(np is None, "NumPy is not installed")
    def test_binary_map_uses_only_declared_codes_and_exact_scalar_key_normalization(self):
        for code, labels, expected in ((np.int64(1), {"1": "supported"}, "supported"),
                                       (np.bool_(False), {"false": "unsupported"}, "unsupported"),
                                       (np.str_("yes"), {"yes": "supported"}, "supported"),
                                       ("supported", None, "supported"),
                                       (np.int64(2), {"0": "unsupported", "1": "supported"}, None),
                                       (np.int64(1), None, None)):
            with self.subTest(code_type=type(code).__name__, labels=labels):
                class Context(ReviewContext):
                    def invoke(self, unit, arguments):
                        self.calls.append(dict(unit_id=unit, arguments=arguments, status="RETURN"))
                        return code
                observation = dict(kind="minicheck_binary", value={"$result": ["mini", 0]})
                if labels is not None:
                    observation["label_map"] = labels
                context = Context()
                scene = MultiSourceReviewScene(dict(schema="review-events/1", checks=[dict(id="mini", stage="intake",
                    calls=[dict(unit_id="minicheck", arguments={})], observation=observation)]), context)
                event = scene.reset(dict(request="Review.", material="text"), 1)
                if expected is None:
                    with self.assertRaises(ValueError):
                        scene.step(self.choose(event, "mini"))
                    self.assertEqual(scene.records, [])
                else:
                    event = scene.step(self.choose(event, "mini"))
                    self.assertEqual(scene.records[0]["value"], expected)
                    self.assertEqual(scene.raw_records[0]["cost_refs"], ["unit_call:0"])
                self.assertEqual(len(context.calls), 1)

    def test_binary_key_normalization_never_calls_a_source_string_hook(self):
        from seampact.scenes.multisource_review_scene import _label_key
        invoked = []
        class Code(int):
            def __str__(self):
                invoked.append("str")
                return "1"
        with self.assertRaises(ValueError):
            _label_key(Code(1))
        self.assertEqual(invoked, [])

    def scene(self):
        config = dict(schema="review-events/1", checks=[
            dict(id="screen", stage="intake", requires=["material"],
                calls=[dict(unit_id="classifier", arguments=dict(text={"$task": ["material"]}))],
                observation=dict(kind="prompt_injection", label_map={"UNTRUSTED": "injection"}, value=dict(
                    raw_label={"$result": ["screen", 0, "original_label"]},
                    score={"$result": ["screen", 0, "original_score"]}))),
            dict(id="redact", stage="publication", calls=[dict(unit_id="anonymizer", arguments=dict(
                text={"$report": ["answer"]}))], observation=dict(kind="privacy", value=dict(
                    entity_count={"$length": {"$result": ["redact", 0, "entities"]}},
                    anonymized_available=True, raw={"$result": ["redact", 0]})),
                updates=dict(answer={"$result": ["redact", 0, "text"]}))])
        context = ReviewContext()
        return MultiSourceReviewScene(config, context), context

    def encoded(self, event):
        config = dict(unit_ids=["classifier", "anonymizer"], representation=dict(
            version=VERSION_2, scales={name: 1 for name in SCALES_2}))
        freeze_representation(config, {})
        represent(event, config["representation"])
        return event["observation"]

    def choose(self, event, identity):
        return next(candidate for candidate in event["candidates"] if candidate["id"] == identity)

    def test_intake_decision_precedes_exposure_and_paid_result_changes_observation(self):
        scene, context = self.scene()
        event = scene.reset(dict(request="Review material.", material="UNTRUSTED INPUT",
            evaluation_reference=dict(secret="GOLD NEVER ONLINE")), 1)
        before = self.encoded(event)
        self.assertEqual(context.model_calls, [])
        self.assertEqual(event["scene_observation"]["observation_records"], [])
        self.assertNotIn("GOLD NEVER ONLINE", json.dumps(event))
        event = scene.step(self.choose(event, "screen"))
        after = self.encoded(event)
        self.assertNotEqual(before, after)
        self.assertEqual(context.model_calls, [])
        record = event["scene_observation"]["observation_records"][0]
        self.assertEqual((record["source"], record["value"]["raw_label"], record["value"]["label"]),
                         ("classifier", "UNTRUSTED", "injection"))
        self.assertEqual(record["cost_refs"], ["unit_call:0"])
        event = scene.step(self.choose(event, "continue"))
        wire = json.dumps(context.model_calls)
        self.assertIn("UNTRUSTED INPUT", wire)
        self.assertNotIn("GOLD NEVER ONLINE", wire)
        self.assertEqual(event["control_event"], "business_draft_returned")

    def test_skipping_has_no_detector_call_and_publication_uses_actual_return(self):
        scene, context = self.scene()
        event = scene.reset(dict(request="Publish safely.", material="text", image=dict(
            input="HIDDEN_TAMPERED_NAME.png", input_sha256="HIDDEN_HASH", width=2, height=2,
            hidden_label="HIDDEN_GOLD"), evaluation_reference=dict(reference="private gold")), 1)
        event = scene.step(self.choose(event, "continue"))
        self.assertEqual(context.calls, [])
        wire = json.dumps(context.model_calls)
        for hidden in ("HIDDEN_TAMPERED_NAME", "HIDDEN_HASH", "HIDDEN_GOLD", "private gold"):
            self.assertNotIn(hidden, wire)
        event = scene.step(self.choose(event, "continue"))
        self.assertEqual(event["control_event"], "before_publication")
        event = scene.step(self.choose(event, "redact"))
        self.encoded(event)
        terminal = scene.step(self.choose(event, "publish"))
        self.assertEqual(terminal["response"]["answer"], "A redacted report.")
        self.assertEqual(terminal["evidence"]["evaluation_reference"], dict(reference="private gold"))
        self.assertEqual([call["unit_id"] for call in context.calls], ["anonymizer"])
        self.assertEqual(context.calls[0]["arguments"]["text"], "Name appears in a draft.")

    def test_hidden_reference_mapping_is_rejected_before_any_business_call(self):
        context = ReviewContext()
        with self.assertRaisesRegex(ValueError, "hidden"):
            MultiSourceReviewScene(dict(schema="review-events/1", checks=[dict(id="invalid", stage="intake",
                calls=[dict(unit_id="classifier", arguments=dict(text={"$task": ["evaluation_reference"]}))])]), context)
        self.assertEqual(context.calls + context.model_calls, [])

    def test_unobserved_future_result_cannot_be_used_as_a_call_argument(self):
        context = ReviewContext()
        scene = MultiSourceReviewScene(dict(schema="review-events/1", checks=[dict(id="future", stage="intake",
            calls=[dict(unit_id="classifier", arguments=dict(text={"$result": ["future", 1]}))])]), context)
        event = scene.reset(dict(request="Review.", material="text"), 1)
        with self.assertRaises(IndexError):
            scene.step(self.choose(event, "future"))
        self.assertEqual(context.calls + context.model_calls, [])

    def test_draft_check_has_real_business_continuation_and_intake_update_has_new_version(self):
        context = ReviewContext()
        scene = MultiSourceReviewScene(dict(schema="review-events/1", checks=[
            dict(id="filter", stage="intake", calls=[dict(unit_id="anonymizer",
                arguments=dict(text={"$task": ["material"]}))], updates=dict(material={"$result": ["filter", 0, "text"]})),
            dict(id="screen", stage="intake", calls=[dict(unit_id="classifier",
                arguments=dict(text={"$task": ["material"]}))]),
            dict(id="review", stage="draft_review", calls=[dict(unit_id="classifier",
                arguments=dict(text={"$report": ["answer"]}))])]), context)
        event = scene.reset(dict(request="Review.", material="original"), 1)
        event = scene.step(self.choose(event, "filter"))
        event = scene.step(self.choose(event, "screen"))
        records = event["scene_observation"]["observation_records"]
        self.assertEqual([record["input_version"] for record in records], ["material:1", "material:2"])
        self.assertEqual(context.calls[1]["arguments"]["text"], "A redacted report.")
        event = scene.step(self.choose(event, "continue"))
        event = scene.step(self.choose(event, "review"))
        self.assertEqual(len(context.model_calls), 2)
        self.assertIn("UNTRUSTED", json.dumps(context.model_calls[1]))
        self.assertEqual(event["scene_observation"]["answer_version"], 2)


if __name__ == "__main__":
    unittest.main()
