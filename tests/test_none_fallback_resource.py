"""A None identity fallback needs its actual frozen value before selection."""

import copy
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from seampact.configuration.dependencies import Dependencies, check_requirements, fingerprint
from seampact.resources import ResourceRegistry
from seampact.source.inspection import Inspector
from seampact.store import Store


class NoneFallbackResourceChecks(unittest.TestCase):
    def freeze(self, assignment='checkpoint = checkpoint if checkpoint is not None else "default"', tail='',
               consumer='self.model = AutoModel.from_pretrained(checkpoint)'):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        source = ('from transformers import AutoModel\nclass Host:\n'
            '    def __init__(self, checkpoint=None):\n        '+assignment+'\n'
            '        '+tail+'\n        '+consumer+'\n'
            '    def run(self, value: str) -> str:\n        return value\n')
        original=root/'original'
        original.mkdir()
        (original/'main.py').write_text(source,encoding='utf-8')
        inspector=Inspector.freeze(original,root/'snapshots','request',['main.py'],'Host.run','original')
        inspector.request['source_id']='source'
        candidate=dict(entries=[dict(id='target',path='main.py',symbol='Host.run',kind='method',
            constructor=dict(symbol='Host',arguments=dict(checkpoint={'$dependency':dict(id='weights',field='path')})))],
            dependencies=[dict(id='weights',kind='weight',configurable=True,required=True,entry_ids=['target'],
                read_time='constructor',owner_scope='original owner',
                evidence=[dict(path='main.py',start=1,end=len(source.splitlines()))],
                mappings=[dict(entry_id='target',location='constructor.arguments',parameter='checkpoint',field='path')])])
        return root,inspector,candidate

    def frozen(self, inspector, candidate, value):
        request=copy.deepcopy(inspector.request)
        request['dependency_bindings']={'weights':dict(origin_id='weights',source_id='source',status='PROPOSED',
            source_version=request['source_version'],
            definition_digest=fingerprint(candidate['dependencies'][0]),resource_id='registered',
            resource=dict(id='registered',path=value,revision=1,config_digest='fixture',
                check=dict(status='present',revision=1,config_digest='fixture',
                    identity=dict(basis='checked_file_sha256'),files=[dict(checksum_matches=True)])))}
        return Inspector(inspector.root,inspector.snapshots,request)

    def test_unselected_reference_schema_and_flag_do_not_prove_a_string(self):
        _,inspector,candidate=self.freeze()
        candidate['entries'][0]['non_none']=True
        row=check_requirements(inspector,candidate)[0]
        self.assertEqual(row['status'],'MISSING')
        self.assertEqual(row['entry_checks']['target']['issues'][0]['issue_code'],'frozen_non_none_value_required')
        self.assertFalse(row['selection_allowed'])

    def test_both_identity_guard_forms_require_the_same_actual_string(self):
        assignments=('checkpoint = checkpoint if checkpoint is not None else "default"',
                     'checkpoint = "default" if checkpoint is None else checkpoint')
        for assignment in assignments:
            with self.subTest(assignment=assignment):
                _,inspector,candidate=self.freeze(assignment)
                row=check_requirements(self.frozen(inspector,candidate,'/registered/model'),candidate)[0]
                self.assertEqual(row['status'],'MAPPING_CHECKED',row.get('reason'))
                proof=row['entry_checks']['target']['mapping_proofs'][0]['proof']['trace'][0]
                self.assertEqual(proof['rule'],'source-non-none-identity-fallback/1')
                self.assertIn('no lifecycle',proof['scope'])

    def test_none_nonstring_wrong_definition_and_other_rewrites_stay_missing(self):
        _,inspector,candidate=self.freeze()
        class StringSubclass(str):
            pass
        for value in (None,False,1,[],{},StringSubclass('/model')):
            with self.subTest(value=value):
                self.assertEqual(check_requirements(self.frozen(inspector,candidate,value),candidate)[0]['status'],'MISSING')
        frozen=self.frozen(inspector,candidate,'/model')
        frozen.request['dependency_bindings']['weights']['definition_digest']='wrong'
        self.assertEqual(check_requirements(frozen,candidate)[0]['status'],'MISSING')
        for version in (None,'another-source-version'):
            frozen=self.frozen(inspector,candidate,'/model')
            frozen.request['dependency_bindings']['weights']['source_version']=version
            self.assertEqual(check_requirements(frozen,candidate)[0]['status'],'MISSING')
        for status in (None,'UNSELECTED','FAILED'):
            frozen=self.frozen(inspector,candidate,'/model')
            frozen.request['dependency_bindings']['weights']['status']=status
            self.assertEqual(check_requirements(frozen,candidate)[0]['status'],'MISSING')
        for assignment,tail in (
            ('checkpoint = checkpoint if checkpoint != None else "default"',''),
            ('checkpoint = checkpoint if checkpoint else "default"',''),
            ('checkpoint = checkpoint+"x" if checkpoint is not None else "default"',''),
            ('checkpoint = checkpoint if checkpoint is not None else "default"','checkpoint = "other"'),
        ):
            _,inspector,candidate=self.freeze(assignment,tail)
            self.assertEqual(check_requirements(self.frozen(inspector,candidate,'/model'),candidate)[0]['status'],'MISSING')

    def test_loader_only_in_the_unselected_fallback_is_not_a_consumer(self):
        _,inspector,candidate=self.freeze(
            'checkpoint = checkpoint if checkpoint is not None else AutoModel.from_pretrained(checkpoint)',
            consumer='self.model = None')
        self.assertEqual(check_requirements(self.frozen(inspector,candidate,'/model'),candidate)[0]['status'],'MISSING')

    def fixture_selection(self, mixed=False):
        root,inspector,candidate=self.freeze()
        if mixed:
            source=inspector.root/'main.py'
            source.write_text(source.read_text(encoding='utf-8')+'\nclass Other:\n'
                '    def __init__(self, checkpoint):\n        self.model = AutoModel.from_pretrained(checkpoint)\n'
                '    def run(self, value: str) -> str:\n        return value\n',encoding='utf-8')
            inspector=Inspector.freeze(inspector.root,root/'snapshots','mixed',['main.py'],'Host.run','original')
            inspector.request['source_id']='source'
            other=copy.deepcopy(candidate['entries'][0])
            other.update(id='other',symbol='Other.run')
            other['constructor']['symbol']='Other'
            candidate['entries'].append(other)
            row=candidate['dependencies'][0]
            row['entry_ids'].append('other')
            row['mappings'].append(dict(row['mappings'][0],entry_id='other'))
            row['evidence'][0]['end']=len(source.read_text(encoding='utf-8').splitlines())
        store=Store(':memory:')
        self.addCleanup(store.close)
        profiles=SimpleNamespace(private_directory=None)
        registry=ResourceRegistry(root/'platform',store,profiles)
        weights=root/'weights'
        weights.mkdir()
        data=b'original weights'
        (weights/'model.bin').write_bytes(data)
        resource=registry.save_weight(dict(name='original',path=str(weights),required_files=[
            dict(path='model.bin',sha256=hashlib.sha256(data).hexdigest())]))
        registry.check_weight(resource['id'])
        dependencies=Dependencies(store,profiles,registry)
        dependencies.record(inspector,candidate)
        body=dict(harness_id='source',dependency_id='source:weights',resource_id=resource['id'])
        return inspector,candidate,store,dependencies,body

    def test_two_phase_selection_checks_unpublished_frozen_value_then_commits(self):
        inspector,candidate,store,dependencies,body=self.fixture_selection()
        original=copy.deepcopy(inspector.request)
        def validate(proposed):
            self.assertEqual(proposed['status'],'PROPOSED')
            self.assertIsNone(store.get('dependency_binding',body['dependency_id']))
            request=copy.deepcopy(inspector.request)
            request['dependency_bindings']={'weights':proposed}
            frozen=Inspector(inspector.root,inspector.snapshots,request)
            return check_requirements(frozen,candidate)[0]
        selected=dependencies.select(body,validate_selection=validate)
        self.assertEqual(selected['status'],'SELECTED')
        self.assertEqual(store.get('dependency_binding',body['dependency_id']),selected)
        self.assertEqual(inspector.request,original)
        self.assertEqual(store.get('resource_dependency',body['dependency_id'])['status'],'MAPPING_CHECKED')
        self.assertEqual(dependencies.selections('source')['weights'],selected)

    def test_failed_or_mismatched_proposal_does_not_publish_selection(self):
        for change in ('incomplete_mapping','different_source','different_definition'):
            inspector,candidate,store,dependencies,body=self.fixture_selection()
            def validate(proposed):
                request=copy.deepcopy(inspector.request)
                request['dependency_bindings']={'weights':proposed}
                row=check_requirements(Inspector(inspector.root,inspector.snapshots,request),candidate)[0]
                if change=='incomplete_mapping': row['status']='MISSING'
                if change=='different_source': row['source_version']='other'
                if change=='different_definition': row['definition_digest']='other'
                return row
            with self.assertRaises(ValueError): dependencies.select(body,validate_selection=validate)
            self.assertIsNone(store.get('dependency_binding',body['dependency_id']))

    def test_other_mapping_failures_cannot_use_provisional_selection(self):
        _,_,store,dependencies,body=self.fixture_selection()
        row=store.get('resource_dependency',body['dependency_id'])
        row['entry_checks']['target']['issues'][0]['issue_code']='parameter_flow_unsupported'
        store.save('resource_dependency',row['id'],row)
        with self.assertRaises(ValueError):
            dependencies.select(body,validate_selection=lambda _:self.fail('unrelated mapping gap'))
        self.assertIsNone(store.get('dependency_binding',body['dependency_id']))

    def test_unchecked_file_identity_never_starts_proposed_binding(self):
        _,_,store,dependencies,body=self.fixture_selection()
        resource=store.get('weight_resource',body['resource_id'])
        resource['check']['config_digest']='stale'
        store.save('weight_resource',resource['id'],resource)
        with self.assertRaises(ValueError):
            dependencies.select(body,validate_selection=lambda _:self.fail('stale identity must stop locally'))
        self.assertIsNone(store.get('dependency_binding',body['dependency_id']))

    def test_a_later_selection_rechecks_the_new_value_and_actual_files(self):
        inspector,candidate,store,dependencies,body=self.fixture_selection()
        def validate(proposed):
            request=copy.deepcopy(inspector.request)
            request['dependency_bindings']={'weights':proposed}
            return check_requirements(Inspector(inspector.root,inspector.snapshots,request),candidate)[0]
        selected=dependencies.select(body,validate_selection=validate)
        before=copy.deepcopy(store.get('resource_dependency',body['dependency_id']))
        other=Path(selected['resource']['path']).parent/'unchecked'
        other.mkdir()
        (other/'model.bin').write_bytes(b'unchecked')
        unverified=dependencies.resources.save_weight(dict(name='unchecked',path=str(other),required_files=[
            dict(path='model.bin',sha256=hashlib.sha256(b'unchecked').hexdigest())]))
        with self.assertRaises(ValueError):
            dependencies.select(dict(body,resource_id=unverified['id']),
                validate_selection=lambda _:self.fail('unchecked alternate must stop before mapping'))
        self.assertEqual(store.get('dependency_binding',body['dependency_id']),selected)
        path=Path(selected['resource']['path'])/'model.bin'
        path.write_bytes(b'changed original file')
        with self.assertRaises(ValueError):
            dependencies.select(body,validate_selection=lambda _:self.fail('changed bytes stop before mapping'))
        self.assertEqual(store.get('dependency_binding',body['dependency_id']),selected)
        self.assertEqual(store.get('resource_dependency',body['dependency_id']),before)

    def test_partial_row_with_one_regular_mapping_still_requires_frozen_value_preflight(self):
        inspector,candidate,store,dependencies,body=self.fixture_selection(mixed=True)
        before=store.get('resource_dependency',body['dependency_id'])
        self.assertTrue(before['selection_allowed'])
        self.assertEqual(before['entry_checks']['target']['status'],'MISSING')
        self.assertEqual(before['entry_checks']['other']['status'],'MAPPING_CHECKED')
        checked=[]
        def validate(proposed):
            checked.append(proposed)
            request=copy.deepcopy(inspector.request)
            request['dependency_bindings']={'weights':proposed}
            return check_requirements(Inspector(inspector.root,inspector.snapshots,request),candidate)[0]
        selected=dependencies.select(body,validate_selection=validate)
        self.assertEqual(len(checked),1)
        self.assertEqual(checked[0]['status'],'PROPOSED')
        self.assertEqual(selected['status'],'SELECTED')

    def test_resource_configuration_change_during_mapping_cannot_publish_old_value(self):
        inspector,candidate,store,dependencies,body=self.fixture_selection()
        before=copy.deepcopy(store.get('resource_dependency',body['dependency_id']))
        def validate(proposed):
            request=copy.deepcopy(inspector.request)
            request['dependency_bindings']={'weights':proposed}
            checked=check_requirements(Inspector(inspector.root,inspector.snapshots,request),candidate)[0]
            dependencies.resources.save_weight(dict(id=proposed['resource_id'],model_revision='changed'))
            return checked
        with self.assertRaisesRegex(ValueError,'configuration changed'):
            dependencies.select(body,validate_selection=validate)
        self.assertEqual(store.get('resource_dependency',body['dependency_id']),before)
        self.assertIsNone(store.get('dependency_binding',body['dependency_id']))

    def test_new_mismatch_observation_during_mapping_blocks_publication(self):
        inspector,candidate,store,dependencies,body=self.fixture_selection()
        before=copy.deepcopy(store.get('resource_dependency',body['dependency_id']))
        def validate(proposed):
            request=copy.deepcopy(inspector.request)
            request['dependency_bindings']={'weights':proposed}
            checked=check_requirements(Inspector(inspector.root,inspector.snapshots,request),candidate)[0]
            (Path(proposed['resource']['path'])/'model.bin').write_bytes(b'changed during mapping')
            dependencies.resources.check_weight(proposed['resource_id'])
            return checked
        with self.assertRaises(ValueError):
            dependencies.select(body,validate_selection=validate)
        self.assertEqual(store.get('resource_dependency',body['dependency_id']),before)
        self.assertIsNone(store.get('dependency_binding',body['dependency_id']))

    def test_normal_platform_binding_uses_the_unmodified_candidate_and_no_model(self):
        from seampact.service import Platform, RULESET, NATIVE_PROFILE
        root,original,candidate=self.freeze()
        platform=Platform(root/'platform')
        self.addCleanup(platform.store.close)
        self.addCleanup(platform.runtime.close)
        self.addCleanup(platform.analysis.close)
        source_id=platform.add_harness(dict(address=str(original.root)))['id']
        source=platform.store.get('harness',source_id)
        inspector=Inspector.freeze_repository(source,platform.home/'snapshots','frozen-request','original')
        inspector.request.update(rules=RULESET,entry_profile=NATIVE_PROFILE,resource_catalog=platform.resource_catalog(),
            dependency_bindings={},preprocessing_mode='none')
        candidate.update(boundaries=[],runtime_groups=[dict(id='whole',entries=['target'])])
        candidate_before=copy.deepcopy(candidate)
        report=platform.analysis._check_candidate(inspector,candidate)
        job=dict(id='automatic-fixture',request_id=inspector.request['id'],status='COMPLETED',
            submitted=dict(candidate=candidate,report=report),model_calls=0,rounds=0,usage=[],messages=[])
        source.update(analysis_id=job['id'],request_id=inspector.request['id'],status='ANALYZED')
        platform.store.save('request',inspector.request['id'],inspector.request)
        platform.store.save('harness',source_id,source)
        platform.store.save('analysis',job['id'],job)
        platform._connect_units(inspector,job)
        forged=dict(origin_id='weights',source_id=source_id,status='PROPOSED',
            source_version=inspector.request['source_version'],
            definition_digest=fingerprint(candidate['dependencies'][0]),resource_id='foreign',
            resource=dict(id='foreign',path='/foreign/model'))
        artifact=dict(schema='seampact-analysis-export/1',rules=RULESET['id'],entry_profile=NATIVE_PROFILE['id'],
            source_version=inspector.request['source_version'],candidate=copy.deepcopy(candidate),
            dependency_bindings={'weights':forged},resource_catalog=dict(weights=[forged['resource']]))
        imported=platform.import_analysis(dict(harness_id=source_id,artifact=artifact))
        imported_job=platform.store.get('analysis',imported['analysis_id'])
        imported_request=platform.store.get('request',imported_job['request_id'])
        self.assertEqual(imported_request['dependency_bindings'],{})
        self.assertFalse(any(w.get('id')=='foreign' for w in imported_request['resource_catalog']['weights']))
        self.assertEqual(platform.store.get('resource_dependency',source_id+':weights')['status'],'MISSING')
        weights=root/'normal-weights'
        weights.mkdir()
        data=b'fixed original resource'
        (weights/'model.bin').write_bytes(data)
        weight=platform.resources.save_weight(dict(name='original',path=str(weights),required_files=[
            dict(path='model.bin',sha256=hashlib.sha256(data).hexdigest())]))
        platform.resources.check_weight(weight['id'])
        result=platform.bind_dependency(dict(harness_id=source_id,dependency_id=source_id+':weights',resource_id=weight['id']))
        self.assertEqual(result['model_calls'],0)
        saved=platform.store.get('analysis',result['analysis_id'])
        self.assertEqual(saved['submitted']['candidate'],candidate_before)
        self.assertEqual(saved['status'],'COMPLETED')
        self.assertEqual(result['binding']['status'],'SELECTED')
        frozen=platform.store.get('request',saved['request_id'])
        self.assertEqual(frozen['dependency_bindings']['weights'],result['binding'])
        row=platform.store.get('resource_dependency',source_id+':weights')
        self.assertEqual(row['status'],'MAPPING_CHECKED')
        self.assertEqual(row['definition_digest'],result['binding']['definition_digest'])
        self.assertEqual(platform.dependencies.selections(source_id)['weights'],result['binding'])
        self.assertEqual(saved['binding_report']['entries'][0]['dependency_closure']['status'],'CHECKED')
        from seampact.configuration.dependencies import DependencyBindings
        from seampact.units import NativeBindings
        provisioned=Inspector(source['root'],platform.home/'snapshots',frozen)
        binding=NativeBindings(provisioned,candidate,None,
            resource_binding=DependencyBindings(platform.dependencies,provisioned,candidate))
        self.assertEqual(binding._resource_values(candidate['entries'][0],'constructor.arguments',
            candidate['entries'][0]['constructor']['arguments']),dict(checkpoint=str(weights)))


if __name__=='__main__':
    unittest.main()
