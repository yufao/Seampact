"""Actual sender inputs remain auditable after a successful business call."""
import copy
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from seampact.scenes.scenarios import SceneContext
from seampact.scenes.multisource_review_scene import MultiSourceReviewScene
from seampact.scenes.evaluation_evidence import passive_evaluation_snapshot


class Profiles:
    def role_profile(self, role):
        return dict(provider="deepseek", model="fixture", base_url="https://service.invalid/v1",
                    max_output_tokens=128, parameters={})

    def role_key(self, role):
        return "unused-fixture-key"


class ModelRequestEvidence(unittest.TestCase):
    def context(self):
        result = SceneContext(SimpleNamespace(profiles=Profiles()), "fixture", catalog=object())
        result.model_roles = ["chat"]
        return result

    def test_snapshot_matches_actual_transport_input_and_is_independent(self):
        context, sent = self.context(), []
        def send(url, key, body):
            sent.append(copy.deepcopy(body))
            body["messages"][0]["content"] = "changed after sender entry"
            return dict(choices=[], usage=dict(total_tokens=1))
        with patch("seampact.providers.Transport.send", side_effect=send):
            context.complete([dict(role="user", content="actual input")])
        self.assertEqual(context.model_calls[0]["request"], sent[0])
        self.assertEqual(context.model_calls[0]["request_provenance"], "transport_input_before_send")
        self.assertNotIn("unused-fixture-key", json.dumps(context.model_calls))

    def test_failed_send_keeps_the_same_attempted_input(self):
        context = self.context()
        with patch("seampact.providers.Transport.send", side_effect=ValueError("fixture transport failure")):
            with self.assertRaisesRegex(ValueError, "fixture transport failure"):
                context.complete([dict(role="user", content="attempted input")])
        self.assertEqual(context.model_calls[0]["request"]["messages"][0]["content"], "attempted input")
        self.assertEqual(context.raw_results[0]["status"], "ERROR")

    def test_successful_scene_retains_sender_body_without_exposing_task_gold(self):
        context = self.context()
        scene = MultiSourceReviewScene(dict(schema="review-events/1", checks=[]), context)
        scene.reset(dict(id="HIDDEN_CASE", request="Review the supplied material.", material="Public material.",
                         evaluation_reference=dict(gold="HIDDEN_GOLD")), 1)
        report = dict(answer="Review completed.", text_assessment="uncertain", image_assessment="not_applicable")
        response = dict(choices=[dict(finish_reason="tool_calls", message=dict(tool_calls=[dict(function=dict(
            name="submit_material_review", arguments=json.dumps(report)))]))], usage=dict(total_tokens=1))
        with patch("seampact.providers.Transport.send", return_value=response):
            scene.step(dict(id="continue"))
        scene.step(dict(id="continue"))
        terminal = scene.step(dict(id="publish"))
        snapshot = passive_evaluation_snapshot(terminal, context.calls, context.model_calls)
        body = json.dumps(snapshot["model_calls"][0]["request"])
        self.assertIn("Public material.", body)
        self.assertNotIn("HIDDEN_CASE", body)
        self.assertNotIn("HIDDEN_GOLD", body)
        self.assertEqual(snapshot["terminal"]["evidence"]["evaluation_reference"], dict(gold="HIDDEN_GOLD"))


if __name__ == "__main__":
    unittest.main()
