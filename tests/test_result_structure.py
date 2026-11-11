"""Successful-return source proofs preserve unknowns and original annotations."""

import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from seampact.source.inspection import Inspector
from seampact.source.result_structure import derive_result_structure


class ResultStructureTests(unittest.TestCase):
    def fixture(self, source, method=False):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        (root / "main.py").write_text(source, encoding="utf-8")
        inspector = Inspector.freeze(root, root / "snapshots", "request", ["main.py"], "run", "result")
        entry = dict(id="run", path="main.py", unit="file", kind="method" if method else "function",
                     symbol="Host.run" if method else "run", boundary_ids=[])
        if method:
            entry["constructor"] = dict(symbol="Host", arguments={})
        candidate = dict(entries=[entry], boundaries=[], dependencies=[])
        return inspector, entry, candidate

    def proof(self, source, method=False):
        return derive_result_structure(*self.fixture(source, method))

    def test_original_tuple_forwarding_conflicts_with_list_annotation_without_typing_unknown_children(self):
        source = ("from typing import List\nclass Backend:\n"
                  "    def run(self, data):\n        return data, data, data, data\n"
                  "class Other:\n    pass\nclass Host:\n"
                  "    def __init__(self):\n        self.model = Backend()\n"
                  "    def run(self, data) -> List[float]:\n"
                  "        if isinstance(self.model, Backend):\n            return self._run(data)\n"
                  "        elif isinstance(self.model, Other):\n            return data\n"
                  "    def _run(self, data):\n        return self.model.run(data)\n")
        proof = self.proof(source, True)
        self.assertEqual(proof["status"], "CHECKED", proof)
        self.assertEqual(proof["result_schema"], {"type": "array", "x-python-container": "tuple",
            "prefixItems": [{}, {}, {}, {}], "minItems": 4, "maxItems": 4})
        self.assertFalse(proof["fully_typed"])
        self.assertEqual(proof["unknown_paths"], ["$[0]", "$[1]", "$[2]", "$[3]"])
        self.assertEqual(proof["annotation"]["expression"], "List[float]")
        self.assertEqual(proof["annotation_conflict"]["status"], "CONFLICT")
        self.assertTrue(all(e["source_sha256"] and e["ast_sha256"] for e in proof["source_evidence"]))

    def test_fresh_literal_structure_and_scalar_types_are_source_proved(self):
        proof = self.proof("def run():\n    return ([1, 0], 'original', True, None)\n")
        self.assertEqual(proof["status"], "CHECKED")
        self.assertTrue(proof["fully_typed"])
        self.assertEqual(proof["result_schema"]["prefixItems"][0]["prefixItems"],
                         [{"type": "integer"}, {"type": "integer"}])

    def test_mutable_owner_flag_does_not_freeze_the_first_call_return(self):
        source = ("class Host:\n    def __init__(self):\n        self.flag = True\n"
                  "    def run(self):\n        if self.flag:\n            self.flag = False\n"
                  "            return (1, 2)\n        return 'later'\n")
        proof = self.proof(source, True)
        self.assertEqual(proof["status"], "UNKNOWN", proof)
        self.assertIn("flag", proof["owner_state"][0]["widened_fields"])

    def test_peer_receiver_rewrite_and_alias_escape_remove_type_branch_invariant(self):
        base = ("class Backend:\n    def run(self):\n        return (1, 2)\n"
                "class Other:\n    def run(self):\n        return 'later'\n"
                "class Host:\n    def __init__(self):\n        self.model=Backend()\n"
                "    def run(self):\n        if isinstance(self.model, Backend):\n"
                "            return self.model.run()\n        return 'later'\n{peer}")
        for peer in ("    def switch(self):\n        self.model=Other()\n",
                     "    def expose(self):\n        return self.model\n",
                     "    def expose(self):\n        alias=self\n        alias.model=Other()\n"):
            with self.subTest(peer=peer):
                self.assertEqual(self.proof(base.format(peer=peer), True)["status"], "UNKNOWN")

    def test_receiver_attribute_hooks_and_properties_cannot_invent_a_type_invariant(self):
        base = ("class Backend:\n    pass\nclass Other:\n    pass\nclass Host:\n"
                "    def __init__(self):\n        self.model=Backend()\n"
                "    def run(self):\n        if isinstance(self.model, Backend):\n"
                "            return (1,2)\n        return 'other'\n{hook}")
        hooks = ["    def __setattr__(self, key, value):\n        pass\n",
                 "    def __getattribute__(self, key):\n        return Other()\n",
                 "    @property\n    def model(self):\n        return Other()\n"
                 "    @model.setter\n    def model(self, value):\n        pass\n"]
        for hook in hooks:
            with self.subTest(hook=hook):
                self.assertEqual(self.proof(base.format(hook=hook), True)["status"], "UNKNOWN")

    def test_all_open_branches_are_joined_instead_of_last_return_winning(self):
        proof = self.proof("def helper(value):\n    if value:\n        return (1, 'a')\n"
                           "    return (2, 3)\ndef run(value):\n    return helper(value)\n")
        self.assertEqual(proof["status"], "CHECKED", proof)
        self.assertEqual(proof["unknown_paths"], ["$[1]"])
        self.assertEqual(proof["result_schema"]["prefixItems"], [{"type": "integer"}, {}])

    def test_implicit_none_is_a_real_normal_path_and_raise_alone_proves_no_shape(self):
        for source in ("def run(value):\n    if value:\n        return (1, 2)\n",
                       "def run():\n    raise RuntimeError('original')\n"):
            with self.subTest(source=source):
                self.assertEqual(self.proof(source)["status"], "UNKNOWN")

    def test_names_subscripts_starred_sdk_and_constructor_results_remain_unknown(self):
        for source in ("def run(value):\n    items=[1,2]\n    return items\n",
                       "def helper():\n    return (1,2)\ndef run():\n    return helper()[0]\n",
                       "def run(value):\n    return (*value,)\n",
                       "import sdk\ndef run():\n    return sdk.result()\n",
                       "class C:\n    def __init__(self):\n        return (1,2)\ndef run():\n    return C()\n"):
            with self.subTest(source=source):
                self.assertEqual(self.proof(source)["status"], "UNKNOWN")

    def test_a_source_return_is_not_forwarded_for_a_mixed_constructor_target(self):
        source = ("class C:\n    pass\ndef helper():\n    return (1,2)\n"
                  "def run(flag):\n    f = helper if flag else C\n    return f()\n")
        self.assertEqual(self.proof(source)["status"], "UNKNOWN")

    def test_control_and_generator_protocols_do_not_become_plain_returns(self):
        for source in ("def run():\n    try:\n        return (1,2)\n    finally:\n        return 'later'\n",
                       "import sdk\ndef run():\n    with sdk.context():\n        return (1,2)\n",
                       "def run(value):\n    while value:\n        pass\n    else:\n        return 'other'\n    return (1,2)\n",
                       "def run():\n    yield 1\n    return (1,2)\n",
                       "async def run():\n    return (1,2)\n"):
            with self.subTest(source=source):
                self.assertEqual(self.proof(source)["status"], "UNKNOWN")

    def test_definition_time_source_annotation_and_default_protocols_remain_unknown(self):
        sources = ["class Box:\n    def __class_getitem__(cls, value):\n        return int\n"
                   "def declared(value: Box[int]):\n    pass\ndef run():\n    return (1,2)\n",
                   "from typing import List\nclass Box:\n    def __class_getitem__(cls, value):\n        return int\n"
                   "class Declared:\n    List=Box\n    def method(self, value: List[int]):\n        pass\n"
                   "def run():\n    return (1,2)\n",
                   "def alter():\n    return 1\ndef declared(value=alter()):\n    pass\n"
                   "def run():\n    return (1,2)\n"]
        for source in sources:
            with self.subTest(source=source):
                proof = self.proof(source)
                self.assertEqual(proof["status"], "UNKNOWN")
                self.assertEqual(proof["negative_scope_status"], "UNKNOWN")

    def test_native_module_and_mutable_global_conditions_are_not_first_call_invariants(self):
        source = "flag=True\ndef run():\n    if flag:\n        return (1,2)\n    return 'other'\n"
        self.assertEqual(self.proof(source)["status"], "UNKNOWN")
        source = ("import sdk\nSTATE=sdk.state()\nclass Backend:\n    pass\nclass Other:\n    pass\n"
                  "class Host:\n    def __init__(self):\n        STATE.field=Backend()\n"
                  "    def run(self):\n        if isinstance(STATE.field,Backend):\n"
                  "            STATE.field=Other()\n            return (1,2)\n        return 'later'\n")
        self.assertEqual(self.proof(source, True)["status"], "UNKNOWN")

    def test_proof_is_deterministic_json_and_does_not_change_candidate(self):
        inspector, entry, candidate = self.fixture("def run(value) -> list[float]:\n    return (value, 1)\n")
        original = copy.deepcopy(candidate)
        first = derive_result_structure(inspector, entry, candidate)
        second = derive_result_structure(inspector, entry, candidate)
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))
        self.assertEqual(candidate, original)


if __name__ == "__main__":
    unittest.main()
