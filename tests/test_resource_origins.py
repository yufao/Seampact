"""Source-only resource distinctions and preserved initialization; no models."""

import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from seampact.inspection import Inspector
from seampact.resource_origins import initialization_gaps, resource_origin_facts
from seampact.runtime import Coordinator, OwnerRuntime
from seampact.store import Store
from seampact.units import UnitCatalog


MODEL = """
class Guard:
    def __init__(self):
        self.weight = 0
        self.trace = []
    def load_state_dict(self, state: int):
        self.weight = state
        self.trace.append('checkpoint')
    def to(self, device: str):
        self.trace.append(device)
    def classify(self, text: str) -> str:
        return str(self.weight) + ':' + ','.join(self.trace) + ':' + text
    def constant(self) -> int:
        return 1
"""


class ResourceOriginsTests(unittest.TestCase):
    def inspector(self, files):
        files = {name: textwrap.dedent(source).lstrip().encode() for name, source in files.items()}
        return SimpleNamespace(files={name: {} for name in files}, data=lambda path: files[path])

    def entry(self):
        return dict(id="classify", path="guard.py", symbol="Guard.classify", kind="method",
                    constructor=dict(symbol="Guard", arguments={}), boundary_ids=["whole"])

    def test_model_identifiers_local_paths_checkpoint_and_cache_are_distinct(self):
        inspector = self.inspector({"load.py": """
            import torch as t
            from transformers import AutoModel as Model
            from huggingface_hub import snapshot_download
            def load(model_id, cache):
                a = Model.from_pretrained('organization/model', cache_dir=cache)
                b = Model.from_pretrained('./models/base')
                c = t.load('weights.pt', map_location='cpu')
                d = snapshot_download(repo_id='organization/model', cache_dir='./cache')
                return Model.from_pretrained(model_id)
        """})
        facts = resource_origin_facts(inspector, [dict(path="load.py", start=1, end=12)])
        models = [fact for fact in facts if fact["role"] == "model"]
        self.assertEqual([fact["source_kind"] for fact in models], ["hub_or_local", "local_path", "hub_id", "unknown"])
        self.assertEqual(models[-1]["accepted_origins"], "hub_or_local")
        checkpoint = next(fact for fact in facts if fact["role"] == "checkpoint")
        self.assertEqual((checkpoint["loader"], checkpoint["source_kind"]), ("torch.load", "local_path"))
        cache = [fact for fact in facts if fact["role"] == "cache"]
        self.assertEqual(len(cache), 2)
        self.assertTrue(all(fact["source_kind"] == "cache_directory" for fact in cache))

    def test_incomplete_evidence_and_shadowed_imports_do_not_create_loader_facts(self):
        inspector = self.inspector({"load.py": """
            import torch
            from transformers import AutoModel
            def missing(torch):
                return torch.load('not-a-verified-loader.pt')
            def shadowed():
                torch = object()
                return torch.load('not-a-verified-loader.pt')
            def actual():
                return AutoModel.from_pretrained(
                    'organization/model',
                )
        """})
        self.assertEqual(resource_origin_facts(inspector, [dict(path="load.py", start=1, end=10)]), [])
        facts = resource_origin_facts(inspector, [dict(path="load.py", start=1, end=11)])
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["loader"], "transformers.AutoModel.from_pretrained")

    def test_branch_literal_candidates_are_cited_unselected_and_lexically_scoped(self):
        inspector = self.inspector({"load.py": """
            from transformers import AutoModel
            def unrelated():
                ckpt = 'wrong/function'
            def load(model_name):
                if model_name == 'large':
                    ckpt = 'organization/large'
                else:
                    ckpt = './models/small'
                def nested():
                    ckpt = 'wrong/nested'
                return AutoModel.from_pretrained(ckpt)
        """})
        facts = resource_origin_facts(inspector, [dict(path="load.py", start=1, end=11)])
        self.assertEqual(len(facts), 1)
        fact = facts[0]
        self.assertEqual((fact["expression"], fact["source_kind"], fact["literal"]), ("ckpt", "unknown", None))
        candidates = fact["literal_candidates"]
        self.assertEqual([(item["literal"], item["source_kind"]) for item in candidates],
                         [("organization/large", "hub_or_local"), ("./models/small", "local_path")])
        self.assertEqual([item["conditions"][0]["branch"] for item in candidates], ["body", "orelse"])
        self.assertTrue(all(item["conditions"][0]["start"] == 5 for item in candidates))
        self.assertTrue(all(item["selection"] == "NOT_INFERRED" and item["lexical_scope"]["name"] == "load" for item in candidates))
        # Covering the loader alone, or one assignment without its condition,
        # does not authorize assignment/condition facts from omitted ranges.
        for evidence in ([dict(path="load.py", start=11, end=11)],
                         [dict(path="load.py", start=6, end=6), dict(path="load.py", start=11, end=11)]):
            self.assertEqual(resource_origin_facts(inspector, evidence)[0]["literal_candidates"], [])

    def test_main_guard_checkpoint_and_device_have_exact_owner_and_locations(self):
        inspector = self.inspector({"guard.py": MODEL, "eval.py": """
            from guard import Guard as ImportedGuard
            import torch
            if __name__ == '__main__':
                model = ImportedGuard()
                alias = model
                alias.load_state_dict(torch.load(args.resume))
                model.to(device)
                evaluate(model)
        """})
        gaps = initialization_gaps(inspector, self.entry())
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]["code"], "initialization_incomplete")
        self.assertEqual(gaps[0]["construction"]["start"], 4)
        self.assertEqual([(operation["path"], operation["start"], operation["method"]) for operation in gaps[0]["operations"]],
                         [("eval.py", 6, "load_state_dict"), ("eval.py", 7, "to")])
        independent = dict(self.entry(), symbol="Guard.constant")
        self.assertEqual(initialization_gaps(inspector, independent), [])

    def test_unrelated_same_named_classes_reassigned_instances_and_imports_are_not_matches(self):
        cases = [
            "from other import Guard\nmodel = Guard()\nmodel.load_state_dict(7)\n",
            "from guard import Guard\nmodel = Guard()\nmodel = Other()\nmodel.load_state_dict(7)\n",
            "from guard import Guard\nGuard = Other\nmodel = Guard()\nmodel.load_state_dict(7)\n",
            "from guard import Guard\ndef run(Guard):\n    model = Guard()\n    model.load_state_dict(7)\n",
            "from guard import Guard\nmodel = Guard()\nif condition:\n    model = Other()\nmodel.load_state_dict(7)\n",
        ]
        for source in cases:
            with self.subTest(source=source):
                inspector = self.inspector({"guard.py": MODEL, "eval.py": source, "other.py": MODEL})
                self.assertEqual(initialization_gaps(inspector, self.entry()), [])

    def test_constructor_internal_checkpoint_is_preserved_by_direct_binding(self):
        source = MODEL.replace("self.trace = []", "self.trace = []\n        self.load_state_dict(7)\n        self.to('cpu')")
        inspector = self.inspector({"guard.py": source})
        self.assertEqual(initialization_gaps(inspector, self.entry()), [])

    def test_explicit_original_initializers_must_preserve_arguments_and_order(self):
        inspector = self.inspector({"guard.py": MODEL + "\ndef run():\n    model = Guard()\n    model.load_state_dict(7)\n    model.to('cpu')\n"})
        entry = self.entry()
        entry["initializers"] = [dict(symbol="Guard.load_state_dict", arguments=dict(state=7)),
                                 dict(symbol="Guard.to", arguments=dict(device="cpu"))]
        self.assertEqual(initialization_gaps(inspector, entry), [])
        entry["initializers"].reverse()
        self.assertEqual(len(initialization_gaps(inspector, entry)), 1)
        entry["initializers"].reverse()
        entry["initializers"][0]["arguments"]["state"] = 0
        self.assertEqual(len(initialization_gaps(inspector, entry)), 1)

    def test_operations_after_first_request_are_not_reported_as_initialization(self):
        inspector = self.inspector({"guard.py": MODEL + "\ndef run():\n    model = Guard()\n    model.classify('request')\n    model.load_state_dict(7)\n"})
        self.assertEqual(initialization_gaps(inspector, self.entry()), [])

    def freeze(self, source):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        (root / "guard.py").write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
        inspector = Inspector.freeze(root, root / "snapshots", "request", ["guard.py"], "Guard.classify", "result")
        inspector.request["source_id"] = "source"
        return root, inspector

    def test_direct_entry_reports_initialization_before_resource_selection(self):
        root, inspector = self.freeze(MODEL + "\nif __name__ == '__main__':\n    model = Guard()\n    model.load_state_dict(7)\n    model.to('cpu')\n")
        store = Store(":memory:")
        self.addCleanup(store.close)
        entry = self.entry()
        candidate = dict(entries=[entry], boundaries=[dict(id="whole", path="guard.py", symbol="Guard.classify", mode="W")])
        catalog = UnitCatalog(root, store, None)
        report = catalog.inspect_candidate(inspector, candidate)["entries"][0]
        self.assertEqual(report["status"], "MISSING")
        self.assertEqual(report["code"], "initialization_incomplete")
        self.assertEqual(report["initialization_gaps"][0]["operations"][0]["method"], "load_state_dict")

    def test_original_b_call_prefix_executes_checkpoint_and_device_in_order(self):
        root, inspector = self.freeze(MODEL + "\ndef run(text: str):\n    model = Guard()\n    model.load_state_dict(7)\n    model.to('cpu')\n    return model.classify(text)\n")
        entry = self.entry()
        entry.pop("constructor")
        call_line = next(index for index, line in enumerate((root / "guard.py").read_text().splitlines(), 1) if "return model.classify" in line)
        entry["binding_program"] = dict(path="guard.py", scope="run", call_line=call_line, receiver="model",
            task_parameters=dict(text="text"), arguments={}, argument_refs={})
        inspector.request["entry_profile"] = dict(confirmed=True, frame_observation_excluded=True, explicit_lifetime=True)
        candidate = dict(entries=[entry], boundaries=[dict(id="whole", path="guard.py", symbol="Guard.classify", mode="W")])
        store = Store(":memory:")
        runtime = OwnerRuntime(Coordinator(store))
        try:
            catalog = UnitCatalog(root, store, runtime)
            report = catalog.inspect_candidate(inspector, candidate)["entries"][0]
            self.assertEqual(report["status"], "READY", report.get("reason"))
            self.assertEqual(report["construction"]["rule"], "B-CALL-straight-prefix/1")
            unit = catalog.install(inspector, candidate, "analysis", "install")[0]
            self.assertEqual(catalog.invoke(unit["id"], dict(text="sample")), "7:checkpoint,cpu:sample")
        finally:
            runtime.close()
            store.close()


if __name__ == "__main__":
    unittest.main()
