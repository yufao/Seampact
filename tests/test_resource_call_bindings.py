"""Fixed cache source binding and conservative resource non-use proofs."""

import copy
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from seampact.configuration.resource_call_bindings import prove_hub_cache_binding, resource_scope
from seampact.source.inspection import Inspector


class ResourceCallBindingTests(unittest.TestCase):
    def fixture(self, options="", selected="active", source=None):
        source = source or ("from transformers import AutoModel, AutoTokenizer\n"
            "class Active:\n    def __init__(self, cache):\n        repo = 'org/active'\n"
            "        self.model = AutoModel.from_pretrained(repo, cache_dir=cache, device_map='auto'" + options + ")\n"
            "        self.tokenizer = AutoTokenizer.from_pretrained(repo, cache_dir=cache" + options + ")\n"
            "class Other:\n    def __init__(self, cache):\n        repo = 'org/other'\n"
            "        self.model = AutoModel.from_pretrained(repo, cache_dir=cache)\n"
            "class Host:\n    def __init__(self, mode='active', cache='/fixed/cache'):\n"
            "        if mode in ['active']:\n            self.model = Active(cache)\n"
            "        else:\n            self.model = Other(cache)\n"
            "    def run(self, value: str):\n        return value\n")
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        (root / "main.py").write_text(source, encoding="utf-8")
        inspector = Inspector.freeze(root, root / "snapshots", "request", ["main.py"], "run", "result")
        entry = dict(id="host", kind="method", path="main.py", symbol="Host.run",
            constructor=dict(symbol="Host", arguments=dict(mode=selected, cache="/fixed/cache")))
        spec = dict(schema="fixed-hub-cache/1", repo_id="org/active", revision="a" * 40,
                    cache_dir="/fixed/cache", ref="main", required_files=[dict(path="config.json", sha256="b" * 64)])
        row = dict(id="active", kind="weight", configurable=False, required=True, entry_ids=["host"], mappings=[],
            evidence=[dict(path="main.py", start=3, end=6)], placement=dict(expression="repo", hub_cache=spec))
        return inspector, entry, row

    def test_exact_selected_calls_bind_original_values_and_require_actual_offline_check(self):
        inspector, entry, row = self.fixture()
        original = copy.deepcopy(row)
        proof = prove_hub_cache_binding(inspector, entry, row)
        self.assertEqual(proof["status"], "CHECKED", proof)
        self.assertTrue(proof["require_offline"])
        self.assertEqual(len(proof["calls"]), 2)
        self.assertTrue(all(call["site"]["call_ast_sha256"] for call in proof["calls"]))
        self.assertTrue(all(call["original_values"]["repo_id"] == "org/active" for call in proof["calls"]))
        self.assertEqual(row, original)

    def test_local_only_and_exact_commit_are_finite_original_options(self):
        inspector, entry, row = self.fixture(options=", local_files_only=True, revision='" + "a" * 40 + "'")
        row["placement"]["hub_cache"]["ref"] = "a" * 40
        proof = prove_hub_cache_binding(inspector, entry, row)
        self.assertEqual(proof["status"], "CHECKED", proof)
        self.assertFalse(proof["require_offline"])

    def test_cache_defaults_mismatch_and_unresolved_dependency_values_do_not_bind(self):
        for change in (dict(cache=None), dict(cache="/different"),
                       dict(cache={"$dependency": dict(id="cache", field="path")})):
            inspector, entry, row = self.fixture()
            entry["constructor"]["arguments"].update(change)
            self.assertEqual(prove_hub_cache_binding(inspector, entry, row)["status"], "MISSING")
        inspector, entry, row = self.fixture()
        row["placement"]["hub_cache"]["repo_id"] = "org/unrelated"
        self.assertEqual(prove_hub_cache_binding(inspector, entry, row)["status"], "MISSING")

    def test_uncovered_cache_options_and_open_keyword_domains_are_rejected(self):
        for option in ("trust_remote_code=False", "subfolder=''", "variant=None", "force_download=False",
                       "local_files_only=None", "revision='branch'", "**unknown"):
            with self.subTest(option=option):
                inspector, entry, row = self.fixture(options=", " + option)
                self.assertEqual(prove_hub_cache_binding(inspector, entry, row)["status"], "MISSING")

    def test_not_used_retains_original_declaration_and_requires_exact_absent_sites(self):
        inspector, entry, row = self.fixture()
        unused = copy.deepcopy(row)
        unused.update(id="other", evidence=[dict(path="main.py", start=8, end=10)])
        original = copy.deepcopy(unused)
        result = resource_scope(inspector, entry, unused)
        self.assertEqual(result["status"], "NOT_USED", result)
        self.assertEqual(result["declared_entry_ids"], ["host"])
        self.assertEqual(unused, original)
        self.assertEqual(resource_scope(inspector, entry, row)["status"], "USED")
        unused["placement"]["expression"] = "not_the_original_expression"
        self.assertEqual(resource_scope(inspector, entry, unused)["status"], "UNKNOWN")

    def test_unknown_selector_or_explicit_reference_cannot_discharge_required_resource(self):
        inspector, entry, row = self.fixture(selected={"$dependency": dict(id="selector", field="parameters.mode")})
        self.assertEqual(prove_hub_cache_binding(inspector, entry, row)["status"], "MISSING")
        inspector, entry, row = self.fixture()
        unused = copy.deepcopy(row)
        unused.update(id="other", evidence=[dict(path="main.py", start=8, end=10)])
        entry["constructor"]["arguments"]["cache"] = {"$dependency": dict(id="other", field="path")}
        self.assertEqual(resource_scope(inspector, entry, unused)["status"], "UNKNOWN")

    def test_false_absence_from_implicit_callbacks_defaults_and_control_flow_stays_unknown(self):
        loader = "AutoModel.from_pretrained('org/active', cache_dir='/fixed/cache')"
        cases = [
            "def load():\n    " + loader + "\nif condition:\n    load()\ndef host():\n    pass\n",
            "def load():\n    " + loader + "\ndef host():\n    external(load)\n",
            "def unused(model=" + loader + "):\n    pass\ndef host():\n    pass\n",
            "def load():\n    " + loader + "\nclass Context:\n    def __enter__(self):\n        load()\n"
                "    def __exit__(self, *args):\n        pass\ndef host():\n    with Context():\n        pass\n",
            "def host():\n    try:\n        raise ValueError('handled')\n    except ValueError:\n        pass\n    " + loader + "\n",
            "def host(value):\n    match value:\n        case _:\n            " + loader + "\n",
        ]
        for body in cases:
            with self.subTest(body=body):
                source = "from transformers import AutoModel\nfrom sdk import external\n" + body
                inspector, _, row = self.fixture(source=source)
                entry = dict(id="host", kind="function", path="main.py", symbol="host")
                row.update(evidence=[dict(path="main.py", start=1, end=len(source.splitlines()))])
                row["placement"]["expression"] = "'org/active'"
                self.assertEqual(resource_scope(inspector, entry, row)["status"], "UNKNOWN")

    def test_asserted_original_call_is_traced_and_stale_candidate_facts_are_ignored(self):
        source = ("from transformers import AutoModel\ndef host():\n"
                  "    assert AutoModel.from_pretrained('org/active', cache_dir='/fixed/cache')\n")
        inspector, _, row = self.fixture(source=source)
        entry = dict(id="host", kind="function", path="main.py", symbol="host")
        row.update(evidence=[dict(path="main.py", start=1, end=3)], resource_origins=[])
        row["placement"]["expression"] = "'org/active'"
        self.assertEqual(resource_scope(inspector, entry, row)["status"], "USED")
        self.assertEqual(prove_hub_cache_binding(inspector, entry, row)["status"], "CHECKED")


if __name__ == "__main__":
    unittest.main()
