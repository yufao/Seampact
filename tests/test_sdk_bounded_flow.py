"""All feasible bounded controls are checked without running source or SDKs."""
import ast
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from seampact.source.call_value_conditions import Context, contextual
from seampact.source.dependency_closure import _Closure, _Function
from seampact.source.inspection import Inspector
from seampact.source.sdk_bounded_values import BoundedString, BoundedInteger, BoundedList, BoundedTuple, BoundedUnknown
from seampact.source.sdk_bounded_flow import BoundedIteration, try_flow, evaluate_expression


class BoundedFlowTests(unittest.TestCase):
    def checker(self, source):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root / 'original.py').write_text(source, encoding='utf-8')
        inspector = Inspector.freeze(root, root / 'snapshots', 'request', ['original.py'], 'chunks', 'result')
        entry = dict(id='chunks', kind='function', path='original.py', symbol='chunks')
        checker = _Closure(contextual(inspector, Context(values={})), dict(entries=[entry], boundaries=[]))
        checker.import_roots = {}
        env = checker.module('original.py')
        return checker, env

    def flow(self, source, **values):
        checker, env = self.checker(source)
        local = dict(env, **values)
        result = try_flow(checker, env['chunks'], local, ast.parse('chunks(value)').body[0].value)
        return checker, result

    def test_all_unknown_chunk_boundaries_prove_only_a_yield_domain(self):
        _, result = self.flow('def chunks(value,n):\n'
            '    current=[]\n    count=0\n    for sentence in value:\n'
            '        size=len(sentence.split())\n        if count+size>n:\n'
            "            yield ' '.join(current)\n            current=[sentence]\n            count=size\n"
            '        else:\n            current.append(sentence)\n            count += size\n'
            "    if current:\n        yield ' '.join(current)\n",
            value=BoundedList(BoundedString(8), 2), n=3)
        self.assertTrue(result.generator)
        self.assertIsInstance(result.value, BoundedIteration)
        self.assertIsInstance(result.value.domain.element, BoundedString)
        self.assertLessEqual(result.value.domain.max_length, 3)
        self.assertEqual(result.requirements[0]['coverage'], 'ALL_FEASIBLE_ORIGINAL_BRANCHES_AND_FINITE_LENGTHS')
        json.dumps(result.requirements)

    def test_unproved_effect_in_either_branch_rejects(self):
        for condition in ('value>0', 'value<=0'):
            with self.subTest(condition=condition):
                with self.assertRaises(BoundedUnknown):
                    self.flow('import sdk\ndef chunks(value):\n    if '+condition+':\n'
                              '        sdk.effect()\n    yield 1\n', value=BoundedInteger(-1, 1))

    def test_borrowed_list_mutation_and_unknown_control_reject(self):
        for body in ('value.append(1)\n    yield 1', 'while value:\n        yield 1',
                     'for item in value:\n        break\n    yield 1'):
            with self.subTest(body=body):
                with self.assertRaises(BoundedUnknown):
                    self.flow('def chunks(value):\n    '+body+'\n', value=BoundedList(1, 2))

    def test_pure_expression_bounds_filter_and_repetition(self):
        checker, env = self.checker('def chunks(value):\n    yield value\n')
        env = dict(env, value=BoundedList(BoundedString(8), 3), count=BoundedInteger(0, 3))
        expression = ast.parse("[text.strip() for text in value if text!='']", mode='eval').body
        result = evaluate_expression(checker, expression, env, 'original.py', 'chunks')
        self.assertEqual(result, BoundedList(BoundedString(8), 3))
        repeated = evaluate_expression(checker, ast.parse("['claim']*count", mode='eval').body, env, 'original.py', 'chunks')
        self.assertEqual(repeated, BoundedList(BoundedString(5,5), 3))
        self.assertEqual(env['value'], BoundedList(BoundedString(8), 3))

    def test_source_input_and_list_limits_are_not_relaxed(self):
        with self.assertRaises(BoundedUnknown):
            self.flow('def chunks(value):\n    for sentence in value:\n'
                      '        yield sentence.split()\n', value=BoundedList(BoundedString(65), 1))
        with self.assertRaises(BoundedUnknown):
            self.flow('def chunks(value):\n    local=value\n    local.append(1)\n    yield 1\n',
                      value=BoundedList(1, 2))

    def test_mutable_yields_and_stored_iterator_aliases_are_rejected(self):
        with self.assertRaises(BoundedUnknown):
            self.flow('def chunks(value):\n    yield value\n', value=BoundedList(1, 2))
        checker, env = self.checker('def chunks(value):\n    yield value\n')
        iterator = BoundedIteration(BoundedList(BoundedString(2), 2), 123)
        with self.assertRaises(BoundedUnknown):
            evaluate_expression(checker, ast.parse('[item for item in value]', mode='eval').body,
                                dict(env,value=iterator), 'original.py', 'chunks')

    def test_large_symbolic_length_can_be_read_but_not_explicitly_iterated(self):
        checker, env = self.checker('def chunks(value):\n    for item in value:\n        yield item\n')
        domain = BoundedList(BoundedString(1000), 1000)
        with self.assertRaises(BoundedUnknown):
            try_flow(checker,env['chunks'],dict(env,value=domain),ast.parse('chunks(value)').body[0].value)
        with self.assertRaises(BoundedUnknown):
            evaluate_expression(checker,ast.parse('[item.strip() for item in value]',mode='eval').body,
                                dict(env,value=domain),'original.py','chunks')
        result=evaluate_expression(checker,ast.parse('len(value)',mode='eval').body,
                                   dict(env,value=domain),'original.py','chunks')
        self.assertEqual(result,BoundedInteger(0,1000))

    def test_source_sdk_helper_propagates_conditional_domain_not_sentences(self):
        checker, env = self.checker('from nltk.tokenize import sent_tokenize\n'
            'def chunks(value):\n    blocks=value.split("\\n")\n'
            '    tokenized=[sent_tokenize(block) for block in blocks]\n    return tokenized\n')
        result = try_flow(checker, env['chunks'], dict(env,value='a.\nb.'), ast.parse('chunks(value)').body[0].value)
        self.assertFalse(result.generator)
        self.assertIsInstance(result.value, list)
        self.assertEqual(len(result.value), 2)
        self.assertTrue(all(type(item) is BoundedList for item in result.value))
        self.assertTrue(any(item['schema']=='source-finite-sdk-result/1' for item in result.requirements))
        self.assertTrue(checker.negative_scope_gaps)

    def test_raw_newline_helper_keeps_all_unknown_sentence_results(self):
        checker, env = self.checker('from nltk.tokenize import sent_tokenize\n'
            'def chunks(value):\n    blocks=value.split("\\n")\n'
            '    tokenized_blocks=[sent_tokenize(block) for block in blocks]\n'
            '    tokenized_text=[]\n    for block in tokenized_blocks:\n'
            '        tokenized_text.extend(block)\n        tokenized_text.append("\\n")\n'
            '    return tokenized_text[:-1]\n')
        result = try_flow(checker, env['chunks'], dict(env,value='a.\nb.'), ast.parse('chunks(value)').body[0].value)
        self.assertEqual(result.value, BoundedList(BoundedString(2), 5, 1))
        json.dumps(result.requirements)

    def test_bounded_native_type_check_never_runs_an_sdk_instance_protocol(self):
        _, result = self.flow('import sdk\ndef chunks(value):\n'
            '    if isinstance(value,list):\n        return value\n'
            '    else:\n        return sdk.effect()\n', value=BoundedList(BoundedString(5), 2))
        self.assertEqual(result.value, BoundedList(BoundedString(5), 2))
        with self.assertRaises(BoundedUnknown):
            self.flow('import sdk\ndef chunks(value):\n'
                '    if isinstance(value,sdk.Marker):\n        return value\n'
                '    return value\n', value=BoundedList(BoundedString(5), 2))

    def test_original_owner_exact_branch_excludes_an_unselected_sdk_method(self):
        source = ('class Source:\n    def __init__(self):\n        self.model_name="flan"\n'
            'def wrapper(self,value):\n    def chunks(value,n):\n'
            '        if self.model_name=="flan":\n            current=[]\n            count=0\n'
            '            for sentence in value:\n                size=len(sentence.split())\n'
            '                if count+size>n:\n                    yield " ".join(current)\n'
            '                    current=[sentence]\n                    count=size\n'
            '                else:\n                    current.append(sentence)\n                    count+=size\n'
            '            if current:\n                yield " ".join(current)\n'
            '        else:\n            for sentence in value:\n                yield self.tokenizer(sentence)\n')
        checker, env = self.checker(source)
        site = ast.parse('Source()').body[0].value
        owner = checker.construct(env['Source'], [], {}, 'original.py', 'wrapper', site)
        checker.entry_receiver = owner
        node = env['wrapper'].node.body[0]
        target = _Function('original.py', 'wrapper.<locals>.chunks', node, closure=dict(env,self=owner))
        local = dict(env,chunks=target,value=BoundedList(BoundedString(8),2))
        expression = ast.parse('[text.strip() for text in chunks(value,3)]',mode='eval').body
        result = evaluate_expression(checker, expression, local, 'original.py', 'wrapper')
        self.assertIsInstance(result, BoundedList)
        self.assertLessEqual(result.max_length, 3)
        owner.attributes['model_name'] = 'other'
        with self.assertRaises(BoundedUnknown):
            evaluate_expression(checker, expression, local, 'original.py', 'wrapper')
        self.assertEqual(owner.attributes['model_name'], 'other')

    def test_nested_owner_path_is_only_a_source_fact_not_a_constructor_postcondition(self):
        checker,env = self.checker('class Inner:\n    def __init__(self):\n        self.mode="original"\n'
            'class Outer:\n    def __init__(self):\n        self.inner=Inner()\n')
        root = checker.construct(env['Outer'],[],{},'original.py','root',ast.parse('Outer()').body[0].value)
        checker.entry_receiver = root
        fact = checker.source_owner_path(root.attributes['inner'])
        self.assertEqual(fact['receiver_field_path'],['inner'])
        self.assertEqual(fact['receiver_class']['symbol'],'Inner')
        self.assertEqual(fact['constructor_invariant_status'],'UNKNOWN')
        self.assertFalse(fact['execution_permission'])

    def test_symbolic_zip_has_native_tuple_shape_and_original_shortest_length(self):
        checker,env = self.checker('def chunks(value):\n    yield value\n')
        values = dict(env,left=BoundedList(BoundedString(6),3,1),right=BoundedList(BoundedString(4),2))
        result = evaluate_expression(checker,ast.parse("['>'.join([a,b]) for a,b in zip(left,right)]",mode='eval').body,
            values,'original.py','chunks')
        self.assertEqual(result,BoundedList(BoundedString(11,1),2))
        with self.assertRaises(BoundedUnknown):
            evaluate_expression(checker,ast.parse('[a for a,b,c in zip(left,right)]',mode='eval').body,
                values,'original.py','chunks')
        with self.assertRaises(BoundedUnknown):
            self.flow('def chunks(value):\n    stored=zip(value,value)\n    return stored\n',value=values['left'])

    def test_original_positive_range_and_fresh_slice_generator_preserve_only_bounds(self):
        _,result = self.flow('def chunks(value,n):\n    for i in range(0,len(value),n):\n'
            '        yield value[i:i+n]\n',value=BoundedList(BoundedString(8),5,1),n=2)
        self.assertIsInstance(result.value,BoundedIteration)
        self.assertEqual(result.value.domain,BoundedList(BoundedList(BoundedString(8),2),3,1))
        with self.assertRaises(BoundedUnknown):
            self.flow('def chunks(value):\n    yield value[:]\n',value=BoundedList(BoundedList(1,2),3))


if __name__ == '__main__':
    unittest.main()
