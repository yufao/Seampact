"""Failure evidence does not invoke unknown object hooks."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from seampact.scenes.evaluation_evidence import PassiveEvidenceGap
from seampact.scenes.scenarios import SceneContext


class FailureEvidenceTests(unittest.TestCase):

    def test_unknown_metaclass_equality_and_serializer_hooks_are_never_run(self):
        hooks = []
        class Meta(type):
            def __eq__(cls, other):
                hooks.append("equality")
                return False
            def __hash__(cls):
                hooks.append("hash")
                return 0
        class Opaque(metaclass=Meta):
            def __deepcopy__(self, memo):
                hooks.append("deepcopy")
                return None
            def __reduce_ex__(self, protocol):
                hooks.append("serializer")
                return dict, ()
        context = SceneContext(type("Platform", (), dict(profiles=object()))(), "fixture", catalog=object())
        value = Opaque()
        source = dict(owner=value, ordinary=[1])
        context.record_failure_evidence(source)
        source.pop("owner")
        source["ordinary"].append(2)
        with self.assertRaises(PassiveEvidenceGap):
            context.freeze_failure_evidence()
        self.assertIs(context.active_failure_terminal["evidence"]["owner"], value)
        self.assertEqual(context.active_failure_terminal["evidence"]["ordinary"], [1])
        self.assertEqual(hooks, [])


if __name__ == "__main__":
    unittest.main()
