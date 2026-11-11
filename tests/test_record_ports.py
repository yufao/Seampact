"""A value snapshot never grants object, source-call or lifecycle permission."""

import importlib.util
import json
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from seampact.source.inspection import Inspector
from seampact.source.record_ports import FrozenRecordPorts, RecordSnapshot
from seampact.hosting.deployment import encode_port, decode_port, PortGap
from seampact.runtime import Coordinator, OwnerRuntime
from seampact.store import Store
from seampact.units import UnitCatalog


class RecordPortTests(unittest.TestCase):
    def test_loaded_numpy_does_not_consult_arbitrary_instance_class(self):
        module, ports, _ = self.source('class Result:\n    def __init__(self, value):\n        self.value = value\n'
            '    def __getattribute__(self, name):\n        raise AssertionError("object hook executed")\n')
        fake = SimpleNamespace(ndarray=type('Array', (), {}), generic=int, sctypeDict={'int': int})
        with patch.dict(sys.modules, {'numpy': fake}):
            with self.assertRaises(PortGap):
                encode_port(module.Result(1), ports)

    def test_official_numpy_array_and_scalar_still_roundtrip(self):
        try:
            import numpy
        except ImportError:
            self.skipTest('optional numpy is unavailable')
        scalar = numpy.float32(1.25)
        array = numpy.array([[1, 2]], dtype=numpy.int64)
        decoded = decode_port(json.loads(json.dumps(encode_port([scalar, array]))))
        self.assertIs(type(decoded[0]), numpy.float32)
        self.assertEqual(decoded[0], scalar)
        numpy.testing.assert_array_equal(decoded[1], array)

    def source(self, text):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        path = root / "records.py"
        path.write_text(text, encoding="utf-8")
        inspector = Inspector.freeze(root, root / "snapshots", "record", ["records.py"], "record", "value")
        name = "record_port_fixture_" + root.name.replace("-", "_")
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        self.addCleanup(sys.modules.pop, name, None)
        spec.loader.exec_module(module)
        return module, FrozenRecordPorts(inspector), path

    def test_nested_original_records_leave_as_values_without_import_or_reconstruction(self):
        module, ports, _ = self.source('class Claim:\n    def __init__(self, content):\n        self.content = content\n'
            'class Result:\n    def __init__(self, claims, response=None):\n        self.claims = claims\n        self.response = response\n')
        original = module.Result([module.Claim(["Paris", "is in", "France"])])
        encoded = encode_port([original], ports)
        restored = decode_port(json.loads(json.dumps(encoded)))
        self.assertIs(type(restored[0]), RecordSnapshot)
        self.assertEqual(restored[0].fields["claims"][0].fields["content"], ["Paris", "is in", "France"])
        self.assertIsNone(restored[0].fields["response"])
        self.assertNotIsInstance(restored[0], module.Result)
        restored[0].fields["claims"][0].fields["content"][0] = "London"
        self.assertEqual(original.claims[0].content[0], "Paris")

    def test_unchecked_arbitrary_object_still_fails(self):
        module, _, _ = self.source('class Result:\n    def __init__(self, value):\n        self.value = value\n')
        with self.assertRaises(PortGap):
            encode_port(module.Result(1))

    def test_original_record_survives_native_module_removal_without_importing_it(self):
        module, ports, _ = self.source('class Result:\n    def __init__(self, value):\n        self.value = value\n')
        original = module.Result(4)
        sys.modules.pop(module.__name__)
        restored = decode_port(encode_port(original, ports))
        self.assertEqual(restored.fields, dict(value=4))
        self.assertNotIn(module.__name__, sys.modules)

    def test_native_whole_call_can_encode_its_record_after_source_context_closes(self):
        module, ports, _ = self.source('class Result:\n    def __init__(self, value):\n        self.value = value\n'
            'def run(value: int):\n    return Result(value)\n')
        inspector = ports.inspector
        inspector.request['source_id'] = 'record-source'
        candidate = dict(entries=[dict(id='run', path='records.py', symbol='run', kind='function',
            boundary_ids=['run-W'])], boundaries=[dict(id='run-W', path='records.py', symbol='run', mode='W')])
        store = Store(':memory:')
        self.addCleanup(store.close)
        runtime = OwnerRuntime(Coordinator(store))
        self.addCleanup(runtime.close)
        catalog = UnitCatalog(inspector.root, store, runtime)
        unit = catalog.install(inspector, candidate, 'analysis', 'installation')[0]
        self.assertEqual(unit['status'], 'READY', unit.get('reason'))
        original = catalog.invoke(unit['id'], dict(value=4))
        self.assertNotIn(type(original).__module__, sys.modules)
        restored = decode_port(encode_port(original, ports))
        self.assertEqual(restored.fields, dict(value=4))
        self.assertIs(type(restored), RecordSnapshot)

    def test_runtime_added_state_and_active_values_fail(self):
        module, ports, _ = self.source('class Result:\n    def __init__(self, value):\n        self.value = value\n')
        value = module.Result(1)
        value.owner = object()
        with self.assertRaises(PortGap):
            encode_port(value, ports)
        with self.assertRaises(PortGap):
            encode_port(module.Result(object()), ports)

    def test_hooks_are_not_called(self):
        module, ports, _ = self.source('class Result:\n    def __init__(self, value):\n        self.value = value\n'
            '    def __getattribute__(self, name):\n        raise AssertionError("must not execute")\n')
        with self.assertRaises(PortGap):
            encode_port(module.Result(1), ports)

    def test_dictionary_descriptor_is_never_executed(self):
        source = ('class Result:\n    def __init__(self, value):\n        self.value = value\n'
                  '    @property\n    def __dict__(self):\n        raise AssertionError("dictionary hook executed")\n')
        module, ports, _ = self.source(source)
        with self.assertRaises(PortGap):
            encode_port(module.Result(1), ports)

    def test_object_hook_added_after_cache_is_rejected_before_reading_fields(self):
        module, ports, _ = self.source('class Result:\n    def __init__(self, value):\n        self.value = value\n')
        value = module.Result(1)
        encode_port(value, ports)
        module.Result.__getattribute__ = lambda *_: (_ for _ in ()).throw(AssertionError("object hook executed"))
        with self.assertRaises(PortGap):
            encode_port(value, ports)

    def test_forged_module_descriptor_and_nonstring_field_keys_do_not_execute_hooks(self):
        module, ports, _ = self.source('class Result:\n    def __init__(self, value):\n        self.value = value\n')
        original = module.Result(1)
        class Descriptor:
            def __get__(self, *args):
                raise AssertionError("module hook executed")
        module.Result.__module__ = Descriptor()
        with self.assertRaises(PortGap):
            encode_port(original, ports)
        module.Result.__module__ = module.__name__
        class FieldKey:
            def __hash__(self):
                return hash('value')
            def __eq__(self, other):
                raise AssertionError("field-key equality executed")
        original.__dict__ = {FieldKey(): 1}
        with self.assertRaises(PortGap):
            encode_port(original, ports)

    def test_changed_source_and_constructor_fail(self):
        module, ports, path = self.source('class Result:\n    def __init__(self, value):\n        self.value = value\n')
        value = module.Result(1)
        path.write_text(path.read_text() + "\n# changed\n", encoding="utf-8")
        with self.assertRaises(PortGap):
            encode_port(value, ports)
        module, ports, path = self.source('class Result:\n    def __init__(self, value):\n        self.value = value\n')
        value = module.Result(1)
        encode_port(value, ports)
        path.write_text(path.read_text() + "\n# changed after checking\n", encoding="utf-8")
        with self.assertRaises(PortGap):
            encode_port(value, ports)
        module, ports, _ = self.source('class Result:\n    def __init__(self, value):\n        self.value = value\n')
        value = module.Result(1)
        encode_port(value, ports)
        module.Result.__init__.__code__ = (lambda self, value: None).__code__
        with self.assertRaises(PortGap):
            encode_port(value, ports)

    def test_descriptor_added_after_check_fails(self):
        module, ports, _ = self.source('class Result:\n    def __init__(self, value):\n        self.value = value\n')
        value = module.Result(1)
        encode_port(value, ports)
        module.Result.value = property(lambda _: (_ for _ in ()).throw(AssertionError("must not execute")))
        with self.assertRaises(PortGap):
            encode_port(value, ports)

    def test_runtime_code_constant_does_not_execute_comparison_hooks(self):
        module, ports, _ = self.source('class Result:\n    def __init__(self, value):\n        self.value = value\n')
        value = module.Result(1)
        class Trap:
            def __eq__(self, other):
                raise AssertionError("code constant comparison hook executed")
        function = module.Result.__init__
        function.__code__ = function.__code__.replace(co_consts=(Trap(), *function.__code__.co_consts[1:]))
        with self.assertRaises(PortGap):
            encode_port(value, ports)

    def test_executable_constructor_and_inheritance_fail(self):
        for source in ('class Result:\n    def __init__(self, value):\n        print("effect")\n        self.value = value\n',
                       'class Parent:\n    pass\nclass Result(Parent):\n    def __init__(self, value):\n        self.value = value\n'):
            with self.subTest(source=source):
                module, ports, _ = self.source(source)
                with self.assertRaises(PortGap):
                    encode_port(module.Result(1), ports)

    def test_malformed_wire_snapshot_does_not_import_a_class(self):
        with self.assertRaises(PortGap):
            decode_port({"$record_snapshot": dict(module="os", symbol="system", source_sha256="invalid", fields={})})


if __name__ == "__main__":
    unittest.main()
