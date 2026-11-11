"""Result-only numeric comparison preserves actual group-port scalar objects."""

from contextlib import contextmanager
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from seampact.hosting.deployment import GROUP_PROFILE, decode_port, encode_port
from seampact.runtime import Coordinator, OwnerRuntime
from seampact.source.inspection import Inspector
from seampact.source.record_ports import FrozenRecordPorts
from seampact.store import Store
from seampact.units import NativeBindings, UnitCatalog, _matches, _result_matches
from seampact.worker import Worker

try:
    import numpy as np
except ImportError:
    np = None


@unittest.skipIf(np is None, "NumPy is not installed")
class NumericResultPortTests(unittest.TestCase):
    def test_only_finite_official_numeric_result_leaves_extend_value_comparison(self):
        for value in (np.float16(.125), np.float32(.125), np.float64(.125), np.int8(-7), np.uint64(2**64 - 1)):
            with self.subTest(dtype=str(value.dtype)):
                self.assertTrue(_result_matches(value, dict(type="number")))
                self.assertFalse(_matches(value, dict(type="number")))
                self.assertFalse(NativeBindings.__new__(NativeBindings).native_matches(value, dict(type="number")))
        self.assertTrue(_result_matches(np.int64(2), dict(type="integer")))
        self.assertFalse(_result_matches(np.float32(2), dict(type="integer")))
        self.assertFalse(_result_matches(np.bool_(True), dict(type="number")))
        self.assertFalse(_result_matches(np.str_("2"), dict(type="number")))
        self.assertTrue(_matches(np.str_("original"), dict(type="string")))
        self.assertTrue(_result_matches(np.str_("original"), dict(type="string")))

    def test_nested_schema_native_precedence_enum_and_original_container_shape_remain_strict(self):
        schema = dict(type="object", properties=dict(scores=dict(type="array", items=dict(type="number"))),
                      required=["scores"], additionalProperties=False)
        self.assertTrue(_result_matches(dict(scores=[np.float32(.5)]), schema))
        self.assertFalse(_result_matches(dict(scores=(np.float32(.5),)), schema))
        self.assertFalse(_result_matches(dict(scores=[np.float32(.5)], extra=1), schema))
        self.assertFalse(_result_matches(np.float32(.5), {"type": "number", "x-native-type": dict(module="numpy", symbol="float32")}))
        self.assertFalse(_result_matches(np.int64(1), dict(enum=[1])))
        mini = ([1], [np.float32(.875)], [["original chunk"]], [[np.float32(.875)]])
        declared = dict(type="array", items=dict(type="number"))
        self.assertFalse(_result_matches(mini, declared))
        self.assertFalse(_result_matches(list(mini), declared))
        self.assertFalse(_result_matches(np.array(.5), dict(type="number")))

    def test_nonfinite_unsupported_dtypes_and_source_hooks_are_rejected(self):
        invoked = []
        class Scalar(np.float32):
            def __float__(self):
                invoked.append("float")
                return .5
            def __array__(self, *args, **kwargs):
                invoked.append("array")
                return np.array(.5)
        class Opaque:
            @property
            def __class__(self):
                invoked.append("class")
                return np.float32
            def __float__(self):
                invoked.append("opaque float")
                return .5
            def __array__(self, *args, **kwargs):
                invoked.append("opaque array")
                return np.array(.5)
            def __deepcopy__(self, memo):
                invoked.append("deepcopy")
                return .5
        bad = [np.float32(float("nan")), np.float64(float("inf")), np.complex64(1),
               np.array((1,), dtype=[("value", "i4")])[()], np.array(object(), dtype=object)[()],
               Scalar(.5), Opaque()]
        if np.dtype(np.longdouble).char not in {"e", "f", "d"}:
            bad.append(np.longdouble("1.0000000000000000001"))
        for value in bad:
            self.assertFalse(_result_matches(value, dict(type="number")))
        self.assertEqual(invoked, [])

    @contextmanager
    def worker(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "original.py").write_text("import numpy as np\ndef score(value: float) -> list[float]:\n"
                                             "    return [np.float32(value)]\n", encoding="utf-8")
            inspector = Inspector.freeze(root, root / "snapshots", "source", ["original.py"], "score", "result")
            inspector.request["source_id"] = "source"
            candidate = dict(entries=[dict(id="score", path="original.py", symbol="score", kind="function", boundary_ids=[])],
                             boundaries=[])
            store = Store(":memory:")
            runtime = OwnerRuntime(Coordinator(store))
            try:
                catalog = UnitCatalog(root, store, runtime)
                unit = catalog.install(inspector, candidate, "analysis", "installation")[0]
                self.assertEqual(unit["status"], "READY", unit)
                worker = Worker.__new__(Worker)
                worker.catalog, worker.store, worker.runtime = catalog, store, runtime
                worker.profile, worker.units = GROUP_PROFILE["id"], {unit["id"]}
                worker.record_ports = FrozenRecordPorts(inspector)
                worker.lock, worker.accepting, worker.in_flight = threading.Lock(), True, 0
                worker._identify, worker.identity = lambda body: None, lambda: {}
                yield worker, unit
            finally:
                runtime.close()
                store.close()

    def test_actual_original_worker_result_keeps_numpy_dtype_after_schema_check(self):
        with self.worker() as (worker, unit):
            original, returned = worker.catalog.invoke, []
            def capture(*args, **kwargs):
                value = original(*args, **kwargs)
                returned.append(value)
                return value
            with patch.object(worker.catalog, "invoke", side_effect=capture) as invoke:
                reply = worker.invoke(dict(unit=unit["id"], arguments=encode_port(dict(value=.125))))
            self.assertNotIn("error", reply, reply)
            self.assertEqual(invoke.call_count, 1)
            self.assertIs(type(returned[0][0]), np.float32)
            self.assertEqual(reply["result"][0]["$ndarray"]["dtype"], np.dtype(np.float32).str)
            self.assertTrue(reply["result"][0]["$ndarray"]["scalar"])
            decoded = decode_port(reply["result"])
            self.assertIs(type(decoded[0]), np.float32)
            self.assertEqual(decoded[0], returned[0][0])
            self.assertEqual(worker.in_flight, 0)


if __name__ == "__main__":
    unittest.main()
