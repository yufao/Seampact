"""Office fact checks use only material in the actual model context."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from seampact.office_application import build_hooks, fact_check_status
from seampact.project_applications import compatible


class OfficeFactCheckTests(unittest.TestCase):
    def setUp(self):
        config = dict(mode="app_config", bindings={"fact_check": "unit"})
        self.hooks = {h.name: h for h in build_hooks(None, "task", config)["hooks"]}
        self.calls = []
        self.output = dict(status="ok", claim_count=1, claims=[dict(
            claim_text="The meeting is Monday", labels_by_reference=["Entailment"], label="Entailment")])
        def invoke(unit, arguments, **options):
            self.assertEqual(options, {"private_events": True})
            self.calls.append((unit, arguments))
            return dict(output=self.output)
        self.context = SimpleNamespace(invoke=invoke)
        self.hooks["office_input_context"].handler(dict(messages=[dict(content="When is the meeting?")]), self.context)

    def observe(self, content, name="read_file", is_error=False):
        self.hooks["office_visible_references"].handler(dict(name=name, content=content, is_error=is_error), self.context)

    def current_context(self, *contents):
        self.hooks["office_reference_context"].handler(dict(tool_contents=list(contents)), self.context)

    def check(self):
        return self.hooks["fact_check:before_delivery"].handler(dict(text="The meeting is Monday."), self.context)

    def test_only_visible_retained_read_result_is_used(self):
        self.observe('{"content":"The meeting is Monday."}')
        self.observe('{"content":"quarantined"}', name="send_email")
        self.observe('{"content":"error"}', is_error=True)
        self.current_context('{"content":"The meeting is Monday."}')
        self.assertEqual(self.check(), dict(action="allow", fact_support="supported"))
        self.assertEqual(self.calls[0][1], dict(question="When is the meeting?", answer="The meeting is Monday.",
                                                references=["The meeting is Monday."]))
        self.current_context()  # dropped from the next actual model request
        self.assertEqual(self.check()["fact_support"], "unknown")
        self.assertEqual(len(self.calls), 1)

    def test_empty_failed_incomplete_and_mismatched_results_are_unknown(self):
        for output in (dict(status="no_claims", claims=[], claim_count=0),
                       dict(status="extraction_failed", claims=[], claim_count=0),
                       dict(status="ok", claims=[], claim_count=0),
                       dict(status="ok", claim_count=1, claims=[dict(claim_text="x", label="Entailment",
                                                                    labels_by_reference=[])]),
                       dict(status="ok", claim_count=1, claims=[dict(claim_text="x", label="Entailment",
                                                                    labels_by_reference=["Neutral"])])):
            self.assertEqual(fact_check_status(output, 1), "unknown")
        unsupported = dict(status="ok", claim_count=1, claims=[dict(claim_text="x", label="Neutral",
                                                                   labels_by_reference=["Neutral"])])
        self.assertEqual(fact_check_status(unsupported, 1), "unsupported")

    def test_only_contract_fields_leave_the_hook(self):
        self.observe("Meeting is Monday.")
        self.current_context("Meeting is Monday.")
        self.output["model_usage"] = {"private": "usage"}
        result = self.check()
        self.assertEqual(result, dict(action="allow", fact_support="supported"))
        self.output = dict(status="checking_failed", claims=[], claim_count=0, errors=["private error"])
        self.assertEqual(self.check(), dict(action="allow", fact_support="unknown"))

    def test_unsupported_outbound_text_is_blocked_before_sending(self):
        self.observe('{"content":"The meeting is Tuesday."}')
        self.current_context('{"content":"The meeting is Tuesday."}')
        self.output["claims"][0].update(label="Contradiction", labels_by_reference=["Contradiction"])
        hook = self.hooks["fact_check:before_tool"]
        payload = dict(name="send_email", arguments=dict(to="colleague@example.invalid", body="The meeting is Monday."))
        self.assertTrue(hook.when(payload))
        self.assertEqual(hook.handler(payload, self.context)["action"], "block")
        self.assertEqual(self.calls[0][1]["answer"], "The meeting is Monday.")
        self.current_context()
        self.assertFalse(hook.when(payload))
        self.assertFalse(self.hooks["fact_check:before_delivery"].when(dict(text="The meeting is Monday.")))

    def test_generated_output_schema_is_accepted_for_explicit_role(self):
        unit = dict(entry_id="fact_check_response", capability=dict(
            input=dict(required=["question", "answer", "references"], properties=dict(
                question=dict(type="string"), answer=dict(type="string"),
                references=dict(type="array", items=dict(type="string")))),
            output=dict(kind="json", schema=dict(properties=dict(status=dict(type="string"),
                claim_count=dict(type="integer"), claims=dict(type="array", items=dict(properties=dict(
                    claim_text=dict(type="string"), label=dict(type="string"),
                    labels_by_reference=dict(type="array", items=dict(type="string"))))))))))
        self.assertTrue(compatible("fact_check", unit))
        unit["capability"]["output"]["schema"]["properties"]["claims"]["items"]["properties"].pop("labels_by_reference")
        self.assertFalse(compatible("fact_check", unit))


if __name__ == "__main__":
    unittest.main()
