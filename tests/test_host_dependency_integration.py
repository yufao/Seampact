"""Host dependency discovery is enforced by the actual install/check path."""

import copy
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from seampact.analysis import AnalysisJobs
from seampact.dependencies import DependencyBindings
from seampact.inspection import Inspector
from seampact.store import Store
from seampact.units import NativeBindings, UnitCatalog


class HostDependencyIntegration(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        code = "from openai import OpenAI\ndef child(url: str) -> str:\n    return OpenAI(base_url=url)\ndef host(url: str) -> str:\n    return child(url)\n"
        (self.root / "main.py").write_text(code, encoding="utf-8")
        self.inspector = Inspector.freeze(self.root, self.root / "snapshots", "r", ["main.py"], "host", "result")
        self.inspector.request["source_id"] = "source"
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.catalog = UnitCatalog(self.root, self.store, None)
        ref = {"$dependency": dict(id="service", field="base_url")}
        self.candidate = dict(boundaries=[], runtime_groups=[], entries=[
            dict(id="host", kind="function", path="main.py", symbol="host", fixed_arguments=dict(url=copy.deepcopy(ref))),
            dict(id="child", kind="function", path="main.py", symbol="child", fixed_arguments=dict(url=copy.deepcopy(ref)))],
            dependencies=[dict(id="service", kind="service", configurable=True, required=True, read_time="call",
                owner_scope="original request", entry_ids=["child"], evidence=[dict(path="main.py", start=1, end=5)],
                mappings=[dict(entry_id="child", location="entry.fixed_arguments", parameter="url", field="base_url")])])

    def test_missing_host_membership_blocks_real_catalog_and_is_model_repairable(self):
        jobs = AnalysisJobs(self.store, None, check_entries=self.catalog.inspect_candidate)
        report = jobs._check_candidate(self.inspector, self.candidate)
        host = next(e for e in report["entry_bindings"]["entries"] if e["id"] == "host")
        self.assertEqual(host["status"], "MISSING")
        self.assertEqual(host["code"], "host_dependency_missing")
        issue = next(i for i in report["feedback"]["model_repairs"] if i["issue_code"] == "host_dependency_missing")
        self.assertEqual(issue["field_paths"], ["dependencies[0].entry_ids"])
        units = self.catalog.install(self.inspector, self.candidate, "a")
        self.assertEqual(next(u for u in units if u["entry_id"] == "host")["status"], "MISSING")

    def test_adding_membership_without_original_mapping_still_does_not_pass(self):
        self.candidate["dependencies"][0]["entry_ids"].append("host")
        bindings = DependencyBindings(None, self.inspector, self.candidate)
        with self.assertRaisesRegex(ValueError, "no original parameter mapping"):
            bindings.check_entry("host")

    def test_checked_host_mapping_still_requires_real_resource_selection(self):
        row = self.candidate["dependencies"][0]
        row["entry_ids"].append("host")
        row["mappings"].append(dict(row["mappings"][0], entry_id="host"))
        bindings = DependencyBindings(None, self.inspector, self.candidate)
        self.assertEqual(bindings.rows["service"]["entry_checks"]["host"]["status"], "MAPPING_CHECKED")
        with self.assertRaisesRegex(ValueError, "unselected"):
            bindings.check_entry("host")
        bindings.selected["service"] = dict(definition_digest=bindings.rows["service"]["definition_digest"],
                                             connection=dict(base_url="https://example.invalid"))
        bindings.check_entry("host")
        self.assertEqual(bindings.values(self.candidate["entries"][0]["fixed_arguments"], "host", "entry.fixed_arguments"),
                         dict(url="https://example.invalid"))
        native = NativeBindings(self.inspector, self.candidate, None, bindings)
        native.environment_gaps = []
        checked = native.describe(self.candidate["entries"][0])
        self.assertEqual(checked[0]["required"], [])


if __name__ == "__main__":
    unittest.main()
