"""Office fact checks use only material in the actual model context."""
import json
import io
import sys
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from seampact.office_application import build_hooks, fact_check_status
from seampact.project_applications import compatible


class OfficeFactCheckTests(unittest.TestCase):
    def setUp(self):
        config = dict(mode="app_config", guard_candidates=[dict(role="fact_check", unit_id="unit",
                      guard=dict(kind="fact_support", subjects=["message", "response"], evidence="references"))],
                      operations={"search_documents_example": {"kind": "retrieve"}})
        self.hooks = {h.name: h for h in build_hooks(None, "task", config)["hooks"]}
        self.calls = []
        self.observation_ids = {}
        self.output = dict(status="ok", claim_count=1, claims=[dict(
            claim_text="The meeting is Monday", labels_by_reference=["Entailment"], label="Entailment")])
        def invoke(unit, arguments, **options):
            self.assertEqual(options, {"private_events": True})
            self.calls.append((unit, arguments))
            return dict(output=self.output)
        self.context = SimpleNamespace(invoke=invoke)
        self.hooks["office_input_context"].handler(dict(messages=[dict(content="When is the meeting?")]), self.context)

    def observe(self, content, name="read_file", is_error=False):
        kind = {"read_file": "read", "send_email": "send", "search_documents_example": "retrieve"}[name]
        call_id = "observation-" + str(len(self.observation_ids))
        self.observation_ids[content] = call_id
        self.hooks["office_visible_references"].handler(dict(name=name, operation={"kind": kind},
                                                             call_id=call_id, arguments={}, content=content,
                                                             is_error=is_error), self.context)

    def current_context(self, *contents):
        self.hooks["office_reference_context"].handler(dict(tool_messages=[dict(
            call_id=self.observation_ids.get(content, "unobserved"), content=content) for content in contents]), self.context)

    def check(self):
        return self.hooks["fact_check:before_delivery"].handler(dict(text="The meeting is Monday."), self.context)

    def test_complete_structured_proof_and_natural_language_fallback(self):
        value = {"person": "Ada", "score": 3, "active": True}
        source = json.dumps(value)
        self.observe(source)
        self.current_context(source)
        self.hooks["office_input_context"].handler(dict(messages=[dict(content=source)]), self.context)
        hook = self.hooks["fact_check:before_delivery"]
        result = hook.handler(dict(text=source), self.context)
        self.assertEqual((result["fact_support"], result["proof"]),
                         ("supported", "complete_visible_structured_result"))
        self.assertFalse(self.calls)
        # The exact result proves fidelity, not that it answers the request.
        self.hooks["office_input_context"].handler(dict(messages=[dict(content="What is Bob's score?")]), self.context)
        result = hook.handler(dict(text=json.dumps({"person": "Bob", "score": 3, "active": True})), self.context)
        self.assertEqual(result["fact_support"], "supported")
        self.assertEqual(len(self.calls), 1)  # NLI made this decision.
        self.assertNotIn("proof", result)

    def test_revision_feedback_does_not_claim_completed_actions_were_reversed(self):
        self.observe('The record says Tuesday.')
        self.current_context('The record says Tuesday.')
        self.output["claims"][0].update(label="Contradiction", labels_by_reference=["Contradiction"])
        result = self.check()
        self.assertIn("不会因答复受阻而撤销", result["reason"])
        self.assertIn("不要重复执行", result["reason"])

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

    def test_non_support_is_an_observation_for_delivery_but_an_outbound_finding(self):
        source = '{"content":"The room was reserved."}'
        self.observe(source)
        self.current_context(source)
        hook = self.hooks["fact_check:before_tool"]
        payload = dict(name="send_email", operation={"kind": "send"}, arguments=dict(body="The meeting is Monday."))
        for label in ("NotSupported", "Neutral"):
            self.output["claims"][0].update(label=label, labels_by_reference=[label])
            result = self.check()
            self.assertEqual((result["action"], result["fact_finding"]), ("allow", "unconfirmed"))
            self.assertNotIn("support_score", result)
            self.assertEqual(hook.handler(payload, self.context)["action"], "deny_operation")
        self.output["claims"][0]["support_probabilities"] = [0.13]
        self.assertEqual(self.check()["support_score"], 0.13)

    def test_executed_receipt_supports_confirmation_only_while_visible(self):
        receipt = '{"status":"sent","id":"mail-1"}'
        hook = self.hooks["office_visible_references"]
        # A plan without an executed call identity must never become evidence.
        hook.handler(dict(name="send_email", operation={"kind": "send"}, arguments={"to": "a@example.invalid"}, content=receipt), self.context)
        self.current_context(receipt)
        self.assertEqual(self.check()["check_status"], "not_applicable")
        hook.handler(dict(name="send_email", operation={"kind": "send"}, call_id="c1", arguments={"to": "a@example.invalid"},
                          content=receipt, is_error=False), self.context)
        self.observation_ids[receipt] = "c1"
        self.current_context(receipt)
        self.check()
        evidence = json.loads(self.calls[-1][1]["references"][0])
        self.assertEqual(evidence["call_id"], "c1")
        self.assertEqual(evidence["reported"], {"status": "sent", "id": "mail-1"})
        self.current_context()
        self.assertEqual(self.check()["check_status"], "not_applicable")

    def test_visible_workspace_stdout_reaches_fact_check_with_source(self):
        source = '{"content":"The meeting is Monday."}'
        self.observe(source)
        output = json.dumps(dict(archive_ref="ref", result=dict(status="COMPLETED", exit_code=0,
                            stdout="391\n", stdout_truncated=False, stderr="private")))
        self.hooks["office_visible_references"].handler(dict(name="run_python", tool_source="workspace",
            operation={"kind": "execute"}, call_id="python-1", arguments={"code": "private code"},
            content=output, is_error=False), self.context)
        self.observation_ids[output] = "python-1"
        self.current_context(source, output)
        self.output["claims"][0]["labels_by_reference"] = ["Entailment", "Entailment"]
        result = self.check()
        self.assertEqual((result["reference_count"], result["source_reference_count"],
                          result["receipt_reference_count"]), (2, 1, 1))
        references = self.calls[-1][1]["references"]
        self.assertEqual(json.loads(references[0])["stdout"], "391\n")
        self.assertEqual(references[1], "The meeting is Monday.")
        self.assertNotIn("private", str(references))
        self.current_context(source)
        self.output["claims"][0]["labels_by_reference"] = ["Entailment"]
        self.check()
        self.assertEqual(self.calls[-1][1]["references"], ["The meeting is Monday."])

    def test_ranked_reads_and_executed_write_share_bounded_references(self):
        batches = [json.dumps({"result": {"output": {"results": [{"content": prefix + str(i)}
                   for i in range(10)]}}}) for prefix in ("first ", "second ")]
        for batch in batches:
            self.observe(batch, name="search_documents_example")
        receipt = "filename: notes.txt\ncontent: updated"
        event = dict(name="append_to_file", operation={"kind": "write"}, call_id="c2", arguments={"file_id": "9", "content": "updated"},
                     content=receipt, is_error=False)
        self.hooks["office_visible_references"].handler(dict(event, content="failed", is_error=True), self.context)
        self.hooks["office_visible_references"].handler(event, self.context)
        self.observation_ids[receipt] = "c2"
        self.current_context(*batches, receipt, "failed")
        self.output["claims"][0]["labels_by_reference"] = ["Entailment"] * 3
        result = self.check()
        texts = self.calls[-1][1]["references"]
        self.assertEqual((result["reference_count"], result["receipt_reference_count"]), (3, 1))
        operation = json.loads(texts[0])
        self.assertEqual((operation["call_id"], operation["target"]), ("c2", {"file_id": "9"}))
        self.assertNotIn("updated", texts[0])
        self.assertEqual([json.loads(text)["call_id"] for text in texts[1:]],
                         [self.observation_ids[batches[1]], self.observation_ids[batches[0]]])
        self.assertEqual(len(json.loads(texts[1])["value"]["result"]["output"]["results"]), 10)
        self.current_context(batches[0])
        self.output["claims"][0]["labels_by_reference"] = ["Entailment"]
        self.assertEqual(self.check()["receipt_reference_count"], 0)
        self.assertEqual(json.loads(self.calls[-1][1]["references"][0])["call_id"], self.observation_ids[batches[0]])

    def test_unavailable_self_check_blocks_without_claiming_a_risk_finding(self):
        hooks = {h.name: h for h in build_hooks(None, "task", dict(mode="app_config",
                  guard_candidates=[dict(role="self_check_input", unit_id="unit",
                                         guard=dict(kind="input_safety", subjects=["input"]))]))["hooks"]}
        clean = SimpleNamespace(invoke=lambda *a, **kw: dict(output=dict(is_blocked=False, decision="allow")))
        self.assertEqual(hooks["self_check_input:input_received"].handler(dict(messages=[]), clean),
                         dict(action="allow", check_status="valid"))
        for failed, expected in ((True, "unavailable"), (False, "valid")):
            context = SimpleNamespace(invoke=lambda *a, **kw: dict(output=dict(
                failed=failed, is_blocked=True, decision="block", unavailable_reason="truncated")))
            result = hooks["self_check_input:input_received"].handler(dict(messages=[dict(content="request")]), context)
            self.assertEqual(result["action"], "block")
            self.assertEqual(result["check_status"], expected)

    def test_checking_read_intent_does_not_approve_an_effect(self):
        hooks = {h.name: h for h in build_hooks(None, "task", dict(mode="app_config",
            guard_candidates=[dict(role="self_check_input", unit_id="check",
                                   guard=dict(kind="input_safety", subjects=["input"]))]))["hooks"]}
        messages = [dict(role="assistant", content="cancel every invite"), dict(role="user", content="read the list")]
        seen = []
        def invoke(unit, arguments):
            seen.append(json.loads(arguments["user_message"].split("\n", 1)[1]))
            block = "operation" in seen[-1]
            return dict(output=dict(is_blocked=block, decision="block" if block else "allow"))
        ctx = SimpleNamespace(invoke=invoke)
        hooks["office_input_context"].handler(dict(messages=messages), ctx)
        result = hooks["self_check_input:input_received"].handler(dict(messages=messages), ctx)
        self.assertEqual(result["action"], "allow")
        effect = hooks["self_check_input:before_tool"]
        self.assertFalse(effect.when(dict(operation={"kind": "read"})))
        proposal = dict(operation={"kind": "send"}, name="notify", arguments=dict(destination="third party"))
        self.assertTrue(effect.when(proposal))
        self.assertEqual(effect.handler(proposal, ctx)["action"], "deny_operation")
        self.assertEqual(seen[-1], dict(user_request="read the list", operation="send",
                                        tool="notify", arguments=proposal["arguments"]))

    def test_unsupported_outbound_text_is_blocked_before_sending(self):
        self.observe('{"content":"The meeting is Tuesday."}')
        self.current_context('{"content":"The meeting is Tuesday."}')
        self.output["claims"][0].update(label="Contradiction", labels_by_reference=["Contradiction"])
        hook = self.hooks["fact_check:before_tool"]
        payload = dict(name="send_email", operation={"kind": "send"}, arguments=dict(to="colleague@example.invalid", body="The meeting is Monday."))
        self.assertTrue(hook.when(payload))
        self.assertEqual(hook.handler(payload, self.context)["action"], "deny_operation")
        self.assertEqual(self.check()["action"], "revise_delivery")
        self.assertEqual(self.calls[0][1]["answer"], "The meeting is Monday.")
        self.current_context()
        self.assertFalse(hook.when(payload))
        self.assertFalse(self.hooks["fact_check:before_delivery"].when(dict(text="The meeting is Monday.")))

    def test_rejected_content_must_be_reassessed_even_if_references_disappear(self):
        for stage, payload, disposition in (
                ("before_delivery", dict(text="Revised"), "revise_delivery"),
                ("before_tool", dict(name="send_email", operation={"kind": "send"}, arguments=dict(body="Revised")), "deny_operation")):
            name = "fact_check:" + stage
            payload["revision_required_hooks"] = [name]
            hook = self.hooks[name]
            self.assertTrue(hook.when(payload))
            result = hook.handler(payload, self.context)
            self.assertEqual((result["action"], result["coverage"]), (disposition, "no_visible_references"))
            self.assertFalse(self.calls)

    def test_alternative_providers_share_evidence_and_keep_recheck_identity(self):
        captured, feedback = {}, []
        def selector(task_id, bindings, observe):
            captured.update(bindings)
            def choose(event, payload, remaining):
                feedback.append(payload.get("checks", {}))
                return dict(kind="run_hook", hook=remaining[-1].name)
            return SimpleNamespace(choose_next=choose)
        candidates = [dict(role="fact_check", unit_id=unit,
                           guard=dict(subjects=["response"], kind="fact_support", evidence="references"))
                      for unit in ("one", "two")]
        app = build_hooks(SimpleNamespace(execution=SimpleNamespace(optional_selector=selector)), "task",
                          dict(mode="scheduler", guard_candidates=candidates))
        hooks = {hook.name: hook for hook in app["hooks"]}
        self.assertEqual(captured["fact_check:before_delivery:two"]["unit_id"], "two")
        self.assertNotIn("fact_check:before_tool:two", hooks)
        self.assertEqual(sum(h.name == "office_delivery_coverage" for h in app["hooks"]), 1)
        hooks["office_input_context"].handler(dict(messages=[dict(content="Question")]), self.context)
        hooks["office_visible_references"].handler(dict(name="lookup", operation={"kind": "read"},
                                                       call_id="lookup-1", content="Evidence"), self.context)
        hooks["office_reference_context"].handler(dict(tool_messages=[dict(call_id="lookup-1", content="Evidence")]), self.context)
        choices = [hooks["fact_check:before_delivery:one"], hooks["fact_check:before_delivery:two"]]
        payload = dict(text="Answer")
        choice = app["optional_hooks"].choose_next("before_delivery", payload, choices)
        self.assertEqual(choice["hook"], "fact_check:before_delivery:two")
        outcome = hooks[choice["hook"]].handler(payload, self.context)
        self.assertEqual(self.calls[-1][0], "two")
        self.assertEqual(self.calls[-1][1]["references"], ["Evidence"])
        following = app["optional_hooks"].choose_next("before_delivery",
            dict(payload, checks={choice["hook"]: outcome}), choices[:1])
        self.assertEqual(following["hook"], "fact_check:before_delivery:one")
        self.assertEqual(feedback[-1][choice["hook"]], outcome)
        hooks["office_reference_context"].handler(dict(tool_messages=[]), self.context)
        recheck = dict(text="Revised", revision_required_hooks=[choice["hook"]])
        self.assertTrue(hooks[choice["hook"]].when(recheck))
        self.assertEqual(hooks[choice["hook"]].handler(recheck, self.context)["action"], "revise_delivery")

    def test_resource_rule_uses_recorded_cost_and_stops_after_support(self):
        candidates = [dict(role="fact_check", unit_id=unit,
                           guard=dict(subjects=["response"], kind="fact_support", evidence="references"))
                      for unit in ("fast", "slow")]
        execution = SimpleNamespace(recent_call_cost={"fast": 0.1, "slow": 3.0}.get)
        app = build_hooks(SimpleNamespace(execution=execution), "task",
                          dict(mode="resource_rule", guard_candidates=candidates))
        hooks = {hook.name: hook for hook in app["hooks"]}
        hooks["office_visible_references"].handler(dict(name="lookup", operation={"kind": "read"},
            call_id="lookup-1", content="Source text"), self.context)
        hooks["office_reference_context"].handler(dict(tool_messages=[dict(call_id="lookup-1", content="Source text")]), self.context)
        fast, slow = (hooks["fact_check:before_delivery:" + unit] for unit in ("fast", "slow"))
        choose = app["optional_hooks"].choose_next
        self.assertEqual(choose("before_delivery", {}, [slow, fast])["hook"], fast.name)
        for verdict, expected in (("supported", "continue"), ("unconfirmed", "run_hook")):
            checks = {fast.name: dict(check_status="valid", fact_support=verdict, fact_finding=verdict)}
            self.assertEqual(choose("before_delivery", dict(checks=checks), [slow])["kind"], expected)

    def test_required_role_demands_a_single_provider(self):
        candidates = [dict(role="privacy", unit_id=unit, guard=dict(kind="privacy", subjects=["message"]))
                      for unit in ("one", "two")]
        with self.assertRaisesRegex(ValueError, "明确选择唯一能力"):
            build_hooks(None, "task", dict(mode="app_config", guard_candidates=candidates,
                                          guard_policy={"required_outbound": ["privacy"]}))
        candidates[1]["guard"]["subjects"] = ["response"]
        app = build_hooks(None, "task", dict(mode="app_config", guard_candidates=candidates,
                                             guard_policy={"required_outbound": ["privacy"]}))
        required = [hook for hook in app["hooks"] if hook.required and hook.event == "before_tool"]
        self.assertEqual([hook.name for hook in required], ["privacy:before_tool:one"])

    def test_missing_guard_is_not_mounted(self):
        app = build_hooks(None, "task", dict(mode="app_config",
                          guard_candidates=[dict(role="privacy", unit_id="unit", guard=None)]))
        self.assertFalse(any(hook.name.startswith("privacy:") for hook in app["hooks"]))

    def test_code_scan_uses_declared_workspace_file_not_tool_name(self):
        paths, calls = [], []
        projects = SimpleNamespace(lock=nullcontext(), task_scope=lambda _: dict(
            project_id="p", conversation_id="c", status="ACTIVE"),
            file_path=lambda *parts: (paths.append(parts), SimpleNamespace(open=lambda _: io.BytesIO(b"print(1)")))[1])
        context = SimpleNamespace(invoke=lambda unit, args: (calls.append((unit, args)),
                                                             dict(output={"issues": []}))[1])
        app = build_hooks(SimpleNamespace(projects=projects), "task", dict(mode="app_config",
                          guard_candidates=[dict(role="code_scan", unit_id="scan",
                                                 guard=dict(kind="code_safety", subjects=["code"]))]))
        hook = next(h for h in app["hooks"] if h.name == "code_scan:before_tool")
        payload = dict(name="arbitrary", operation={"kind": "execute", "code_workspace_file": "solution.py"},
                       arguments={})
        self.assertTrue(hook.when(payload))
        self.assertEqual(hook.handler(payload, context)["action"], "allow")
        self.assertEqual(paths[0][-1], "solution.py")
        self.assertEqual(calls[0][1]["files"][0]["source"], "print(1)")
        payload["operation"]["code_workspace_file"] = "C:/secret.py"
        self.assertEqual(hook.handler(payload, context)["check_status"], "unavailable")
        self.assertEqual(len(paths), 1)

    def test_perception_tracks_visible_sources_even_without_fact_checker(self):
        def selector(task_id, bindings, observe):
            self.assertEqual(bindings["privacy:before_tool"]["hook_family"], "privacy")
            return SimpleNamespace(choose_next=lambda event, payload, remaining: observe(event, payload))
        platform = SimpleNamespace(execution=SimpleNamespace(optional_selector=selector))
        app = build_hooks(platform, "task", dict(mode="ppo", guard_candidates=[dict(
            role="privacy", unit_id="unit", guard=dict(kind="privacy", subjects=["message"]))],
            operations={"lookup": {"kind": "retrieve"}, "notify": {"kind": "send"}}))
        hooks = {h.name: h for h in app["hooks"]}
        # Choosing an applicable first-time check remains voluntary for the policy.
        check = hooks["privacy:before_tool"]
        outbound = dict(name="notify", arguments=dict(content="summary"))
        self.assertTrue(check.when(outbound))
        self.assertFalse(check.required)
        hooks["office_visible_references"].handler(dict(name="lookup", call_id="c", content="Document."), self.context)
        retain = hooks["office_reference_context"].handler
        retain(dict(tool_messages=[dict(call_id="c", content="Document.")]), self.context)
        facts = app["optional_hooks"].choose_next("before_tool", outbound, [check])
        self.assertEqual((facts["operation_effect"], facts["operation_phase"], facts["operation_destination"]),
                         ("send", "pending", "external"))
        self.assertTrue(facts["linked_source"])
        self.assertFalse(facts["linked_receipt"])
        retain(dict(tool_messages=[]), self.context)
        self.assertTrue(app["optional_hooks"].choose_next("before_tool", outbound, [check])["missing_evidence"])

    def test_generated_output_schema_is_accepted_for_explicit_role(self):
        unit = dict(entry_id="fact_check_response", capability=dict(
            guard=dict(kind="fact_support", subjects=["response"], evidence="references",
                       provenance=[dict(path="source.py", start=1, end=1)]),
            input=dict(required=["question", "answer", "references"], properties=dict(
                question=dict(type="string"), answer=dict(type="string"),
                references=dict(type="array", items=dict(type="string")))),
            output=dict(kind="json", schema=dict(required=["status", "claims", "claim_count"], properties=dict(status=dict(type="string"),
                claim_count=dict(type="integer"), claims=dict(type="array", items=dict(properties=dict(
                    claim_text=dict(type="string"), label=dict(type="string"),
                    labels_by_reference=dict(type="array", items=dict(type="string"))))))))))
        self.assertTrue(compatible("fact_check", unit))
        unit["capability"]["output"]["schema"]["properties"]["claims"]["items"]["properties"].pop("labels_by_reference")
        self.assertFalse(compatible("fact_check", unit))


if __name__ == "__main__":
    unittest.main()
