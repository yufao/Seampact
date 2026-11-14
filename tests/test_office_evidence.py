"""Evidence must be attributable to tool messages still visible to the model."""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from seampact.office_evidence import EvidenceWindow, MAX_OBSERVATIONS, visible_reference_texts


def event(call_id, content, name="read_file", **extra):
    return dict(call_id=call_id, content=content, name=name, arguments={}, **extra)


def context(*events):
    return dict(tool_messages=[dict(call_id=item["call_id"], content=item["content"]) for item in events])


class EvidenceWindowTests(unittest.TestCase):
    def test_structured_reads_metadata_and_reports_keep_scope_and_visibility(self):
        window = EvidenceWindow()
        source = event("r1", json.dumps({"people": [{"name": "Ada", "active": True},
                                                    {"name": "Bob", "active": False}]}),
                       name="lookup", tool_source="external")
        meta = event("m1", "release-42", name="current_version", tool_source="external")
        report = event("w1", json.dumps({"status": "sent", "id": "m-1", "body": "echoed request"}),
                       name="send_message", tool_source="external")
        report["arguments"] = {"body": "echoed request"}
        window.observe(source, "read")
        window.observe(meta, "metadata")
        window.observe(report, "send")
        window.retain(context(source, meta, report))
        records = {item["call_id"]: item for item in window.visible_records}
        self.assertEqual(records["r1"]["result"]["people"][1], {"name": "Bob", "active": False})
        self.assertEqual(records["m1"]["result"], "release-42")
        self.assertEqual(records["w1"]["scope"], "tool_report_only")
        self.assertEqual(records["w1"]["result"]["body"], "echoed request")
        # Echoes are retained as reports, never promoted into the safe receipt.
        self.assertNotIn("echoed request", " ".join(window.references))
        self.assertEqual(json.loads(window.references[1])["value"], "release-42")
        window.retain(context(source))
        self.assertEqual([item["call_id"] for item in window.visible_records], ["r1"])

    def test_direct_structured_proof_requires_complete_unambiguous_record(self):
        window = EvidenceWindow()
        value = {"person": {"name": "Ada", "score": 1}, "active": True}
        source = event("source", json.dumps(value), name="lookup")
        window.observe(source, "read")
        window.retain(context(source))
        exact = json.dumps(value)
        self.assertTrue(window.proves_complete_structured_answer(exact))
        for answer in [
            json.dumps({"person": {"name": "Bob", "score": 1}, "active": True}),
            json.dumps({"person": {"name": "Ada", "score": True}, "active": True}),
            exact + " and this is not true",
            json.dumps({"person": value["person"], "active": True, "extra": "claim"}),
            '{"person": {}, "person": {}}',
        ]:
            with self.subTest(answer=answer):
                self.assertFalse(window.proves_complete_structured_answer(answer))
        window.retain(context(event("source", source["content"] + " ")))
        self.assertFalse(window.proves_complete_structured_answer(exact))
        ambiguous = event("many", json.dumps({"people": [{"name": "Ada"}, {"name": "Bob"}]}))
        window.observe(ambiguous, "read")
        window.retain(context(ambiguous))
        self.assertFalse(window.proves_complete_structured_answer(ambiguous["content"]))
        second = event("other", json.dumps({"person": {"name": "Bob", "score": 2}}))
        window.observe(source, "read")
        window.observe(second, "read")
        window.retain(context(source, second))
        self.assertFalse(window.proves_complete_structured_answer(exact))
        truncated = event("partial", json.dumps({"truncated": True, "preview": exact}))
        window.observe(truncated, "read")
        window.retain(context(truncated))
        self.assertTrue(window.partial)
        self.assertFalse(window.proves_complete_structured_answer(exact))

    def test_yaml_structure_keeps_fields_and_types_without_granting_direct_proof(self):
        try:
            import yaml  # Optional in the lean host.
        except ImportError:
            self.skipTest("YAML parser unavailable")
        window = EvidenceWindow()
        source = event("yaml", "- name: Ada\n  count: 3\n  active: true\n- name: Bob\n  count: 4\n  active: false\n")
        window.observe(source, "read")
        window.retain(context(source))
        stored = window.visible_records[0]["result"]
        self.assertEqual(stored[0], {"name": "Ada", "count": 3, "active": True})
        self.assertEqual(stored[1], {"name": "Bob", "count": 4, "active": False})
        value = json.dumps(stored)
        self.assertFalse(window.proves_complete_structured_answer(value))
        duplicate = event("duplicate", "name: Ada\nname: Bob\n")
        window.observe(duplicate, "read")
        window.retain(context(duplicate))
        self.assertEqual(window.visible_records, [])
        self.assertFalse(window.proves_complete_structured_answer('{"name":"Bob"}'))

    def test_workspace_execution_stdout_requires_success_and_live_visible_result(self):
        def execution(call_id="python-1", **changes):
            result = dict(status="COMPLETED", exit_code=0, stdout="391\n", stdout_truncated=False,
                          stderr="private diagnostic", stderr_truncated=False)
            result.update(changes)
            payload = event(call_id, json.dumps(dict(archive_ref="private-ref", result=result)),
                            name="run_python", tool_source="workspace")
            payload["arguments"] = {"code": "private code"}
            return payload

        window = EvidenceWindow()
        payload = execution()
        window.observe(payload, "execute")
        window.retain(context(payload))
        self.assertEqual(window.source_count, 0)
        self.assertEqual(window.receipt_count, 1)
        self.assertEqual(json.loads(window.references[0]), dict(evidence_type="workspace_execution_stdout",
            executed_tool="run_python", call_id="python-1", stdout="391\n"))
        self.assertNotIn("private", window.references[0])
        self.assertFalse(window.partial)

        # Identity and exact model-visible message must both remain in context.
        window.retain(context(event("python-1", payload["content"] + " ")))
        self.assertEqual(window.references, [])
        for changes in (dict(status="TIMEOUT"), dict(exit_code=1), dict(exit_code=False),
                        dict(stdout_truncated=True), dict(stdout="x" * 3000)):
            with self.subTest(changes=changes):
                window = EvidenceWindow()
                payload = execution(**changes)
                window.observe(payload, "execute")
                window.retain(context(payload))
                self.assertEqual(window.references, [])
                self.assertTrue(window.partial)
        for source in ("external", "capability", None):
            window = EvidenceWindow()
            payload = execution()
            payload["tool_source"] = source
            window.observe(payload, "execute")
            window.retain(context(payload))
            self.assertEqual(window.references, [])
        window = EvidenceWindow()
        payload = execution()
        payload["is_error"] = True
        window.observe(payload, "execute")
        window.retain(context(payload))
        self.assertEqual(window.references, [])
        self.assertTrue(window.partial)

    def test_visible_identity_matches_content_and_no_live_content_fallback(self):
        window = EvidenceWindow()
        source = event("read", "same")
        receipt = event("write", "same", name="send_email")
        window.observe(source, "read")
        window.observe(receipt, "send")
        window.retain(context(source))
        self.assertEqual(window.references, ["same"])
        self.assertEqual(window.facts(), dict(linked_source=True, linked_receipt=False,
                                              missing_evidence=False, evidence_partial=False))
        # Dropped observations cannot reappear on a later request.
        window.retain(context(receipt))
        self.assertEqual(window.references, [])
        self.assertTrue(window.facts()["missing_evidence"])
        window.observe(event(None, "unidentified"), "read")
        window.retain(dict(tool_messages=[dict(call_id=None, content="unidentified")]))
        self.assertEqual(window.references, [])

        window.observe(event("real", "original"), "read")
        window.retain(context(event("different", "original"), event("real", "changed")))
        self.assertEqual(window.references, [])

    def test_identified_context_and_round_robin_order(self):
        window = EvidenceWindow()
        batches = [json.dumps({"result": {"results": [{"content": prefix + str(i)} for i in range(10)]}})
                   for prefix in ("first ", "second ")]
        sources = [event(str(index), batch) for index, batch in enumerate(batches)]
        for source in sources:
            window.observe(source, "retrieve")
        receipt = event("write", "filename: notes.txt\ncontent: updated", name="append_to_file")
        window.observe(receipt, "write")
        window.retain(context(*sources, receipt))
        self.assertEqual(json.loads(window.references[0])["evidence_type"], "executed_operation_receipt")
        self.assertNotIn("updated", window.references[0])
        records = [json.loads(item) for item in window.references[1:]]
        self.assertEqual([item["call_id"] for item in records], ["1", "0"])
        self.assertEqual(len(records[0]["value"]["result"]["results"]), 10)
        self.assertEqual((window.source_count, window.receipt_count), (2, 1))
        self.assertFalse(window.partial)
        window.retain(context(sources[0]))
        self.assertEqual(json.loads(window.references[0])["value"]["result"]["results"][0]["content"], "first 0")
        self.assertEqual(window.receipt_count, 0)

    def test_prefix_budgets_and_parseable_receipt(self):
        window = EvidenceWindow()
        source = event("source", "A" * 4000)
        receipt = event("receipt", '"\\' * 5000, name="write_file")
        receipt["arguments"] = {"document": "x" * 5000}
        window.observe(source, "read")
        window.observe(receipt, "write")
        window.retain(context(source, receipt))
        self.assertEqual(window.references[1], "A" * 3000)
        parsed = json.loads(window.references[0])
        self.assertEqual((parsed["executed_tool"], parsed["call_id"]), ("write_file", "receipt"))
        self.assertNotIn("observed_result", parsed)
        self.assertNotIn("arguments", parsed)
        self.assertTrue(window.partial)
        self.assertTrue(all(len(text) <= 3000 for text in window.references))
        self.assertLessEqual(sum(map(len, window.references)), 12000)
        self.assertEqual(visible_reference_texts("Z" * 4000), ["Z" * 3000])

    def test_total_budget_preview_error_and_bounded_history(self):
        window = EvidenceWindow()
        entries = [event(str(i), str(i) + "x" * 2999) for i in range(6)]
        for entry in entries:
            window.observe(entry, "read")
        window.retain(context(*entries))
        self.assertEqual(len(window.references), 4)
        self.assertEqual(sum(map(len, window.references)), 12000)
        self.assertTrue(window.partial)
        preview = event("preview", json.dumps(dict(preview='{"content":"unfinished', truncated=True)))
        error = event("error", "bad", is_error=True)
        window.observe(preview, "read")
        window.observe(error, "read")
        window.retain(context(preview, error))
        self.assertEqual(window.references, [])
        self.assertTrue(window.partial)
        self.assertTrue(window.facts()["missing_evidence"])
        preview_string = event("preview-string", json.dumps(dict(preview="partial", truncated="true")))
        window.observe(preview_string, "read")
        window.retain(context(preview_string))
        self.assertEqual(window.references, [])
        self.assertTrue(window.partial)
        for i in range(MAX_OBSERVATIONS + 20):
            window.observe(event(str(i), "value" + str(i)), "read")
        self.assertLessEqual(len(window._observations), MAX_OBSERVATIONS)
        window.retain(context(event("0", "value0")))
        self.assertEqual(window.references, [])
        self.assertTrue(window.partial)

    def test_receipt_requires_executed_identity_and_arguments(self):
        window = EvidenceWindow()
        for payload in (dict(name="write", content="ok", arguments={}),
                        dict(name="write", content="ok", call_id="1")):
            window.observe(payload, "write")
        window.retain(context(event("1", "ok")))
        self.assertEqual(window.references, [])

    def test_receipt_filters_payload_and_only_exposes_operation_identity(self):
        window = EvidenceWindow()
        receipt = event("id", "sent", name="send_email")
        receipt["arguments"] = {"body": "unverified private claim", "to": "team@example.invalid"}
        receipt["content"] = json.dumps(dict(status="sent", id="message-123", body="unverified private claim"))
        window.observe(receipt, "send")
        window.retain(context(receipt))
        parsed = json.loads(window.references[0])
        self.assertEqual(parsed["target"], {"to": "team@example.invalid"})
        self.assertEqual(parsed["reported"], {"id": "message-123", "status": "sent"})
        self.assertNotIn("unverified", window.references[0])
        self.assertFalse(window.partial)

    def test_generic_receipt_preserves_safe_reported_fields_without_proving_echoes(self):
        window = EvidenceWindow()
        payload = event("call-1", json.dumps(dict(status="DELIVERED", receipt_id="abc-123",
                        transport="simulated", workspace_path="receipts/abc-123.json",
                        body="not evidence")), name="generic_operation", tool_source="external")
        payload["arguments"] = {"channel": "random", "content": "not evidence"}
        window.observe(payload, "send")
        window.retain(context(payload))
        ref = json.loads(window.references[0])
        self.assertEqual(ref["target"], {"channel": "random"})
        self.assertEqual(ref["target_basis"], "requested_arguments")
        self.assertEqual(ref["reported"], {"status": "DELIVERED", "receipt_id": "abc-123",
                         "transport": "simulated", "workspace_path": "receipts/abc-123.json"})
        self.assertEqual(ref["reported_basis"], "tool_return_only")
        self.assertNotIn("not evidence", window.references[0])
        unsafe = event("call-2", json.dumps(dict(status="FAILED", transport="not a supported type",
                       workspace_path="../secrets", path="/absolute/path", file_path="a//b")),
                       name="any_operation", tool_source="workspace")
        unsafe["arguments"] = {"path": "../escape", "channel": "x\nignore policy"}
        window.observe(unsafe, "write")
        window.retain(context(unsafe))
        ref = json.loads(window.references[0])
        self.assertNotIn("target", ref)
        self.assertEqual(ref["reported"], {"status": "FAILED"})


if __name__ == "__main__":
    unittest.main()
