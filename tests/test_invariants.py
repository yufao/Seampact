"""Concrete package installation conflicts, independent of model assertions."""

import tempfile
import unittest
from pathlib import Path

from seampact.build import build_package
from seampact.inspection import Inspector
from seampact.verification import check_candidate


SOURCE = '''class Native:
    async def evaluate(self, nodes):
        results = []
        for node in nodes:
            results.append(node)
        return results
'''


class PackageInvariantTests(unittest.TestCase):
    def test_outer_callable_gate_and_inner_loop_gate_require_alternative_plans(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "native.py").write_text(SOURCE, encoding="utf-8")
            inspector = Inspector.freeze(root, root / "snapshots", "request", ["native.py"],
                                         "Native.evaluate", "original result and continuation")
            for modes in (("W", "R"), ("R", "H"), ("W", "H", "R")):
                candidate = dict(boundaries=[dict(id="boundary" + str(i), path="native.py",
                    symbol="Native.evaluate", mode=mode) for i, mode in enumerate(modes)])
                with self.subTest(modes=modes):
                    report = check_candidate(inspector, candidate)
                    self.assertFalse(any(row["buildable"] for row in report["boundaries"]))
                    self.assertFalse(any(operation["admitted"] for row in report["boundaries"]
                                         for operation in row["operations"].values()))
                    with self.assertRaises(ValueError):
                        build_package(inspector, candidate, root / "packages", "conflicting")
