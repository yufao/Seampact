"""Conditional SDK facts stay on the normal source and actual-target chain."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from seampact.source.call_value_conditions import call_context, contextual
from seampact.source.dependency_closure import resolve_resource_calls
from seampact.source.inspection import Inspector
from seampact.source.sdk_value_summaries import RULE, GAP, verify_sdk_targets


class SDKValueSummaryTests(unittest.TestCase):
    def trace(self, source, value):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        (root / 'original.py').write_text(source, encoding='utf-8')
        inspector = Inspector.freeze(root, root / 'snapshots', 'request', ['original.py'], 'score', 'result')
        entry = dict(id='score', kind='function', path='original.py', symbol='score')
        candidate = dict(entries=[entry], boundaries=[])
        ctx, _ = call_context(dict(value=value))
        return resolve_resource_calls(contextual(inspector, ctx), entry, candidate)

    def requirements(self, trace):
        return [row['target_requirements'] for row in trace['external_protocols'] if row['rule'] == RULE]

    def test_normal_trace_preserves_finite_progress_order_and_opaque_target_obligation(self):
        trace = self.trace('from tqdm import tqdm\n'
            'def score(value):\n    result=[]\n'
            '    for left,right in tqdm(zip(value,[1,2]),desc="original",total=len(value)):\n'
            '        assert isinstance(left,str)\n        assert isinstance(right,int)\n'
            '        result.append(left)\n    return result\n', ['a','b'])
        requirements = self.requirements(trace)
        self.assertEqual(len(requirements), 1)
        self.assertEqual(requirements[0]['input_status'], 'CHECKED')
        self.assertFalse(requirements[0]['execution_permission'])
        self.assertEqual([g['reason'] for g in trace['negative_scope_gaps']], [GAP])
        self.assertEqual(requirements[0]['summary']['result']['order'], 'original input order')
        self.assertFalse(verify_sdk_targets(requirements)['allowed'])

    def test_unknown_progress_option_or_opaque_input_cannot_supply_a_result(self):
        for expression in ('tqdm(value,file=value)', 'tqdm(sdk.items())'):
            trace = self.trace('import sdk\nfrom tqdm import tqdm\ndef score(value):\n'
                '    for item in '+expression+':\n        assert isinstance(item,str)\n    return 1\n', ['a'])
            self.assertTrue(any(g['reason'] != GAP for g in trace['negative_scope_gaps']))
            self.assertEqual(self.requirements(trace)[0]['input_status'], 'UNKNOWN')

    def test_sentence_outer_structure_never_invents_sentences_or_count(self):
        trace = self.trace('from nltk.tokenize import sent_tokenize\ndef score(value):\n'
            '    result=sent_tokenize(value)\n    assert isinstance(result,list)\n'
            '    for sentence in result:\n        assert isinstance(sentence,str)\n    return result\n', 'a. b.')
        req = self.requirements(trace)[0]
        self.assertEqual(req['input_status'], 'CHECKED')
        self.assertEqual(req['summary']['result']['exact_values'], 'UNKNOWN')
        self.assertTrue(any(g['reason'] != GAP for g in trace['negative_scope_gaps']))

    def test_target_checker_rejects_candidate_permission_or_hash_only_fact(self):
        trace = self.trace('from nltk.tokenize import sent_tokenize\ndef score(value):\n'
                           '    return sent_tokenize(value)\n', 'a')
        req = self.requirements(trace)[0]
        with patch('seampact.source.lifecycle_resources.sdk_identity', return_value=dict(status='CHECKED')):
            report = verify_sdk_targets([req])
        self.assertEqual(report['status'], 'UNKNOWN')
        self.assertFalse(report['allowed'])
        self.assertFalse(verify_sdk_targets([dict(req, execution_permission=True)])['allowed'])
        self.assertFalse(verify_sdk_targets([dict(req, summary=dict(id='candidate'))])['allowed'])

    def test_storing_a_progress_iterator_is_an_escape_not_a_plain_list(self):
        trace = self.trace('from tqdm import tqdm\ndef score(value):\n'
            '    iterator=tqdm(value)\n    for item in iterator:\n        pass\n    return 1\n', ['a'])
        self.assertTrue(any('escapes immediate' in g['reason'] for g in trace['negative_scope_gaps']))

    def test_normal_helper_generator_and_filtered_comprehension_consume_bounded_domain(self):
        source = '''from nltk.tokenize import sent_tokenize
def sentences(text):
    blocks=text.split('\\n')
    tokenized_blocks=[sent_tokenize(block) for block in blocks]
    tokenized_text=[]
    for block in tokenized_blocks:
        tokenized_text.extend(block)
        tokenized_text.append('\\n')
    return tokenized_text[:-1]
def pieces(lst,n):
    current=[]
    count=0
    for sentence in lst:
        size=len(sentence.split())
        if count+size>n:
            yield ' '.join(current)
            current=[sentence]
            count=size
        else:
            current.append(sentence)
            count+=size
    if current:
        yield ' '.join(current)
def score(value):
    sents=sentences(value)
    sents=sents or ['']
    chunks=[chunk.replace(' \\n ','\\n').strip() for chunk in pieces(sents,2)]
    chunks=[chunk for chunk in chunks if chunk!='']
    assert isinstance(chunks,list)
    return chunks
'''
        trace = self.trace(source, 'a. b.')
        self.assertEqual(trace['status'], 'CHECKED')
        self.assertEqual([g['reason'] for g in trace['negative_scope_gaps']], [GAP])
        facts = [x['target_requirements'] for x in trace['external_protocols']
                 if x['rule'] == 'source-bounded-value-flow/1']
        self.assertEqual({x['target_symbol'] for x in facts}, {'sentences','pieces'})
        self.assertTrue(all(x['execution_permission'] is False for x in facts))
        long_text = self.trace(source, 'word ' * 24 + '.')
        self.assertEqual([g['reason'] for g in long_text['negative_scope_gaps']], [GAP])
        # Every possible branch remains subject to the same original effects.
        unsafe = source.replace("yield ' '.join(current)\n            current=[sentence]",
                                "sdk.unproved()\n            yield ' '.join(current)\n            current=[sentence]")
        failed = self.trace('import sdk\n'+unsafe, 'a. b.')
        self.assertTrue(any(g['reason'] != GAP for g in failed['negative_scope_gaps']))


if __name__ == '__main__':
    unittest.main()
