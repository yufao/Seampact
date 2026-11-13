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
        self.assertEqual(self.check()["fact_support"], "supported")
        self.assertEqual(self.check()["check_status"], "valid")
        self.assertEqual(self.calls[0][1], dict(question="When is the meeting?", answer="The meeting is Monday.",
                                                references=["The meeting is Monday."]))
        self.current_context()  # dropped from the next actual model request
        self.assertEqual(self.check()["fact_support"], "unknown")
        self.assertEqual(len(self.calls), 2)

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
        self.assertEqual(result["fact_support"], "supported")
        self.assertEqual(result["label_counts"]["Entailment"], 1)
        self.assertNotIn("claim_text", str(result))
        self.assertNotIn("private", str(result))
        self.output = dict(status="checking_failed", claims=[], claim_count=0, errors=["private error"])
        result = self.check()
        self.assertEqual((result["action"], result["fact_support"], result["check_status"]),
                         ("allow", "unknown", "unavailable"))
        self.assertNotIn("private", str(result))

    def test_executed_receipt_supports_confirmation_only_while_visible(self):
        import json
        receipt = '{"status":"sent","id":"mail-1"}'
        hook = self.hooks["office_visible_references"]
        # A plan without an executed call identity must never become evidence.
        hook.handler(dict(name="send_email", arguments={"to": "a@example.invalid"}, content=receipt), self.context)
        self.current_context(receipt)
        self.assertEqual(self.check()["check_status"], "not_applicable")
        hook.handler(dict(name="send_email", call_id="c1", arguments={"to": "a@example.invalid"},
                          content=receipt, is_error=False), self.context)
        self.current_context(receipt)
        self.check()
        evidence = json.loads(self.calls[-1][1]["references"][0])
        self.assertEqual(evidence["call_id"], "c1")
        self.assertEqual(evidence["observed_result"], receipt)
        self.current_context()
        self.assertEqual(self.check()["check_status"], "not_applicable")

    def test_unavailable_self_check_blocks_without_claiming_a_risk_finding(self):
        hooks = {h.name: h for h in build_hooks(None, "task", dict(mode="app_config",
                  bindings={"self_check_input": "unit"}))["hooks"]}
        for failed, expected in ((True, "unavailable"), (False, "valid")):
            context = SimpleNamespace(invoke=lambda *a, **kw: dict(output=dict(
                failed=failed, is_blocked=True, decision="block", unavailable_reason="truncated")))
            result = hooks["self_check_input:input_received"].handler(dict(messages=[dict(content="request")]), context)
            self.assertEqual(result["action"], "block")
            self.assertEqual(result["check_status"], expected)

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
