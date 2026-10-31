"""Long-report feedback and explicit continuation use preserved checked proposals."""

import copy
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from seampact.analysis import AnalysisJobs, RULES, preparation_input, content_digest
from seampact.analysis_records import load_report, read_report
from seampact.inspection import Inspector
from seampact.providers import ProviderError
from seampact.store import Store
from seampact.units import UnitCatalog


class AnalysisContextChecks(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root / 'main.py').write_text('def target(value: str) -> str:\n    return value\n', encoding='utf-8')
        self.inspector = Inspector.freeze(root, root / 'snapshots', 'request', ['main.py'], 'target', 'result')
        self.inspector.request['source_id'] = 'source'
        self.store = Store(':memory:')
        self.addCleanup(self.store.close)
        self.profile = dict(provider='fixture', model='fixture', base_url='https://example.invalid', auth=dict(mode='none'))
        self.profiles = SimpleNamespace(profile=lambda _:copy.deepcopy(self.profile), key=lambda _: '')
        catalog = UnitCatalog(root, self.store, None)
        self.jobs = AnalysisJobs(self.store, self.profiles, check_entries=catalog.inspect_candidate)
        self.candidate = dict(entries=[dict(id='target', path='main.py', symbol='target', kind='function')],
                              boundaries=[], runtime_groups=[dict(id='whole', entries=['target'])])

    def response(self, tool, arguments, identity):
        return dict(choices=[dict(finish_reason='tool_calls', message=dict(role='assistant', content=None,
            tool_calls=[dict(id=identity, type='function', function=dict(name=tool, arguments=json.dumps(arguments)))]))])

    def failed(self, kind='context_length'):
        sent=[]
        def send(url,key,body):
            sent.append(copy.deepcopy(body))
            if len(sent)==1:
                return self.response('check_candidate', dict(candidate=self.candidate), 'checked')
            raise ProviderError('provider HTTP 400: fixture', kind=kind,
                                diagnostic=dict(http_status=400, category=kind))
        self.jobs.transport=SimpleNamespace(send=send)
        job=dict(id='failed', request_id='request', status='RUNNING', rounds=0, usage=[],
            pipeline_stage='analysis', preprocessing=dict(mode='none', status='SKIPPED'),
            usage_by_stage=dict(analysis=[],preprocessing=[]),
            messages=[dict(role='system',content=RULES),dict(role='user',content='frozen request')])
        self.jobs._run(self.inspector,self.profile,'',job,threading.Event())
        return job,sent

    def test_context_error_preserves_full_report_and_does_not_implicitly_retry(self):
        job,sent=self.failed()
        self.assertEqual(job['status'],'FAILED')
        self.assertEqual(len(sent),2)
        self.assertEqual(job['failure_diagnostic'],dict(http_status=400,category='context_length'))
        self.assertGreater(job['last_request_size']['request_bytes'],0)
        reply=json.loads(job['messages'][-1]['content'])
        self.assertEqual(reply['schema'],'compact-check-feedback/1')
        original=load_report(self.store,job,reply['report_ref'])
        self.assertEqual(original['candidate'],self.candidate)
        self.assertIn('execution_contracts',original['report'])
        self.assertNotIn('execution_contracts',reply)
        self.assertTrue(self.jobs.can_resume(job))

    def test_manual_continuation_uses_last_checked_candidate_then_runs_complete_checks(self):
        job,_=self.failed()
        with patch('seampact.analysis.threading.Thread.start'):
            identity=self.jobs.start(self.inspector,'analysis',resume=job,preprocessing='none')
        continued=self.store.get('analysis',identity)
        self.assertEqual(continued['resume_mode'],'checked_candidate')
        self.assertEqual(len(continued['messages']),2)
        recovery=json.loads(continued['messages'][1]['content'])['recovery']
        self.assertEqual(recovery['candidate'],self.candidate)
        self.assertEqual(recovery['from_analysis'],'failed')
        sent=[]
        def send(url,key,body):
            sent.append(copy.deepcopy(body))
            if len(sent)==1:
                return self.response('read_analysis_report',dict(report_ref=recovery['report']['report_ref'],
                    path='/entry_bindings/entries',count=1),'detail')
            page=json.loads(body['messages'][-1]['content'])
            self.assertEqual(page['schema'],'analysis-report-page/1')
            return self.response('submit_candidate',dict(candidate=self.candidate),'submitted')
        self.jobs.transport=SimpleNamespace(send=send)
        self.jobs._run(self.inspector,self.profile,'',continued,threading.Event())
        self.assertEqual(continued['status'],'COMPLETED',continued.get('error'))
        self.assertEqual(continued['execution'],'NOT_RUN')
        self.assertIn('entry_readiness',continued['submitted']['report'])
        self.assertEqual(self.store.get('analysis','failed')['messages'],job['messages'])

    def test_unclassified_400_does_not_become_context_continuation(self):
        job,_=self.failed('invalid_request')
        self.assertFalse(self.jobs.can_resume(job))

    def test_report_pages_keep_scope_and_check_saved_identity(self):
        job,_=self.failed()
        reference=job['last_check']['report_ref']
        page=read_report(self.store,job,reference,path='/entry_bindings/entries',count=1)
        self.assertEqual(page['total'],1)
        self.assertIsNone(page['next_offset'])
        with self.assertRaises(ValueError):read_report(self.store,dict(report_refs=[]),reference)
        with self.assertRaises(ValueError):read_report(self.store,job,reference,count=0)
        with self.assertRaises(ValueError):read_report(self.store,job,reference,path='/entry_bindings/entries/999')
        record=self.store.get('analysis_tool_report',reference)
        record['report']['status']='FORGED'
        self.store.save('analysis_tool_report',reference,record)
        with self.assertRaises(ValueError):load_report(self.store,job,reference)

    def test_changed_frozen_input_rejects_candidate_continuation(self):
        job,_=self.failed()
        self.inspector.request['configuration']={'different':True}
        with patch('seampact.analysis.threading.Thread.start') as start:
            with self.assertRaisesRegex(ValueError,'冻结输入不一致'):
                self.jobs.start(self.inspector,'analysis',resume=job,preprocessing='none')
            start.assert_not_called()

    def test_completed_context_keeps_its_original_report_references(self):
        job,_=self.failed()
        job.update(status='COMPLETED', submitted=dict(candidate=self.candidate),
                   preprocessing=dict(mode='none', status='SKIPPED'))
        self.store.save('request','request',self.inspector.request)
        with patch('seampact.analysis.threading.Thread.start'):
            identity=self.jobs.start(self.inspector,'analysis',previous=job,preprocessing='none')
        continued=self.store.get('analysis',identity)
        self.assertEqual(continued['refinement_of'],job['id'])
        reference=job['last_check']['report_ref']
        self.assertIn(reference,continued['report_refs'])
        self.assertEqual(load_report(self.store,continued,reference)['candidate'],self.candidate)

    def test_proactive_analysis_compaction_accounts_summary_and_preserves_history(self):
        sent=[]
        history=[dict(role='system',content=RULES),dict(role='user',content='original exact task')]+[
            dict(role='assistant',content='old source findings '*300) for _ in range(10)]
        self.profile['declared_context_window']=10000
        def send(url,key,body):
            sent.append(copy.deepcopy(body))
            if len(sent)==1:
                return dict(choices=[dict(finish_reason='stop',message=dict(content='source facts remain unverified'))],
                            usage=dict(prompt_tokens=100,completion_tokens=20))
            self.assertEqual(body['messages'][1]['content'],'original exact task')
            self.assertIn('historical_memory',body['messages'][2]['content'])
            return self.response('submit_candidate',dict(candidate=self.candidate),'done')
        self.jobs.transport=SimpleNamespace(send=send)
        job=dict(id='auto',request_id='request',status='RUNNING',rounds=0,usage=[],messages=copy.deepcopy(history),
                 pipeline_stage='analysis',usage_by_stage=dict(analysis=[],preprocessing=[]))
        self.jobs._run(self.inspector,self.profile,'',job,threading.Event())
        self.assertEqual(job['status'],'COMPLETED',job.get('error'))
        self.assertEqual(job['model_calls'],2)
        self.assertEqual(job['rounds'],1)
        self.assertEqual(job['context_events'][0]['usage']['completion_tokens'],20)
        self.assertTrue(job['context_archives'])

    def test_context_recovery_before_first_candidate_uses_complete_tool_boundary(self):
        history=[dict(role='system',content=RULES),dict(role='user',content='original task')]+[
            dict(role='assistant',content='old findings '*300) for _ in range(8)]
        history.extend([dict(role='assistant',content=None,tool_calls=[dict(id='old-call',type='function',
            function=dict(name='read_file',arguments='{"path":"main.py"}'))]),
            dict(role='tool',tool_call_id='old-call',content='original source')])
        job=dict(id='pre-candidate',request_id='request',status='FAILED',rounds=1,usage=[],messages=history,
            pipeline_stage='analysis',failure_phase='model_request',error_kind='context_length',submitted=None,
            context_scope='analysis/pre-candidate',preprocessing=dict(mode='none'),
            usage_by_stage=dict(analysis=[],preprocessing=[]))
        self.assertTrue(self.jobs.can_resume(job))
        with patch('seampact.analysis.threading.Thread.start'):
            identity=self.jobs.start(self.inspector,'analysis',resume=job,preprocessing='none')
        continued=self.store.get('analysis',identity)
        self.assertEqual(continued['resume_mode'],'archived_context')
        sent=[]
        def send(url,key,body):
            sent.append(body)
            if len(sent)==1:
                return dict(choices=[dict(finish_reason='stop',message=dict(content='facts still require source checks'))])
            self.assertTrue(any(m.get('tool_call_id')=='old-call' for m in body['messages']))
            return self.response('submit_candidate',dict(candidate=self.candidate),'final')
        self.jobs.transport=SimpleNamespace(send=send)
        self.jobs._run(self.inspector,self.profile,'',continued,threading.Event())
        self.assertEqual(continued['status'],'COMPLETED',continued.get('error'))
        self.assertEqual(len(sent),2)

    def test_revision_candidate_recovery_keeps_original_task_and_requires_model_binding(self):
        job,_=self.failed()
        repair=dict(revision_id='revision',base_analysis_id='base',base_candidate_digest=content_digest(self.candidate),
                    candidate=self.candidate,mode='refine',guidance='exact instruction',reason='original reason')
        original=dict(task='original complete repair request',repair=repair,facts_index=['keep-original-reference'])
        job.update(revision_id='revision',repair=repair)
        job['messages'][1]['content']=json.dumps(original)
        with patch('seampact.analysis.threading.Thread.start'):
            identity=self.jobs.start(self.inspector,'analysis',resume=job,preprocessing='none')
        request=json.loads(self.store.get('analysis',identity)['messages'][1]['content'])
        self.assertEqual(request['task'],original['task'])
        self.assertEqual(request['repair'],repair)
        self.assertEqual(request['facts_index'],original['facts_index'])
        self.profile['parameters']=dict(temperature=0.8)
        with self.assertRaisesRegex(ValueError,'生成配置已改变'):
            self.jobs.start(self.inspector,'analysis',resume=job,preprocessing='none')

    def test_cached_source_facts_require_current_material_basis(self):
        from seampact.revisions import build_review_context
        facts=build_review_context(self.inspector,self.inspector,self.store)
        facts['basis']=dict(source_version=self.inspector.request['source_version'],
            input_digest=content_digest(preparation_input(self.inspector)),implementation=dict(self.jobs.implementation))
        job=dict(repair=dict(facts=facts))
        result=self.jobs._read_revision_facts(self.inspector,job,'main.py')
        self.assertEqual(result['status'],'AVAILABLE',result)
        self.inspector.request['configuration']=dict(changed=True)
        self.assertEqual(self.jobs._read_revision_facts(self.inspector,job,'main.py')['reason'],'material_identity_changed')


if __name__=='__main__':
    unittest.main()
