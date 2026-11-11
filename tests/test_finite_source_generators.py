"""Original generators stay lazy; only a finite pure AST domain is checked."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from seampact.source.call_value_conditions import call_context, contextual
from seampact.source.dependency_closure import resolve_resource_calls
from seampact.source.inspection import Inspector


class FiniteSourceGeneratorTests(unittest.TestCase):
    def trace(self, source, value, *, symbol='score', kind='function'):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root / 'original.py').write_text(source, encoding='utf-8')
        inspector = Inspector.freeze(root, root / 'snapshots', 'request', ['original.py'], 'score', 'result')
        entry = dict(id='score', kind=kind, path='original.py', symbol=symbol)
        if kind == 'method':
            entry['constructor'] = dict(symbol=symbol.split('.')[0], arguments={})
        candidate = dict(entries=[entry], boundaries=[])
        context, _ = call_context(dict(value=value))
        return resolve_resource_calls(contextual(inspector, context), entry, candidate)

    def checked(self, trace):
        self.assertEqual(trace['status'], 'CHECKED', trace)
        self.assertEqual(trace['negative_scope_status'], 'CHECKED', trace)

    def unknown(self, trace):
        self.assertTrue(trace['negative_scope_status'] == 'UNKNOWN' or trace['unresolved_targets'], trace)

    def test_exact_chunk_generator_yields_fresh_slices_in_original_order(self):
        self.checked(self.trace('def chunks(value, n):\n'
            '    for index in range(0,len(value),n):\n        yield value[index:index+n]\n'
            'def score(value):\n    result=[]\n    for batch in chunks(value,2):\n'
            '        result.extend(batch)\n    assert isinstance(result,list)\n    return result\n', ['a','b','c']))

    def test_original_method_generator_can_ignore_its_owner_without_transferring_it(self):
        self.checked(self.trace('class Source:\n'
            '    def chunks(self,value,n):\n        for i in range(0,len(value),n):\n            yield value[i:i+n]\n'
            '    def score(self,value):\n        for batch in self.chunks(value,2):\n'
            '            assert isinstance(batch,list)\n        return 1\n', ['a','b'], symbol='Source.score', kind='method'))

    def test_local_string_accumulator_and_exact_integer_branch(self):
        self.checked(self.trace('def pieces(value,n):\n    current=[]\n    count=0\n'
            '    for text in value:\n        size=len(text.split())\n        if count+size > n:\n'
            "            yield ' '.join(current)\n            current=[text]\n            count=size\n"
            '        else:\n            current.append(text)\n            count += size\n'
            "    if current:\n        yield ' '.join(current)\n"
            'def score(value):\n    result=[text.strip() for text in pieces(value,2)]\n'
            '    for text in result:\n        assert isinstance(text,str)\n    return result\n', ['a','b c']))

    def test_return_stops_all_nested_generator_control(self):
        trace = self.trace('import sdk\ndef pieces(value):\n    if value == 0:\n        return\n    yield 1\n'
            'def score(value):\n    for item in pieces(value):\n        sdk.never_called()\n    return 1\n', 0)
        self.checked(trace)
        self.assertFalse(any(row['target'] == 'sdk.never_called' for row in trace['calls']))

    def test_creation_does_not_execute_deferred_body(self):
        trace = self.trace('import sdk\ndef pieces(value):\n    sdk.deferred()\n    yield value\n'
            'def score(value):\n    return sdk.wrap(pieces(value))\n', 'a')
        self.unknown(trace)
        self.assertFalse(any(row['target'] == 'sdk.deferred' for row in trace['calls']))

    def test_unknown_calls_effects_and_control_reject_consumption(self):
        for body in ('sdk.effect()\n    yield value', 'while value:\n        yield value',
                     'yield from value', 'yield [item for item in value if item]',
                     'try:\n        yield value\n    finally:\n        sdk.effect()'):
            with self.subTest(body=body):
                self.unknown(self.trace('import sdk\ndef pieces(value):\n    '+body+'\n'
                    'def score(value):\n    result=[item for item in pieces(value)]\n    return result\n', ['a']))

    def test_original_binding_must_be_complete_and_unambiguous(self):
        for expression in ('pieces()', 'pieces(value,value=value)', 'pieces(value,1,2)', 'pieces(value,other=1)'):
            with self.subTest(expression=expression):
                self.unknown(self.trace('def pieces(value,n=2):\n    yield value\n'
                    'def score(value):\n    for item in '+expression+':\n        pass\n    return 1\n', 'a'))
        self.checked(self.trace('def pieces(value,n=2):\n    yield n\n'
            'def score(value):\n    for item in pieces(value):\n        assert isinstance(item,int)\n    return 1\n', 'a'))

    def test_suspended_input_and_closure_mutation_are_unknown(self):
        self.unknown(self.trace('def pieces(value):\n    for item in value:\n        yield item\n'
            "def score(value):\n    for item in pieces(value):\n        value.append('changed')\n    return 1\n", ['a']))
        self.unknown(self.trace('def score(value):\n    captured=value\n    def pieces():\n'
            '        yield captured\n        yield captured\n    for item in pieces():\n'
            "        captured='changed'\n    return 1\n", 'a'))

    def test_retained_mutable_yield_storage_is_not_an_independent_value(self):
        for body in ('yield value', 'current=[]\n    yield current\n    current.append(1)'):
            with self.subTest(body=body):
                self.unknown(self.trace('def pieces(value):\n    '+body+'\n'
                    'def score(value):\n    for item in pieces(value):\n        pass\n    return 1\n', ['a']))

    def test_empty_exceptional_and_over_limit_results_are_distinct(self):
        self.checked(self.trace('def pieces(value):\n    for i in range(value):\n        yield i\n'
            'def score(value):\n    result=[item for item in pieces(value)]\n    return result\n', 0))
        for source, value in (
            ('def pieces(value):\n    for i in range(value):\n        yield i\n', 65),
            ("def pieces(value):\n    yield ''.join(value)\n", [1]),
            ('def pieces(value):\n    for i in range(64):\n        yield i\n        yield i\n', 0),
        ):
            with self.subTest(value=value):
                self.unknown(self.trace(source+'def score(value):\n    result=[item for item in pieces(value)]\n    return result\n', value))

    def test_stored_iterators_break_and_opaque_consumers_remain_unknown(self):
        for body in ('iterator=pieces(value)\n    for item in iterator:\n        pass',
                     'for item in pieces(value):\n        break', 'return pieces(value)',
                     'for item in sdk.wrap(pieces(value)):\n        pass'):
            with self.subTest(body=body):
                self.unknown(self.trace('import sdk\ndef pieces(value):\n    yield value\n'
                    'def score(value):\n    '+body+'\n    return 1\n', 'a'))

    def test_tuple_values_never_gain_a_list_type_fact(self):
        for generator, setup in (
            ('def pieces(value):\n    yield (1,2)\n', ''),
            ('def pieces(value):\n    yield value[:]\n', 'value=(1,2)\n    '),
            ('def pieces(value):\n    yield value\n', 'value=(1,2)\n    '),
        ):
            with self.subTest(setup=setup):
                trace = self.trace('import sdk\n'+generator+'def score(value):\n    '+setup+
                    'result=[isinstance(item,list) for item in pieces(value)]\n'
                    '    if result[0]:\n        return 1\n    sdk.danger()\n    return 1\n', 0)
                self.unknown(trace)
                self.assertTrue(any(row['target'] == 'sdk.danger' for row in trace['calls']))

    def test_loop_targets_cannot_invoke_original_attribute_setters(self):
        self.unknown(self.trace('import sdk\nclass Source:\n'
            '    @property\n    def x(self):\n        return 0\n'
            '    @x.setter\n    def x(self,value):\n        sdk.danger()\n'
            '    def pieces(self,values):\n        for self.x in values:\n            yield 1\n'
            '    def score(self,value):\n        for item in self.pieces(value):\n            pass\n        return 1\n',
            [7], symbol='Source.score', kind='method'))

    def test_list_augmented_assignment_does_not_lose_original_alias_semantics(self):
        trace = self.trace('import sdk\ndef pieces(value):\n    xs=[]\n    alias=xs\n'
            '    xs += [1]\n    yield len(alias)\ndef score(value):\n'
            '    result=[item for item in pieces(value)]\n    if result[0] == 0:\n        return 1\n'
            '    sdk.danger()\n    return 1\n', 0)
        self.unknown(trace)
        self.assertTrue(any(row['target'] == 'sdk.danger' for row in trace['calls']))

    def test_mutated_original_function_defaults_and_code_are_not_call_facts(self):
        for assignment in ('pieces.__defaults__=(2,)', 'pieces.__code__=sdk.code'):
            with self.subTest(assignment=assignment):
                trace = self.trace('import sdk\ndef pieces(n=1):\n    yield n\ndef score(value):\n    '+assignment+'\n'
                    '    result=[item for item in pieces()]\n    if result[0] == 1:\n        return 1\n'
                    '    sdk.danger()\n    return 1\n', 0)
                self.unknown(trace)
        self.unknown(self.trace('class Source:\n'
            '    def pieces(self,n=1):\n        yield n\n'
            '    def score(self,value):\n        self.pieces.__func__.__defaults__=(2,)\n'
            '        result=[item for item in self.pieces()]\n        return result\n',
            0, symbol='Source.score', kind='method'))

    def test_yielded_iterator_proof_does_not_escape_as_an_ordinary_value(self):
        self.unknown(self.trace('def pieces(value):\n    yield range(2)\ndef score(value):\n'
            '    result=[item for item in pieces(value)]\n    return result\n', 0))

    def test_async_generator_never_becomes_a_synchronous_iterable(self):
        self.unknown(self.trace('async def pieces(value):\n    yield value\ndef score(value):\n'
            '    result=[item for item in pieces(value)]\n    return result\n', 'a'))

    def test_failed_generator_is_not_a_proved_empty_list(self):
        for operation in ('result=[item for item in pieces(value)]', 'result=[]\n    result.extend(pieces(value))'):
            with self.subTest(operation=operation):
                trace = self.trace('import sdk\ndef pieces(value):\n    sdk.unknown_effect()\n    yield value\n'
                    'def score(value):\n    '+operation+'\n    if result:\n        sdk.possible()\n    return 1\n', 'a')
                self.unknown(trace)
                self.assertTrue(any(row['target'] == 'sdk.possible' for row in trace['calls']))

    def test_unproved_definition_time_annotations_are_not_generator_permission(self):
        self.unknown(self.trace('import sdk\ndef pieces(value: sdk.annotation()):\n    yield value\ndef score(value):\n'
            '    result=[item for item in pieces(value)]\n    return result\n', 'a'))

    def test_unknown_unused_arguments_and_mutable_omitted_defaults_stay_unknown(self):
        for helper, invocation in (
            ('def pieces(value):\n    yield 1\n', 'pieces(sdk.value())'),
            ('def pieces(value=[]):\n    yield len(value)\n', 'pieces()'),
        ):
            with self.subTest(invocation=invocation):
                self.unknown(self.trace('import sdk\n'+helper+'def score(value):\n'
                    '    result=[item for item in '+invocation+']\n    return result\n', 0))

    def test_target_requirements_expose_real_definition_scope_without_permission(self):
        trace = self.trace('def pieces(value,n=2):\n    yield n\ndef score(value):\n'
            '    result=[item for item in pieces(value)]\n    return result\n', 'a')
        facts = [row['target_requirements'] for row in trace['external_protocols'] if row['rule'] == 'source-finite-generator/1']
        self.assertEqual(len(facts), 1)
        self.assertEqual((facts[0]['target_symbol'],facts[0]['binding_scope']), ('pieces','module'))
        self.assertEqual(facts[0]['omitted_defaults'], [dict(name='n',value=2)])
        self.assertFalse(facts[0]['execution_permission'])


if __name__ == '__main__':
    unittest.main()
