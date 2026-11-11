"""Finite source proofs only: no original module, iterator or SDK is run."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from seampact.source.call_value_conditions import call_context, contextual, finite_range, finite_split, CallUnknown
from seampact.source.dependency_closure import resolve_resource_calls, _Closure
from seampact.source.inspection import Inspector


class OrdinaryIterableTests(unittest.TestCase):
    def trace(self, body, values=None, prefix=''):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        source = prefix + 'def score(value):\n' + ''.join('    ' + line + '\n' for line in body.splitlines())
        (root / 'original.py').write_text(source, encoding='utf-8')
        inspector = Inspector.freeze(root, root / 'snapshots', 'request', ['original.py'], 'score', 'result')
        entry = dict(id='score', kind='function', path='original.py', symbol='score', boundary_ids=['whole'])
        candidate = dict(entries=[entry], boundaries=[dict(id='whole', mode='W', path='original.py', symbol='score')])
        context, _ = call_context({'value': [] if values is None else values})
        return resolve_resource_calls(contextual(inspector, context), entry, candidate)

    def checked(self, trace):
        self.assertEqual(trace['status'], 'CHECKED', trace)
        self.assertEqual(trace['negative_scope_status'], 'CHECKED', trace)

    def unknown(self, trace):
        self.assertTrue(trace['negative_scope_status'] == 'UNKNOWN' or trace['unresolved_targets'], trace)

    def test_split_and_slice_propagate_all_exact_text_values(self):
        self.checked(self.trace("parts=value.split('\\n')[:-1]\n"
            "result=[isinstance(part,str) or isinstance(part,sdk.Marker) for part in parts]\nreturn result",
            'a\nb\n', 'import sdk\n'))

    def test_native_range_immediate_consumption_and_empty_short_circuit(self):
        self.checked(self.trace('for item in range(len(value)):\n'
            '    assert isinstance(item,int)\nreturn 1', ['a', 'b']))
        trace = self.trace('for item in range(value):\n    sdk.never_called()\nreturn 1', 0, 'import sdk\n')
        self.checked(trace)
        self.assertFalse(any(row['target'] == 'sdk.never_called' for row in trace['calls']))

    def test_zip_is_finite_direct_consumption_and_stops_at_shortest(self):
        self.checked(self.trace('for left,right in zip(value,[1]):\n'
            '    assert isinstance(left,str)\n    assert isinstance(right,int)\nreturn 1', ['a', 1]))
        self.checked(self.trace('result=[isinstance(item,int) for item in range(3)]\n'
            'for item in result:\n    assert isinstance(item,bool)\nreturn result'))

    def test_every_comprehension_element_is_checked(self):
        self.unknown(self.trace('result=[isinstance(item,str) or isinstance(item,sdk.Marker) for item in value]\n'
            'return result', ['a', 1], 'import sdk\n'))
        self.unknown(self.trace('result=[isinstance(left,str) or isinstance(left,sdk.Marker) for left,right in zip(value,[0,1])]\n'
            'return result', ['a', 1], 'import sdk\n'))

    def test_unknown_elements_do_not_gain_a_zip_or_iteration_proof(self):
        for expression in ('[sdk.item()]', 'zip([sdk.item()],[1])'):
            with self.subTest(expression=expression):
                self.unknown(self.trace(f'for item in {expression}:\n    pass\nreturn 1', prefix='import sdk\n'))

    def test_filter_effects_are_retained_without_a_general_filter_rule(self):
        trace = self.trace('result=[item for item in value if sdk.keep(item)]\nreturn result', ['a'], 'import sdk\n')
        self.unknown(trace)
        self.assertTrue(any(row['target'] == 'sdk.keep' for row in trace['calls']))

    def test_alias_pollution_reaches_nested_values_and_branch_copies(self):
        for body in (
            'alias=value[0]\nsdk.change(value)\nfor item in range(len(alias)):\n    pass\nreturn 1',
            'alias=value\nsdk.change(value)\ncopy=alias[:]\nfor item in copy:\n    pass\nreturn 1',
            'alias=value\nsdk.change(value)\nif sdk.flag():\n    assert alias or [1]\nreturn 1',
        ):
            with self.subTest(body=body):
                self.unknown(self.trace(body, [['a']], 'import sdk\n'))

    def test_mutating_a_list_or_zip_source_during_consumption_is_unknown(self):
        for expression in ('value', 'zip(value,[1,2])'):
            with self.subTest(expression=expression):
                self.unknown(self.trace(f'for item in {expression}:\n    value.append(3)\nreturn 1', [1, 2]))

    def test_unmodelled_list_methods_and_bound_method_aliases_invalidate_receivers(self):
        for operation in ('value.clear()', 'value.pop()', 'value.reverse()', 'value.sort()',
                          'method=value.clear\nmethod()'):
            with self.subTest(operation=operation):
                trace = self.trace(operation + '\nif value:\n    return 1\nsdk.danger()\nreturn 1', [1], 'import sdk\n')
                self.unknown(trace)
                self.assertTrue(any(row['target'] == 'sdk.danger' for row in trace['calls']))

    def test_subscript_writes_cannot_leave_old_list_aliases_or_zip_snapshots(self):
        for target in ('value[1]', 'value[:]', 'value[0][0]'):
            with self.subTest(target=target):
                self.unknown(self.trace(f'alias=value\n{target}=1\n'
                    'for item in alias:\n    pass\nreturn 1', [[1], 2]))
        self.unknown(self.trace('result=[]\nfor item in value:\n    value[1]=1\n    result.append(item)\n'
            'if result[1]==2:\n    return 1\nsdk.danger()\nreturn 1', [1, 2], 'import sdk\n'))

    def test_exact_list_inplace_addition_preserves_aliases(self):
        trace = self.trace('alias=value\nvalue += [2]\nif len(alias)==1:\n    return 1\nsdk.danger()\nreturn 1',
            [1], 'import sdk\n')
        self.checked(trace)
        self.assertTrue(any(row['target'] == 'sdk.danger' for row in trace['calls']))

    def test_over_limit_and_exceptional_builtin_results_stay_unknown(self):
        for expression, value in (
            ('range(value)', 65), ('range(0,2,value)', 0), ('range(value)', 10 ** 100),
            ('value.split(\'\')', 'text'), ('value.split()', 'a ' * 65), ('value[::0]', [1]),
        ):
            with self.subTest(expression=expression):
                self.unknown(self.trace(f'for item in {expression}:\n    pass\nreturn 1', value))

    def test_finite_bound_is_a_proof_limit_and_native_split_is_bounded(self):
        self.checked(self.trace('for item in range(value):\n    assert isinstance(item,int)\nreturn 1', 64))
        self.checked(self.trace('for item in value.split():\n    assert isinstance(item,str)\nreturn 1', 'a ' * 64))
        for value, args in (('a', [None, 10 ** 100]), ('x' * 65537, [])):
            with self.assertRaises(CallUnknown): finite_split(value, args, {})

    def test_source_and_runtime_builtin_rebinding_are_not_name_permissions(self):
        self.unknown(self.trace('for item in range(2):\n    pass\nreturn 1',
            prefix='import builtins\nbuiltins.range=lambda n: []\n'))
        self.unknown(self.trace('range=sdk.range\nfor item in range(2):\n    pass\nreturn 1', prefix='import sdk\n'))
        import builtins
        with patch.object(builtins, 'zip', lambda *args: ()):
            self.unknown(self.trace('for item in zip(value,value):\n    pass\nreturn 1', [1]))

    def test_lazy_aliases_wrappers_generators_and_expansions_are_not_ordinary(self):
        for body in (
            'items=range(2)\nfor item in items:\n    pass\nreturn 1',
            'return zip(value,value)',
            'for item in sdk.wrap(zip(value,value)):\n    pass\nreturn 1',
            'items=(item for item in value)\nfor item in items:\n    pass\nreturn 1',
            'for item in zip(*[value,value]):\n    pass\nreturn 1',
            "for item in value.split(**{'sep':' '}):\n    pass\nreturn 1",
        ):
            with self.subTest(body=body):
                self.unknown(self.trace(body, ['a'], 'import sdk\n'))

    def test_unknown_object_and_metaclass_hooks_are_never_used_by_finite_helpers(self):
        touched = []
        class Meta(type):
            def __eq__(cls, other): touched.append('meta_eq'); return False
            def __hash__(cls): touched.append('meta_hash'); return 0
        class Unknown(metaclass=Meta):
            @property
            def __class__(self): touched.append('class'); return list
            def __index__(self): touched.append('index'); return 1
            def __iter__(self): touched.append('iter'); return iter([])
            def __len__(self): touched.append('len'); return 1
            def __repr__(self): touched.append('repr'); return 'unknown'
        value = Unknown()
        checker = _Closure.__new__(_Closure)
        checker.call_value_taints = {}
        self.assertFalse(checker.call_plain(value))
        self.assertIsNone(checker.iteration_items(object(), value))
        self.assertIsNone(checker.iteration_stamp(value))
        with self.assertRaises(CallUnknown): finite_range([value])
        with self.assertRaises(CallUnknown): finite_range(value)
        with self.assertRaises(CallUnknown): finite_split('a', [value], {})
        with self.assertRaises(CallUnknown): finite_split('a', value, {})
        self.assertEqual(touched, [])


if __name__ == '__main__':
    unittest.main()
