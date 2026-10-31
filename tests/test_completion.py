"""Actual lightweight CLI exits, artifact validation and resume observations."""

import struct
import subprocess
import sys
import tempfile
import textwrap
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from seampact.chat import public_result
from seampact.completion import after_completion, before_completion, check_completion
from seampact.composition import NativePlanOwner
from seampact.inspection import Inspector
from seampact.runtime import Coordinator, OwnerRuntime
from seampact.store import Store
from seampact.units import UnitCatalog


SOURCE = textwrap.dedent('''
    import argparse
    import json
    import os
    import sys
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--mode', default='good')
    args = parser.parse_args()
    source = args.input
    target = args.output
    if args.mode == 'missing':
        sys.exit(0)
    if args.mode == 'invalid':
        with open(target, 'w') as handle:
            handle.write('broken')
        sys.exit(0)
    if args.mode == 'missing_key':
        with open(target, 'w') as handle:
            handle.write('{}')
        sys.exit(0)
    if os.path.isfile(target):
        sys.exit(0)
    with open(target, 'w') as handle:
        json.dump({'map': [[1]], 'imgsize': [1, 1]}, handle)
    if args.mode == 'failure':
        sys.exit(3)
''').lstrip()


def npy(shape=(1, 1), dtype='<f4', payload=None):
    header = repr(dict(descr=dtype, fortran_order=False, shape=shape)).encode('ascii')
    header += b' ' * ((64 - (10 + len(header) + 1) % 64) % 64) + b'\n'
    if payload is None:
        payload = struct.pack('<f', 1.0)
    return b'\x93NUMPY\x01\x00' + struct.pack('<H', len(header)) + header + payload


class CompletionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.script = self.root / 'original.py'
        self.script.write_text(SOURCE, encoding='utf-8')
        (self.root / 'image.bin').write_bytes(b'input')

    def proposal(self, source=SOURCE, format_='json'):
        inspector = SimpleNamespace(data=lambda path: source.encode('utf-8'))
        contract = dict(schema='native-completion/1', input_parameter='image',
                        output_parameter='result', format=format_, required_keys=['map', 'imgsize'],
                        evidence=[dict(path='original.py', unit='file', start=1, end=len(source.splitlines()))])
        entry = dict(id='predict', path='original.py', kind='cli', symbol='__main__',
                     command=['python', 'original.py', '--input', '{image}', '--output', '{result}'],
                     completion=contract)
        return inspector, entry

    def contract(self, source=SOURCE, format_='json'):
        return check_completion(*self.proposal(source, format_))

    def run_cli(self, mode='good', output='result.json'):
        arguments = dict(image='image.bin', result=output)
        snapshot = before_completion(self.contract(), arguments, self.root)
        result = subprocess.run([sys.executable, str(self.script), '--input', arguments['image'],
                                 '--output', output, '--mode', mode], cwd=self.root,
                                capture_output=True, check=False)
        report = after_completion(snapshot, result)
        self.assertIs(type(result), subprocess.CompletedProcess)
        self.assertIs(result.seampact_completion, report)
        return result, report

    def test_zero_exit_without_result_is_incomplete(self):
        result, report = self.run_cli('missing')
        self.assertEqual(result.returncode, 0)
        self.assertEqual(report['task_status'], 'INCOMPLETE')
        self.assertEqual(report['result']['status'], 'MISSING')
        self.assertFalse(report['newly_completed'])

    def test_invalid_or_missing_key_result_is_incomplete(self):
        for mode in ['invalid', 'missing_key']:
            with self.subTest(mode=mode):
                result, report = self.run_cli(mode, mode + '.json')
                self.assertEqual(result.returncode, 0)
                self.assertEqual(report['result']['status'], 'INVALID')
                self.assertEqual(report['task_status'], 'INCOMPLETE')

    def test_new_result_and_unchanged_preexisting_result_are_different_facts(self):
        _, first = self.run_cli()
        self.assertEqual(first['task_status'], 'COMPLETE')
        self.assertTrue(first['newly_completed'])
        self.assertFalse(first['output']['preexisting'])
        original = (self.root / 'result.json').read_bytes()
        result, resumed = self.run_cli()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(resumed['result']['status'], 'VALID')
        self.assertTrue(resumed['output']['preexisting'])
        self.assertFalse(resumed['output']['changed'])
        self.assertFalse(resumed['newly_completed'])
        self.assertEqual(resumed['task_status'], 'UNKNOWN')
        self.assertEqual((self.root / 'result.json').read_bytes(), original)
        self.assertEqual(public_result(result)['completion'], resumed)

    def test_nonzero_exit_is_not_completion_even_with_valid_result(self):
        result, report = self.run_cli('failure')
        self.assertEqual(result.returncode, 3)
        self.assertEqual(report['result']['status'], 'VALID')
        self.assertEqual(report['task_status'], 'INCOMPLETE')
        self.assertFalse(report['newly_completed'])

    def test_input_change_prevents_completion(self):
        snapshot = before_completion(self.contract(), dict(image='image.bin', result='out.json'), self.root)
        (self.root / 'out.json').write_text('{"map": 1, "imgsize": [1, 1]}')
        (self.root / 'image.bin').write_bytes(b'different')
        report = after_completion(snapshot, subprocess.CompletedProcess([], 0))
        self.assertEqual(report['task_status'], 'INCOMPLETE')
        self.assertFalse(report['input_unchanged'])

    def test_no_contract_is_explicitly_unknown(self):
        inspector, entry = self.proposal()
        entry.pop('completion')
        self.assertIsNone(check_completion(inspector, entry))
        report = after_completion(None, subprocess.CompletedProcess([], 0))
        self.assertEqual(report['task_status'], 'UNKNOWN')
        self.assertFalse(report['contract_declared'])

    def test_glob_directory_and_nonexact_output_are_not_guessed(self):
        for image, output in [('*.bin', 'out.json'), ('.', 'out.json'), ('image.bin', 'out'), ('image.bin', '.')]:
            with self.subTest(image=image, output=output), self.assertRaises(ValueError):
                before_completion(self.contract(), dict(image=image, result=output), self.root)

    def test_source_requires_actual_option_output_write_keys_and_evidence(self):
        for replacement in [SOURCE.replace('json.dump', 'unrelated.dump'),
                            SOURCE.replace("'imgsize': [1, 1]", "'different': [1, 1]"),
                            SOURCE.replace("target = args.output", "target = 'other.json'"),
                            SOURCE.replace("target = args.output", "target = 'seampact-result.json'"),
                            SOURCE.replace('import json', 'import json\njson = unrelated')]:
            with self.subTest(source=replacement), self.assertRaises(ValueError):
                self.contract(replacement)
        inspector, entry = self.proposal()
        entry['completion']['evidence'][0]['end'] -= 4
        with self.assertRaises(ValueError):
            check_completion(inspector, entry)
        inspector, entry = self.proposal()
        entry['command'][2] = '--unrelated'
        with self.assertRaises(ValueError):
            check_completion(inspector, entry)

    def npz_contract(self):
        source = textwrap.dedent('''
            import argparse
            import os
            import numpy as np
            parser = argparse.ArgumentParser()
            parser.add_argument('--input')
            parser.add_argument('--output')
            args = parser.parse_args()
            input = args.input
            output = args.output
            if os.path.splitext(os.path.basename(output))[1] == '':
                filename_out = os.path.join(output, 'unresolved-directory-mapping.npz')
            else:
                filename_out = output
            if not filename_out.endswith('.npz'):
                filename_out = filename_out + '.npz'
            if not os.path.isfile(filename_out):
                try:
                    out_dict = dict()
                    out_dict['map'] = prediction
                    out_dict['imgsize'] = dimensions
                    np.savez(filename_out, **out_dict)
                except:
                    pass
        ''').lstrip()
        return self.contract(source, 'npz')

    def test_npz_validates_numeric_arrays_dimensions_and_never_loads_pickle(self):
        contract = self.npz_contract()
        for mode in ['valid', 'missing_key', 'truncated', 'object', 'badzip']:
            output = mode + '.npz'
            snapshot = before_completion(contract, dict(image='image.bin', result=output), self.root)
            target = self.root / output
            if mode == 'badzip':
                target.write_bytes(b'not a zip')
            else:
                with zipfile.ZipFile(target, 'w') as archive:
                    archive.writestr('map.npy', npy(payload=b'bad') if mode == 'truncated' else
                                     npy(dtype='|O8', payload=b'pickle-never-read') if mode == 'object' else npy())
                    if mode != 'missing_key':
                        archive.writestr('imgsize.npy', npy(shape=(2,), dtype='<i8', payload=struct.pack('<qq', 1, 1)))
            report = after_completion(snapshot, subprocess.CompletedProcess([], 0))
            with self.subTest(mode=mode):
                self.assertEqual(report['task_status'], 'COMPLETE' if mode == 'valid' else 'INCOMPLETE')
                self.assertEqual(report['effect_quality'], 'UNKNOWN')

    def test_catalog_invocation_uses_actual_cwd_and_records_reused_output_unknown(self):
        app = self.root / 'app'
        app.mkdir()
        (app / 'original.py').write_text(SOURCE, encoding='utf-8')
        (app / 'README.md').write_text('python original.py --input image.bin --output result.json\n')
        (app / 'image.bin').write_bytes(b'workload')
        inspector = Inspector.freeze(self.root, self.root / 'snapshots', 'test',
                                     ['app/original.py', 'app/README.md'], 'test', 'static')
        inspector.request['source_id'] = 'test'
        _, entry = self.proposal()
        entry.update(path='app/original.py', boundary_ids=[],
                     command=['python', 'app/original.py', '--input', '{image}', '--output', '{result}'],
                     cwd=dict(path='app', evidence=[dict(path='app/README.md', start=1, end=1)]))
        entry['completion']['evidence'][0]['path'] = 'app/original.py'
        store = Store(':memory:')
        runtime = OwnerRuntime(Coordinator(store))
        try:
            catalog = UnitCatalog(self.root, store, runtime)
            candidate = dict(entries=[entry], boundaries=[])
            unit = catalog.install(inspector, candidate, 'analysis', 'install')[0]
            self.assertEqual(unit['status'], 'READY', unit.get('reason'))
            for expected in ['COMPLETE', 'UNKNOWN']:
                result = catalog.invoke(unit['id'], dict(image='image.bin', result='result.json'))
                self.assertIs(type(result), subprocess.CompletedProcess)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.seampact_completion['task_status'], expected)
                self.assertEqual(store.get('unit', unit['id'])['last_completion'], result.seampact_completion)
            self.assertTrue((app / 'result.json').is_file())
            self.assertFalse((self.root / 'result.json').exists())
        finally:
            runtime.close()
            store.close()

    def test_plan_does_not_release_successor_for_incomplete_or_reused_result(self):
        for mode in ['missing', 'good']:
            if mode == 'good':
                (self.root / 'result.json').write_text('{"map": 1, "imgsize": [1,1]}')
            result, report = self.run_cli(mode)
            calls = []
            def invoke(unit_id, arguments, native_arguments=()):
                calls.append(unit_id)
                return result
            catalog = SimpleNamespace(invoke=invoke)
            plan = dict(id='plan', data_edges=[], control_edges=[], steps=[
                dict(id='first', unit_id='first', arguments={}, native_arguments=[], after=[]),
                dict(id='second', unit_id='second', arguments={}, native_arguments=[], after=['first'])])
            record, success = NativePlanOwner(plan).execute(catalog, public_result)
            self.assertFalse(success)
            self.assertFalse(record['completed'])
            self.assertEqual(record['execution_status'], 'STOPPED_AT_INCOMPLETE_RESULT')
            self.assertEqual(calls, ['first'])
            self.assertIn(report['task_status'], {'INCOMPLETE', 'UNKNOWN'})


if __name__ == '__main__':
    unittest.main()
