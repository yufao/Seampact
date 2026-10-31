import copy
import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from seampact.analysis_feedback import compact_report


class TestCompactReport(unittest.TestCase):
    def test_preserves_decisions_interfaces_mappings_and_issue_origins(self):
        issue = {"issue_code": "bad_map", "entry_id": "e", "dependency_id": "d", "field_paths": ["mappings[0]"], "repair_scope": "source", "model_repairable": False, "reason": "unknown", "repair_action": "inspect", "source_evidence": [{"path": "a", "start": 1}, {"path": "b"}, {"path": "c"}]}
        report = {"status": "MISSING", "outcome": "UNKNOWN", "execution": "UNKNOWN", "admitted": False,
                  "feedback": {"external_conditions": [issue]},
                  "entry_readiness": [{"entry_id": "e", "status": "UNKNOWN", "parameters": {"x": "int"}, "issues": [issue], "operations": {"boundaries": {"b": {"status": "UNKNOWN"}}}, "dependency_closure": {"call_edges": [1, 2]}}],
                  "dependencies": [{"id": "d", "mappings": [{"entry_id": "e", "field": "f", "parameter": "p"}], "entry_checks": {"e": {"status": "MISSING"}}}],
                  "entry_bindings": {"runtime_groups": {"groups": [{"status": "LOCAL_ONLY", "same_process": True}]}},
                  "classification": {"profiles": [1, 2]}}
        before = copy.deepcopy(report)
        result = compact_report(report, {"id": "r"})
        assert report == before
        assert result["schema"] == "compact-check-feedback/1"
        assert result["report_ref"] == {"id": "r"}
        assert result["execution"] == "UNKNOWN" and result["admitted"] is False
        assert result["entry_readiness"][0]["parameters"] == {"x": "int"}
        assert result["entry_readiness"][0]["operations"]["boundaries"]["b"]["status"] == "UNKNOWN"
        assert result["dependencies"][0]["mappings"][0]["field"] == "f"
        assert result["dependencies"][0]["entry_checks"]["e"]["status"] == "MISSING"
        assert result["entry_bindings"]["runtime_groups"]["groups"][0]["status"] == "LOCAL_ONLY"
        assert len(result["feedback"]["external_conditions"]) == 1
        assert len(result["feedback"]["external_conditions"][0]["source_paths"]) == 2
        assert result["feedback"]["external_conditions"][0]["evidence_count"] == 3
        assert len(result["feedback"]["external_conditions"][0]["evidence_preview"]) == 2
        assert any(d["path"] == "/classification" for d in result["details_not_inlined"])
        result["report_ref"]["id"] = "changed"
        result["feedback"]["external_conditions"][0]["field_paths"].append("x")
        assert report == before


    def test_distinct_obligations_not_merged_and_no_permissions_inferred(self):
        a = {"issue_code": "mapping", "field_paths": ["mappings[0]"], "reason": "first", "repair_action": "a"}
        b = {"issue_code": "mapping", "field_paths": ["mappings[1]"], "reason": "second", "repair_action": "b"}
        result = compact_report({"feedback": {"model_repairs": [a, b]}, "status": "UNKNOWN"}, "r")
        assert len(result["feedback"]["model_repairs"]) == 2
        assert "admitted" not in result and "model_repairable" not in result["feedback"]["model_repairs"][0]
        assert result["feedback"]["model_repairs"][1]["field_paths"] == ["mappings[1]"]
