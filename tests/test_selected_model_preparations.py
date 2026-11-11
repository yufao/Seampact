import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from seampact.scenes.multisource_review_scene import MultiSourceReviewScene
from seampact.scenes.evaluation_evidence import PassiveEvidenceGap
from seampact.source.record_ports import RecordSnapshot


class PreparationContext:
    def __init__(self, fail_sample=None):
        self.calls, self.model_calls, self.failure_evidence = [], [], None
        self.sample_count, self.fail_sample = 0, fail_sample

    def complete(self, messages, tools):
        self.model_calls.append(dict(messages=copy.deepcopy(messages), tools=tools))
        if tools is not None:
            report = dict(answer="DRAFT_TO_ISOLATE", text_assessment="uncertain",
                          image_assessment="not_applicable")
            return dict(choices=[dict(finish_reason="tool_calls", message=dict(tool_calls=[dict(function=dict(
                name="submit_material_review", arguments=json.dumps(report)))]))])
        self.sample_count += 1
        if self.sample_count == self.fail_sample:
            raise ValueError("recorded sampling transport failure")
        return dict(choices=[dict(finish_reason="stop", message=dict(
            content="Independent answer " + str(self.sample_count)))])

    def invoke(self, unit, arguments):
        self.calls.append(dict(unit_id=unit, arguments=copy.deepcopy(arguments)))
        return 0.2

    def record_failure_evidence(self, evidence, output_artifacts=()):
        self.failure_evidence = copy.deepcopy(evidence)


def configuration():
    return dict(schema="review-events/1", model_preparation_budget=3, checks=[dict(
        id="consistency", stage="publication", requires=["question"],
        model_preparations=[dict(id="independent", count=3,
            system_prompt="Answer the supplied question independently using the supplied material and evidence.",
            fields=["question", "material", "evidence"])],
        calls=[dict(unit_id="selfcheck", arguments=dict(sentences=[{"$report": ["answer"]}],
            sampled_passages={"$generated": ["consistency", "independent"]}))],
        observation=dict(kind="selfcheck_score", value={"$result": ["consistency", 0]}))])


class SelectedModelPreparationTests(unittest.TestCase):
    @staticmethod
    def choose(event, identity):
        return next(item for item in event["candidates"] if item["id"] == identity)

    def at_publication(self, context):
        scene = MultiSourceReviewScene(configuration(), context)
        event = scene.reset(dict(request="Review", question="Original question", material="VISIBLE_MATERIAL",
            evidence=["VISIBLE_EVIDENCE"], evaluation_reference=dict(gold="GOLD_TO_ISOLATE")), 1)
        event = scene.step(self.choose(event, "continue"))
        event = scene.step(self.choose(event, "continue"))
        return scene, event

    def test_previews_and_skipping_do_not_sample_or_fabricate_unit_arguments(self):
        context = PreparationContext()
        scene, event = self.at_publication(context)
        check = self.choose(event, "consistency")
        self.assertEqual(scene.preview_calls(check), [dict(unit_id="selfcheck", arguments=None)])
        self.assertEqual(context.sample_count, 0)
        self.assertEqual(len(context.model_calls), 1)
        self.assertEqual(context.calls, [])
        terminal = scene.step(self.choose(event, "publish"))
        self.assertEqual(context.sample_count, 0)
        self.assertEqual(terminal["evidence"]["generated_inputs"], {})
        self.assertEqual(terminal["evidence"]["model_preparations"], [])

    def test_selected_samples_are_independent_actual_texts_with_normal_cost_refs(self):
        context = PreparationContext()
        scene, event = self.at_publication(context)
        scene.records.append(dict(kind="other", status="available", value="PRIOR_CHECK_TO_ISOLATE"))
        event = scene.step(self.choose(event, "consistency"))
        expected = ["Independent answer 1", "Independent answer 2", "Independent answer 3"]
        self.assertEqual(len(context.model_calls), 4)
        self.assertEqual(context.calls, [dict(unit_id="selfcheck", arguments=dict(
            sentences=["DRAFT_TO_ISOLATE"], sampled_passages=expected))])
        sample_requests = context.model_calls[1:]
        self.assertEqual(sample_requests[0], sample_requests[1])
        self.assertEqual(sample_requests[1], sample_requests[2])
        for request in sample_requests:
            self.assertIsNone(request["tools"])
            wire = json.dumps(request)
            self.assertIn("Original question", wire)
            self.assertIn("VISIBLE_EVIDENCE", wire)
            self.assertNotIn("DRAFT_TO_ISOLATE", wire)
            self.assertNotIn("PRIOR_CHECK_TO_ISOLATE", wire)
            self.assertNotIn("GOLD_TO_ISOLATE", wire)
        self.assertEqual(list(json.loads(context.model_calls[0]["messages"][1]["content"]))[:4],
                         ["evidence", "material", "question", "request"])
        terminal = scene.step(self.choose(event, "publish"))
        record = terminal["evidence"]["model_preparations"][0]
        self.assertEqual(record["status"], "COMPLETED")
        self.assertEqual(record["completed"], 3)
        self.assertEqual(record["cost_refs"], ["model_call:1", "model_call:2", "model_call:3"])
        self.assertEqual(terminal["evidence"]["generated_inputs"]["consistency"]["independent"], expected)

    def test_partial_generation_failure_retains_paid_calls_and_never_invokes_unit(self):
        context = PreparationContext(fail_sample=2)
        scene, event = self.at_publication(context)
        with self.assertRaisesRegex(ValueError, "sampling transport failure"):
            scene.step(self.choose(event, "consistency"))
        self.assertEqual(context.calls, [])
        self.assertEqual(context.sample_count, 2)
        self.assertEqual(len(context.model_calls), 3)
        self.assertNotIn("consistency", scene.used)
        evidence = context.failure_evidence
        self.assertEqual(evidence["generated_inputs"]["consistency"]["independent"], ["Independent answer 1"])
        self.assertEqual(evidence["original_results"]["consistency"], [])
        record = evidence["model_preparations"][0]
        self.assertEqual(record["status"], "FAILED")
        self.assertEqual(record["completed"], 1)
        self.assertEqual(record["cost_refs"], ["model_call:1", "model_call:2"])

    def test_frozen_counts_visible_fields_and_same_check_generated_ports_are_required(self):
        mutations = [
            lambda cfg: cfg.update(model_preparation_budget=2),
            lambda cfg: cfg.update(model_preparation_budget=True),
            lambda cfg: cfg["checks"][0]["model_preparations"][0].update(count=0),
            lambda cfg: cfg["checks"][0]["model_preparations"][0].update(count=True),
            lambda cfg: cfg["checks"][0]["model_preparations"][0].update(fields=["evaluation_reference"]),
            lambda cfg: cfg["checks"][0]["model_preparations"][0].update(fields=["answer"]),
            lambda cfg: cfg["checks"][0]["model_preparations"].append(
                copy.deepcopy(cfg["checks"][0]["model_preparations"][0])),
            lambda cfg: cfg["checks"][0]["calls"][0]["arguments"].update(
                sampled_passages={"$generated": ["other-check", "independent"]}),
            lambda cfg: cfg["checks"][0]["calls"][0]["arguments"].update(
                sampled_passages={"$generated": ["consistency", "undeclared"]}),
        ]
        for change in mutations:
            with self.subTest(change=change):
                cfg, context = configuration(), PreparationContext()
                change(cfg)
                with self.assertRaises(ValueError):
                    MultiSourceReviewScene(cfg, context)
                self.assertEqual(context.model_calls, [])

    def test_typed_ports_preserve_record_values_and_reject_unknown_returns_or_business_exposure(self):
        record = RecordSnapshot("original", "Claim", "a" * 64, dict(content=["A", "is", "B"]))
        class PortContext(PreparationContext):
            def __init__(self, returned):
                super().__init__()
                self.returned = returned
            def invoke(self, unit, arguments):
                self.calls.append(dict(unit_id=unit, arguments=arguments))
                return self.returned if unit == "producer" else "checked"
        cfg = dict(schema="review-events/1", draft_source="provided_material", checks=[dict(
            id="chain", stage="intake", calls=[dict(unit_id="producer", arguments={}),
                dict(unit_id="consumer", arguments=dict(claims={"$port": ["chain", 0]}))])])
        context = PortContext([record])
        scene = MultiSourceReviewScene(cfg, context)
        event = scene.reset(dict(request="Review", material="Original provided draft"), 1)
        event = scene.step(self.choose(event, "chain"))
        self.assertIs(context.calls[1]["arguments"]["claims"][0], record)
        self.assertIs(scene.result_ports["chain"][0][0], record)
        self.assertIs(type(scene.results["chain"][0][0]), dict)
        self.assertEqual(scene.results["chain"][0][0]["fields"]["content"], ["A", "is", "B"])
        event = scene.step(self.choose(event, "continue"))
        event = scene.step(self.choose(event, "continue"))
        terminal = scene.step(self.choose(event, "publish"))
        self.assertIs(type(terminal["evidence"]["original_results"]["chain"][0][0]), dict)
        self.assertNotIn("result_ports", terminal["evidence"])
        self.assertEqual(context.model_calls, [])
        context = PortContext([object()])
        scene = MultiSourceReviewScene(cfg, context)
        event = scene.reset(dict(request="Review", material="Original provided draft"), 1)
        with self.assertRaises(PassiveEvidenceGap):
            scene.step(self.choose(event, "chain"))
        self.assertEqual([call["unit_id"] for call in context.calls], ["producer"])
        self.assertEqual(scene.result_ports["chain"], [])
        cfg["checks"][0]["observation"] = dict(kind="other", value={"$port": ["chain", 0]})
        with self.assertRaisesRegex(ValueError, "only to explicit call arguments"):
            MultiSourceReviewScene(cfg, context)

    def test_map_join_projects_actual_record_triplets_and_rejects_malformed_leaves(self):
        class TripletContext(PreparationContext):
            def __init__(self, contents):
                super().__init__()
                self.contents = contents
            def invoke(self, unit, arguments):
                self.calls.append(dict(unit_id=unit, arguments=copy.deepcopy(arguments)))
                if unit == "extract":
                    claims = [RecordSnapshot("original", "Claim", "a" * 64, dict(content=content))
                              for content in self.contents]
                    return [RecordSnapshot("original", "ExtractResult", "a" * 64, dict(claims=claims))]
                return [["Entailment"] * len(arguments["batch_claims"][0])]
        cfg = dict(schema="review-events/1", draft_source="provided_material", checks=[dict(
            id="ref", stage="intake", calls=[dict(unit_id="extract", arguments={}),
                dict(unit_id="nli", arguments=dict(batch_claims=[{"$map_join": dict(
                    value={"$result": ["ref", 0, 0, "fields", "claims"]}, item_path=["fields", "content"],
                    joiner=" ", item_length=3)}]))],
            observation=dict(kind="nli", source_call=1, value=dict(labels={"$result": ["ref", 1, 0]},
                answer_version={"$version": True})),
            additional_observations=[dict(kind="other", source_call=0,
                value={"$length": {"$result": ["ref", 0, 0, "fields", "claims"]}})])])
        context = TripletContext([["A", "is", "B"], ["C", "has", "D"]])
        scene = MultiSourceReviewScene(cfg, context)
        event = scene.reset(dict(request="Review", material="Original provided draft"), 1)
        scene.step(self.choose(event, "ref"))
        self.assertEqual(context.calls[1]["arguments"], dict(batch_claims=[["A is B", "C has D"]]))
        self.assertEqual(scene.results["ref"][0][0]["fields"]["claims"][0]["fields"]["content"], ["A", "is", "B"])
        self.assertEqual([(record["source"], record["value"]) for record in scene.records], [
            ("nli", dict(labels=["Entailment", "Entailment"], answer_version=0)), ("extract", 2)])
        for contents in ([["incomplete"]], [["A", 3, "B"]]):
            with self.subTest(contents=contents):
                context = TripletContext(contents)
                scene = MultiSourceReviewScene(cfg, context)
                event = scene.reset(dict(request="Review", material="Original provided draft"), 1)
                with self.assertRaisesRegex(ValueError, "map_join leaves"):
                    scene.step(self.choose(event, "ref"))
                self.assertEqual([call["unit_id"] for call in context.calls], ["extract"])
        cfg["checks"][0]["calls"][1]["arguments"]["batch_claims"][0]["$map_join"]["value"] = {"$task": ["evidence"]}
        with self.assertRaisesRegex(ValueError, "actual returned values"):
            MultiSourceReviewScene(cfg, PreparationContext())

    def test_publication_check_updates_business_answer_only_with_explicit_continuation(self):
        class ImageContext(PreparationContext):
            def invoke(self, unit, arguments):
                self.calls.append(dict(unit_id=unit, arguments=arguments))
                return 0.8
            def complete(self, messages, tools):
                self.model_calls.append(dict(messages=copy.deepcopy(messages), tools=tools))
                observed = json.loads(messages[1]["content"]).get("prior_check_observations", [])
                report = dict(answer="Checked image" if observed else "Unverified image",
                              text_assessment="not_applicable", image_assessment="tampered" if observed else "uncertain")
                return dict(choices=[dict(finish_reason="tool_calls", message=dict(tool_calls=[dict(function=dict(
                    name="submit_material_review", arguments=json.dumps(report)))]))])
        for continuation in (False, True):
            with self.subTest(continuation=continuation):
                context = ImageContext()
                scene = MultiSourceReviewScene(dict(schema="review-events/1", checks=[dict(id="image",
                    stage="publication", business_continuation=continuation,
                    calls=[dict(unit_id="image", arguments={})],
                    observation=dict(kind="image_score", value={"$result": ["image", 0]}))]), context)
                event = scene.reset(dict(request="Report image score with threshold 0.5."), 1)
                event = scene.step(self.choose(event, "continue"))
                event = scene.step(self.choose(event, "continue"))
                event = scene.step(self.choose(event, "image"))
                terminal = scene.step(self.choose(event, "publish"))
                self.assertEqual(len(context.model_calls), 2 if continuation else 1)
                self.assertEqual(terminal["response"]["image_assessment"], "tampered" if continuation else "uncertain")
                self.assertEqual(terminal["response"]["answer"], "Checked image" if continuation else "Unverified image")
                self.assertEqual(len(context.calls), 1)


if __name__ == "__main__":
    unittest.main()
