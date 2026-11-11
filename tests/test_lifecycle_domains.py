"""Closed argument shapes do not grant source/SDK effects or native-object permission."""

import ast
import copy
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from seampact.source.inspection import Inspector
from seampact.source.keyword_domain import narrow_keywords
from seampact.source.lifecycle_domains import DomainGap, request_domain, value_domain, EXACT_REQUEST_CODEC
from seampact.scheduling.lifecycle import build_lifecycle_evidence
from seampact.hosting.deployment import encode_port, decode_port, PortGap
from seampact.runtime import Coordinator, OwnerRuntime
from seampact.store import Store
from seampact.units import UnitCatalog


class ValueDomainTests(unittest.TestCase):
    def test_finite_record_and_list_literals_have_only_value_evidence(self):
        values = [{"format": "triplet", "parts": ["a", "b", "c"], "valid": True}, [1, 2], None]
        proof = value_domain({"enum": values})
        self.assertTrue(proof["contains_passive_records"])
        self.assertTrue(proof["contains_lists"])
        self.assertEqual(proof["choices"], 3)
        self.assertNotIn("lifecycle", proof)
        changed = copy.deepcopy(values)
        changed[0]["valid"] = 1
        self.assertNotEqual(proof["values_digest"], value_domain({"enum": changed})["values_digest"])

    def test_recursive_typed_lists_mappings_and_union_are_closed(self):
        schema = {"type": "object", "additionalProperties": {"type": "array", "items":
            {"anyOf": [{"type": "string"}, {"type": "null"}, {"type": "integer"}]}}}
        self.assertEqual(value_domain(schema)["kind"], "string_keyed_mapping")

    def test_unknown_open_custom_and_native_object_arms_remain_unknown(self):
        native = {"x-native-type": {"source_version": "frozen", "path": "record.py", "symbol": "Passive"}}
        # A source class name, even a passive constructor, does not exclude a
        # mutable aliased instance/subclass at an original native input port.
        for schema in ({}, {"type": "array"}, {"type": "array", "items": {}}, native,
                       {"anyOf": [{"type": "string"}, native]},
                       {"type": "object", "properties": {"a": {"type": "string"}}, "additionalProperties": False},
                       {"oneOf": [{"type": "string"}]}, {"enum": []}, {"enum": [float("nan")]},
                       {"enum": [object()]}, {"enum": [{"bad": {1: "not a JSON key"}}]}):
            with self.subTest(schema=schema):
                with self.assertRaises(DomainGap):
                    value_domain(schema)

    def test_keyword_schema_is_rebuilt_from_original_signature_and_exact_choices(self):
        node = ast.parse("def run(text: str, **options):\n    return text\n").body[0]
        entry = dict(keyword_domain={"record": [{"parts": ["a", "b"]}]})
        original = dict(type="object", properties={"text": {"type": "string"}}, required=["text"], additionalProperties=True)
        schema = narrow_keywords(original, node, entry["keyword_domain"])
        proof = request_domain(schema, entry, node, EXACT_REQUEST_CODEC)
        self.assertEqual(proof["status"], "CHECKED")
        self.assertTrue(proof["keyword_domain"]["all_optional"])
        self.assertEqual(proof["properties"]["record"]["kind"], "finite_literal_values")
        changed = copy.deepcopy(schema)
        changed["properties"]["record"]["enum"].append({"parts": ["changed"]})
        self.assertEqual(request_domain(changed, entry, node, EXACT_REQUEST_CODEC)["status"], "UNKNOWN")
        changed = copy.deepcopy(schema)
        changed["required"].append("record")
        self.assertEqual(request_domain(changed, entry, node, EXACT_REQUEST_CODEC)["status"], "UNKNOWN")

    def test_open_or_forged_keyword_domains_do_not_erase_other_proven_fields(self):
        node = ast.parse("def run(text: str, **options):\n    return text\n").body[0]
        schema = dict(type="object", properties={"text": {"type": "string"}, "opaque": {}},
                      required=["text"], additionalProperties=True)
        proof = request_domain(schema, {}, node, EXACT_REQUEST_CODEC)
        self.assertEqual(proof["status"], "UNKNOWN")
        self.assertEqual(proof["properties"]["text"]["status"], "CHECKED")
        self.assertEqual(proof["properties"]["opaque"]["status"], "UNKNOWN")
        schema["additionalProperties"] = False
        proof = request_domain(schema, dict(keyword_domain={"text": ["override"]}), node, EXACT_REQUEST_CODEC)
        self.assertEqual(proof["status"], "UNKNOWN")

    def test_structure_without_exact_transport_is_not_an_enforced_domain(self):
        node = ast.parse("def run(text: str):\n    return text\n").body[0]
        schema = dict(type="object", properties={"text": {"type": "string"}}, required=["text"], additionalProperties=False)
        for codec in (None, "JSON", "group-port/2", "candidate-says-safe"):
            proof = request_domain(schema, {}, node, codec)
            self.assertEqual(proof["shape_status"], "CHECKED")
            self.assertEqual(proof["status"], "UNKNOWN")
            self.assertEqual(proof["enforcement"]["status"], "UNKNOWN")
        proof = request_domain(schema, {}, node, EXACT_REQUEST_CODEC)
        self.assertEqual(proof["status"], "CHECKED")
        self.assertIn("local NativeBindings", proof["scope"])


SOURCE = '''def score(values: list[float], **options) -> float:
    if "record" in options:
        return sum(values) + options["record"]["bias"]
    return sum(values)
'''


class LifecycleDomainIntegrationTests(unittest.TestCase):
    def catalog(self, source=SOURCE, domain=None):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root / "original.py").write_text(source, encoding="utf-8")
        inspector = Inspector.freeze(root, root / "snapshots", "request", ["original.py"], "score", "original result")
        inspector.request["source_id"] = "source"
        entry = dict(id="score", kind="function", path="original.py", symbol="score", boundary_ids=["score-W"])
        if domain is not None:
            entry["keyword_domain"] = domain
        candidate = dict(entries=[entry], boundaries=[dict(id="score-W", mode="W", path="original.py", symbol="score")])
        store = Store(":memory:")
        self.addCleanup(store.close)
        runtime = OwnerRuntime(Coordinator(store))
        self.addCleanup(runtime.close)
        catalog = UnitCatalog(root, store, runtime)
        report = catalog.inspect_candidate(inspector, candidate)
        life = report["runtime_groups"]["groups"][0]["contract"]["lifecycle"]
        return catalog, inspector, candidate, life

    def test_closed_keyword_record_values_preserve_actual_omission_and_native_call(self):
        catalog, inspector, candidate, life = self.catalog(domain={"record": [{"bias": 2, "labels": ["safe"]}]})
        self.assertEqual(life["replicate"]["status"], "SUPPORTED", life)
        evidence = life["source_evidence"]["members"][0]["request_domain"]
        self.assertEqual(evidence["status"], "CHECKED")
        self.assertEqual(evidence["enforcement"]["codec"], EXACT_REQUEST_CODEC)
        unit = catalog.install(inspector, candidate, "analysis", "installation")[0]
        self.assertEqual(catalog.invoke(unit["id"], {"values": [1.0, 2.0]}), 3.0)
        self.assertEqual(catalog.invoke(unit["id"], {"values": [1.0, 2.0], "record": {"bias": 2, "labels": ["safe"]}}), 5.0)
        with self.assertRaises(ValueError):
            catalog.invoke(unit["id"], {"values": [1.0], "record": {"bias": 3, "labels": ["safe"]}})
        with self.assertRaises(ValueError):
            catalog.invoke(unit["id"], {"values": [1.0], "callback": "escape"})

    def test_local_native_subclasses_nonfinite_values_and_nonstring_keys_are_not_transport_proofs(self):
        cases = [('text: str', 'text + "!"', 'text', str),
                 ('values: list[float]', 'sum(values)', 'values', list),
                 ('record: dict[str, int]', 'record["x"]', 'record', dict)]
        for signature, body, parameter, parent in cases:
            effects = []
            if parent is str:
                class Custom(str):
                    def __add__(self, other):
                        effects.append("source hook")
                        return super().__add__(other)
                value = Custom("hello")
            elif parent is list:
                class Custom(list):
                    def __iter__(self):
                        effects.append("iterator hook")
                        return super().__iter__()
                value = Custom([1.0, 2.0])
            else:
                class Custom(dict):
                    def __getitem__(self, key):
                        effects.append("source hook")
                        return super().__getitem__(key)
                value = Custom(x=1)
            source = 'def score(' + signature + '):\n    return ' + body + '\n'
            with self.subTest(parent=parent.__name__):
                catalog, inspector, candidate, life = self.catalog(source)
                unit = catalog.install(inspector, candidate, 'analysis', 'installation')[0]
                catalog.invoke(unit['id'], {parameter: value})
                self.assertTrue(effects, "the actual local call retained the subtype hook")
                with self.assertRaises(PortGap):
                    encode_port({parameter: value})
                report = catalog.inspect_candidate(inspector, candidate)
                rows = [dict(entry_id='score', entry=candidate['entries'][0], check=report['entries'][0])]
                without_transport = build_lifecycle_evidence(inspector, rows, ['score'])
                self.assertEqual(without_transport['checks']['request_isolation']['status'], 'UNKNOWN')
                self.assertIn('enforcement is missing', without_transport['members'][0]['reason'])
        catalog, inspector, candidate, _ = self.catalog('def score(value: float) -> float:\n    return value\n')
        unit = catalog.install(inspector, candidate, 'analysis', 'installation')[0]
        for value in (float('nan'), float('inf'), -float('inf')):
            # The existing original-native-reference arm deliberately does not
            # perform JSON canonicalization. It is not the lifecycle wire domain.
            actual = catalog.invoke(unit['id'], {'value': value}, native_arguments=['value'])
            self.assertTrue(actual != actual if value != value else actual == value)
            with self.assertRaises(PortGap):
                encode_port({'value': value})
        catalog, inspector, candidate, _ = self.catalog('def score(record: dict[str, int]) -> int:\n    return len(record)\n')
        unit = catalog.install(inspector, candidate, 'analysis', 'installation')[0]
        self.assertEqual(catalog.invoke(unit['id'], {'record': {1: 2}}), 1)
        with self.assertRaises(PortGap):
            encode_port({'record': {1: 2}})
        plain = dict(text='hello', values=[1.0, 2.0], record={'x': 1})
        restored = decode_port(encode_port(plain))
        self.assertEqual(restored, plain)
        self.assertIs(type(restored['text']), str)
        self.assertIs(type(restored['values']), list)
        self.assertIs(type(restored['record']), dict)

    def test_closed_port_does_not_grant_external_or_python_effect_permission(self):
        for source in ("import torch\n" + SOURCE,
                       SOURCE.replace("return sum(values)", "print(values)\n    return sum(values)"),
                       SOURCE.replace('return sum(values) + options["record"]["bias"]', 'options["record"]["bias"] = 99\n        return sum(values)')):
            with self.subTest(source=source):
                _, _, _, life = self.catalog(source, {"record": [{"bias": 2}]})
                member = life["source_evidence"]["members"][0]
                self.assertEqual(member["request_domain"]["status"], "CHECKED")
                self.assertEqual(life["replicate"]["status"], "UNKNOWN")

    def test_nested_variadic_helper_and_open_root_remain_unknown(self):
        source = "def helper(**options):\n    return 1.0\n\ndef score(values: list[float], **options) -> float:\n    return helper()\n"
        _, _, _, life = self.catalog(source, {})
        self.assertEqual(life["replicate"]["status"], "UNKNOWN")
        self.assertIn("own checked finite domain", life["source_evidence"]["members"][0]["reason"])
        _, _, _, life = self.catalog()
        self.assertEqual(life["replicate"]["status"], "UNKNOWN")
        self.assertIn("keyword", life["source_evidence"]["members"][0]["reason"])


if __name__ == "__main__":
    unittest.main()
