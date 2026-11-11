"""Unused source-callable imports are scoped without optional-library rules."""

import copy
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from seampact.source.dependency_closure import source_import_scope
from seampact.source.inspection import Inspector
from seampact.store import Store
from seampact.units import NativeBindings, UnitCatalog, MissingBinding


MISSING = "seampact_missing_optional_backend_fixture"
BACKENDS = ("class Active:\n    def __init__(self):\n        pass\n"
            "    def run(self, value: str) -> str:\n        return value\n"
            "class Other:\n    def __init__(self):\n        import " + MISSING + "\n")
MAIN = ("from backends import Active, Other\nclass Host:\n"
        "    def __init__(self, mode: str = 'active'):\n"
        "        if mode == 'active':\n            self.backend = Active()\n"
        "        else:\n            self.backend = Other()\n"
        "    def run(self, value: str) -> str:\n        return self.backend.run(value)\n")


class SourceImportScopeTests(unittest.TestCase):
    def fixture(self, main=MAIN, backend=BACKENDS):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        for path, text in (("main.py", main), ("backends.py", backend)):
            (root / path).write_text(text, encoding="utf-8")
        inspector = Inspector.freeze(root, root / "snapshots", "request", ["main.py", "backends.py"], "run", "result")
        inspector.request["source_id"] = "source"
        entry = dict(id="run", path="main.py", unit="file", kind="method", symbol="Host.run",
                     constructor=dict(symbol="Host", arguments={}), boundary_ids=[])
        candidate = dict(entries=[entry], boundaries=[], dependencies=[])
        return root, inspector, entry, candidate

    def optional(self, proof):
        return [site for site in proof["sites"] if site["module"] == MISSING]

    def test_normal_describe_excludes_only_the_unvisited_backend_constructor_import(self):
        root, inspector, entry, candidate = self.fixture()
        store = Store(":memory:")
        self.addCleanup(store.close)
        report = UnitCatalog(root, store, None).inspect_candidate(inspector, candidate)
        check = report["entries"][0]
        self.assertEqual(check["status"], "READY", check)
        proof = check["dependency_import_scope"]
        self.assertEqual(proof["status"], "CHECKED")
        self.assertEqual([site["status"] for site in self.optional(proof)], ["NOT_REACHED"])
        self.assertTrue(self.optional(proof)[0]["source_sha256"])
        self.assertTrue(self.optional(proof)[0]["owner"]["ast_sha256"])
        self.assertNotIn(MISSING, sys.modules)

    def test_calls_without_entry_context_keep_the_original_conservative_requirement(self):
        _, inspector, _, candidate = self.fixture()
        binding = NativeBindings(inspector, candidate, None)
        with self.assertRaisesRegex(MissingBinding, MISSING):
            binding._dependencies("main.py", "file")

    def test_module_and_class_initialization_imports_are_never_excluded(self):
        for backend in ("import " + MISSING + "\n" + BACKENDS,
                        BACKENDS + "\nclass ImportedClass:\n    import " + MISSING + "\n"):
            with self.subTest(backend=backend):
                _, inspector, entry, candidate = self.fixture(backend=backend)
                proof = source_import_scope(inspector, entry, candidate)
                retained = [site for site in self.optional(proof) if site["source_scope"] != "CALLABLE_BODY"]
                self.assertTrue(retained)
                self.assertTrue(all(site["status"] != "NOT_REACHED" for site in retained))

    def test_initializers_remain_in_the_same_exact_trace(self):
        main = MAIN + "\n    def prepare(self):\n        import " + MISSING + "\n"
        _, inspector, entry, candidate = self.fixture(main=main)
        entry["initializers"] = [dict(symbol="prepare", arguments={})]
        proof = source_import_scope(inspector, entry, candidate)
        active = [site for site in self.optional(proof) if site["path"] == "main.py"]
        self.assertEqual(active[0]["status"], "RETAIN")
        self.assertTrue(active[0]["execution_observed"])

    def test_unknown_callback_or_definition_time_call_does_not_prove_unused_import(self):
        cases = [
            MAIN.replace("return self.backend.run(value)", "unknown(value)\n        return value"),
            MAIN + "\ndef source_callback():\n    return Other()\ndef extra(value=source_callback()):\n    pass\n",
        ]
        for main in cases:
            with self.subTest(main=main):
                _, inspector, entry, candidate = self.fixture(main=main)
                proof = source_import_scope(inspector, entry, candidate)
                self.assertEqual(proof["status"], "UNKNOWN")
                self.assertTrue(all(site["status"] != "NOT_REACHED" for site in self.optional(proof)))

    def test_b_call_and_imports_within_a_visited_callable_remain_conservative(self):
        main = MAIN.replace("return self.backend.run(value)", "if False:\n            import " + MISSING + "\n        return value")
        _, inspector, entry, candidate = self.fixture(main=main)
        proof = source_import_scope(inspector, entry, candidate)
        visited_site = [site for site in self.optional(proof) if site["path"] == "main.py"][0]
        self.assertEqual(visited_site["status"], "RETAIN")
        changed = copy.deepcopy(entry)
        changed["binding_program"] = dict(path="main.py", scope="prefix")
        proof = source_import_scope(inspector, changed, candidate)
        self.assertEqual(proof["status"], "UNKNOWN")
        self.assertTrue(all(site["status"] != "NOT_REACHED" for site in self.optional(proof)))


if __name__ == "__main__":
    unittest.main()
