"""Source-only harness hints, with no model, source execution or resource probes."""

import json
import sys
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from seampact.classification import classify_target, inspect_harness
from seampact.inspection import Inspector


class HarnessClassificationChecks(unittest.TestCase):
    def inspector(self, files, withheld=()):
        payloads = {path: textwrap.dedent(source).lstrip().encode() for path, source in files.items()}
        return SimpleNamespace(files={path: dict(path=path, sha256="frozen-" + path) for path in files},
            request=dict(source_version="frozen-source", withheld_sources=[dict(path=path) for path in withheld]),
            data=Mock(side_effect=lambda path: payloads[path]))

    def ids(self, profile, axis):
        return {item["id"] for item in profile[axis]}

    def target(self, inspector, symbol, path="source.py", **kwargs):
        return classify_target(inspector, dict(path=path, unit="file", symbol=symbol, **kwargs))

    def test_multiple_families_keep_separate_state_and_resource_constraints(self):
        inspector = self.inspector({"source.py": """
            class Agent:
                def __init__(self, path):
                    self.model = Loader.from_pretrained(path)
                async def run(self, query):
                    docs = self.retriever.search(query)
                    self.client.bind_tools(tools=self.tools)
                    for doc in docs:
                        if self.guard.validate(doc):
                            yield self.model.generate(doc)
        """})
        profile = self.target(inspector, "Agent.run", kind="method")
        self.assertTrue({"retrieval", "tool_agent", "guardrail", "model_inference"} <= self.ids(profile, "families"))
        self.assertTrue({"object", "workflow"} <= self.ids(profile, "forms"))
        self.assertTrue({"lifecycle", "model_load", "instance_state", "state_write", "loop", "async", "generator"} <= self.ids(profile, "traits"))
        self.assertEqual(profile["execution"], "NOT_RUN")
        self.assertFalse(profile["admission_granted"])
        for axis in ("forms", "families", "traits"):
            self.assertTrue(all(row["evidence"] for row in profile[axis] if row["status"] != "UNKNOWN"))
        self.assertTrue(all(row["status"] == "HYPOTHESIS" for row in profile["families"]))
        self.assertIn("resource_mapping", {row["id"] for row in profile["recipes"]})

    def test_target_scope_excludes_unrelated_functions_and_methods(self):
        inspector = self.inspector({"source.py": """
            import requests
            def unrelated():
                return requests.get('https://example.invalid')
            class Thing:
                def __init__(self, value):
                    self.value = value
                def read(self, query):
                    return self.value + query
                def train(self):
                    return trainer.fit()
            def outer():
                def nested():
                    return engine.search('ignored')
                return 1
        """})
        profile = self.target(inspector, "Thing.read", kind="method")
        self.assertEqual(self.ids(profile, "families"), {"unknown"})
        self.assertNotIn("service", self.ids(profile, "traits"))
        self.assertIn("lifecycle", self.ids(profile, "traits"))
        self.assertEqual(self.ids(self.target(inspector, "outer"), "families"), {"unknown"})

    def test_static_method_does_not_inherit_instance_loading(self):
        inspector = self.inspector({"source.py": """
            class Thing:
                def __init__(self):
                    self.model = Loader.from_pretrained('ignored')
                @staticmethod
                def value(x):
                    return x
        """})
        profile = self.target(inspector, "Thing.value", kind="method")
        self.assertNotIn("model_load", self.ids(profile, "traits"))
        self.assertNotIn("lifecycle", self.ids(profile, "traits"))

    def test_names_are_not_family_evidence_and_source_is_never_executed(self):
        inspector = self.inspector({"guardrail_rag_agent.py": """
            raise RuntimeError('must never run')
            def retrieval_guardrail_critic_train(query):
                return query
        """})
        profile = self.target(inspector, "retrieval_guardrail_critic_train", path="guardrail_rag_agent.py")
        self.assertEqual(self.ids(profile, "families"), {"unknown"})
        self.assertFalse(profile["admission_granted"])
        self.assertNotIn("operations", profile)
        self.assertTrue(any(row["id"] == "resource_availability" for row in profile["unknowns"]))

    def test_pagination_counts_files_and_notebook_preserves_code_units(self):
        book = json.dumps(dict(cells=[dict(cell_type="markdown", source=["not code"]),
            dict(cell_type="code", source=["def first(x):\n", "    return x\n"]),
            dict(cell_type="code", source=["!pip install something\n"])]))
        inspector = self.inspector({"a.ipynb": book, "b.py": "def second():\n    return 2", "c.py": "x = 3"})
        page = inspect_harness(inspector, count=1)
        self.assertEqual(page["schema"], "harness-classification/1")
        self.assertEqual(page["total"], 3)
        self.assertEqual(page["next_offset"], 1)
        self.assertEqual([row["unit"] for row in page["profiles"]], ["cell:1", "cell:2"])
        self.assertEqual(page["profiles"][1]["unknowns"][0]["id"], "PARSE_UNSUPPORTED")
        self.assertFalse(page["scope"]["whole_repository_coverage"])
        self.assertEqual(inspector.data.call_count, 1)
        self.assertEqual(inspect_harness(inspector, path="b", count=1)["total"], 1)
        self.assertEqual(inspect_harness(inspector, offset=10)["profiles"], [])

    def test_config_and_withheld_not_read_or_misclassified(self):
        inspector = self.inspector({"config.yaml": "tools: [agent]\nmodel: rag", "bad.py": "def nope(:"}, withheld=["private.py"])
        result = inspect_harness(inspector)
        by_path = {row["path"]: row for row in result["profiles"]}
        self.assertEqual(by_path["config.yaml"]["unknowns"][0]["id"], "NON_PYTHON_NOT_INTERPRETED")
        self.assertEqual(by_path["private.py"]["unknowns"][0]["id"], "WITHHELD")
        self.assertEqual(by_path["bad.py"]["unknowns"][0]["id"], "PARSE_UNSUPPORTED")
        inspector.data.assert_called_once_with("bad.py")

    def test_dynamic_calls_and_resources_remain_unknown(self):
        inspector = self.inspector({"source.py": """
            def run(callback, obj, name):
                value = callback()
                return getattr(obj, name)(value)
        """})
        profile = self.target(inspector, "run")
        self.assertIn("dynamic_call", self.ids(profile, "traits"))
        self.assertTrue({"dynamic_resolution", "resource_availability", "state_effect_closure"} <= {row["id"] for row in profile["unknowns"]})
        self.assertFalse(profile["admission_granted"])
        self.assertEqual(self.target(inspector, "missing")["unknowns"][0]["id"], "TARGET_NOT_LOCATED")

    def test_cli_and_target_runtime_routing(self):
        inspector = self.inspector({"source.py": """
            if __name__ == '__main__':
                print('hello')
        """})
        self.assertIn("cli", self.ids(self.target(inspector, "__main__", kind="cli"), "forms"))
        inspector.target_runtime = SimpleNamespace(call=Mock(return_value={"routed": True}))
        result = Inspector.inspect_harness(inspector, path="src", offset=2, count=3)
        self.assertEqual(result, {"routed": True})
        inspector.target_runtime.call.assert_called_once_with("inspect_harness", path="src", offset=2, count=3)
        target = dict(path="source.py", symbol="run")
        classify_target(inspector, target)
        inspector.target_runtime.call.assert_called_with("classify_target", target=target)

    def test_missing_duplicate_and_withheld_targets_return_unknown(self):
        inspector = self.inspector({"source.py": "def run(): pass\ndef run(): pass"}, withheld=["private.ipynb"])
        self.assertEqual(self.target(inspector, "run")["unknowns"][0]["id"], "TARGET_NOT_LOCATED")
        self.assertEqual(self.target(inspector, "run", path="missing.py")["unknowns"][0]["id"], "SOURCE_NOT_FROZEN")
        profile = classify_target(inspector, dict(path="private.ipynb", unit="cell:2", symbol="run"))
        self.assertEqual(profile["unknowns"][0]["id"], "WITHHELD")
        self.assertEqual(profile["unit"], "cell:2")

    def test_bad_pagination_is_rejected_without_source_reads(self):
        inspector = self.inspector({"source.py": "x = 1"})
        for kwargs in (dict(offset=-1), dict(offset=True), dict(count=0), dict(count="1"), dict(path=None)):
            with self.assertRaises(ValueError):
                inspect_harness(inspector, **kwargs)
        inspector.data.assert_not_called()

    def test_each_functional_signal_has_a_distinct_investigation_recipe(self):
        examples = [("search", "retrieval_flow"), ("bind_tools", "tool_registration"),
                    ("validate", "guardrail_gate"), ("revise", "feedback_loop"),
                    ("generate", "inference_lifecycle"), ("evaluate", "offline_online_role")]
        for call, recipe in examples:
            with self.subTest(call=call):
                inspector = self.inspector({"source.py": f"def run(x):\n    return engine.{call}(x)\n"})
                profile = self.target(inspector, "run")
                recipes = {item["id"]: item for item in profile["recipes"]}
                self.assertIn(recipe, recipes)
                self.assertEqual(len(set(recipes) & {item[1] for item in examples}), 1)
                self.assertTrue(all(step["arguments"]["path"] == "source.py"
                    for step in recipes[recipe]["steps"]))

    def test_recipe_reads_target_neighborhood_and_evidence_previews_are_bounded(self):
        inspector = self.inspector({"source.py": "# padding\n" * 200 +
            "def run(callback):\n" + "    callback()\n" * 24 + "    return engine.search('q')\n"})
        profile = self.target(inspector, "run")
        dynamic = next(item for item in profile["traits"] if item["id"] == "dynamic_call")
        self.assertEqual(dynamic["evidence_count"], 24)
        self.assertEqual(len(dynamic["evidence"]), 8)
        self.assertTrue(dynamic["evidence_truncated"])
        unknowns = [item for item in profile["unknowns"] if item["id"] == "dynamic_resolution"]
        self.assertEqual(len(unknowns), 1)
        self.assertEqual(unknowns[0]["evidence_count"], 24)
        for recipe in profile["recipes"]:
            for step in recipe["steps"]:
                if step["tool"] == "read_file":
                    self.assertGreater(step["arguments"]["start"], 180)

    def test_class_target_is_isolated_and_cli_accepts_script_symbol(self):
        inspector = self.inspector({"source.py": """
            class Retriever:
                def __init__(self):
                    self.index = Loader.from_pretrained('index')
                def run(self, query):
                    return self.index.search(query)
            class Trainer:
                def run(self):
                    return trainer.fit()
            if __name__ == '__main__':
                Retriever().run('q')
        """})
        profile = self.target(inspector, "Retriever", kind="class")
        self.assertIn("retrieval", self.ids(profile, "families"))
        self.assertNotIn("offline_training_evaluation", self.ids(profile, "families"))
        self.assertIn("model_load", self.ids(profile, "traits"))
        cli = self.target(inspector, "source.py", kind="cli")
        self.assertIn("cli", self.ids(cli, "forms"))
        self.assertIn("冻结脚本范围", cli["scope"])

    def test_malformed_target_returns_unknown_without_reading_source(self):
        inspector = self.inspector({"source.py": "def run(): pass"})
        for target in ({}, dict(path=None), dict(path=""), None):
            self.assertEqual(classify_target(inspector, target)["unknowns"][0]["id"], "INVALID_TARGET")
        inspector.data.assert_not_called()


if __name__ == "__main__":
    unittest.main()
