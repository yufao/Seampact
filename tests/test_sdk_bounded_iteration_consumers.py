"""Normal For consumption checks every finite length without fake elements."""
import ast
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from seampact.source.inspection import Inspector
from seampact.source.call_value_conditions import Context,contextual
from seampact.source.dependency_closure import _Closure
from seampact.source.sdk_bounded_flow import BoundedIteration,ITERATION_RULE,consume_normal_for
from seampact.source.sdk_bounded_values import BoundedList,BoundedString,BoundedInteger,BoundedUnknown
from seampact.source.sdk_transformer_protocols import SDKValue


class BoundedIterationConsumerTests(unittest.TestCase):
    def checker(self,source):
        temporary=tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root=Path(temporary.name)
        (root/'original.py').write_text(source,encoding='utf-8')
        inspector=Inspector.freeze(root,root/'snapshots','request',['original.py'],'score','result')
        entry=dict(id='score',kind='function',path='original.py',symbol='score')
        checker=_Closure(contextual(inspector,Context(values={})),dict(entries=[entry],boundaries=[]))
        checker.import_roots={}
        return checker,checker.module('original.py')

    def invoke(self,source,**values):
        checker,module=self.checker(source)
        target=module['score']
        result=checker.invoke(target,[],values,'original.py','<entry>',target.node,public_entry=True)
        return checker,result

    def consume(self,source,domain,**local):
        checker,module=self.checker(source)
        target=module['score']
        loop=next(item for item in target.node.body if type(item) is ast.For)
        env=dict(module,**local)
        checker.registration_frames.append(dict(target=target,local=env))
        iterator=BoundedIteration(domain,id(loop.iter))
        return checker,target,loop,env,iterator

    def test_normal_source_helper_to_for_checks_all_counts_and_for_else(self):
        checker,result=self.invoke('from nltk.tokenize import sent_tokenize\n'
            'def chunks(text):\n    for item in sent_tokenize(text):\n        yield item\n'
            'def score(text):\n    total=0\n    for item in chunks(text):\n'
            '        total += len(item)\n    else:\n        total += 5\n    return total\n',text='aa. bb.')
        self.assertEqual(result,BoundedInteger(5,26))
        rows=[row for row in checker.protocols if row['rule']==ITERATION_RULE]
        self.assertEqual(len(rows),1)
        self.assertEqual((rows[0]['minimum_length'],rows[0]['maximum_length']),(0,3))
        self.assertFalse(any('loop iterable is UNKNOWN' in row['reason'] for row in checker.negative_scope_gaps))
        self.assertTrue(any('actual worker verification' in row['reason'] for row in checker.negative_scope_gaps))

    def test_original_empty_local_accumulator_uses_replacement_envelopes(self):
        checker,result=self.invoke('from nltk.tokenize import sent_tokenize\n'
            'def chunks(text):\n    for item in sent_tokenize(text):\n        yield item\n'
            'def score(text):\n    output=[]\n    for item in chunks(text):\n'
            '        output.append(item.strip())\n    else:\n        output.append("end")\n'
            '    return output\n',text='aa. bb.')
        self.assertEqual(result,BoundedList(BoundedString(7),4,1))
        self.assertTrue(any(row['rule']==ITERATION_RULE for row in checker.protocols))

    def test_borrowed_or_aliased_accumulator_rejects_without_mutation(self):
        for preamble,extra in [('',{}),('    output=[]\n    alias=output\n',{'alias':None})]:
            checker,target,loop,env,iterator=self.consume('def score(stream,output):\n'+preamble+
                '    for item in stream:\n        output.append(item)\n    return output\n',
                BoundedList(BoundedString(4),2),output=[])
            borrowed=env['output']
            if extra:
                env['alias']=borrowed
            with self.assertRaises(BoundedUnknown):
                consume_normal_for(checker,loop,iterator,env,'original.py','score')
            self.assertEqual(borrowed,[])
            self.assertIs(env['output'],borrowed)

    def test_unknown_arm_effect_and_attribute_writer_reject(self):
        for body in ('        if item:\n            unknown.effect()\n',
                     '        owner.mode = item\n'):
            checker,target,loop,env,iterator=self.consume('import unknown\ndef score(stream,owner):\n'
                '    for item in stream:\n'+body+'    return 1\n',
                BoundedList(BoundedString(4),2),owner=object())
            before=dict(env)
            with self.assertRaises(BoundedUnknown):
                consume_normal_for(checker,loop,iterator,env,'original.py','score')
            self.assertEqual(env,before)

    def test_zero_count_does_not_invent_a_later_loop_variable(self):
        checker,target,loop,env,iterator=self.consume('def score(stream):\n'
            '    for item in stream:\n        pass\n    return item\n',BoundedList(1,2))
        with self.assertRaisesRegex(BoundedUnknown,'unbound'):
            consume_normal_for(checker,loop,iterator,env,'original.py','score')
        self.assertNotIn('item',env)

    def test_site_large_domain_and_nonlocal_control_do_not_gain_coverage(self):
        for domain,body in [(BoundedList(1,65),'        pass\n'),
                            (BoundedList(1,2),'        break\n'),
                            (BoundedList(1,2),'        global changed\n        changed=item\n')]:
            checker,target,loop,env,iterator=self.consume('def score(stream):\n'
                '    for item in stream:\n'+body+'    return 1\n',domain)
            with self.assertRaises(BoundedUnknown):
                consume_normal_for(checker,loop,iterator,env,'original.py','score')
        with self.assertRaisesRegex(BoundedUnknown,'site'):
            consume_normal_for(checker,loop,BoundedIteration(BoundedList(1,2),0),env,'original.py','score')

    def test_call_in_field_receiver_is_not_executed_while_finding_a_target(self):
        checker,target,loop,env,iterator=self.consume('state=[]\ndef writer():\n'
            '    state.append(1)\n    return unknown\ndef score(stream):\n'
            '    total=0\n    for item in stream:\n        total=writer().field\n    return total\n',
            BoundedList(1,2),total=0)
        with self.assertRaises(BoundedUnknown):
            consume_normal_for(checker,loop,iterator,env,'original.py','score')
        self.assertEqual(checker.module('original.py')['state'],[])

    def test_equal_sdk_origin_is_not_cross_branch_object_identity(self):
        left=SDKValue('tensor',('same-original-site',))
        right=SDKValue('tensor',('same-original-site',))
        checker,target,loop,env,iterator=self.consume('def score(stream,token,original):\n'
            '    result=original\n    for item in stream:\n        result=token\n    return result\n',
            BoundedList(1,1),result=left,token=right,original=left)
        with self.assertRaises(BoundedUnknown):
            consume_normal_for(checker,loop,iterator,env,'original.py','score')
        self.assertIs(env['result'],left)
        self.assertIsNot(left,right)


if __name__=='__main__': unittest.main()
