"""Source-only forwarding proofs, including rejected misleading call chains."""

import copy
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from seampact.dependencies import check_requirements
from seampact.inspection import Inspector


class ResourceFlowChecks(unittest.TestCase):
    def check_flow(self, files, *, field="base_url", parameter="url", constructor=False, evidence=None):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        for name, source in files.items():
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_text(source, encoding="utf-8")
        inspector = Inspector.freeze(root, root / "snapshots", "request", list(files), "goal", "original result")
        ref = {"$dependency": dict(id="resource", field=field)}
        entry = dict(id="target", path="main.py", symbol="Host.run" if constructor else "run",
                     kind="method" if constructor else "function")
        if constructor:
            entry["constructor"] = dict(symbol="Host", arguments={parameter: ref})
        else:
            entry["fixed_arguments"] = {parameter: ref}
        dep = dict(id="resource", kind="weight" if field == "path" else "service", configurable=True,
                   required=True, entry_ids=["target"], read_time="constructor" if constructor else "call",
                   owner_scope="original", evidence=evidence or [dict(path=p, start=1, end=len(s.splitlines()))
                        for p, s in files.items()], mappings=[dict(entry_id="target", parameter=parameter, field=field,
                        location="constructor.arguments" if constructor else "entry.fixed_arguments")])
        candidate = dict(entries=[entry], dependencies=[dep])
        original = copy.deepcopy(candidate)
        result = check_requirements(inspector, candidate)[0]
        self.assertEqual(candidate, original)
        return result

    def test_imported_function_forwarding_and_local_alias(self):
        files = {"main.py": "from helper import query\ndef run(url):\n    endpoint = url\n    return query(endpoint)\n",
                 "helper.py": "import requests as transport\ndef query(endpoint):\n    return transport.post(endpoint)\n"}
        result = self.check_flow(files)
        self.assertEqual(result["status"], "MAPPING_CHECKED", result["reason"])
        proof = result["entry_checks"]["target"]["mapping_proofs"][0]["proof"]
        self.assertEqual({r["path"] for r in proof["trace"]}, {"main.py", "helper.py"})

    def test_constructor_attribute_and_called_method_use_sdk_dictionary(self):
        result = self.check_flow({
            "main.py": "from backend import Backend\nclass Host:\n    def __init__(self, url):\n        self.backend = Backend(url) if url else None\n    def run(self):\n        return self.backend.generate()\n",
            "backend.py": "from openai import OpenAI\nclass Backend:\n    def __init__(self, model_name):\n        self.client = OpenAI()\n        self.model_name = model_name\n    def generate(self):\n        return self.result().text\n    def result(self):\n        kwargs = {'model': self.model_name}\n        if request_unknown:\n            kwargs.update({'temperature': None})\n        return self.client.chat.completions.create(**kwargs)\n"},
            constructor=True, field="model")
        self.assertEqual(result["status"], "MAPPING_CHECKED", result["reason"])

    def test_sdk_api_base_alias_requires_actual_litellm_import(self):
        for statement, expected in (("from litellm import batch_completion", "MAPPING_CHECKED"),
                                    ("from other_sdk import batch_completion", "MISSING")):
            with self.subTest(statement=statement):
                result = self.check_flow({"main.py": statement + "\ndef run(url):\n    return batch_completion(api_base=url)\n"})
                self.assertEqual(result["status"], expected, result["reason"])

    def test_cross_file_loader_preserves_path_parameter(self):
        result = self.check_flow({"main.py": "from helper import build\ndef run(url):\n    return build(url)\n",
                                 "helper.py": "from transformers import AutoModel\ndef build(checkpoint):\n    return AutoModel.from_pretrained(checkpoint)\n"}, field="path")
        self.assertEqual(result["status"], "MAPPING_CHECKED", result["reason"])
        self.assertTrue(result["resource_origins"])

    def test_missing_alias_or_callee_evidence_returns_exact_ranges(self):
        files = {"main.py": "from helper import query\ndef run(url):\n    endpoint = url\n    return query(endpoint)\n",
                 "helper.py": "import requests\ndef query(endpoint):\n    return requests.post(endpoint)\n"}
        for citations in ([dict(path="main.py", start=1, end=4)],
                          [dict(path="main.py", start=1, end=2), dict(path="main.py", start=4, end=4),
                           dict(path="helper.py", start=1, end=3)]):
            with self.subTest(citations=citations):
                result = self.check_flow(files, evidence=citations)
                self.assertEqual(result["status"], "MISSING")
                issue = result["entry_checks"]["target"]["issues"][0]
                self.assertEqual(issue["issue_code"], "evidence_incomplete")
                self.assertTrue(issue["evidence"])

    def test_reassignment_shadow_dynamic_and_uncalled_scopes_are_not_proofs(self):
        helper = "import requests\ndef query(endpoint):\n    return requests.post(endpoint)\n"
        variants = [
            "from helper import query\ndef run(url):\n    endpoint = url\n    endpoint = 'other'\n    return query(endpoint)\n",
            "from helper import query\ndef run(url, query=None):\n    return query(url)\n",
            "from helper import query\ndef run(url):\n    query = lambda v: v\n    return query(url)\n",
            "from helper import query\ndef run(url):\n    def nested():\n        return query(url)\n    return None\n",
            "import helper\ndef run(url):\n    return getattr(helper, 'query')(url)\n",
            "from helper import query\ndef run(url):\n    endpoint = None\n    if url:\n        endpoint = url\n    return query(endpoint)\n",
        ]
        for source in variants:
            with self.subTest(source=source):
                result = self.check_flow({"main.py": source, "helper.py": helper})
                self.assertEqual(result["status"], "MISSING", result)

    def test_receiver_overwrite_and_other_instance_cannot_supply_resource(self):
        backend = "from openai import OpenAI\nclass Backend:\n    def __init__(self, name):\n        self.name = name\n        self.client = OpenAI()\n    def generate(self):\n        return self.client.chat.completions.create(model=self.name)\n"
        for run in ("return Backend('other').generate()", "self.backend = Backend('other')\n        return self.backend.generate()"):
            with self.subTest(run=run):
                source = ("from backend import Backend\nclass Host:\n    def __init__(self, url):\n        self.backend = Backend(url)\n"
                          "    def run(self):\n        " + run + "\n")
                result = self.check_flow({"main.py": source, "backend.py": backend}, field="model", constructor=True)
                self.assertEqual(result["status"], "MISSING", result)

    def test_arbitrary_field_alias_and_escaped_owner_do_not_count_as_scalar(self):
        source = "from helper import query\ndef run(url):\n    return query(url)\n"
        for helper in ("def query(endpoint):\n    return unknown(api_base=endpoint)\n",
                       "from openai import OpenAI\nclass Holder:\n    def __init__(self, value):\n        self.value = value\ndef query(endpoint):\n    return OpenAI(base_url=Holder(endpoint))\n"):
            with self.subTest(helper=helper):
                self.assertEqual(self.check_flow({"main.py": source, "helper.py": helper})["status"], "MISSING")

    def test_mutation_escape_expansion_and_dead_paths_are_rejected(self):
        source = "from helper import query\ndef run(url):\n    return query(url)\n"
        variants = [
            "import requests\nrequests.post = lambda x: None\ndef query(endpoint):\n    return requests.post(endpoint)\n",
            "from litellm import completion\ndef query(endpoint):\n    kw = {'api_base': endpoint}\n    del kw['api_base']\n    return completion(**kw)\n",
            "from litellm import completion\ndef query(endpoint):\n    kw = {'api_base': endpoint}\n    alias = kw\n    del alias['api_base']\n    return completion(**kw)\n",
            "from litellm import completion\ndef query(endpoint):\n    kw = {'api_base': endpoint}\n    overwrite(kw)\n    return completion(**kw)\n",
            "import requests\ndef query(endpoint):\n    if False:\n        return requests.post(endpoint)\n",
            "import requests\ndef query(endpoint):\n    while False:\n        return requests.post(endpoint)\n",
            "import requests\ndef query(endpoint):\n    if endpoint:\n        return None\n    else:\n        return None\n    return requests.post(endpoint)\n",
        ]
        for helper in variants:
            with self.subTest(helper=helper):
                self.assertEqual(self.check_flow({"main.py": source, "helper.py": helper})["status"], "MISSING")
        result = self.check_flow({"main.py": source.replace("query(url)", "query(*[], url)"),
            "helper.py": "import requests\ndef query(unused=None, endpoint='other'):\n    return requests.post(endpoint)\n"})
        self.assertEqual(result["status"], "MISSING")
        for branch in ("if False", "while False"):
            self.assertEqual(self.check_flow({"main.py": "def run(url):\n    " + branch + ":\n        return Client(base_url=url)\n"})["status"], "MISSING")

    def test_dynamic_dispatch_reflection_and_loop_receiver_targets_are_rejected(self):
        base = "from openai import OpenAI\nclass Backend:\n    def __init__(self, name):\n        self.client = OpenAI()\n        self.name = name\n    def generate(self):\n        return self.client.chat.completions.create(model=self.name)\n"
        main = "from backend import Backend\nclass Host:\n    def __init__(self, url):\n        self.backend = Backend(url)\n    def run(self):\n        return self.backend.generate()\n"
        variants = [
            base + "    def __getattribute__(self, name):\n        return 'other'\n",
            base + "    def __new__(cls, name):\n        return object()\n",
            base.replace("        return self.client", "        setattr(self, 'name', 'other')\n        return self.client"),
            base.replace("        return self.client", "        self.client.chat.completions.create = lambda **kw: None\n        return self.client"),
        ]
        for backend in variants:
            with self.subTest(backend=backend):
                self.assertEqual(self.check_flow({"main.py": main, "backend.py": backend}, field="model", constructor=True)["status"], "MISSING")
        looping = main.replace("        return self.backend.generate()", "        for self.backend.name in ['other']:\n            return self.backend.generate()")
        self.assertEqual(self.check_flow({"main.py": looping, "backend.py": base}, field="model", constructor=True)["status"], "MISSING")

    def test_dotted_import_binds_package_root_and_does_not_guess_submodule(self):
        files = {"main.py": "import package.backend\ndef run(url):\n    return package.query(url)\n",
                 "package/__init__.py": "def query(endpoint):\n    return None\n",
                 "package/backend.py": "import requests\ndef query(endpoint):\n    return requests.post(endpoint)\n"}
        self.assertEqual(self.check_flow(files)["status"], "MISSING")

    def test_hub_id_and_cache_are_not_fixed_local_weight_locations(self):
        for loader, expression in (("Model.from_pretrained('org/model')", "'org/model'"),
                                   ("Model.from_pretrained('org/model', cache_dir='./cache')", "'./cache'")):
            with self.subTest(loader=loader):
                source = "from transformers import AutoModel as Model\ndef run(url):\n    return " + loader + "\n"
                temporary = tempfile.TemporaryDirectory()
                self.addCleanup(temporary.cleanup)
                root = Path(temporary.name)
                (root / "main.py").write_text(source)
                inspector = Inspector.freeze(root, root / "snapshots", "request", ["main.py"], "goal", "result")
                result = check_requirements(inspector, dict(entries=[dict(id="target", path="main.py", symbol="run", kind="function")],
                    dependencies=[dict(id="weights", kind="weight", configurable=False, required=True, entry_ids=["target"],
                        owner_scope="original", read_time="call", evidence=[dict(path="main.py", start=1, end=3)],
                        mappings=[], placement=dict(expression=expression, required_files=["config.json"]))]))[0]
                self.assertEqual(result["status"], "MISSING")
                self.assertEqual(result["entry_checks"]["target"]["issue_code"], "resource_locator_unsupported")
                self.assertNotIn("resolved_path", result["placement"])


if __name__ == "__main__":
    unittest.main()
