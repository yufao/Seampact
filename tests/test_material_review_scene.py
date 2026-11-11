import copy
import json
import hashlib
import subprocess
import tempfile
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from seampact.scenes.material_review_scene import MaterialReviewScene
from seampact.source.record_ports import RecordSnapshot
from seampact.capabilities import (VERSION_2, SCALES_2, JOINT_SCALES, OBSERVATION_SCHEMA_2, CANDIDATE_SCHEMA_2,
                                   freeze_representation, represent, joint_observation)


class ReviewContext:
    def __init__(self):
        self.calls, self.model_calls = [], []
        self.labels = [["Entailment"]]
        self.assessments = ("supported", "not_applicable")

    def complete(self, messages, tools):
        self.model_calls.append(copy.deepcopy(messages))
        report = dict(answer="Paris is in France.", text_assessment=self.assessments[0], image_assessment=self.assessments[1])
        return dict(choices=[dict(finish_reason="tool_calls", message=dict(tool_calls=[dict(
            function=dict(name="submit_material_review", arguments=json.dumps(report)))]))])

    def invoke(self, unit, arguments):
        self.calls.append(dict(unit_id=unit, arguments=copy.deepcopy(arguments)))
        if unit == "extract":
            claim = RecordSnapshot("original", "Claim", "a" * 64, dict(content=["Paris", "is in", "France"]))
            return [RecordSnapshot("original", "Result", "b" * 64,
                dict(claims=[claim], extractor_response='("Paris", "is in", "France")'))]
        if unit == "nli":
            return self.labels
        raise AssertionError("foreign Unit")


class MaterialReviewTests(unittest.TestCase):
    def encoded(self, event):
        scene = dict(unit_ids=["extract", "nli", "image"], representation=dict(
            version=VERSION_2, scales={key: 1 for key in SCALES_2}))
        freeze_representation(scene, {})
        represent(event, scene["representation"])
        workload = {name: 0 for name in ("tasks_active", "tasks_at_decision", "tasks_done", "roots_waiting",
            "roots_dispatchable", "roots_active", "slots_free", "oldest_wait_ms", "instances_held",
            "instances_ready", "instances_starting", "instances_draining", "instances_idle",
            "replicas_remaining", "resource_remaining", "resource_ops_in_row", "window_elapsed_ms")}
        joint = joint_observation("business", {key: 1 for key in JOINT_SCALES}, workload, event["observation"])
        return event["observation"], joint

    def test_separate_modalities_and_result_visibility_in_public_vectors(self):
        scene, context, task = self.scene()
        task["image"] = dict(input="private-name", input_sha256="hidden-hash", width=2, height=2,
                             class_hint="tampered")
        context.assessments = ("contradicted", "authentic")
        first = scene.reset(task, 1)
        before, joint_before = self.encoded(first)
        self.assertEqual(before[OBSERVATION_SCHEMA_2.index("result:nli:missing")], 1)
        self.assertEqual(before[OBSERVATION_SCHEMA_2.index("result:nli:entailment")], 0)
        self.assertNotIn("hidden-hash", json.dumps(context.model_calls))
        self.assertNotIn("class_hint", json.dumps(context.model_calls))
        self.assertNotIn("private-name", json.dumps(context.model_calls))
        context.assessments = ("supported", "tampered")
        other, joint_other = self.encoded(scene.reset(task, 1))
        self.assertNotEqual(before, other)
        self.assertNotEqual(joint_before, joint_other)
        after, _ = self.encoded(scene.step(dict(id="text")))
        self.assertEqual(after[OBSERVATION_SCHEMA_2.index("result:nli:available")], 1)
        self.assertEqual(after[OBSERVATION_SCHEMA_2.index("result:nli:entailment")], 1)
        self.assertEqual(after[OBSERVATION_SCHEMA_2.index("result:image_score:missing")], 1)
        records = scene.observation_records
        result = next(record for record in records if record["kind"] == "nli")
        self.assertEqual(result["input_version"], "answer:1")
        self.assertEqual(result["cost_refs"], ["unit_call:0", "unit_call:1"])

    def test_old_representation_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unsupported"):
            freeze_representation(dict(unit_ids=[], representation=dict(version="capability-features/2", scales={})), {})

    def test_candidate_checks_the_selected_input_version(self):
        scene, _, task = self.scene()
        event = scene.reset(task, 1)
        candidate = next(item for item in event["candidates"] if item["id"] == "text")
        event["scene_observation"]["observation_records"].append(dict(kind="other", status="available",
            source="nli", event="check_returned:1", input_version="material:1",
            cost_refs=["unit_call:0"], value="original result"))
        candidate["input_version"] = "material:1"
        self.encoded(event)
        same = list(candidate["features"])
        index = CANDIDATE_SCHEMA_2.index("result:source_match:current_version")
        self.assertEqual(same[index], 1)
        candidate["input_version"] = "material:2"
        event["scene_observation"]["material_version"] = 2
        self.encoded(event)
        self.assertEqual(candidate["features"][index], 0)
        self.assertEqual(candidate["features"][CANDIDATE_SCHEMA_2.index("result:source_match:available")], 1)
        self.assertNotEqual(same, candidate["features"])

    def test_original_prompt_and_privacy_observations_require_returned_provenance(self):
        scene, _, task = self.scene()
        event = scene.reset(task, 1)
        facts = event["scene_observation"]
        facts["result_status"] = dict(prompt_injection="not_applicable", privacy="missing")
        before, _ = self.encoded(event)
        self.assertEqual(before[OBSERVATION_SCHEMA_2.index("result:prompt_injection:not_applicable")], 1)
        facts["observation_records"].extend([
            dict(kind="prompt_injection", status="available", source="classifier", event="returned:1",
                 input_version="material:1", cost_refs=["unit_call:0"],
                 value=dict(raw_label="source-original", label="injection", score=.23)),
            dict(kind="privacy", status="available", source="analyzer", event="returned:2",
                 input_version="answer:1", cost_refs=["unit_call:1"],
                 value=dict(entity_count=3, anonymized_available=False, raw=[{"start": 0, "end": 4}]))])
        after, _ = self.encoded(event)
        self.assertEqual(after[OBSERVATION_SCHEMA_2.index("result:prompt_injection:injection")], 1)
        self.assertEqual(after[OBSERVATION_SCHEMA_2.index("result:prompt_injection:score")], .23)
        self.assertEqual(after[OBSERVATION_SCHEMA_2.index("result:privacy:entity_count")], 3)
        self.assertEqual(facts["observation_records"][-1]["value"]["raw"][0]["end"], 4)
        facts["answer_version"] = 2
        stale, _ = self.encoded(event)
        self.assertEqual(stale[OBSERVATION_SCHEMA_2.index("assessment:text:stale")], 1)
        self.assertEqual(stale[OBSERVATION_SCHEMA_2.index("assessment:text:supported")], 0)
        facts["observation_records"].extend([
            dict(kind="minicheck_binary", status="available", source="mini", event="returned:3",
                 input_version="answer:2", cost_refs=["unit_call:2"], value="unsupported"),
            dict(kind="selfcheck_score", status="available", source="selfcheck", event="returned:4",
                 input_version="answer:2", cost_refs=["unit_call:3"], value=.4)])
        source_values, _ = self.encoded(event)
        self.assertEqual(source_values[OBSERVATION_SCHEMA_2.index("result:minicheck_binary:unsupported")], 1)
        self.assertEqual(source_values[OBSERVATION_SCHEMA_2.index("result:selfcheck_score:value")], .4)
        facts["observation_records"][-1]["cost_refs"] = []
        with self.assertRaisesRegex(ValueError, "provenance"):
            self.encoded(event)
    def scene(self):
        context = ReviewContext()
        scene = MaterialReviewScene(dict(units=dict(extract="extract", nli="nli", image="image"),
            nli_arguments=dict(is_joint=False, merge_psg=True, max_reference_segment_length=200)), context)
        task = dict(id="task", request="Review the answer", question="Where is Paris?", material="Paris is in France.",
                    evidence=["Paris is a city in France."], evaluation_reference=dict(secret_gold="feedback_only"))
        return scene, context, task

    def test_skip_delivers_actual_business_answer_without_module_call(self):
        scene, context, task = self.scene()
        event = scene.reset(task, 1)
        self.assertEqual(event["required_units"], [])
        self.assertEqual({c["id"] for c in event["candidates"]}, {"finish", "continue", "text"})
        end = scene.step(dict(id="finish"))
        self.assertEqual(len(context.model_calls), 1)
        self.assertEqual(context.calls, [])
        self.assertEqual(end["response"]["answer"], "Paris is in France.")
        self.assertEqual(end["evidence"]["evaluation_reference"], task["evaluation_reference"])
        self.assertNotIn("secret_gold", json.dumps(event))
        self.assertNotIn("secret_gold", json.dumps(context.model_calls))

    def test_selected_text_preserves_extraction_then_nli_then_business_continuation(self):
        scene, context, task = self.scene()
        scene.reset(task, 1)
        event = scene.step(dict(id="text"))
        self.assertEqual([c["unit_id"] for c in context.calls], ["extract", "nli"])
        self.assertEqual(context.calls[1]["arguments"]["batch_claims"], [["Paris is in France"]])
        self.assertEqual(len(context.model_calls), 2)
        self.assertIn("Entailment", json.dumps(context.model_calls[1]))
        self.assertNotIn("secret_gold", json.dumps(context.model_calls))
        self.assertEqual([c["id"] for c in event["candidates"]], ["finish"])

    def test_missing_nli_labels_fail_instead_of_delivering_success(self):
        scene, context, task = self.scene()
        scene.reset(task, 1)
        context.labels = [[]]
        with self.assertRaisesRegex(ValueError, "every extracted claim"):
            scene.step(dict(id="text"))
        self.assertEqual(len(context.model_calls), 1)

    def test_continue_is_bounded_and_does_not_invent_a_safety_result(self):
        scene, context, task = self.scene()
        scene.reset(task, 1)
        event = scene.step(dict(id="continue"))
        self.assertNotIn("continue", [c["id"] for c in event["candidates"]])
        self.assertEqual(context.calls, [])
        self.assertIn("no safety checker was run", json.dumps(context.model_calls[-1]))

    def test_input_applicability_does_not_consult_risk_label(self):
        scene, context, task = self.scene()
        safe = scene.reset(task, 1)
        context.model_calls.clear()
        task["evaluation_reference"]["risk"] = "positive"
        risky = scene.reset(task, 1)
        self.assertEqual(safe, risky)
        task.pop("evidence")
        no_evidence = scene.reset(task, 1)
        self.assertNotIn("text", [c["id"] for c in no_evidence["candidates"]])

    def test_image_bridge_uses_the_exclusive_request_output_namespace(self):
        import numpy as np
        from seampact.completion import before_completion
        scene,context,task=self.scene()
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);image=root/'input.png';image.write_bytes(b'bridge-input-identity')
            scene.config.update(platform_inputs=str(root),worker_inputs=str(root),platform_outputs=str(root),
                worker_outputs=str(root),gpu='0',image_threshold=.5)
            task['image']=dict(input=image.name,input_sha256=hashlib.sha256(image.read_bytes()).hexdigest(),height=2,width=2)
            def invoke(unit,arguments):
                self.assertEqual(unit,'image')
                context.calls.append(dict(unit_id=unit, arguments=copy.deepcopy(arguments)))
                output=Path(arguments['output'])
                self.assertEqual(output.name,'result.npz')
                self.assertEqual(output.parent.parent,root)
                self.assertFalse(output.parent.exists())
                before_completion(dict(format='npz',input_parameter='input',output_parameter='output',
                    output_scope='fresh-parent/1'),arguments,root)
                np.savez(output,imgsize=np.array([2,2]),score=.8,map=np.zeros((2,2)),conf=np.ones((2,2)))
                return subprocess.CompletedProcess(['original-cli'],0)
            context.invoke=invoke
            initial = scene.reset(task,1)
            before, _ = self.encoded(initial)
            self.assertEqual(before[OBSERVATION_SCHEMA_2.index('result:image_score:missing')], 1)
            event = scene.step(dict(id='image'))
            after, _ = self.encoded(event)
            self.assertEqual(after[OBSERVATION_SCHEMA_2.index('result:image_score:available')], 1)
            self.assertEqual(after[OBSERVATION_SCHEMA_2.index('result:image_score:value')], .8)
            artifact=scene.outputs[0]['observation']['artifact']
            self.assertTrue((root/artifact).is_file())
            self.assertEqual(scene.outputs[0]['observation']['score'],.8)


if __name__ == "__main__":
    unittest.main()
