"""Original inherited calls retain their subclass receiver and source checks."""

import inspect
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from seampact.inspection import Inspector
from seampact.dependency_closure import dependency_closure
from seampact.runtime import Coordinator, OwnerRuntime
from seampact.store import Store
from seampact.units import NativeBindings, UnitCatalog, MissingBinding


BASE = """
class CheckerBase:
    def __init__(self, prefix: str = 'base'):
        self.prefix = prefix

    def check(self, values: list[str]) -> list[str]:
        results = []
        for value in values:
            results.append(self._check(value))
        return results

    def _check(self, value: str) -> str:
        return 'WRONG BASE'
"""

CHILD = """
from base import CheckerBase

class NLIChecker(CheckerBase):
    def __init__(self, suffix: str = '!'):
        super().__init__()
        self.suffix = suffix

    def _check(self, value: str) -> str:
        return self.prefix + ':' + value + self.suffix
"""


class InheritedNativeBindingTests(unittest.TestCase):
    def freeze(self, base=BASE, child=CHILD, extras=None):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        files = {"base.py": base, "child.py": child, **(extras or {})}
        for path, source in files.items():
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
        inspector = Inspector.freeze(root, root / "snapshots", "request", list(files), "NLIChecker.check", "native check")
        inspector.request["source_id"] = "source"
        entry = dict(id="check", path="child.py", symbol="NLIChecker.check", kind="method",
                     constructor=dict(symbol="NLIChecker", arguments={}), boundary_ids=["check-W"])
        candidate = dict(entries=[entry], boundaries=[dict(id="check-W", path="base.py", symbol="CheckerBase.check", mode="W")])
        return root, inspector, candidate

    def report(self, inspector, candidate):
        store = Store(":memory:")
        self.addCleanup(store.close)
        return UnitCatalog(inspector.root, store, None).inspect_candidate(inspector, candidate)

    def test_cross_file_original_loop_dispatches_to_original_subclass(self):
        root, inspector, candidate = self.freeze()
        report = self.report(inspector, candidate)
        self.assertEqual(report["entries"][0]["status"], "READY", report["entries"])
        facts = report["entries"][0]["inheritance_binding"]
        self.assertEqual(facts["owner"]["path"], "child.py")
        self.assertEqual(facts["definition"]["symbol"], "CheckerBase.check")
        self.assertEqual(facts["definition"]["path"], "base.py")
        self.assertEqual([item["symbol"] for item in facts["class_chain"]], ["NLIChecker", "CheckerBase"])
        self.assertTrue(all(item["file_sha256"] and item["ast_sha256"] for item in facts["class_chain"]))
        closure = report["entries"][0]["dependency_closure"]
        self.assertEqual(closure["status"], "CHECKED", closure)
        edges = {(item["target_path"], item["target_symbol"]) for item in closure["call_edges"]}
        self.assertTrue({("child.py", "NLIChecker.__init__"), ("base.py", "CheckerBase.__init__"),
                         ("base.py", "CheckerBase.check"), ("child.py", "NLIChecker._check")} <= edges, edges)
        self.assertNotIn(("base.py", "CheckerBase._check"), edges)
        store = Store(":memory:")
        runtime = OwnerRuntime(Coordinator(store))
        self.addCleanup(store.close)
        self.addCleanup(runtime.close)
        catalog = UnitCatalog(root, store, runtime)
        unit = catalog.install(inspector, candidate, "analysis", "install")[0]
        self.assertEqual(catalog.invoke(unit["id"], {"values": ["A", "B"]}), ["base:A!", "base:B!"])
        self.assertEqual(catalog.invoke(unit["id"], {"values": ["C"]}), ["base:C!"])
        binding = catalog.bindings[unit["id"]]
        self.assertEqual(len(binding.owners), 1)
        owner = next(iter(binding.owners.values()))["owner"]
        self.assertEqual(type(owner).__name__, "NLIChecker")
        self.assertEqual(inspect.unwrap(owner.check).__qualname__, "CheckerBase.check")
        # Binding an inherited method does not award lifecycle eligibility.
        lifecycle = report["runtime_groups"]["groups"][0]["contract"]["lifecycle"]
        self.assertEqual(lifecycle["replicate"]["status"], "UNKNOWN")

    def test_inherited_constructor_and_relative_import_reexport(self):
        child = CHILD.replace("from base import CheckerBase", "from .exports import CheckerBase").replace(
            "    def __init__(self, suffix: str = '!'):\n        super().__init__()\n        self.suffix = suffix\n\n", "").replace(" + self.suffix", "")
        root, inspector, candidate = self.freeze(child="pass\n", extras={
            "pkg/__init__.py": "", "pkg/exports.py": "from base import CheckerBase\n", "pkg/child.py": child})
        candidate["entries"][0]["path"] = "pkg/child.py"
        report = self.report(inspector, candidate)
        self.assertEqual(report["entries"][0]["status"], "READY", report["entries"])

    def test_child_override_cannot_bind_a_parent_whole_boundary(self):
        child = CHILD + "\n    def check(self, values: list[str]) -> list[str]:\n        return ['override']\n"
        _, inspector, candidate = self.freeze(child=child)
        row = self.report(inspector, candidate)["entries"][0]
        self.assertEqual(row["status"], "MISSING")
        self.assertIn("whole original W definition", row["reason"])
        candidate["boundaries"][0].update(path="child.py", symbol="NLIChecker.check")
        row = self.report(inspector, candidate)["entries"][0]
        self.assertEqual(row["status"], "READY", row)
        self.assertEqual(row["inheritance_binding"]["definition"]["path"], "child.py")

    def test_unprovable_inheritance_and_source_rewrites_stay_missing(self):
        cases = [
            CHILD.replace("CheckerBase):", "CheckerBase, object):"),
            CHILD.replace("CheckerBase):", "make_base()):"),
            CHILD.replace("from base import CheckerBase", "from external import CheckerBase"),
            CHILD.replace("class NLIChecker", "@decorate\nclass NLIChecker"),
            CHILD + "\nCheckerBase.check = NLIChecker._check\n",
            CHILD + "\nsetattr(CheckerBase, 'check', NLIChecker._check)\n",
            CHILD.replace("    def _check", "    def __getattribute__(self, name):\n        return object.__getattribute__(self, name)\n\n    def _check"),
            CHILD + "\n    check = property(lambda self: self._check)\n",
            CHILD.replace("self.suffix = suffix", "self._check = lambda value: 'replaced'"),
            CHILD.replace("self.suffix = suffix", "setattr(self, '_check', lambda value: 'replaced')"),
            CHILD + "\ndef patch():\n    CheckerBase.check = NLIChecker._check\n",
        ]
        for source in cases:
            with self.subTest(source=source):
                _, inspector, candidate = self.freeze(child=source)
                self.assertEqual(self.report(inspector, candidate)["entries"][0]["status"], "MISSING")

    def test_runtime_replaced_base_and_method_are_rejected(self):
        for replacement in ("class Fake: pass\nnamespace['NLIChecker'] = Fake",
                            "namespace['NLIChecker'].__bases__[0].check = namespace['NLIChecker']._check"):
            with self.subTest(replacement=replacement):
                _, inspector, candidate = self.freeze()
                binding = NativeBindings(inspector, candidate, None)
                binding.describe(candidate["entries"][0])
                with binding._context():
                    namespace = binding._namespace(candidate["entries"][0])
                    exec(replacement, {"namespace": namespace})
                    with self.assertRaises(MissingBinding):
                        binding._assert_inherited_owner(namespace["NLIChecker"], binding.inherited_entries["check"])

    def test_decorated_override_definition_is_found_without_inspecting_its_body(self):
        child = CHILD.replace("from base import CheckerBase", "from base import CheckerBase\nimport backend").replace(
            "    def _check", "    @backend.no_grad()\n    def _check").replace(
            "return self.prefix + ':' + value + self.suffix", "return hidden_call(value)")
        _, inspector, candidate = self.freeze(child=child)
        closure = dependency_closure(inspector, candidate)["entries"]["check"]
        self.assertEqual(closure["status"], "MISSING")
        self.assertTrue(closure["scope_covered"])
        self.assertTrue(any(edge["target_symbol"] == "CheckerBase.check" for edge in closure["call_edges"]))
        decorated = [row for row in closure["unresolved_targets"]
                     if row["target"] == "NLIChecker._check" and row["reason"] == "decorated_method_target_unsupported"]
        self.assertTrue(any(e["path"] == "child.py" for row in decorated for e in row["evidence"]))
        self.assertFalse(any(row["target"] == "hidden_call" for row in closure["unresolved_targets"]))

    def test_child_dependency_membership_propagates_from_the_inherited_call(self):
        _, inspector, candidate = self.freeze()
        candidate["entries"].append(dict(id="child_check", path="child.py", symbol="NLIChecker._check", kind="method",
                                          constructor=dict(symbol="NLIChecker", arguments={})))
        candidate["dependencies"] = [dict(id="backend", entry_ids=["child_check"])]
        closure = dependency_closure(inspector, candidate)["entries"]["check"]
        self.assertEqual(closure["reachable_entries"], ["child_check"])
        self.assertEqual(closure["missing_dependency_ids"], ["backend"])
        self.assertEqual(closure["status"], "MISSING")

    def test_external_base_is_not_promoted_to_a_covered_native_call(self):
        _, inspector, candidate = self.freeze(child=CHILD.replace("from base import CheckerBase", "from unavailable import CheckerBase"))
        closure = dependency_closure(inspector, candidate)["entries"]["check"]
        self.assertEqual(closure["status"], "MISSING")
        self.assertTrue(closure["unresolved_targets"])
        self.assertFalse(closure["call_edges"])

    def test_original_torch_zero_argument_wrapper_preserves_body_reachability(self):
        child = CHILD.replace("from base import CheckerBase", "from base import CheckerBase\nimport torch as tensor_backend").replace(
            "    def _check", "    @tensor_backend.no_grad()\n    def _check")
        _, inspector, candidate = self.freeze(child=child)
        original = inspector.data("child.py")
        closure = dependency_closure(inspector, candidate)["entries"]["check"]
        self.assertEqual(closure["status"], "CHECKED", closure)
        self.assertTrue(any(edge["target_symbol"] == "NLIChecker._check" for edge in closure["call_edges"]))
        protocol = next(row for row in closure["external_protocols"] if row["target"] == "torch.no_grad")
        self.assertEqual(protocol["rule"], "external-zero-argument-call-wrapper/1")
        self.assertEqual(inspector.data("child.py"), original)

    def test_torch_wrapper_shadow_mutation_and_nonzero_arguments_stay_missing(self):
        child = CHILD.replace("from base import CheckerBase", "from base import CheckerBase\nimport torch").replace(
            "    def _check", "    @torch.no_grad()\n    def _check")
        cases = [
            (child.replace("@torch.no_grad()", "@torch.no_grad(True)"), {}),
            (child.replace("@torch.no_grad()", "@torch.no_grad\n    @other"), {}),
            (child + "\ntorch.no_grad = unknown\n", {}),
            (child, {"patch.py": "import torch as backend\nbackend.no_grad = unknown\n"}),
            (child, {"patch.py": "import torch\nsetattr(torch, 'no_grad', unknown)\n"}),
            (child, {"patch.py": "import torch\ntorch.__dict__['no_grad'] = unknown\n"}),
            (child, {"torch.py": "def no_grad():\n    return unknown\n"}),
        ]
        for source, extras in cases:
            with self.subTest(source=source, extras=extras):
                _, inspector, candidate = self.freeze(child=source, extras=extras)
                closure = dependency_closure(inspector, candidate)["entries"]["check"]
                self.assertEqual(closure["status"], "MISSING")
                self.assertFalse(any(row.get("target") == "torch.no_grad" for row in closure["external_protocols"]))


if __name__ == "__main__":
    unittest.main()
