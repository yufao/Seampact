"""A record proof is usable only when both actual argument checkers enforce it."""

import ast
import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from seampact.source.lifecycle_domains import DomainGap, value_domain, request_domain, EXACT_REQUEST_CODEC
from seampact.units import _matches, NativeBindings


def record(properties, required=(), additional=False):
    return dict(type="object", properties=properties, required=list(required), additionalProperties=additional)


class RecordDomainTests(unittest.TestCase):
    def schema(self):
        return record(dict(items=dict(type="array", items=record(dict(text=dict(type="string"),
            score=dict(type="number")), ["text"])), note=dict(anyOf=[dict(type="string"), dict(type="null")])), ["items"])

    def assert_match(self, value, schema, expected):
        self.assertEqual(_matches(value, schema), expected)
        self.assertEqual(NativeBindings.__new__(NativeBindings).native_matches(value, schema), expected)

    def test_nested_required_optional_and_forbidden_fields_are_enforced_in_both_paths(self):
        schema = self.schema()
        for value in [dict(items=[]), dict(items=[dict(text="evidence")]),
                      dict(items=[dict(text="evidence", score=.8)], note=None)]:
            self.assert_match(value, schema, True)
        for value in [{}, dict(items=[{}]), dict(items=[], unexpected=True),
                      dict(items=[dict(text="evidence", extra=1)]), dict(items=[dict(text=7)]),
                      dict(items=[dict(text="evidence", score=True)]), dict(items=[], note=4)]:
            with self.subTest(value=value):
                self.assert_match(value, schema, False)

    def test_named_field_rule_takes_precedence_over_typed_extra_values(self):
        schema = record(dict(title=dict(type="string")), ["title"], dict(type="integer"))
        self.assert_match(dict(title="report", count=3), schema, True)
        self.assert_match(dict(title=3), schema, False)
        self.assert_match(dict(title="report", count="3"), schema, False)
        self.assert_match(dict(count=3), schema, False)
        self.assertEqual(value_domain(schema)["additional_values"], dict(kind="primitive", type="integer"))

    def test_nested_record_domain_requires_the_same_exact_transport_guard(self):
        schema = record(dict(payload=self.schema()), ["payload"])
        node = ast.parse("def run(payload):\n    return payload\n").body[0]
        proof = request_domain(schema, {}, node, EXACT_REQUEST_CODEC)
        self.assertEqual(proof["status"], "CHECKED")
        self.assertEqual(proof["properties"]["payload"]["kind"], "record")
        self.assertEqual(request_domain(schema, {}, node)["status"], "UNKNOWN")
        # This checks a supplied schema's mechanics, not inference of a type
        # for an original unannotated parameter.
        untyped = record(dict(payload={}), ["payload"])
        self.assertEqual(request_domain(untyped, {}, node, EXACT_REQUEST_CODEC)["status"], "UNKNOWN")

    def test_open_unknown_native_and_ambiguous_record_fields_remain_unknown(self):
        native = {"x-native-type": dict(path="original.py", symbol="Context", source_version="frozen")}
        invalid = [record(dict(field={})), record(dict(field=native)), record(dict(field=dict(type="string")), additional=True),
            record({}, ["missing"]), record(dict(field=dict(type="string")), ["field", "field"]),
            record(dict(field=dict(type="string")), additional={})]
        for schema in invalid:
            with self.subTest(schema=schema):
                with self.assertRaises(DomainGap):
                    value_domain(schema)

    def test_record_constraints_survive_array_and_union_recursion(self):
        schema = dict(anyOf=[dict(type="null"), dict(type="array", items=self.schema())])
        self.assert_match(None, schema, True)
        self.assert_match([dict(items=[])], schema, True)
        self.assert_match([dict(items=[{}])], schema, False)
        self.assertEqual(value_domain(schema)["kind"], "union")

    def test_missing_required_name_is_not_satisfied_by_permissive_extra_fields(self):
        schema = record(dict(name=dict(type="string")), ["name"], True)
        self.assert_match(dict(other="value"), schema, False)
        self.assert_match(dict(name="ok", other=object()), schema, True)
        with self.assertRaises(DomainGap):
            value_domain(schema)


if __name__ == "__main__":
    unittest.main()
