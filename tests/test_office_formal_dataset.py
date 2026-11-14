"""Family isolation and counting checks for local dataset preparation."""
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from prepare_office_formal_dataset import assemble, make_groups, summarize


class OfficeFormalDatasetTests(unittest.TestCase):
    def test_prefix_and_normalized_prompt_edges_propagate_as_one_family(self):
        data = [
            dict(id="old-1", instruction="Calculate the total in A1 for 12 approved expenses"),
            dict(id="old-2", instruction="Find the latest delivery date in the purchase register"),
            dict(id="new", instruction="Calculate the total in B9 for 99 approved expenses"),
            dict(id="distinct", instruction="Remove duplicate supplier names before sorting the sheet"),
        ]
        family_for, groups, _ = make_groups(data)
        self.assertEqual(family_for["old-1"], family_for["old-2"])
        self.assertEqual(family_for["new"], family_for["old-1"])
        self.assertNotEqual(family_for["distinct"], family_for["old-1"])
        reordered, reordered_groups, _ = make_groups(list(reversed(data)))
        self.assertEqual(family_for, reordered)
        self.assertEqual(groups, reordered_groups)

    def test_near_identical_long_instructions_join_beyond_exact_equality(self):
        text = ("Extract all approved invoice rows from the register and copy the matching supplier "
                "names dates amounts and payment references to the output sheet without changing the source data")
        family_for, _, edges = make_groups([
            dict(id="a", instruction=text),
            dict(id="b", instruction=text + " please"),
        ])
        self.assertEqual(family_for["a"], family_for["b"])
        self.assertTrue(any(edge["reason"] == "instruction_token_trigram_jaccard" for edge in edges))

    def test_variants_do_not_inflate_originals_or_families(self):
        rows = [dict(original_task="one", family_id="family", variant=variant)
                for variant in ("normal", "risk_attempt", "risk_attempt")]
        self.assertEqual(summarize(rows), dict(distinct_cases=3, original_tasks=1, families=1,
                                               normal_cases=1, risk_cases=2))

    def test_finalization_rejects_family_leakage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.json"
            source.write_text(json.dumps({}), encoding="utf-8")
            args = SimpleNamespace(output=root, seed=1, holdout_fraction=0.15, rag_metadata=None,
                                   run_evidence=[], **{name: source for name in
                                       ("archive", "prior_workload", "prior_spreadsheet", "agentdojo", "privacy")})
            dojo = dict(cases=[dict(id="first", split="train_pool", family_id="shared", suite="workspace",
                                    user_task_id="user_task_1")])
            sheets = dict(cases=[dict(id="second", split="evaluation", family_id="shared", source_id="different")])
            with self.assertRaisesRegex(ValueError, "family crosses"):
                assemble(args, {}, dojo, dict(cases=[]), sheets, {"prior_workload": {}})


if __name__ == "__main__":
    unittest.main()
