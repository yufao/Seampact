"""Focused source-only checks; no source import, model, service or platform store."""

import copy
import sys
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from seampact.dependencies import Dependencies, DependencyBindings, check_requirements


class DependencyBindingChecks(unittest.TestCase):
    def candidate(self, source, entries=None, mappings=None, keys=None):
        source = textwrap.dedent(source).lstrip()
        entries = entries or ["first"]
        inspector = SimpleNamespace(root=Path.cwd(), request=dict(source_version="frozen-source", source_id="source"),
            data=lambda path: source.encode("utf-8"))
        parameter = "config" if keys else "url"
        reference = {"$dependency": dict(id="service", field="base_url")}
        value = reference
        for key in reversed(keys or []):
            value = {key: value}
        proposed = [dict(id=entry_id, path="original.py", symbol=entry_id, kind="function",
                         fixed_arguments={parameter: copy.deepcopy(value)}) for entry_id in entries]
        mapping = dict(entry_id=entries[0], location="entry.fixed_arguments", parameter=parameter, field="base_url")
        if keys:
            mapping["parameter_path"] = keys
        requirement = dict(id="service", kind="service", configurable=True, required=True, entry_ids=list(entries),
            read_time="call", owner_scope="original request", evidence=[dict(path="original.py", unit="file",
                start=1, end=len(source.splitlines()))], mappings=mappings if mappings is not None else [mapping])
        return inspector, dict(entries=proposed, dependencies=[requirement])

    def bound(self, inspector, candidate):
        row = check_requirements(inspector, candidate)[0]
        inspector.request["dependency_bindings"] = dict(service=dict(definition_digest=row["definition_digest"],
            connection=dict(base_url="https://example.invalid")))
        return row, DependencyBindings(None, inspector, candidate)

    def test_required_mapping_is_checked_for_each_affected_entry(self):
        inspector, candidate = self.candidate("""
            def first(url):
                return Client(base_url=url)
            def second(url):
                return Client(base_url=url)
        """, entries=["first", "second"])
        row, bindings = self.bound(inspector, candidate)
        self.assertEqual(row["status"], "MISSING")
        self.assertTrue(row["selection_allowed"])
        self.assertIsNone(row["selection_reason"])
        self.assertEqual(row["entry_checks"]["first"]["status"], "MAPPING_CHECKED")
        self.assertEqual(row["entry_checks"]["second"]["mapping_count"], 0)
        bindings.check_entry("first")
        with self.assertRaisesRegex(ValueError, "service.*second.*no original parameter mapping"):
            bindings.check_entry("second")
        candidate["dependencies"][0]["mappings"].append(dict(candidate["dependencies"][0]["mappings"][0], entry_id="second"))
        row, bindings = self.bound(inspector, candidate)
        self.assertEqual(row["status"], "MAPPING_CHECKED")
        bindings.check_entry("second")

    def test_destination_gap_is_local_but_malformed_field_is_row_level(self):
        inspector, candidate = self.candidate("""
            def first(url):
                return Client(base_url=url)
            def second(url):
                return Client(base_url=url)
        """, entries=["first", "second"])
        candidate["dependencies"][0]["mappings"].append(dict(candidate["dependencies"][0]["mappings"][0], entry_id="second"))
        candidate["entries"][1]["fixed_arguments"] = {}
        row, bindings = self.bound(inspector, candidate)
        self.assertTrue(row["selection_allowed"])
        bindings.check_entry("first")
        with self.assertRaisesRegex(ValueError, "second.*absent"):
            bindings.check_entry("second")
        candidate["dependencies"][0]["mappings"][1]["field"] = "unknown"
        row, bindings = self.bound(inspector, candidate)
        self.assertFalse(row["selection_allowed"])
        self.assertEqual(row["selection_reason"], "unsupported resource field")
        with self.assertRaisesRegex(ValueError, "unsupported resource field"):
            bindings.check_entry("first")

    def test_empty_unresolved_scope_cannot_bypass_required_resources(self):
        inspector, candidate = self.candidate("def first(url):\n    return Client(base_url=url)\n")
        candidate["dependencies"][0].update(entry_ids=[], mappings=[])
        row, bindings = self.bound(inspector, candidate)
        self.assertFalse(row["selection_allowed"])
        self.assertEqual(row["entry_ids"], ["first"])
        with self.assertRaisesRegex(ValueError, "affected original entries"):
            bindings.check_entry("first")

    def test_mapping_uses_its_own_validated_evidence_across_source_files(self):
        inspector, candidate = self.candidate("def first(url):\n    return Client(base_url=url)\n")
        files = {"original.py": inspector.data("original.py"), "context.py": b"purpose = 'service'\n"}
        inspector.data = lambda path: files[path]
        row = candidate["dependencies"][0]
        own_evidence = copy.deepcopy(row["evidence"])
        row["evidence"] = [dict(path="context.py", start=1, end=1)]
        self.assertEqual(check_requirements(inspector, candidate)[0]["status"], "MISSING")
        row["mappings"][0]["evidence"] = own_evidence
        self.assertEqual(check_requirements(inspector, candidate)[0]["status"], "MAPPING_CHECKED")

    def test_mapping_evidence_does_not_fall_back_when_empty_or_invalid(self):
        inspector, candidate = self.candidate("def first(url):\n    return Client(base_url=url)\n")
        for evidence in ([], None, "original.py", [None], [dict(path="original.py", start=1, end=99)]):
            with self.subTest(evidence=evidence):
                candidate["dependencies"][0]["mappings"][0]["evidence"] = evidence
                row = check_requirements(inspector, candidate)[0]
                self.assertEqual(row["status"], "MISSING")
                self.assertFalse(row["selection_allowed"])

    def test_mapping_evidence_must_cover_its_parameter_and_complete_call(self):
        inspector, candidate = self.candidate("def first(url):\n    return Client(\n        base_url=url\n    )\n")
        mapping = candidate["dependencies"][0]["mappings"][0]
        for start, end in ((1, 1), (1, 3), (2, 4)):
            with self.subTest(start=start, end=end):
                mapping["evidence"] = [dict(path="original.py", start=start, end=end)]
                self.assertEqual(check_requirements(inspector, candidate)[0]["status"], "MISSING")
        mapping["evidence"] = [dict(path="original.py", start=1, end=4)]
        self.assertEqual(check_requirements(inspector, candidate)[0]["status"], "MAPPING_CHECKED")

    def test_mapping_evidence_for_one_entry_does_not_admit_another(self):
        inspector, candidate = self.candidate(
            "def first(url):\n    return Client(base_url=url)\ndef second(url):\n    return Client(base_url=url)\n",
            entries=["first", "second"])
        row = candidate["dependencies"][0]
        row["mappings"].append(dict(row["mappings"][0], entry_id="second"))
        for mapping in row["mappings"]:
            mapping["evidence"] = [dict(path="original.py", start=1, end=2)]
        checked = check_requirements(inspector, candidate)[0]
        self.assertEqual(checked["entry_checks"]["first"]["status"], "MAPPING_CHECKED")
        self.assertEqual(checked["entry_checks"]["second"]["status"], "MISSING")

    def test_invalid_falsey_required_flag_cannot_skip_an_unresolved_dependency(self):
        inspector, candidate = self.candidate("def first(url):\n    return Client(base_url=url)\n")
        for invalid in (None, [], 0, ""):
            with self.subTest(required=invalid):
                candidate["dependencies"][0]["required"] = invalid
                row, bindings = self.bound(inspector, candidate)
                self.assertEqual(row["status"], "MISSING")
                self.assertFalse(row["selection_allowed"])
                with self.assertRaisesRegex(ValueError, "boolean required"):
                    bindings.check_entry("first")
        candidate["dependencies"][0].update(required=False, mappings=[])
        _, bindings = self.bound(inspector, candidate)
        with self.assertRaisesRegex(ValueError, "no original parameter mapping"):
            bindings.check_entry("first")
        candidate["entries"][0]["fixed_arguments"] = {}
        _, bindings = self.bound(inspector, candidate)
        bindings.check_entry("first")

    def test_legacy_mapping_implies_its_entry_and_invalid_evidence_blocks_selection(self):
        inspector, candidate = self.candidate("def first(url):\n    return Client(base_url=url)\n")
        candidate["dependencies"][0]["entry_ids"] = []
        row, bindings = self.bound(inspector, candidate)
        self.assertEqual(row["entry_ids"], ["first"])
        bindings.check_entry("first")
        candidate["dependencies"][0]["evidence"][0]["end"] = 100
        row, bindings = self.bound(inspector, candidate)
        self.assertFalse(row["selection_allowed"])
        with self.assertRaisesRegex(ValueError, "evidence line range"):
            bindings.check_entry("first")

    def test_partial_requirement_can_be_selected_and_listed(self):
        inspector, candidate = self.candidate("""
            def first(url):
                return Client(base_url=url)
            def second(url):
                return Client(base_url=url)
        """, entries=["first", "second"])
        row = check_requirements(inspector, candidate)[0]
        row.update(id="source:service", origin_id="service", source_id="source")
        records = {"resource_dependency": {row["id"]: row}, "dependency_binding": {}}
        store = SimpleNamespace(get=lambda kind, identity: records[kind].get(identity),
            list=lambda kind: list(records[kind].values()),
            save=lambda kind, identity, value: records[kind].update({identity: value}))
        profiles = SimpleNamespace(public_connections=lambda: [dict(id="connection", auth=dict(mode="none"))])
        dependencies = Dependencies(store, profiles, None)
        selected = dependencies.select(dict(dependency_id=row["id"], harness_id="source", connection_id="connection"))
        self.assertEqual(dependencies.public()[1], [selected])
        row.update(selection_allowed=False, selection_reason="malformed field")
        self.assertEqual(dependencies.public()[1], [])
        with self.assertRaises(ValueError):
            dependencies.select(dict(dependency_id=row["id"], harness_id="source", connection_id="connection"))

    def test_dictionary_alias_closure_and_literal_suffix_are_supported(self):
        inspector, candidate = self.candidate("""
            def first(config):
                endpoint = config["service"]["url"]
                base = endpoint
                def request():
                    return Client(base_url=base + "/chat")
                return request()
        """, keys=["service", "url"])
        row, bindings = self.bound(inspector, candidate)
        self.assertEqual(row["status"], "MAPPING_CHECKED", row["reason"])
        bindings.check_entry("first")
        resolved = bindings.values(candidate["entries"][0]["fixed_arguments"], "first", "entry.fixed_arguments")
        self.assertEqual(resolved["config"]["service"]["url"], "https://example.invalid")

    def test_dictionary_rejects_dynamic_namespace_access_including_aliases(self):
        sources = [
            "def first(config):\n    exec(\"config['url']='changed'\")\n    return Client(base_url=config['url'])\n",
            "from builtins import exec as run\ndef first(config):\n    run('pass')\n    return Client(base_url=config['url'])\n",
            "runner = exec\ndef first(config):\n    runner('pass')\n    return Client(base_url=config['url'])\n",
            "def first(config):\n    namespace = locals()\n    return Client(base_url=config['url'])\n",
            "def first(config):\n    inspect.currentframe()\n    return Client(base_url=config['url'])\n",
            "def first(config):\n    getattr(Client, 'invoke')(base_url=config['url'])\n",
        ]
        for source in sources:
            with self.subTest(source=source.splitlines()[0]):
                inspector, candidate = self.candidate(source, keys=["url"])
                row = check_requirements(inspector, candidate)[0]
                self.assertFalse(row["selection_allowed"])
                self.assertIn("dynamic", row["reason"])

    def test_dictionary_rejects_mutation_escape_shadowing_and_bad_alias_order(self):
        sources = [
            "def first(config):\n    config['url'] = 'changed'\n    return Client(base_url=config['url'])\n",
            "def first(config):\n    endpoint = config['url']\n    mutate(endpoint)\n    return Client(base_url=endpoint)\n",
            "def first(config):\n    endpoint = config['url']\n    endpoint.clear()\n    return Client(base_url=endpoint)\n",
            "def first(config):\n    endpoint = config['url']\n    endpoint = 'changed'\n    return Client(base_url=endpoint)\n",
            "def first(config):\n    def request(config):\n        return Client(base_url=config['url'])\n    return request({})\n",
            "def first(config):\n    earlier = later\n    later = config['url']\n    return Client(base_url=earlier)\n",
            "def first(config):\n    if enabled:\n        endpoint = config['url']\n    return Client(base_url=endpoint)\n",
        ]
        for source in sources:
            with self.subTest(source=source):
                inspector, candidate = self.candidate(source, keys=["url"])
                row = check_requirements(inspector, candidate)[0]
                self.assertEqual(row["status"], "MISSING")
                self.assertFalse(row["selection_allowed"])


if __name__ == "__main__":
    unittest.main()
