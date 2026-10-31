"""Context budgets preserve source history, tool groups and current constraints."""

import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from seampact.context_memory import ContextMemory, ContextError, context_status, summary_profile
from seampact.providers import completion_body
from seampact.store import Store


class ContextMemoryChecks(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.addCleanup(self.store.close)
        self.memory = ContextMemory(self.store, 'test/one')

    def messages(self):
        return [dict(role='system', content='mandatory rules'), dict(role='user', content='original task')] + [
            dict(role='assistant', content='historical fact ' * 400) for _ in range(10)]

    def summary(self, messages, profile):
        self.summary_request = copy.deepcopy(messages)
        return dict(choices=[dict(finish_reason='stop', message=dict(content='old facts; one unresolved item'))],
                    usage=dict(prompt_tokens=100, completion_tokens=10))

    def test_automatic_high_watermark_preserves_originals_and_current_user(self):
        messages=self.messages()+[dict(role='user', content='current exact constraint')]
        original=copy.deepcopy(messages)
        events=[]
        compact,status=self.memory.prepare(dict(declared_context_window=50000,declared_max_output_tokens=1000),
            messages,summarize=self.summary,record=events.append,force=True)
        self.assertEqual(messages,original)
        self.assertEqual(compact[:2],messages[:2])
        self.assertEqual(compact[-1],messages[-1])
        note=json.loads(compact[2]['content'])
        self.assertEqual(note['type'],'historical_memory')
        self.assertIn('untrusted',note['authority'])
        self.assertEqual(events[0]['usage']['completion_tokens'],10)
        raw=self.memory.read(note['archive_ref'],path='/messages/0/content',count=2000)
        self.assertIn('historical fact',raw['text'])
        self.assertEqual(status['status'],'COMPACTED')

    def test_known_window_triggers_early_but_unknown_window_has_no_fake_ratio(self):
        messages=self.messages()
        _,status=self.memory.prepare(dict(declared_context_window=10000),messages,summarize=self.summary)
        self.assertEqual(status['status'],'COMPACTED')
        original,status=self.memory.prepare({},messages,summarize=lambda *args:self.fail('unknown window must not trigger'))
        self.assertEqual(status['status'],'WINDOW_UNKNOWN')
        self.assertIsNone(status['estimated_ratio'])
        self.assertEqual(original,messages)

    def test_tool_pairs_are_not_split_or_summarized_with_missing_results(self):
        call=dict(role='assistant',content=None,tool_calls=[dict(id='a'),dict(id='b')])
        results=[dict(role='tool',tool_call_id='a',content='A'),dict(role='tool',tool_call_id='b',content='B')]
        messages=self.messages()[:2]+[call]+results+self.messages()[2:]
        compact,_=self.memory.prepare({},messages,summarize=self.summary,force=True)
        data=json.loads(self.summary_request[1]['content'])
        self.assertNotIn(call,data)
        index=compact.index(call)
        self.assertEqual(compact[index:index+3],[call]+results)
        with self.assertRaises(ContextError):
            self.memory.prepare({},messages[:4],summarize=self.summary,force=True)

    def test_multimodal_bytes_stay_in_original_messages(self):
        image=dict(role='user',content=[dict(type='image_url',image_url=dict(url='data:image/png;base64,fixture'))])
        messages=self.messages()+[image]+self.messages()[2:]
        compact,status=self.memory.prepare({},messages,summarize=self.summary,force=True)
        self.assertIn(image,compact)
        self.assertNotIn('data:image',self.summary_request[1]['content'])
        self.assertIsNone(status['estimated_ratio'])
        self.memory.observe(dict(prompt_tokens=900),messages)
        _,status=self.memory.prepare(dict(declared_context_window=2000),messages,summarize=self.summary)
        self.assertEqual(status['last_actual_input_tokens'],900)
        self.assertIsNone(status['estimated_ratio'])

    def test_summary_profile_does_not_conflict_with_user_output_ceiling(self):
        profile=dict(model='fixture',declared_max_output_tokens=100000,parameters=dict(max_tokens=100000))
        selected=summary_profile(profile)
        self.assertEqual(completion_body(selected,[])['max_tokens'],2048)
        self.assertEqual(profile['declared_max_output_tokens'],100000)
        self.assertEqual(summary_profile(dict(capabilities=dict(selected=dict(max_output_tokens=512))))['parameters']['max_tokens'],512)

    def test_archive_scope_digest_and_pages(self):
        reference=self.memory.archive(self.messages())
        with self.assertRaises(ValueError):ContextMemory(self.store,'test/two').read(reference)
        with self.assertRaises(ValueError):self.memory.read(reference,path='/messages/999')
        row=self.store.get('context_archive',reference)
        row['messages'][0]['content']='modified'
        self.store.save('context_archive',reference,row)
        with self.assertRaises(ValueError):self.memory.read(reference)

    def test_disabled_management_and_uncompressible_input(self):
        messages=self.messages()
        result,status=self.memory.prepare(dict(declared_context_window=100,context_management=False),messages,
            summarize=lambda *args:self.fail('disabled'))
        self.assertEqual(result,messages)
        with self.assertRaises(ContextError):
            self.memory.prepare(dict(declared_context_window=100,declared_max_output_tokens=100),messages)
        with self.assertRaises(ContextError):
            self.memory.prepare({},messages[:2],summarize=self.summary,force=True)

    def test_window_configuration_stays_local_to_platform(self):
        import tempfile
        from seampact.providers import Profiles
        with tempfile.TemporaryDirectory() as root:
            profiles=Profiles(Path(root))
            connection=profiles.save_connection(dict(name='fixture',base_url='https://example.invalid/v1',model='fixture',
                auth=dict(mode='none'),context_window=128000,context_management=False,max_output_tokens=1024))
            profile=profiles.profile(connection['id'])
            body=completion_body(profile,[dict(role='user',content='task')])
            self.assertEqual(profile['declared_context_window'],128000)
            self.assertFalse(profile['context_management'])
            self.assertNotIn('context_window',body)
            self.assertNotIn('context_management',body)
            self.assertEqual(body['max_tokens'],1024)

    def test_interrupted_summary_keeps_archive_and_unknown_usage_event(self):
        events=[]
        def interrupted(*args):
            raise RuntimeError('fixture interruption')
        with self.assertRaises(RuntimeError):
            self.memory.prepare({},self.messages(),summarize=interrupted,record=events.append,force=True)
        self.assertEqual(events[0]['status'],'FAILED')
        self.assertIsNone(events[0]['usage'])
        self.assertTrue(self.memory.read(events[0]['archive_ref'],path='/messages')['total'])


if __name__=='__main__':
    unittest.main()
