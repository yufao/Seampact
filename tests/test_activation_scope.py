"""Complete activation bounds never inherit negative-check assumptions."""
import ast
import copy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from seampact.source.activation_scope import ActivationScope, RULE, GAP, verify_activation_targets
from seampact.source.external_source_protocols import ExternalSourceProtocols
from seampact.source.inspection import Inspector
from seampact.source.call_value_conditions import Context, contextual, digest
from seampact.source.dependency_closure import resolve_resource_calls


class ActivationScopeTests(unittest.TestCase):
    def fixture(self, files, path='scope_entry.py'):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        for name, text in files.items():
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding='utf-8')
        inspector = Inspector.freeze(root, root / 'snapshots', 'scope', list(files), 'score', 'result')
        entry = dict(id='score', kind='function', path=path, symbol='score')
        return inspector, entry, dict(entries=[entry], boundaries=[])

    def test_independent_complete_upper_bound_excludes_unactivated_source_by_edges(self):
        inspector, entry, candidate = self.fixture({
            'scope_entry.py': 'from scope_lib import helper\ndef score(value):\n    return value\n',
            'scope_lib.py': 'def helper(value):\n    return value\n',
            'unactivated_class.py': 'class Outside:\n    pass\ndef unused():\n    return isinstance(None,Outside)\n',
            'arbitrary/place.py': 'import builtins\nbuiltins.isinstance = changed\n',
            'another_fragment.py': '    invalid fragment\n'})
        with patch.object(ExternalSourceProtocols, 'unmodified', side_effect=AssertionError('negative cycle')):
            scope = ActivationScope(inspector, entry, candidate)
        self.assertTrue(scope.closed, scope.frontiers)
        self.assertEqual(scope.paths(), ('scope_entry.py', 'scope_lib.py'))
        protocols = ExternalSourceProtocols(inspector)
        self.assertFalse(protocols.type_references.builtins_closed())
        protocols.bind_activation(entry, candidate)
        self.assertTrue(protocols.type_references.builtins_closed())
        self.assertEqual(protocols.type_references.source_paths(), scope.paths())
        outside_tree = protocols.tree('unactivated_class.py')
        outside_call = next(node for node in ast.walk(outside_tree) if isinstance(node, ast.Call))
        self.assertEqual(protocols.type_references.check('unactivated_class.py', outside_call,
            outside_call.args[1])['status'], 'UNKNOWN')
        self.assertFalse(scope.requirements()['execution_permission'])

    def test_all_dormant_bodies_branches_and_parent_initializers_are_included(self):
        inspector, entry, candidate = self.fixture({
            'pkg/__init__.py': 'FLAG="original"\n',
            'pkg/entry.py': 'def dormant():\n    if False:\n        from . import leaf\n'
                            'def score(value):\n    return value\n',
            'pkg/leaf.py': 'import external_sdk\n',
            'elsewhere.py': 'def unused():\n    pass\n'}, path='pkg/entry.py')
        scope = ActivationScope(inspector, entry, candidate)
        self.assertEqual(set(scope.modules), {'pkg/__init__.py', 'pkg/entry.py', 'pkg/leaf.py'})
        self.assertFalse(scope.closed)
        self.assertTrue(any(row['module'] == 'external_sdk' for row in scope.frontiers if 'module' in row))
        self.assertIn('elsewhere.py', scope.paths())  # Unknown means full source, not a partial domain.

    def test_dynamic_callbacks_defaults_protocols_and_candidate_flags_stay_unknown(self):
        for source in ('def score(value):\n    return value()\n',
                       'def score(value=opaque()):\n    return value\n',
                       'def score(value):\n    return value.attribute\n',
                       'def score(value):\n    return str(value)\n',
                       'def score(value):\n    return __import__("external")\n',
                       'def score(value):\n    if value:\n        return 1\n    return 0\n',
                       'class Item(metaclass=opaque):\n    pass\ndef score(value):\n    return value\n'):
            with self.subTest(source=source):
                inspector, entry, candidate = self.fixture({'scope_entry.py': source, 'outside.py': 'pass\n'})
                candidate['activation_scope'] = dict(status='CHECKED', paths=['scope_entry.py'])
                entry['complete_scope'] = True
                scope = ActivationScope(inspector, entry, candidate)
                self.assertFalse(scope.closed)
                self.assertEqual(scope.paths(), ('outside.py', 'scope_entry.py'))

    def test_parent_source_mutation_and_builtin_alias_remain_in_domain(self):
        inspector, entry, candidate = self.fixture({
            'pkg/__init__.py': 'from . import changed\n',
            'pkg/changed.py': 'import builtins\nbuiltins.object = changed\n',
            'pkg/entry.py': 'def score(value):\n    return value\n'}, path='pkg/entry.py')
        scope = ActivationScope(inspector, entry, candidate)
        self.assertFalse(scope.closed)
        protocols = ExternalSourceProtocols(inspector)
        protocols.bind_activation(entry, candidate)
        self.assertFalse(protocols.type_references.builtins_closed())

    def test_normal_source_consumer_retains_worker_obligation(self):
        inspector, entry, candidate = self.fixture({
            'scope_entry.py': 'def score(value):\n    return isinstance(None,str)\n',
            'unactivated.py': 'import builtins\nbuiltins.isinstance = changed\n'})
        trace = resolve_resource_calls(contextual(inspector, Context(values={'value': 'ordinary'})), entry, candidate)
        requirements = [row['target_requirements'] for row in trace['external_protocols'] if row['rule'] == RULE]
        self.assertEqual(len(requirements), 1)
        self.assertEqual(requirements[0]['source_status'], 'STRUCTURE_CHECKED')
        self.assertTrue(any(row['reason'] == GAP for row in trace['negative_scope_gaps']))
        checked = verify_activation_targets(inspector, entry, candidate, trace)
        self.assertFalse(checked['allowed'])
        forged = dict(requirements[0], source_status='CHECKED', execution_permission=True)
        altered = copy.deepcopy(trace)
        next(row for row in altered['external_protocols'] if row['rule'] == RULE)['target_requirements'] = forged
        self.assertFalse(verify_activation_targets(inspector, entry, candidate, altered)['allowed'])

    def test_existing_full_source_positives_and_incomplete_bounds_add_no_scope_protocol(self):
        for source in ('def score(value):\n    return value\n',
                       'class Marker:\n    pass\ndef score(value):\n    return value\n',
                       'def unused(value):\n    return opaque(value)\ndef score(value):\n    return value\n'):
            with self.subTest(source=source):
                inspector, entry, candidate = self.fixture({'scope_entry.py': source})
                trace = resolve_resource_calls(contextual(inspector, Context(values={'value': 'ordinary'})), entry, candidate)
                self.assertFalse(any(row['rule'] == RULE for row in trace['external_protocols']))
                self.assertFalse(any(row['reason'] == GAP for row in trace['negative_scope_gaps']))

    def test_actual_original_binding_receipts_and_same_lock_have_a_finite_positive(self):
        from seampact.units import NativeBindings
        inspector, entry, candidate = self.fixture({
            'scope_lib.py': 'TOKEN="original"\ndef helper(value=None):\n    return value\n',
            'scope_entry.py': 'from scope_lib import helper\ndef score(value):\n    return isinstance(None,str)\n',
            'unactivated.py': 'import builtins\nbuiltins.isinstance = changed\n'})
        binding = NativeBindings(inspector, candidate, None)
        self.addCleanup(binding.modules.clear)
        ctx = Context(values={'value': 'ordinary'})
        trace = resolve_resource_calls(contextual(inspector, ctx), entry, candidate)
        plan = dict(modules_order=['scope_lib.py', 'scope_entry.py'])
        # Use the normal original frozen loader/target capture, without calling
        # a source function or constructing an owner. The binding owns modules.
        with binding._context():
            namespace = binding._namespace(entry)
            receipt = binding._capture_import_only_target(entry, namespace)
            receipt['helpers'] = binding._capture_imported_helpers(plan)
            receipt['instance_identity'] = dict(session='s', instance_id='i', generation=1, process_nonce='n')
            binding.import_only_entries['unit'] = receipt
        self.assertFalse(verify_activation_targets(inspector, entry, candidate, trace,
            binding=binding, receipt=receipt)['allowed'])  # Outside the original read/use lock.
        with binding._context():
            checked = verify_activation_targets(inspector, entry, candidate, trace,
                binding=binding, receipt=receipt)
            self.assertTrue(checked['allowed'], checked)
            self.assertEqual(checked['requirement_digests'], [digest(ActivationScope(inspector, entry).requirements())])
            replay = resolve_resource_calls(contextual(inspector, Context(values=ctx.values,
                runtime_verified=tuple(checked['requirement_digests']))), entry, candidate)
            self.assertFalse(any(row['reason'] == GAP for row in replay['negative_scope_gaps']))
            binding.modules['scope_lib'].__dict__['TOKEN'] = 'changed'
            self.assertFalse(verify_activation_targets(inspector, entry, candidate, trace,
                binding=binding, receipt=receipt)['allowed'])

    def test_normal_worker_import_call_and_actual_mutation_recheck(self):
        import threading
        from seampact.runtime import Coordinator, OwnerRuntime
        from seampact.store import Store
        from seampact.units import UnitCatalog
        from seampact.worker import Worker
        inspector, entry, candidate = self.fixture({
            'pkg/__init__.py': 'TOKEN="original"\n',
            'pkg/entry.py': 'def score(value):\n    return isinstance(None,str)\n',
            'arbitrary_path.py': 'import builtins\nbuiltins.isinstance = changed\n'}, path='pkg/entry.py')
        inspector.request['source_id'] = 'scope-source'
        store = Store(':memory:')
        self.addCleanup(store.close)
        runtime = OwnerRuntime(Coordinator(store))
        self.addCleanup(runtime.close)
        catalog = UnitCatalog(inspector.root, store, runtime)
        unit = catalog.install(inspector, candidate, 'analysis', 'installation')[0]
        self.assertEqual(unit['status'], 'CALL_CONDITION_DEFERRED', unit.get('reason'))
        identity = dict(session='s', instance_id='i', generation=1, process_nonce='n')
        catalog._worker_call_identity = identity
        worker = Worker.__new__(Worker)
        worker.catalog, worker.store, worker.runtime = catalog, store, runtime
        worker.session, worker.instance, worker.generation, worker.process_nonce = identity.values()
        worker.units, worker.accepting, worker.lock = {unit['id']}, True, threading.Lock()
        worker.in_flight, worker.preparing = 0, 0
        payload = dict(unit=unit['id'], arguments={'value': 'ordinary'}, **identity)
        cold = worker.check_call(payload)
        self.assertFalse(cold['allowed'])
        self.assertEqual(cold['reason_code'], 'SDK_BINDING_UNKNOWN', cold)
        self.assertEqual(cold['import_plan']['status'], 'CHECKED')
        imported = worker.import_only(dict(unit=unit['id'], plan_digest=cold['import_plan']['plan_digest'], **identity))
        self.assertEqual(imported['status'], 'IMPORTED', imported)
        checked = worker.check_call(payload)
        self.assertTrue(checked['allowed'], checked)
        binding, _ = catalog.deferred[unit['id']]
        self.assertEqual(binding.owners, {})
        self.assertEqual(catalog.invoke(unit['id'], payload['arguments']), False)
        self.assertTrue(worker.check_call(payload)['allowed'])
        binding.modules['pkg'].__dict__['TOKEN'] = 'changed'
        changed = worker.check_call(payload)
        self.assertFalse(changed['allowed'], changed)
        self.assertNotIn('arbitrary_path', binding.modules)


if __name__ == '__main__':
    unittest.main()
