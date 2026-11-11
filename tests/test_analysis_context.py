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

from seampact.analysis import AnalysisJobs, RULES, preparation_input, content_digest, _read_file_reply
from seampact.analysis_support.analysis_records import load_report, read_report
from seampact.source.inspection import Inspector, digest
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
        self.profile = dict(provider='fixture', model='fixture', base_url='https://example.invalid', auth=dict(mode='none'),
                            declared_context_window=1000000)
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

    def cancelled_boundary(self):
        job, sent = self.failed('transport')
        job.update(status='CANCELLED', phase='model_request',
            cancel_requested_at='2026-01-01T00:00:00+00:00', cancelled_at='2026-01-01T00:00:01+00:00',
            unreported_usage=dict(status='UNKNOWN', reason='cancelled_before_model_response'))
        job['timings'][-1]['outcome'] = 'cancelled'
        self.store.save('analysis', job['id'], job)
        return job, sent

    def test_cancelled_unanswered_request_reuses_exact_context_and_preserves_prior_unknown_cost(self):
        job, _ = self.cancelled_boundary()
        original = copy.deepcopy(job)
        self.assertTrue(self.jobs.can_resume(job))
        with patch('seampact.analysis.threading.Thread.start'), patch.object(self.jobs.transport, 'send') as send:
            identity = self.jobs.start(self.inspector, 'analysis', resume=job, preprocessing='none')
        send.assert_not_called()
        continued = self.store.get('analysis', identity)
        self.assertEqual(continued['messages'], original['messages'])
        self.assertEqual(continued['resumed_from'], original['id'])
        self.assertEqual(continued['prior_rounds'], original['rounds'])
        self.assertEqual(continued['rounds'], 0)
        self.assertEqual(continued['usage'], [])
        self.assertEqual(continued['prior_unreported_usage'][0]['analysis_id'], original['id'])
        self.assertEqual(continued['prior_unreported_usage'][0]['usage'], original['unreported_usage'])
        self.assertNotIn('unreported_usage', continued)
        self.assertEqual(self.store.get('analysis', original['id']), original)

    def test_cancelled_resume_requires_the_recorded_unanswered_request_and_exact_saved_messages(self):
        job, _ = self.cancelled_boundary()
        cases = [dict(phase='source_tools'), dict(phase='context_compaction'),
                 dict(active_operation=dict(phase='model_request')), dict(cancelled_at=None),
                 dict(cancel_requested_at=None), dict(unreported_usage=None),
                 dict(unreported_usage=dict(status='UNKNOWN', reason='model_response_unavailable')),
                 dict(submitted=dict(candidate=self.candidate))]
        for change in cases:
            with self.subTest(change=change):
                self.assertFalse(self.jobs.can_resume(dict(job, **change)))
        for change in (dict(outcome='completed'), dict(round=job['rounds']), dict(stage='other')):
            changed = copy.deepcopy(job)
            changed['timings'][-1].update(change)
            self.assertFalse(self.jobs.can_resume(changed))
        changed = copy.deepcopy(job)
        changed['messages'][-1]['content'] += ' altered'
        self.assertFalse(self.jobs.can_resume(changed))
        changed = copy.deepcopy(job)
        changed['last_request_size']['request_bytes'] = 0
        self.assertFalse(self.jobs.can_resume(changed))

    def test_cancelled_incomplete_tool_pairs_and_changed_rules_are_not_replayed(self):
        job, _ = self.cancelled_boundary()
        for transform in (lambda messages: messages[:-1],
                          lambda messages: messages + [copy.deepcopy(messages[-1])],
                          lambda messages: [dict(role='system', content='different rules')] + messages[1:]):
            changed = copy.deepcopy(job)
            changed['messages'] = transform(changed['messages'])
            changed['last_request_size']['message_characters'] = sum(
                len(json.dumps(message, ensure_ascii=False)) for message in changed['messages'])
            self.assertFalse(self.jobs.can_resume(changed))

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
        # The verbatim rules, tools and protected suffix must themselves fit.
        self.profile['declared_context_window']=24000
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

    def test_local_summary_preflight_failure_has_no_model_call_or_unknown_provider_usage(self):
        profile=dict(self.profile)
        profile.pop('declared_context_window')
        history=[dict(role='system',content=RULES),dict(role='user',content='original task')]+[
            dict(role='assistant',content='old source findings '*300) for _ in range(8)]
        job=dict(id='local-preflight',request_id='request',status='RUNNING',rounds=0,usage=[],
            messages=history,pipeline_stage='analysis',force_context_compaction=True,
            usage_by_stage=dict(analysis=[],preprocessing=[]))
        with patch.object(self.jobs.transport,'send') as send:
            self.jobs._run(self.inspector,profile,'',job,threading.Event())
        send.assert_not_called()
        self.assertEqual(job['status'],'FAILED')
        self.assertEqual(job['failure_phase'],'context_preparation')
        self.assertEqual(job['model_calls'],0)
        self.assertEqual(job['usage'],[])
        self.assertNotIn('unreported_usage',job)

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
        from seampact.analysis_support.revisions import build_review_context
        facts=build_review_context(self.inspector,self.inspector,self.store)
        facts['basis']=dict(source_version=self.inspector.request['source_version'],
            input_digest=content_digest(preparation_input(self.inspector)),implementation=dict(self.jobs.implementation))
        job=dict(repair=dict(facts=facts))
        result=self.jobs._read_revision_facts(self.inspector,job,'main.py')
        self.assertEqual(result['status'],'AVAILABLE',result)
        self.inspector.request['configuration']=dict(changed=True)
        self.assertEqual(self.jobs._read_revision_facts(self.inspector,job,'main.py')['reason'],'material_identity_changed')

    def test_repeated_read_executes_original_tool_and_references_visible_full_reply(self):
        arguments=dict(path='main.py',start=1,count=120)
        sent=[]
        def send(url,key,body):
            sent.append(copy.deepcopy(body))
            if len(sent)<=2:
                return self.response('read_file',arguments,'page-'+str(len(sent)))
            return self.response('submit_candidate',dict(candidate=self.candidate),'done')
        self.jobs.transport=SimpleNamespace(send=send)
        job=dict(id='repeat-source',request_id='request',status='RUNNING',rounds=0,usage=[],
            pipeline_stage='analysis',usage_by_stage=dict(analysis=[],preprocessing=[]),
            messages=[dict(role='system',content=RULES),dict(role='user',content='frozen request')])
        with patch.object(self.inspector,'read_file',wraps=self.inspector.read_file) as read:
            self.jobs._run(self.inspector,dict(self.profile,context_management=False),'',job,threading.Event())
        self.assertEqual(job['status'],'COMPLETED',job.get('error'))
        self.assertEqual(read.call_count,2)
        tools={message['tool_call_id']:message for message in sent[-1]['messages'] if message.get('role')=='tool'}
        original=json.loads(tools['page-1']['content'])
        reference=json.loads(tools['page-2']['content'])
        self.assertEqual(reference['schema'],'source-read-reference/1')
        self.assertNotIn('lines',reference)
        self.assertEqual(reference['reference']['tool_call_id'],'page-1')
        self.assertEqual(reference['reference']['arguments'],arguments)
        self.assertTrue(reference['reference']['original_in_current_context'])
        self.assertEqual(reference['source_version'],self.inspector.request['source_version'])
        for field in ('path','unit','sha256','start','total_lines','next_line'):
            self.assertEqual(reference[field],original[field])

    def test_read_reference_requires_frozen_identity_arguments_and_exact_response(self):
        arguments=dict(path='main.py',start=1,count=120)
        result=self.inspector.read_file(**arguments)
        version=self.inspector.request['source_version']
        changed=copy.deepcopy(result)
        changed['lines'][0]+=' changed'
        cases=[('different',arguments,result),('',arguments,result),(None,arguments,result),(1,arguments,result),
               (version,dict(arguments,count=121),result),(version,arguments,changed)]
        for selected,args,page in cases:
            with self.subTest(version=selected,arguments=args,page=page):
                seen={}
                _read_file_reply(version,arguments,result,[],seen,'original')
                messages=[dict(role='tool',tool_call_id='original',
                    content=json.dumps(result,ensure_ascii=False,separators=(',',':')))]
                self.assertIs(_read_file_reply(selected,args,page,messages,seen,'next'),page)

    def test_outbound_references_restore_exact_reads_after_prepare_archives_original(self):
        root=self.inspector.root
        path=root/'main.py'
        path.write_text('# '+('source padding '*1000)+'\n'+path.read_text(encoding='utf-8'),encoding='utf-8')
        inspector=Inspector.freeze(root,root/'long-snapshots','request',['main.py'],'target','result')
        inspector.request['source_id']='source'
        arguments=dict(path='main.py',start=1,count=120)
        expected=inspector.read_file(**arguments)
        requests=[]
        summaries=[]
        job=dict(id='compacted-reads',request_id='request',status='RUNNING',rounds=0,usage=[],
            pipeline_stage='analysis',usage_by_stage=dict(analysis=[],preprocessing=[]),
            messages=[dict(role='system',content=RULES),dict(role='user',content='frozen request')])
        def send(url,key,body):
            if not body.get('tools'):
                summaries.append(copy.deepcopy(body))
                return dict(choices=[dict(finish_reason='stop',message=dict(content='Old source was read; no new permissions.'))])
            requests.append(copy.deepcopy(body))
            messages=body['messages']
            for message in messages:
                if message.get('role')!='tool':
                    continue
                reply=json.loads(message['content'])
                if reply.get('schema')=='source-read-reference/1':
                    original=[row for row in messages if row.get('role')=='tool'
                              and row.get('tool_call_id')==reply['reference']['tool_call_id']]
                    self.assertEqual(len(original),1)
                    self.assertEqual(digest(original[0]['content'].encode('utf-8')),
                                     reply['reference']['response_sha256'])
                    self.assertIn('lines',json.loads(original[0]['content']))
            number=len(requests)
            if number in (1,2,5):
                if number==5:
                    job['force_context_compaction']=True
                return self.response('read_file',arguments,'page-'+str(number))
            if number in (3,4):
                return self.response('inspect_symbols',dict(path='main.py'),'symbols-'+str(number))
            self.assertEqual(number,6)
            self.assertFalse(any(message.get('tool_call_id')=='page-1' for message in messages))
            self.assertTrue(any('historical_memory' in (message.get('content') or '')
                                for message in messages if message.get('role')=='assistant'))
            tools={message['tool_call_id']:json.loads(message['content'])
                   for message in messages if message.get('role')=='tool'}
            self.assertEqual(tools['page-2'],expected)
            self.assertEqual(tools['page-5'],expected)
            return self.response('submit_candidate',dict(candidate=self.candidate),'done')
        self.jobs.transport=SimpleNamespace(send=send)
        with patch.object(inspector,'read_file',wraps=inspector.read_file) as read:
            self.jobs._run(inspector,dict(self.profile,context_management=False),'',job,threading.Event())
        self.assertEqual(job['status'],'COMPLETED',job.get('error'))
        self.assertEqual((read.call_count,len(requests),len(summaries)),(3,6,1))
        self.assertTrue(job['context_archives'])
        archived=self.store.get('context_archive',job['context_archives'][0])
        self.assertTrue(any(message.get('tool_call_id')=='page-1' for message in archived['messages']))

    def referenced_history(self,inspector):
        arguments=dict(path='main.py',start=1,count=120)
        result=inspector.read_file(**arguments)
        full=dict(role='tool',tool_call_id='original',
            content=json.dumps(result,ensure_ascii=False,separators=(',',':')))
        seen={}
        _read_file_reply(inspector.request['source_version'],arguments,result,[],seen,'original')
        reference=_read_file_reply(inspector.request['source_version'],arguments,result,[full],seen,'reference')
        history=[dict(role='system',content=RULES),dict(role='user',content='original frozen task'),
            self.response('read_file',arguments,'original')['choices'][0]['message'],full,
            self.response('read_file',arguments,'reference')['choices'][0]['message'],
            dict(role='tool',tool_call_id='reference',content=json.dumps(reference,ensure_ascii=False,separators=(',',':')))]
        for index in range(3):
            identity='symbols-'+str(index)
            history.extend([self.response('inspect_symbols',dict(path='main.py'),identity)['choices'][0]['message'],
                dict(role='tool',tool_call_id=identity,content=json.dumps(inspector.inspect_symbols('main.py')))])
        return result,history

    def test_resume_recovers_prior_references_before_first_prepare_archives_original(self):
        root=self.inspector.root
        path=root/'main.py'
        path.write_text('# '+('source padding '*1000)+'\n'+path.read_text(encoding='utf-8'),encoding='utf-8')
        inspector=Inspector.freeze(root,root/'resume-snapshots','request',['main.py'],'target','result')
        inspector.request['source_id']='source'
        expected,history=self.referenced_history(inspector)
        self.profile['context_management']=False
        previous=dict(id='prior-run',request_id='request',status='FAILED',rounds=5,usage=[],messages=history,
            pipeline_stage='analysis',failure_phase='model_request',error_kind='context_length',submitted=None,
            context_scope='analysis/prior-run',preprocessing=dict(mode='none',status='SKIPPED'),
            model_binding=self.jobs.model_binding(self.profile),usage_by_stage=dict(analysis=[],preprocessing=[]))
        self.store.save('request','request',inspector.request)
        with patch('seampact.analysis.threading.Thread.start'):
            identity=self.jobs.start(inspector,'analysis',resume=previous,preprocessing='none')
        continued=self.store.get('analysis',identity)
        self.assertTrue(continued['force_context_compaction'])
        requests=[]
        summaries=[]
        def send(url,key,body):
            if not body.get('tools'):
                summaries.append(copy.deepcopy(body))
                return dict(choices=[dict(finish_reason='stop',message=dict(content='Historical source read; no new permissions.'))])
            requests.append(copy.deepcopy(body))
            tools={message['tool_call_id']:json.loads(message['content'])
                   for message in body['messages'] if message.get('role')=='tool'}
            self.assertNotIn('original',tools)
            self.assertEqual(tools['reference'],expected)
            self.assertFalse(any(reply.get('schema')=='source-read-reference/1' for reply in tools.values()))
            return self.response('submit_candidate',dict(candidate=self.candidate),'done')
        self.jobs.transport=SimpleNamespace(send=send)
        with patch.object(inspector,'read_file',wraps=inspector.read_file) as read:
            self.jobs._run(inspector,self.profile,'',continued,threading.Event())
        self.assertEqual(continued['status'],'COMPLETED',continued.get('error'))
        self.assertEqual((read.call_count,len(requests),len(summaries)),(0,1,1))
        self.assertEqual(previous['messages'],history)

    def test_resume_rejects_unverifiable_app_reference_before_transport(self):
        _,history=self.referenced_history(self.inspector)
        for failure in ('scope','response_digest','page','arguments','missing_original','malformed'):
            with self.subTest(failure=failure):
                messages=copy.deepcopy(history)
                reply=json.loads(messages[5]['content'])
                if failure=='scope':
                    reply['source_version']='different'
                elif failure=='response_digest':
                    reply['reference']['response_sha256']='different'
                elif failure=='page':
                    reply['next_line']=999
                elif failure=='arguments':
                    reply['reference']['arguments']['count']=121
                messages[5]['content']=json.dumps(reply,ensure_ascii=False,separators=(',',':'))
                if failure=='missing_original':
                    messages.pop(3)
                elif failure=='malformed':
                    messages[5]['content']=messages[5]['content'][:-1]
                job=dict(id='bad-'+failure,request_id='request',status='RUNNING',rounds=0,usage=[],messages=messages,
                    pipeline_stage='analysis',usage_by_stage=dict(analysis=[],preprocessing=[]))
                self.jobs.transport=SimpleNamespace(send=lambda *args:self.fail('unverifiable reference reached transport'))
                self.jobs._run(self.inspector,self.profile,'',job,threading.Event())
                self.assertEqual(job['status'],'FAILED')
                self.assertEqual(job['failure_phase'],'context_reference_recovery')
                self.assertIn('已有 read_file 引用',job['error'])

    def test_read_reference_never_substitutes_archived_summary_reference_or_ambiguous_call(self):
        arguments=dict(path='main.py')
        result=self.inspector.read_file(**arguments)
        version=self.inspector.request['source_version']
        full=dict(role='tool',tool_call_id='original',
            content=json.dumps(result,ensure_ascii=False,separators=(',',':')))
        absent=[[],[dict(role='assistant',content=full['content'])],[full,copy.deepcopy(full)]]
        for messages in absent:
            with self.subTest(messages=messages):
                seen={}
                _read_file_reply(version,arguments,result,[],seen,'original')
                self.assertIs(_read_file_reply(version,arguments,result,messages,seen,'reread'),result)
        seen={}
        _read_file_reply(version,arguments,result,[],seen,'original')
        self.assertIs(_read_file_reply(version,arguments,result,[full],seen,'original'),result)
        reference=_read_file_reply(version,arguments,result,[full],seen,'repeat')
        messages=[dict(role='tool',tool_call_id='repeat',content=json.dumps(reference,ensure_ascii=False,separators=(',',':')))]
        self.assertIs(_read_file_reply(version,arguments,result,messages,seen,'reread'),result)
        messages.append(dict(role='tool',tool_call_id='reread',content=full['content']))
        recovered=_read_file_reply(version,arguments,result,messages,seen,'after-reread')
        self.assertEqual(recovered['reference']['tool_call_id'],'reread')


if __name__=='__main__':
    unittest.main()
