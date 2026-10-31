"""Independent diagnostics must survive unselected resources and partial config."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from seampact.candidate_checks import candidate_issues, dictionary_requirements
from seampact.inspection import Inspector
from seampact.intake_feedback import explain_candidate


class CandidateChecks(unittest.TestCase):
    def source(self, code):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "source.py").write_text(code, encoding="utf-8")
        return Inspector.freeze(root, root / "snapshots", "r", ["source.py"], "goal", "original")

    def test_incomplete_dictionary_and_wrong_reference_are_both_reported(self):
        inspector = self.source("def run(config):\n    workers = config['max_workers']\n    generation = config['llm']['generation']\n    return generation.get('temperature', 0)\n")
        entry = dict(id="run", path="source.py", symbol="run", kind="function",
            fixed_arguments=dict(config=dict(llm=dict(model={"$dependency": dict(id="svc", field="model")}))))
        dependency = dict(id="svc", kind="service", configurable=True, required=True, entry_ids=["run"],
                          mappings=[dict(entry_id="run", location="environment", parameter="MODEL", field="model")])
        issues = candidate_issues(inspector, entry, [dependency])
        self.assertEqual({i["issue_code"] for i in issues}, {"configuration_incomplete", "dependency_destination_mismatch"})
        self.assertIn("fixed_arguments.config.max_workers", [p for i in issues for p in i["field_paths"]])
        report = explain_candidate(inspector, dict(entries=[entry], dependencies=[dependency]), dict(
            entry_bindings=dict(entries=[dict(id="run", status="MISSING", reason="resource unselected")]), dependencies=[]))
        self.assertEqual(report["outcome"], "NEEDS_CANDIDATE_REPAIR")
        self.assertGreaterEqual(len(report["feedback"]["model_repairs"]), 3)
        self.assertFalse(report["execution_contracts"][0]["admission_granted"])
        self.assertEqual(report["execution_contracts"][0]["resources"]["initialization"], "NOT_RUN")

    def test_optional_keys_and_conditional_reads_do_not_become_required(self):
        inspector = self.source("def run(config):\n    optional = config.get('optional', {})\n    if optional:\n        result = config['only_on_branch']\n    return 1\n")
        entry = dict(id="run", path="source.py", symbol="run", kind="function", fixed_arguments=dict(config={}))
        self.assertEqual(dictionary_requirements(inspector, entry), [])
        self.assertEqual(candidate_issues(inspector, entry), [])

    def test_complete_original_config_and_nested_alias_reads(self):
        inspector = self.source("def run(config):\n    generation = config['llm']['generation']\n    tokens = generation['max_tokens']\n    return tokens\n")
        entry = dict(id="run", path="source.py", symbol="run", kind="function",
                     fixed_arguments=dict(config=dict(llm=dict(generation={}))))
        self.assertEqual(candidate_issues(inspector, entry)[0]["field_paths"], ["fixed_arguments.config.llm.generation.max_tokens"])
        entry["fixed_arguments"]["config"]["llm"]["generation"]["max_tokens"] = 12
        self.assertEqual(candidate_issues(inspector, entry), [])

    def test_task_config_is_not_filled_with_invented_values(self):
        inspector = self.source("def run(config):\n    return config['required']\n")
        entry = dict(id="run", path="source.py", symbol="run", kind="function")
        self.assertEqual(candidate_issues(inspector, entry), [])
        self.assertNotIn("fixed_arguments", entry)


if __name__ == "__main__":
    unittest.main()
