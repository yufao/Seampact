"""Checked original imports are paid setup, never owner construction or a call."""
import sys
import threading
import unittest
import queue
import time
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parents[1] / 'src'))
from seampact.hosting.deployment import Deployments
from seampact.runtime import Coordinator, OwnerRuntime
from seampact.scheduling.residency import WorkingSets
from seampact.scheduling.joint import Window
from seampact.scenes.scenarios import SceneSession
from seampact.source.import_only import compile_import_only_plan
from seampact.source.inspection import Inspector
from seampact.store import Store
from seampact.units import UnitCatalog
from seampact.worker import Worker
import test_activation_prefix as prefix_fixture
import test_call_value_conditions as condition_fixture


IDENTITY = dict(session='s', instance_id='i', generation=1, process_nonce='n')


class ImportOnlyControlTests(unittest.TestCase):
    def test_worker_plan_is_bound_to_frozen_facts_and_replayed_before_import(self):
        from seampact.source.call_value_conditions import digest
        import copy
        catalog, unit, worker = self.catalog('def score(value, option=None):\n'
            '    assert isinstance(value,list)\n    return 7\n')
        binding, _ = catalog.deferred[unit['id']]
        plan = catalog.require_import_only_plan(unit['id'])
        self.assertEqual(catalog.validate_worker_import_plan(unit['id'], plan), plan)
        self.assertEqual(binding.modules, {})
        for key in ('source_version', 'entry_digest', 'candidate_digest', 'resource_digest'):
            changed = copy.deepcopy(plan)
            changed[key] = 'different'
            changed['plan_digest'] = digest({k: v for k, v in changed.items() if k != 'plan_digest'})
            with self.subTest(key=key), self.assertRaises(ValueError):
                catalog.validate_worker_import_plan(unit['id'], changed)
        # A receiver's native-JSON proposal is never enough to execute a prefix.
        answer = worker.import_only(dict(unit=unit['id'], plan_digest='forged', **IDENTITY))
        self.assertEqual(answer['status'], 'FAILED')
        self.assertEqual(binding.modules, {})
        self.assertEqual(binding.owners, {})

    def catalog(self, source, *, symbol='score', kind='function', selected_boundary=False):
        fixture = prefix_fixture.ActivationPrefixTests('test_parent_package_runs_before_child_and_is_not_repeated')
        self.addCleanup(fixture.doCleanups)
        inspector, entry, candidate = fixture.fixture(
            {'pkg/__init__.py': 'FLAG="ready"\n', 'pkg/child.py': source},
            'pkg/child.py', symbol=symbol, kind=kind)
        inspector.request['source_id'] = 'source'
        if selected_boundary:
            entry['boundary_ids'] = ['whole']
            candidate['boundaries'] = [dict(id='whole', mode='W', path='pkg/child.py', symbol=symbol)]
        store = Store(':memory:')
        self.addCleanup(store.close)
        runtime = OwnerRuntime(Coordinator(store))
        self.addCleanup(runtime.close)
        catalog = UnitCatalog(inspector.root, store, runtime)
        unit = catalog.install(inspector, candidate, 'analysis', 'installation')[0]
        self.assertEqual(unit['status'], 'CALL_CONDITION_DEFERRED', unit.get('reason'))
        catalog._worker_call_identity = dict(IDENTITY)
        worker = Worker.__new__(Worker)
        worker.catalog, worker.store, worker.runtime = catalog, store, runtime
        worker.session, worker.instance, worker.generation, worker.process_nonce = IDENTITY.values()
        worker.units, worker.accepting, worker.lock = {unit['id']}, True, threading.Lock()
        worker.in_flight, worker.preparing = 0, 0
        return catalog, unit, worker

    def test_function_endpoint_requires_real_full_claim_then_checks_actual_default(self):
        catalog, unit, worker = self.catalog('def score(value, option=None):\n'
            '    assert isinstance(value,list)\n    return 7\n')
        binding, _ = catalog.deferred[unit['id']]
        cold = worker.check_call(dict(unit=unit['id'], arguments={'value': []}, **IDENTITY))
        self.assertEqual(cold['reason_code'], 'DEFAULT_BINDING_UNKNOWN')
        plan = catalog.require_import_only_plan(unit['id'])
        self.assertEqual(plan['modules_order'], ['pkg/__init__.py', 'pkg/child.py'])
        self.assertEqual(binding.modules, {})
        group = unit['runtime_group']
        working = WorkingSets(dict(pinned=[], tasks={'task': []}, allowed={'task': [unit['id']]},
            calls={unit['id']: [group]}), {'ram': 1}, {group: {'ram': 1}})
        router = Deployments.__new__(Deployments)
        router.lock, router.run_locks = threading.RLock(), {}
        record = dict(id='i', group_id=group, state='READY', accepting=True, pending_stop=None,
                      in_flight=0, units=[unit['id']], endpoint='http://unused',
                      session='s', generation=1, process_nonce='n')
        router.instances = {'i': record}
        router._group = lambda identity: {'units': [unit['id']]}
        router._event = lambda *args, **kwargs: None
        claim = lambda: working.covers('context', 'task', [unit['id']])
        request = dict(unit=unit['id'], instance_id='i', instance_identity=IDENTITY,
                       plan_digest=plan['plan_digest'], task_id='context')
        with self.assertRaises(ValueError):
            router.import_only(group, request, run_id='run', claim_check=claim)
        self.assertEqual(binding.modules, {})
        self.assertTrue(working.admit('context', 'task', units=[unit['id']]))
        with patch('seampact.hosting.deployment.json_request', side_effect=lambda endpoint, body: worker.import_only(body)), \
             patch.object(binding, '_load', side_effect=AssertionError('owner load is forbidden')), \
             patch.object(binding, 'prepare', side_effect=AssertionError('prewarm is forbidden')):
            answer = router.import_only(group, request, run_id='run', claim_check=claim)
            self.assertEqual(answer['status'], 'IMPORTED', answer)
            checked = worker.check_call(dict(unit=unit['id'], arguments={'value': []}, **IDENTITY))
        self.assertTrue(checked['allowed'], checked)
        self.assertEqual(binding.owners, {})
        self.assertEqual(binding.entries, {})
        self.assertEqual(set(binding.modules), {'pkg', 'pkg.child'})
        self.assertEqual(catalog.invoke(unit['id'], {'value': []}), 7)

    def test_original_unbound_method_default_is_checked_without_owner(self):
        catalog, unit, worker = self.catalog('class Checker:\n'
            '    def __init__(self):\n        self.token="ready"\n'
            '    def score(self,value,option=None):\n        assert isinstance(value,list)\n        return 7\n',
            symbol='Checker.score', kind='method')
        binding, _ = catalog.deferred[unit['id']]
        plan = catalog.require_import_only_plan(unit['id'])
        with patch.object(binding, '_load', side_effect=AssertionError('owner load is forbidden')):
            answer = worker.import_only(dict(unit=unit['id'], plan_digest=plan['plan_digest'], **IDENTITY))
            checked = worker.check_call(dict(unit=unit['id'], arguments={'value': []}, **IDENTITY))
        self.assertEqual(answer['status'], 'IMPORTED', answer)
        self.assertTrue(checked['allowed'], checked)
        self.assertEqual(binding.owners, {})
        self.assertEqual(binding.entries, {})
        self.assertEqual(catalog.invoke(unit['id'], {'value': []}), 7)

    def test_method_descriptor_and_default_unknowns_do_not_run_hooks(self):
        for field in ('descriptor', 'default'):
            with self.subTest(field=field):
                catalog, unit, worker = self.catalog('class Checker:\n'
                    '    def score(self,value,option=None):\n        assert isinstance(value,list)\n        return 7\n',
                    symbol='Checker.score', kind='method')
                plan = catalog.require_import_only_plan(unit['id'])
                self.assertEqual(worker.import_only(dict(unit=unit['id'], plan_digest=plan['plan_digest'], **IDENTITY))['status'], 'IMPORTED')
                binding, _ = catalog.deferred[unit['id']]
                original = binding.import_only_entries[unit['id']]['class_']
                hits = []
                class Meta(type):
                    def __eq__(cls, value): hits.append('meta_eq'); return True
                class Unknown(metaclass=Meta):
                    def __getattribute__(self, name): hits.append('object_attr'); raise AssertionError(name)
                if field == 'descriptor':
                    original.score = Unknown()
                else:
                    original.__dict__['score'].__defaults__ = (Unknown(),)
                checked = worker.check_call(dict(unit=unit['id'], arguments={'value': []}, **IDENTITY))
                self.assertFalse(checked['allowed'])
                self.assertEqual(hits, [])

    def test_original_function_gate_keeps_import_receipt_valid_for_repeated_calls(self):
        catalog, unit, worker = self.catalog('def score(value, option=None):\n'
            '    assert isinstance(value,list)\n    return 7\n', selected_boundary=True)
        plan = catalog.require_import_only_plan(unit['id'])
        self.assertEqual(worker.import_only(dict(unit=unit['id'], plan_digest=plan['plan_digest'], **IDENTITY))['status'], 'IMPORTED')
        self.assertEqual(catalog.invoke(unit['id'], {'value': []}), 7)
        self.assertTrue(catalog.check_call_conditions(unit['id'], {'value': []})['allowed'])
        self.assertEqual(catalog.invoke(unit['id'], {'value': []}), 7)

    def test_unverified_sdk_and_download_prefix_never_imports(self):
        fixture = prefix_fixture.ActivationPrefixTests('test_parent_package_runs_before_child_and_is_not_repeated')
        self.addCleanup(fixture.doCleanups)
        files = {'pkg/__init__.py': "import sdk\ntry:\n sdk.find('resource')\nexcept LookupError:\n sdk.download('resource')\n",
                 'pkg/child.py': 'def score(value,option=None):\n assert isinstance(value,list)\n return 7\n'}
        inspector, entry, candidate = fixture.fixture(files, 'pkg/child.py')
        plan = compile_import_only_plan(inspector, entry, candidate, dict(requirements=[], bindings={}))
        self.assertEqual(plan['status'], 'UNKNOWN')
        self.assertNotIn('sdk', sys.modules)

    def test_normal_selected_intent_reaches_real_worker_import_and_recheck(self):
        self._selected_intent_worker_flow()

    def test_worker_local_plan_crosses_the_normal_router_and_full_claim(self):
        # Simulate the missing-SDK preparation marker, then use the real source
        # worker, plan validation, resource ledger and original import loader.
        self._selected_intent_worker_flow(worker_plan=True)

    def _selected_intent_worker_flow(self, worker_plan=False):
        worker_catalog, unit, worker = self.catalog('def score(value, option=None):\n'
            '    assert isinstance(value,list)\n    return 7\n')
        worker_binding, _ = worker_catalog.deferred[unit['id']]
        store = Store(':memory:')
        self.addCleanup(store.close)
        runtime = OwnerRuntime(Coordinator(store))
        self.addCleanup(runtime.close)
        controller = UnitCatalog(worker_binding.inspector.root, store, runtime)
        source = Inspector(worker_binding.inspector.root, worker_binding.inspector.snapshots, worker_binding.inspector.request)
        installed = controller.install(source, worker_binding.candidate, 'analysis', 'installation')[0]
        self.assertEqual(installed['id'], unit['id'])
        group = unit['runtime_group']
        record = dict(id='i', group_id=group, state='READY', accepting=True, pending_stop=None,
            in_flight=0, units=[unit['id']], endpoint='http://unused',
            session='s', generation=1, process_nonce='n')
        router = Deployments.__new__(Deployments)
        router.lock, router.run_locks, router.instances = threading.RLock(), {}, {'i': record}
        router._group = lambda identity: {'units': [unit['id']]}
        router._event = lambda *args, **kwargs: None
        controller.remote_router = router
        class Catalog:
            placement = {unit['id']: dict(kind='REMOTE', group_id=group)}
            def dispatch_status(self, identity): return dict(status='READY')
            def check_call_conditions(self, identity, arguments): return controller.check_call_conditions(identity, arguments)
            def require_call_condition_structure(self, identity): return controller.require_call_condition_structure(identity)
            def require_import_only_plan(self, identity, proposal=None):
                return (controller.validate_worker_import_plan(identity, proposal) if proposal is not None
                        else controller.require_import_only_plan(identity))
        catalog = Catalog()
        scene = SimpleNamespace(preview_calls=lambda candidate: [dict(unit_id=unit['id'], arguments={'value': []})])
        session = SceneSession.__new__(SceneSession)
        session.catalog, session.environment = catalog, scene
        session.context = SimpleNamespace(catalog=catalog)
        candidate = dict(id='call', action_type='invoke', legal=True, units=[unit['id']], features=[])
        task = SimpleNamespace(index=0, task={'id': 'task'}, context='task-context', session=session,
            event=dict(candidates=[candidate], observation=[], references={}), committed_units=set(), decisions=[],
            status='DECISION', inbox=queue.Queue(), pending_action=None, pending_units=[],
            pending_condition_groups=set(), pending_condition_signatures=None)
        window = Window.__new__(Window)
        window.tasks, window.service_groups, window.rid = [task], set(), 'run'
        window.config = dict(functional_policy='required_only', capacity=dict(resources={'ram': 1}, replica_cap=1))
        window.working = WorkingSets(dict(pinned=[], tasks={'task': []}, allowed={'task': [unit['id']]},
            calls={unit['id']: [group]}), {'ram': 1}, {group: {'ram': 1}})
        window.pending_groups, window.reserved, window.stopping = set(), {}, set()
        window.ops, window.op_walls, window.events = [], {}, queue.Queue()
        window.start, window.closing, window.unknown_outcomes = time.time(), None, []
        window.groups, window.deployments = {group: {}}, router
        window.platform = SimpleNamespace(managed_services=SimpleNamespace(acquire=lambda *args, **kw: None))
        window._record, window._count = lambda **row: row, lambda *args: None
        window._operation_allowed = lambda *args: True
        window._services_ready = lambda *args: True
        views = {group: dict(instances=[record], operations={'start': 'SUPPORTED'}, start_gap=None)}
        window._state = lambda: (views, [])
        window._choose = lambda *args: (0, 'single', 0, 0)
        def request(endpoint, body, **kwargs):
            if endpoint.endswith('/check_call'):
                answer = worker.check_call(body)
                if worker_plan and answer.get('reason_code') == 'DEFAULT_BINDING_UNKNOWN':
                    answer.update(reason_code='SDK_BINDING_UNKNOWN',
                                  import_plan=worker_catalog.require_import_only_plan(unit['id']))
                return answer
            if endpoint.endswith('/import_only'):
                return worker.import_only(body)
            raise AssertionError('unexpected original call')
        with patch('seampact.hosting.deployment.json_request', side_effect=request), \
             patch.object(worker_binding, '_load', side_effect=AssertionError('owner load is forbidden')):
            window._business(task)
            self.assertFalse(candidate['decision_legal'])
            window._admit_tasks()
            self.assertEqual(window.working.claims['task-context'], {group})
            window._on_demand()
            submission = window.events.get(timeout=15)
            self.assertEqual(submission[0], 'op_submitted')
            window._handle(submission)
            self.assertEqual(window.ops[0]['mode'], 'import_only')
            self.assertEqual(window.ops[0]['status'], 'DONE')
            self.assertTrue(task.inbox.empty())
            window._admit_tasks()
            self.assertEqual(task.inbox.get_nowait(), 0)
        self.assertEqual(worker_binding.owners, {})
        self.assertEqual(worker_binding.entries, {})

    def test_generator_target_code_default_and_global_changes_are_unknown(self):
        source = 'LIMIT=2\ndef pieces(value,n=2):\n    yield n+LIMIT\n' \
                 'def score(value):\n    result=[item for item in pieces(value)]\n' \
                 '    assert isinstance(result,list)\n    return result\n'
        for mutation in ('binding', 'code', 'default', 'global'):
            with self.subTest(mutation=mutation):
                fixture = condition_fixture.CallValueConditionTests('test_catalog_and_worker_require_actual_same_call_before_source_load')
                self.addCleanup(fixture.doCleanups)
                _, _, _, catalog, unit = fixture.catalog(source)
                catalog._worker_call_identity = dict(IDENTITY)
                before = catalog.check_call_conditions(unit['id'], {'value': ['a']})
                self.assertEqual(before['reason_code'], 'GENERATOR_BINDING_UNKNOWN')
                plan = catalog.require_import_only_plan(unit['id'])
                catalog.prepare_import_only(unit['id'], plan['plan_digest'])
                self.assertTrue(catalog.check_call_conditions(unit['id'], {'value': ['a']})['allowed'])
                binding, _ = catalog.deferred[unit['id']]
                namespace = binding.import_only_entries[unit['id']]['namespace']
                original = namespace['pieces']
                if mutation == 'binding':
                    namespace['pieces'] = lambda value, n=2: iter([n])
                elif mutation == 'code':
                    original.__code__ = original.__code__.replace(co_consts=original.__code__.co_consts + (99,))
                elif mutation == 'default':
                    original.__defaults__ = (3,)
                else:
                    namespace['LIMIT'] = 3
                self.assertFalse(catalog.check_call_conditions(unit['id'], {'value': ['a']})['allowed'])

    def test_mutable_generator_default_remains_unknown_after_in_place_append(self):
        source = 'def pieces(value,state=[]):\n    yield value\n' \
                 'def score(value):\n    result=[item for item in pieces(value)]\n' \
                 '    assert isinstance(result,list)\n    return result\n'
        fixture = condition_fixture.CallValueConditionTests('test_catalog_and_worker_require_actual_same_call_before_source_load')
        self.addCleanup(fixture.doCleanups)
        _, _, _, catalog, unit = fixture.catalog(source)
        catalog._worker_call_identity = dict(IDENTITY)
        self.assertFalse(catalog.check_call_conditions(unit['id'], {'value': ['a']})['allowed'])
        plan = catalog.require_import_only_plan(unit['id'])
        catalog.prepare_import_only(unit['id'], plan['plan_digest'])
        binding, _ = catalog.deferred[unit['id']]
        namespace = binding.import_only_entries[unit['id']]['namespace']
        namespace['pieces'].__defaults__[0].append('changed')
        self.assertFalse(catalog.check_call_conditions(unit['id'], {'value': ['a']})['allowed'])


if __name__ == '__main__':
    unittest.main()
