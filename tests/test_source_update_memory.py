"""Source revision materials and execution evidence; no model or remote calls."""

import copy
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from seampact.analysis import content_digest, preparation_input
from seampact.dependencies import Dependencies, DependencyBindings, _execution_basis
from seampact.inspection import Inspector
from seampact.intake import RepositoryIntake
from seampact.revisions import build_review_context
from seampact.service import Platform
from seampact.store import Store


class SourceUpdateMemoryChecks(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.home = Path(self.directory.name)
        self.root = self.home / "original"
        self.root.mkdir()
        self.store = Store(self.home / "platform.sqlite")
        self.addCleanup(self.store.close)
        self.intake = RepositoryIntake(self.home / "managed")
        self.platform = Platform.__new__(Platform)
        self.platform.home = self.home
        self.platform.store = self.store
        self.platform.source_lock = threading.RLock()
        self.implementation = {"inspection.py": "current-checker"}
        self.platform.analysis = SimpleNamespace(freeze_implementation=lambda inspector: self.implementation)

    def write(self, name, text):
        (self.root / name).write_text(text, encoding="utf-8", newline="\n")

    def freeze(self, identity):
        source = self.intake.prepare(str(self.root))
        source["id"] = "source"
        inspector = Inspector.freeze_repository(source, self.home / "snapshots", identity, "original observation")
        return source, inspector

    def test_refresh_discovers_additions_deletions_and_keeps_old_frozen_bytes(self):
        self.write("changed.py", "def original():\n    return 1\n")
        self.write("deleted.py", "def removed():\n    return 2\n")
        self.write("stable.py", "import changed\n")
        _, before = self.freeze("before")
        build_review_context(before, before, self.store)
        self.write("changed.py", "def replacement():\n    return 3\n")
        (self.root / "deleted.py").unlink()
        self.write("added.py", "def main():\n    return 4\n")
        _, after = self.freeze("after")
        context = build_review_context(before, after, self.store)
        self.assertEqual(context["source_diff"]["added"], ["added.py"])
        self.assertEqual(context["source_diff"]["removed"], ["deleted.py"])
        self.assertEqual(context["source_diff"]["modified"], ["changed.py"])
        self.assertEqual(context["reuse"]["hits"], 1)
        names = {symbol["symbol"] for fact in context["facts"] for symbol in fact.get("symbols", [])}
        self.assertIn("main", names)
        self.assertIn("replacement", names)
        self.assertEqual(before.data("deleted.py"), b"def removed():\n    return 2\n")
        self.assertIn(b"original", before.data("changed.py"))
        self.assertIn(b"replacement", after.data("changed.py"))
        self.assertNotEqual(before.request["source_version"], after.request["source_version"])
        reference = next(item for item in context["related_candidates"] if item["path"] == "stable.py")
        self.assertEqual(reference["sha256"], after.files["stable.py"]["sha256"])
        self.assertEqual(context["mappings"], "REQUIRE_CURRENT_CHECK")
        self.assertEqual(context["resource_selections"], "REQUIRE_CURRENT_CHECK")

    def revision(self):
        self.write("original.py", "def main():\n    return 1\n")
        source, inspector = self.freeze("base-request")
        self.store.create("request", inspector.request["id"], inspector.request)
        self.store.create("analysis", "base", dict(id="base", request_id=inspector.request["id"]))
        materials = build_review_context(inspector, inspector, self.store)
        materials["basis"] = self.platform._revision_material_basis(inspector, self.implementation)
        candidate = dict(entries=[], runtime_groups=[])
        revision = dict(id="r", harness_id="source", base_analysis_id="base", proposed_source=source,
            request_id=inspector.request["id"],
            source_version=inspector.request["source_version"], materials=materials,
            implementation=copy.deepcopy(self.implementation), status="CHECKED", report={"status": "READY"},
            checked_digest=content_digest(candidate), candidate=candidate, candidate_digest=content_digest(candidate),
            base_candidate=copy.deepcopy(candidate), provenance=[], result={"status": "succeeded"},
            result_basis={"source_version": inspector.request["source_version"]}, check_identity={"old": True})
        return revision, inspector

    def test_resource_change_rebinds_materials_and_invalidates_old_check(self):
        revision, inspector = self.revision()
        old_basis = copy.deepcopy(revision["materials"]["basis"])
        inspector.request["resource_catalog"] = {"weights": [{"id": "selected", "config_digest": "new"}]}
        self.platform._refresh_revision_scope(revision, inspector)
        basis = revision["materials"]["basis"]
        self.assertEqual(basis["source_version"], old_basis["source_version"])
        self.assertNotEqual(basis["input_digest"], old_basis["input_digest"])
        self.assertEqual(basis["input_digest"], content_digest(preparation_input(inspector)))
        self.assertEqual(revision["materials"]["reuse"]["hits"], 1)
        self.assertIsNone(revision["report"])
        self.assertIsNone(revision["checked_digest"])
        self.assertNotIn("check_identity", revision)
        self.assertEqual(revision["result"]["status"], "unknown")
        self.assertEqual(revision["status"], "DRAFT")

    def test_current_model_report_survives_expanded_material_scope(self):
        revision, inspector = self.revision()
        revision["analysis_id"] = "model"
        inspector.request["configuration"] = {"entries": {"main": {"fixed_arguments": {"value": 2}}}}
        self.store.create("analysis_revision", "r", revision)
        report = {"status": "CURRENT_CHECK"}
        job = dict(id="model", revision_id="r", implementation=self.implementation,
            submitted=dict(candidate=revision["candidate"], report=report))
        self.platform._complete_revision(inspector, job)
        saved = self.store.get("analysis_revision", "r")
        self.assertEqual(saved["status"], "CHECKED")
        self.assertEqual(saved["report"], report)
        self.assertEqual(saved["checked_digest"], saved["candidate_digest"])
        self.assertEqual(saved["check_identity"]["input_digest"], saved["materials"]["basis"]["input_digest"])

    def test_implementation_change_invalidates_materials_even_for_unchanged_source(self):
        revision, inspector = self.revision()
        self.implementation["inspection.py"] = "new-checker"
        self.platform._refresh_revision_scope(revision, inspector)
        self.assertEqual(revision["materials"]["basis"]["implementation"], self.implementation)
        self.assertIsNone(revision["checked_digest"])

    def test_update_keeps_complete_candidate_without_inheriting_ready_or_installing(self):
        self.write("original.py", "def main():\n    return 1\n")
        source, inspector = self.freeze("base-request")
        candidate = dict(entries=[dict(id="one"), dict(id="two")], runtime_groups=[dict(entries=["one", "two"])])
        source.update(analysis_id="base", request_id=inspector.request["id"], status="ANALYZED")
        self.store.create("request", inspector.request["id"], inspector.request)
        self.store.create("harness", "source", source)
        self.store.create("analysis", "base", dict(id="base", request_id=inspector.request["id"], status="COMPLETED",
            submitted=dict(candidate=candidate, report={"status": "READY"})))
        self.platform.intake = self.intake
        self.platform.dependencies = SimpleNamespace(selections=lambda source_id: {})
        self.platform.resource_catalog = lambda: {}
        self.write("original.py", "def main():\n    return 2\n")
        self.write("new_capability.py", "def main():\n    return 3\n")
        created = self.platform.create_revision(dict(analysis_id="base", mode="update"))["revision"]
        self.assertEqual(created["candidate"], candidate)
        self.assertEqual(created["status"], "DRAFT")
        self.assertIsNone(created["checked_digest"])
        self.assertIsNone(created["report"])
        self.assertNotEqual(created["source_version"], inspector.request["source_version"])
        self.assertIn("new_capability.py", created["source_diff"]["added"])
        self.assertEqual(self.store.get("harness", "source")["analysis_id"], "base")
        self.assertEqual(self.store.get("analysis", "base")["submitted"]["report"], {"status": "READY"})


class SourceExecutionEvidenceChecks(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = Store(Path(self.directory.name) / "platform.sqlite")
        self.addCleanup(self.store.close)
        self.registry = Dependencies(self.store, None, None)
        self.inspector = SimpleNamespace(request=dict(source_id="source", source_version="v1"))
        self.row = dict(id="model", definition_digest="same-original-mapping", entry_ids=["main"],
            status="MAPPING_CHECKED", selection_allowed=True)
        self.selection = dict(dependency_id="source:model", origin_id="model", source_id="source",
            definition_digest=self.row["definition_digest"], revision_digest="resource-v1", status="SELECTED")
        self.store.create("dependency_binding", "source:model", self.selection)

    def record(self):
        with patch("seampact.dependencies.check_requirements", return_value=[copy.deepcopy(self.row)]):
            self.registry.record(self.inspector, {})
            bindings = DependencyBindings(None, self.inspector, {})
        bindings.selected = {"model": copy.deepcopy(self.selection)}
        return bindings.snapshot()

    def test_execution_basis_matches_analysis_input_contract(self):
        self.inspector.request.update(resource_catalog={"weights": []}, configuration={"entries": {}},
            runtime_environment={"python": "3.11"}, dependency_bindings={"model": self.selection})
        self.assertEqual(_execution_basis(self.inspector)["input_digest"],
                         content_digest(preparation_input(self.inspector)))

    def test_same_mapping_new_source_does_not_inherit_execution_or_accept_old_result(self):
        old = self.record()
        outcome = self.registry.record_execution("source", "main", True, old)
        self.assertEqual(outcome["resources"][0]["execution_basis"]["source_version"], "v1")
        self.assertEqual(self.store.get("resource_dependency", "source:model")["execution"], "SUCCEEDED")
        self.inspector.request["source_version"] = "v2"
        self.record()
        self.registry.record_execution("source", "main", True, old)
        row = self.store.get("resource_dependency", "source:model")
        self.assertEqual(row["execution"], "NOT_RUN")
        self.assertNotIn("last_execution", row)
        selection = self.store.get("dependency_binding", "source:model")
        self.assertEqual(selection["execution"], "NOT_RUN")
        self.assertNotIn("last_execution", selection)
        self.assertEqual(self.registry.selections("source")["model"]["revision_digest"], "resource-v1")

    def test_configuration_change_does_not_accept_old_frozen_result(self):
        old = self.record()
        self.inspector.request["configuration"] = {"entries": {"main": {"fixed_arguments": {"model": "new"}}}}
        self.record()
        self.registry.record_execution("source", "main", True, old)
        self.assertEqual(self.store.get("resource_dependency", "source:model")["execution"], "NOT_RUN")

    def test_old_resource_revision_does_not_update_new_selection_success(self):
        old = self.record()
        current = self.store.get("dependency_binding", "source:model")
        current["revision_digest"] = "resource-v2"
        self.store.save("dependency_binding", "source:model", current)
        self.registry.record_execution("source", "main", True, old)
        self.assertEqual(self.store.get("dependency_binding", "source:model")["execution"], "NOT_RUN")
        self.assertEqual(self.store.get("resource_dependency", "source:model")["execution"], "NOT_RUN")


if __name__ == "__main__":
    unittest.main()
