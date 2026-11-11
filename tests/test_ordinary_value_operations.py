"""Bounded primitive propagation; no analyzed module, SDK or object hooks run."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from seampact.source import call_value_conditions as conditions
from seampact.source.call_value_conditions import (
    CallUnknown, call_context, contextual, finite_integer, finite_string,
)
from seampact.source.dependency_closure import resolve_resource_calls
from seampact.source.inspection import Inspector


class OrdinaryValueOperationTests(unittest.TestCase):
    def trace(self, body, value=0, prefix=''):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        source = prefix + 'def score(value):\n' + ''.join('    ' + line + '\n' for line in body.splitlines())
        (root / 'original.py').write_text(source, encoding='utf-8')
        inspector = Inspector.freeze(root, root / 'snapshots', 'request', ['original.py'], 'score', 'result')
        entry = dict(id='score', kind='function', path='original.py', symbol='score', boundary_ids=['whole'])
        candidate = dict(entries=[entry], boundaries=[dict(id='whole', mode='W', path='original.py', symbol='score')])
        context, _ = call_context({'value': value})
        return resolve_resource_calls(contextual(inspector, context), entry, candidate)

    def checked(self, trace):
        self.assertEqual(trace['status'], 'CHECKED', trace)
        self.assertEqual(trace['negative_scope_status'], 'CHECKED', trace)
        self.assertEqual(trace['unresolved_targets'], [], trace)

    def unknown(self, trace):
        self.assertTrue(trace['negative_scope_status'] == 'UNKNOWN' or trace['unresolved_targets'], trace)

    def test_exact_integer_sign_addition_and_subtraction_propagate(self):
        trace = self.trace('result=+value - -2 + -1\n'
            'if result == 4:\n    return 1\nsdk.danger()\nreturn 1', 3, 'import sdk\n')
        self.checked(trace)
        self.assertFalse(any(row['target'] == 'sdk.danger' for row in trace['calls']))
        for operation, args, expected in (
            ('positive', [-3], -3), ('negative', [-3], 3), ('add', [-3, 2], -1), ('subtract', [-3, -2], -1),
        ):
            with self.subTest(operation=operation):
                self.assertEqual(finite_integer(operation, args), expected)

    def test_name_integer_augmented_assignment_preserves_old_immutable_alias(self):
        trace = self.trace('alias=value\nvalue += 2\nvalue -= 1\n'
            'if alias == 3:\n    if value == 4:\n        return 1\nsdk.danger()\nreturn 1', 3, 'import sdk\n')
        self.checked(trace)
        self.assertFalse(any(row['target'] == 'sdk.danger' for row in trace['calls']))
        self.unknown(self.trace('value += sdk.result()\nreturn value', 3, 'import sdk\n'))

    def test_integer_proof_cutoff_checks_inputs_and_results(self):
        largest = (1 << 4096) - 1
        self.assertEqual(finite_integer('positive', [largest]), largest)
        self.assertEqual(finite_integer('subtract', [-largest, 0]), -largest)
        for operation, args in (('positive', [1 << 4096]), ('add', [largest, 1]),
                                ('subtract', [-largest, 1])):
            with self.subTest(operation=operation):
                with self.assertRaises(CallUnknown): finite_integer(operation, args)
        self.unknown(self.trace('return -value', 1 << 4096))
        self.unknown(self.trace('return value + 1', largest))

    def test_integer_rule_does_not_convert_bool_float_or_other_operations(self):
        for value in (True, 1.5, '1', [1]):
            with self.subTest(value=value):
                self.unknown(self.trace('return -value', value))
                with self.assertRaises(CallUnknown): finite_integer('add', [value, 1])
        for operation, args in (('positive', []), ('negative', [1, 2]), ('add', [1]), ('multiply', [1, 2])):
            with self.subTest(operation=operation):
                with self.assertRaises(CallUnknown): finite_integer(operation, args)
        self.unknown(self.trace('assert isinstance(value * 2,int)\nreturn 1', 3))

    def test_source_numeric_protocols_are_unknown_without_running_them(self):
        prefix = ('import sdk\nclass Number:\n'
                  '    def __neg__(self):\n        sdk.danger()\n        return 1\n'
                  '    def __add__(self, other):\n        sdk.danger()\n        return 1\n')
        for expression in ('-Number()', 'Number() + value'):
            with self.subTest(expression=expression):
                self.unknown(self.trace('return ' + expression, 3, prefix))

    def test_join_replace_strip_chain_propagates_exact_unicode_text(self):
        trace = self.trace("text=' / '.join(value).replace('/', '—', -1).strip()\n"
            "if text == '甲 — 乙':\n    return 1\nsdk.danger()\nreturn 1", [' 甲', '乙 '], 'import sdk\n')
        self.checked(trace)
        self.assertFalse(any(row['target'] == 'sdk.danger' for row in trace['calls']))
        self.assertEqual(finite_string('join', ',', [[]], {}), '')
        self.assertEqual(finite_string('replace', 'aba', ['a', 'x', 1], {}), 'xba')
        self.assertEqual(finite_string('replace', 'ab', ['', '.', 2], {}), '.a.b')
        self.assertEqual(finite_string('replace', 'ab', ['', '.', 0], {}), 'ab')
        self.assertEqual(finite_string('strip', '\u2003 text\t', [], {}), 'text')
        self.assertEqual(finite_string('strip', 'abba', ['ab'], {}), '')
        self.assertEqual(finite_string('strip', ' text ', [None], {}), 'text')

    def test_text_rules_retain_explicit_none_versus_unknown(self):
        self.checked(self.trace('text=value.strip(None)\nassert isinstance(text,str)\nreturn text', ' x '))
        self.unknown(self.trace('return value.strip(sdk.result())', ' x ', 'import sdk\n'))
        self.unknown(self.trace("return value.replace('x','y',None)", 'x'))

    def test_join_checks_every_element_and_ordinary_alias_pollution(self):
        for pieces in (['a', 1], ['a', None], ['a', []]):
            with self.subTest(pieces=pieces):
                self.unknown(self.trace("return ','.join(value)", pieces))
        trace = self.trace("alias=value\nsdk.change(alias)\nresult=','.join(value)\n"
            "if result == 'a':\n    return 1\nsdk.danger()\nreturn 1", ['a'], 'import sdk\n')
        self.unknown(trace)
        self.assertTrue(any(row['target'] == 'sdk.danger' for row in trace['calls']))

    def test_string_binding_forms_are_not_guessed(self):
        expressions = (
            "value.join()", "value.join(['a'],['b'])", "value.join(iterable=['a'])",
            "value.replace('a')", "value.replace('a','b',1,2)", "value.replace('a','b',count=1)",
            "value.replace(old='a',new='b')", "value.strip('a','b')", "value.strip(chars='a')",
            "value.replace(*['a','b'])", "value.strip(**{'chars':'a'})",
        )
        for expression in expressions:
            with self.subTest(expression=expression):
                self.unknown(self.trace('return ' + expression, ' a '))
        for operation, args in (('replace', ['a', 'b', True]), ('replace', ['a', 'b', 1 << 100]),
                                ('strip', [1]), ('join', [('a',)])):
            with self.subTest(operation=operation):
                with self.assertRaises(CallUnknown): finite_string(operation, 'a', args, {})

    def test_string_limits_bound_allocation_before_calling_native_operation(self):
        self.assertEqual(len(finite_string('join', '', [['x' * 65536]], {})), 65536)
        self.assertEqual(len(finite_string('replace', 'a' * 32768, ['a', 'bb'], {})), 65536)
        self.assertEqual(finite_string('join', '', [[''] * 64], {}), '')
        for operation, receiver, args in (
            ('join', '', [[''] * 65]), ('join', 'x' * 65536, [['a', 'b']]),
            ('replace', 'a' * 32769, ['a', 'bb']), ('replace', '', ['', 'x' * 65537]),
            ('strip', 'x' * 65537, []), ('strip', 'x', ['x' * 65537]),
        ):
            with self.subTest(operation=operation):
                with self.assertRaises(CallUnknown): finite_string(operation, receiver, args, {})
        self.unknown(self.trace("return value.replace('a','bb')", 'a' * 32769))

    def test_original_bindings_are_checked_before_new_operations(self):
        for body, value in (('return value + 1', 1), ("return value.strip()", ' x ')):
            with self.subTest(body=body):
                self.unknown(self.trace(body, value, 'import builtins\nbuiltins.str=lambda x: x\n'))
        touched = []
        def unexpected(*args):
            touched.append('replacement'); return 'wrong'
        with patch.dict(conditions._STR_OPERATIONS, {'join': unexpected}):
            self.unknown(self.trace("return ','.join(value)", ['a']))
        with patch.dict(conditions._INT_OPERATIONS, {'negative': unexpected}):
            self.unknown(self.trace('return -value', 1))
        self.assertEqual(touched, [])

    def test_filters_generators_and_opaque_wrappers_remain_unknown(self):
        for body in (
            "return ''.join(item for item in value)",
            "return ''.join([item for item in value if item != ''])",
            "for item in sdk.wrap(zip(value,value)):\n    pass\nreturn 1",
        ):
            with self.subTest(body=body):
                self.unknown(self.trace(body, ['a'], 'import sdk\n'))

    def test_unknown_objects_and_metaclass_hooks_are_never_run(self):
        touched = []
        class Meta(type):
            def __eq__(cls, other): touched.append('meta_eq'); return False
            def __hash__(cls): touched.append('meta_hash'); return 0
            def __getattribute__(cls, name): touched.append('meta_attr'); return super().__getattribute__(name)
        class Unknown(metaclass=Meta):
            @property
            def __class__(self): touched.append('class'); return int
            def __pos__(self): touched.append('positive'); return 1
            def __neg__(self): touched.append('negative'); return 1
            def __add__(self, other): touched.append('add'); return 1
            def __sub__(self, other): touched.append('subtract'); return 1
            def __index__(self): touched.append('index'); return 1
            def __iter__(self): touched.append('iter'); return iter([])
            def __len__(self): touched.append('len'); return 0
            def __str__(self): touched.append('str'); return 'unknown'
            def __repr__(self): touched.append('repr'); return 'unknown'
        value = Unknown()
        for operation in ('positive', 'negative', 'add', 'subtract'):
            with self.assertRaises(CallUnknown): finite_integer(operation, [value])
            with self.assertRaises(CallUnknown): finite_integer(operation, [1, value])
        with self.assertRaises(CallUnknown): finite_integer('positive', value)
        for operation in ('join', 'replace', 'strip'):
            with self.assertRaises(CallUnknown): finite_string(operation, value, [], {})
            with self.assertRaises(CallUnknown): finite_string(operation, '', value, {})
            with self.assertRaises(CallUnknown): finite_string(operation, '', [], value)
        with self.assertRaises(CallUnknown): finite_string('join', '', [[value]], {})
        with self.assertRaises(CallUnknown): finite_string('replace', 'a', ['a', value], {})
        with self.assertRaises(CallUnknown): finite_string('replace', 'a', ['a', 'b', value], {})
        with self.assertRaises(CallUnknown): finite_string('strip', '', [value], {})
        with self.assertRaises(CallUnknown): call_context({'value': value})
        self.assertEqual(touched, [])


if __name__ == '__main__':
    unittest.main()
