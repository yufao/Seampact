"""Preserve actual CLI results across group transport without granting object ports."""
import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from seampact.chat import public_result
from seampact.completion import after_completion
from seampact.composition import NativePlanOwner
from seampact.deployment import GROUP_PROFILE, PORT_CODEC, PortGap, check_group_host, decode_port, encode_port
from seampact.inspection import Inspector
from seampact.runtime import Coordinator, OwnerRuntime
from seampact.store import Store
from seampact.units import UnitCatalog


class CliGroupResultTests(unittest.TestCase):
    def transfer(self, process):
        return decode_port(json.loads(json.dumps(encode_port(process), allow_nan=False)))

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
