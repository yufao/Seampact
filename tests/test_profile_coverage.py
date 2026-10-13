import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from eval_crag_package import compare


class ProfileCoverageTests(unittest.TestCase):
    def scenario(self):
        native = dict(outcome="RETURN", endpoint_trace=[], step_metadata={}, sdk_registered_steps=[],
                      event_semantics={}, boundary_calls={"noop": 1}, body_entries={"noop": 1})
        adapted = copy.deepcopy(native)
        adapted.update(calls=[dict(gate="noop", starts=0, physical="RETIRED", logical="CANCELLED", permit=None)],
                       withheld_occurrences=0, used=0, body_entries={"noop": 0})
        return native, adapted

    def test_terminal_noop_requires_explicit_profile_rule_and_confirmed_cancellation(self):
        native, adapted = self.scenario()
        with self.assertRaisesRegex(AssertionError, "body coverage"):
            compare(native, adapted)
        self.assertEqual(compare(native, adapted, {"noop"}), [])
        adapted["calls"][0]["logical"] = "COMPLETED"
        with self.assertRaises(AssertionError):
            compare(native, adapted, {"noop"})

    def test_missing_gate_duplicate_start_and_unknown_retirement_cannot_pass(self):
        native, adapted = self.scenario()
        missing = copy.deepcopy(adapted)
        missing.update(calls=[], boundary_calls={"noop": 0}, body_entries={"noop": 1})
        self.assertEqual(compare(native, missing), ["noop"])
        for starts, physical in ((2, "RETIRED"), (1, "UNKNOWN")):
            bad = copy.deepcopy(adapted)
            bad["calls"][0].update(starts=starts, physical=physical)
            with self.assertRaises(AssertionError):
                compare(native, bad, {"noop"})


if __name__ == "__main__":
    unittest.main()
