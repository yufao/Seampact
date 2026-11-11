"""Bounded sets never become guessed values or actual branch outcomes."""
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from seampact.source.sdk_bounded_values import (
    BoundedString, BoundedInteger, BoundedList, BoundedBoolean, BoundedUnknown,
    abstract, append_list, compare, extend_list, integer, length, merge,
    possible_truth, slice_list, string_operation, truth, value_kind,
)


class BoundedOrdinaryValueTests(unittest.TestCase):
    def test_no_abstract_value_is_a_runtime_boolean_or_iterator(self):
        for value in (BoundedString(10), BoundedInteger(0, 3),
                      BoundedList(BoundedString(4), 3), BoundedBoolean()):
            with self.subTest(value=value):
                with self.assertRaises(BoundedUnknown): bool(value)
                with self.assertRaises(TypeError): iter(value)

    def test_unknown_comparison_requires_both_original_branches(self):
        count = integer('add', BoundedInteger(0, 5), BoundedInteger(0, 4))
        self.assertEqual(count, BoundedInteger(0, 9))
        self.assertEqual(possible_truth(compare('gt', count, 4)), (False, True))
        self.assertIs(compare('gt', count, 12), False)
        self.assertIs(compare('le', count, 12), True)
        self.assertEqual(integer('subtract', BoundedInteger(-3, 4), BoundedInteger(1, 6)), BoundedInteger(-9, 3))

    def test_split_and_join_prove_domains_without_guessing_sentences(self):
        words = string_operation('split', BoundedString(8))
        self.assertEqual(words, BoundedList(BoundedString(8), 8))
        self.assertEqual(length(words), BoundedInteger(0, 8))
        chunks = append_list([], BoundedString(8))
        chunks = append_list(chunks, BoundedString(7))
        self.assertEqual(string_operation('join', ' ', [chunks]), BoundedString(17, 1))
        self.assertEqual(string_operation('strip', BoundedString(20)), BoundedString(20))
        self.assertEqual(string_operation('replace', BoundedString(5), ['a', 'xxx']), BoundedString(15))

    def test_branch_join_hulls_preserve_empty_and_nonempty_possibilities(self):
        combined = merge([], [BoundedString(8)])
        self.assertEqual(combined, BoundedList(BoundedString(8), 1))
        self.assertIsNone(truth(combined))
        self.assertIs(truth(BoundedList(BoundedString(2), 2, 1)), True)
        self.assertEqual(merge('known', 'other'), BoundedString(5, 5))
        self.assertIsInstance(compare('eq', BoundedString(5), 'x'), BoundedBoolean)
        self.assertIs(compare('eq', BoundedString(0), ''), True)
        self.assertIs(compare('eq', BoundedString(8, 5), 'x'), False)

    def test_length_only_slice_enumeration_cannot_invent_an_element(self):
        source = BoundedList(BoundedString(7), 6, 2)
        self.assertEqual(slice_list(source, None, -1), BoundedList(BoundedString(7), 5, 1))
        self.assertEqual(slice_list(source, None, None, -1), source)
        self.assertEqual(extend_list([], source), source)
        self.assertEqual(value_kind(abstract([1, 3])), 'list')

    def test_limits_and_non_normal_results_remain_unknown(self):
        for action in (
            lambda: BoundedString(65537), lambda: BoundedInteger(0, 1 << 4096),
            lambda: BoundedList(BoundedString(1), 65538),
            lambda: abstract([1]*65),
            lambda: string_operation('split', BoundedString(5), ['']),
            lambda: string_operation('replace', BoundedString(65536), ['', 'x']),
            lambda: string_operation('join', ' ', [[BoundedString(65536), 'x']]),
            lambda: merge(BoundedString(1), BoundedInteger(0, 1)),
            lambda: slice_list(BoundedList(1, 2), step=0),
            lambda: append_list(BoundedList(1, 65537), 1),
        ):
            with self.subTest(action=action):
                with self.assertRaises(BoundedUnknown): action()

    def test_symbolic_cardinality_does_not_expand_exact_input_or_iteration_domain(self):
        self.assertEqual(length(string_operation('split', BoundedString(1000))), BoundedInteger(0,1000))
        self.assertEqual(BoundedList(BoundedString(1),65).max_length,65)
        self.assertEqual(slice_list(BoundedList(1,1000),None,-1),BoundedList(1,999))
        with self.assertRaises(BoundedUnknown): abstract([1]*65)

    def test_native_type_gates_do_not_invoke_user_hooks(self):
        calls = []
        class Evil(int):
            def __le__(self, other): calls.append('le'); return True
            def __bool__(self): calls.append('bool'); return True
            def bit_length(self): calls.append('bits'); return 0
        for action in (lambda: BoundedInteger(Evil(0), 1), lambda: BoundedString(Evil(4)),
                       lambda: integer('add', Evil(0), 1), lambda: compare('eq', Evil(0), 1)):
            with self.assertRaises(BoundedUnknown): action()
        self.assertEqual(calls, [])


if __name__ == '__main__':
    unittest.main()
