"""Preserve actual CLI results across group transport without granting object ports."""
import copy
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from seampact.chat import public_result
from seampact.completion import after_completion
from seampact.composition import NativePlanOwner
from seampact.deployment import (GROUP_PROFILE, REMOTE_PROFILE, PORT_CODEC, Deployments, PortGap,
                                check_group_host, decode_port, encode_port)
from seampact.inspection import Inspector
from seampact.runtime import Coordinator, OwnerRuntime, ProtocolError
from seampact.store import Store
from seampact.units import UnitCatalog
from seampact.worker import Worker


class CliGroupResultTests(unittest.TestCase):
    def transfer(self, process):
        return decode_port(json.loads(json.dumps(encode_port(process), allow_nan=False)))

    @contextmanager
    def cli_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'original.py').write_text(
                "import argparse, sys\nfrom pathlib import Path\n"
                "p=argparse.ArgumentParser()\np.add_argument('--code')\np.add_argument('--counter')\n"
                "a=p.parse_args()\nc=Path(a.counter)\nc.write_text(c.read_text()+'x' if c.exists() else 'x')\n"
                "sys.stdout.buffer.write(b'out\\x00\\xff')\nsys.stderr.buffer.write(b'err\\x00\\xfe')\n"
                "raise SystemExit(int(a.code))\n", encoding='utf-8')
            inspector = Inspector.freeze(root, root / 'snapshots', 'source', ['original.py'], 'test', 'static')
            inspector.request['source_id'] = 'source'
            candidate = dict(entries=[dict(id='cli', kind='cli', path='original.py', symbol='__main__',
                command=['python', 'original.py', '--code', '{code}', '--counter', '{counter}'],
                boundary_ids=[])], boundaries=[])
            store = Store(':memory:')
            runtime = OwnerRuntime(Coordinator(store))
            try:
                catalog = UnitCatalog(root, store, runtime)
                unit = catalog.install(inspector, candidate, 'analysis', 'installation')[0]
                self.assertEqual(unit['status'], 'READY', unit.get('reason'))
                group = catalog.inspect_candidate(inspector, candidate)['runtime_groups']['groups'][0]
                self.assertEqual(group['host']['status'], 'HOST_CHECKED')
                worker = Worker.__new__(Worker)
                worker.catalog, worker.store, worker.runtime = catalog, store, runtime
                worker.profile, worker.units = GROUP_PROFILE['id'], {unit['id']}
                worker.session, worker.instance, worker.generation, worker.process_nonce = 'session', 'instance', 0, 'nonce'
                worker.lock, worker.accepting, worker.in_flight = threading.Lock(), True, 0
                yield worker, unit, root / 'counter.txt'
            finally:
                runtime.close()
                store.close()

    def worker_route(self, worker):
        router = Deployments.__new__(Deployments)
        router.lock, router.unit_state = threading.RLock(), {}
        router.platform = SimpleNamespace(store=worker.store, runtime=worker.runtime)
        record = dict(id=worker.instance, profile=worker.profile, group_id='group', state='READY',
            accepting=True, in_flight=0, endpoint='http://worker.invalid', initialization={},
            **worker.identity())
        # Exercise the real caller and worker; replace only the network hop.
        def send(url, body):
            self.assertEqual(url, record['endpoint'] + '/invoke')
            return json.loads(json.dumps(worker.invoke(json.loads(json.dumps(body))), allow_nan=False))
        return router, record, send

    def test_worker_and_caller_preserve_real_cli_exit_and_only_enable_successors_on_zero(self):
        for code in (0, 7):
            with self.subTest(code=code), self.cli_worker() as (worker, unit, counter):
                router, record, send = self.worker_route(worker)
                calls, returned = [], []
                def invoke(unit_id, arguments, native_arguments=()):
                    calls.append(unit_id)
                    if unit_id == 'successor':
                        return None
                    value = router._call(record, unit, arguments, native_arguments)[0]
                    returned.append(value)
                    return value
                plan = dict(id='plan', data_edges=[], control_edges=[], steps=[
                    dict(id='first', unit_id=unit['id'], arguments=dict(code=str(code), counter=str(counter)),
                         native_arguments=[], after=[]),
                    dict(id='next', unit_id='successor', arguments={}, native_arguments=[], after=['first'])])
                with patch('seampact.deployment.json_request', side_effect=send) as request, \
                     patch.object(router, '_event'):
                    result, success = NativePlanOwner(plan).execute(SimpleNamespace(invoke=invoke), public_result)
                self.assertEqual(success, code == 0, result)
                self.assertEqual(calls, [unit['id'], 'successor'] if code == 0 else [unit['id']])
                self.assertEqual(result['results'][0]['result']['returncode'], code)
                process = returned[0]
                self.assertIs(type(process), subprocess.CompletedProcess)
                self.assertEqual((process.returncode, process.stdout, process.stderr), (code, b'out\x00\xff', b'err\x00\xfe'))
                self.assertEqual(process.args[-4:], ['--code', str(code), '--counter', str(counter)])
                self.assertEqual(process.seampact_completion['task_status'], 'UNKNOWN')
                self.assertEqual(counter.read_text(), 'x')
                self.assertEqual(request.call_count, 1)
                receipts = list(worker.runtime.snapshot()['calls'].values())
                self.assertEqual(len(receipts), 1)
                self.assertEqual((receipts[0]['receipt'], receipts[0]['starts']), ('RETURN', 1))
                self.assertEqual(worker.in_flight, 0)

    def test_worker_type_error_after_real_return_is_not_replayed_by_caller(self):
        with self.cli_worker() as (worker, unit, counter):
            router, record, send = self.worker_route(worker)
            original = worker.catalog.invoke
            def wrong_result(*args, **kwargs):
                original(*args, **kwargs)
                return {'returncode': 0}
            with patch.object(worker.catalog, 'invoke', side_effect=wrong_result), \
                 patch('seampact.deployment.json_request', side_effect=send) as request, \
                 patch.object(router, '_event'):
                with self.assertRaisesRegex(ProtocolError, 'ValueError: 原实际返回不符合核对的结果类型'):
                    router._call(record, unit, dict(code='0', counter=str(counter)), ())
            self.assertEqual(counter.read_text(), 'x')
            self.assertEqual(request.call_count, 1)
            receipts = list(worker.runtime.snapshot()['calls'].values())
            self.assertEqual([(row['receipt'], row['starts']) for row in receipts], [('RETURN', 1)])

    def test_worker_does_not_admit_process_results_for_scalar_or_function_ports(self):
        for profile, kind in ((REMOTE_PROFILE['id'], 'cli'), (GROUP_PROFILE['id'], 'function')):
            with self.subTest(profile=profile, kind=kind), self.cli_worker() as (worker, unit, counter):
                worker.profile = profile
                original = worker.catalog.invoke
                def invoke(*args, **kwargs):
                    result = original(*args, **kwargs)
                    # Simulate an unsupported declared port after a real original call.
                    worker.catalog.entry_specs[unit['id']]['kind'] = kind
                    return result
                with patch.object(worker.catalog, 'invoke', side_effect=invoke):
                    response = worker.invoke(dict(unit=unit['id'], arguments=dict(code='0', counter=str(counter)),
                        **worker.identity()))
                self.assertNotIn('result', response)
                self.assertEqual(response['error_type'], 'ValueError')
                self.assertEqual(counter.read_text(), 'x')

    def test_actual_process_exit_output_and_completion_survive_transport(self):
        for code in (0, 3):
            with self.subTest(code=code):
                process = subprocess.run([sys.executable, '-c',
                    "import sys; sys.stdout.buffer.write(b'out\\x00\\xff'); sys.stderr.write('err'); sys.exit(" + str(code) + ")"],
                    capture_output=True, check=False)
                after_completion(None, process)
                restored = self.transfer(process)
                self.assertIs(type(restored), subprocess.CompletedProcess)
                self.assertEqual(vars(restored), vars(process))
                self.assertEqual(restored.returncode, code)
                self.assertEqual(restored.stdout, b'out\x00\xff')
                self.assertEqual(restored.stderr, b'err')
                self.assertEqual(restored.seampact_completion['task_status'], 'UNKNOWN')
                self.assertEqual(public_result(restored), public_result(process))

    def test_text_tuple_args_and_absent_output_are_preserved(self):
        for args in ('command', ('command', 'argument'), [b'command', b'argument']):
            process = subprocess.CompletedProcess(args, -9, 'text', None)
            self.assertEqual(vars(self.transfer(process)), vars(process))
        self.assertEqual(PORT_CODEC['id'], 'group-port/2')
        self.assertEqual(GROUP_PROFILE['codec'], PORT_CODEC['id'])

    def test_unknown_attributes_subclasses_and_nonprocess_objects_are_rejected(self):
        class CustomProcess(subprocess.CompletedProcess):
            pass
        process = subprocess.CompletedProcess([], 0)
        process.other_state = object()
        for value in (process, CustomProcess([], 0), object()):
            with self.subTest(value=type(value).__name__), self.assertRaises(PortGap):
                encode_port(value)

    def test_malformed_process_fields_are_rejected_before_construction(self):
        good = dict(args=['command'], returncode=0, stdout=None, stderr=None)
        for payload in (None, {}, dict(good, returncode=True), dict(good, args={'dynamic': 1}),
                        dict(good, stdout=[]), dict(good, extra='state'), dict(good, completion='claimed success')):
            with self.subTest(payload=payload), self.assertRaises(PortGap):
                decode_port({'$process_result': payload})
        with self.assertRaises(PortGap):
            encode_port(subprocess.CompletedProcess([], True))

    def test_only_checked_cli_process_results_are_admitted_by_native_group_host(self):
        row = dict(entry_id='cli', entry=dict(kind='cli'), check=dict(status='READY',
            result_schema={'x-native-type': dict(module='subprocess', symbol='CompletedProcess')}))
        host = check_group_host([row], ['cli'], scalar=False)
        self.assertEqual(host['status'], 'HOST_CHECKED')
        self.assertEqual(host['members']['cli']['prepare']['status'], 'UNSUPPORTED')
        self.assertEqual(check_group_host([row], ['cli'], scalar=True)['status'], 'MISSING')
        function = copy.deepcopy(row)
        function['entry']['kind'] = 'function'
        self.assertEqual(check_group_host([function], ['cli'], scalar=False)['status'], 'MISSING')
        unknown = copy.deepcopy(row)
        unknown['check']['result_schema']['x-native-type']['symbol'] = 'OtherObject'
        self.assertEqual(check_group_host([unknown], ['cli'], scalar=False)['status'], 'MISSING')
        missing = copy.deepcopy(row)
        missing['check'].update(status='MISSING', reason='source not checked')
        self.assertEqual(check_group_host([missing], ['cli'], scalar=False)['status'], 'MISSING')

    def test_catalog_cli_result_can_cross_transport_and_retains_unknown_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'original.py').write_text("import argparse\np=argparse.ArgumentParser()\np.add_argument('--value')\na=p.parse_args()\nprint(a.value)\n", encoding='utf-8')
            inspector = Inspector.freeze(root, root / 'snapshots', 'source', ['original.py'], 'test', 'static')
            inspector.request['source_id'] = 'source'
            candidate = dict(entries=[dict(id='cli', kind='cli', path='original.py', symbol='__main__',
                command=['python', 'original.py', '--value', '{value}'], boundary_ids=[])], boundaries=[])
            store = Store(':memory:')
            runtime = OwnerRuntime(Coordinator(store))
            try:
                catalog = UnitCatalog(root, store, runtime)
                unit = catalog.install(inspector, candidate, 'analysis', 'installation')[0]
                self.assertEqual(unit['status'], 'READY', unit.get('reason'))
                checked = catalog.inspect_candidate(inspector, candidate)
                group = checked['runtime_groups']['groups'][0]
                self.assertEqual(group['host']['status'], 'HOST_CHECKED')
                self.assertEqual(group['host']['members']['cli']['ports']['codec'], 'group-port/2')
                process = catalog.invoke(unit['id'], dict(value='actual invocation'))
                restored = self.transfer(process)
                self.assertEqual(restored.returncode, 0, restored.stderr)
                self.assertEqual(restored.stdout.strip(), b'actual invocation')
                self.assertEqual(restored.seampact_completion['task_status'], 'UNKNOWN')
            finally:
                runtime.close()
                store.close()

    def test_transported_nonzero_exit_stops_plan_successor(self):
        stopped = self.transfer(subprocess.run([sys.executable, '-c', 'raise SystemExit(7)'], capture_output=True))
        class Catalog:
            def __init__(self): self.called = []
            def invoke(self, unit, arguments, native_arguments=()):
                self.called.append(unit)
                return stopped
        catalog = Catalog()
        plan = dict(id='plan', data_edges=[], control_edges=[], steps=[
            dict(id='first', unit_id='first', arguments={}, native_arguments=[], after=[]),
            dict(id='second', unit_id='second', arguments={}, native_arguments=[], after=['first'])])
        result, success = NativePlanOwner(plan).execute(catalog, public_result)
        self.assertFalse(success)
        self.assertEqual(catalog.called, ['first'])
        self.assertEqual(result['execution_status'], 'STOPPED_AT_NON_SUCCESS_EXIT')

    def test_transported_zero_exit_does_not_override_incomplete_artifact_observation(self):
        process = subprocess.run([sys.executable, '-c', 'pass'], capture_output=True)
        process.seampact_completion = dict(contract_declared=True, task_status='INCOMPLETE', newly_completed=False)
        restored = self.transfer(process)
        calls = []
        class Catalog:
            def invoke(self, unit, arguments, native_arguments=()):
                calls.append(unit)
                return restored
        plan = dict(id='plan', data_edges=[], control_edges=[], steps=[
            dict(id='first', unit_id='first', arguments={}, native_arguments=[], after=[]),
            dict(id='second', unit_id='second', arguments={}, native_arguments=[], after=['first'])])
        result, success = NativePlanOwner(plan).execute(Catalog(), public_result)
        self.assertFalse(success)
        self.assertEqual(calls, ['first'])
        self.assertEqual(result['execution_status'], 'STOPPED_AT_INCOMPLETE_RESULT')


if __name__ == '__main__':
    unittest.main()
