import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from seampact.scenes.evaluation_evidence import (
    PassiveEvidenceGap, passive_evaluation_snapshot, verify_passive_evaluation, passive_value)
from seampact.source.record_ports import RecordSnapshot

try:
    import numpy as np
except ImportError:
    np = None


class PassiveEvaluationEvidence(unittest.TestCase):
    @unittest.skipIf(np is None, "NumPy is not installed")
    def test_official_numpy_values_detach_without_narrowing_or_truncation(self):
        arrays = dict(integers=np.array([[0, 2**64 - 1]], dtype=np.uint64),
            floats=np.array([0.125, 0.875], dtype=np.float32),
            booleans=np.array([True, False], dtype=np.bool_),
            text=np.array(["原结果", "β"], dtype=np.str_), scalar=np.int64(-7),
            probability=np.float32(0.1), zero_dimensional=np.array(3, dtype=np.int16))
        result = passive_value(arrays)
        self.assertEqual(result["integers"], [[0, 2**64 - 1]])
        self.assertEqual(result["floats"], [0.125, 0.875])
        self.assertEqual(result["booleans"], [True, False])
        self.assertEqual(result["text"], ["原结果", "β"])
        self.assertEqual(result["scalar"], -7)
        self.assertEqual(result["zero_dimensional"], 3)
        self.assertEqual(result["probability"], float(arrays["probability"]))
        arrays["floats"][:] = 0
        arrays["text"][:] = "changed"
        self.assertEqual(result["floats"], [0.125, 0.875])
        self.assertEqual(result["text"], ["原结果", "β"])
        json.dumps(result, allow_nan=False)

    @unittest.skipIf(np is None, "NumPy is not installed")
    def test_numpy_subclasses_and_opaque_array_hooks_are_never_executed(self):
        invoked = []
        class Array(np.ndarray):
            def __array__(self, *args, **kwargs):
                invoked.append("array")
                return np.array([1])
            def tolist(self):
                invoked.append("tolist")
                return [1]
            def __deepcopy__(self, memo):
                invoked.append("deepcopy")
                return [1]
        class Scalar(np.float32):
            def __float__(self):
                invoked.append("float")
                return 1.0
        class Opaque:
            @property
            def __class__(self):
                invoked.append("class")
                return np.ndarray
            def __array__(self, *args, **kwargs):
                invoked.append("opaque array")
                return np.array([1])
            def __reduce_ex__(self, protocol):
                invoked.append("serializer")
                return dict, ()
        for value in (np.array([1]).view(Array), Scalar(1), Opaque()):
            with self.assertRaises(PassiveEvidenceGap):
                passive_value(value)
        self.assertEqual(invoked, [])

    @unittest.skipIf(np is None, "NumPy is not installed")
    def test_nonfinite_and_nonpassive_numpy_dtypes_remain_gaps(self):
        for value in (np.array([float("nan")]), np.float32(float("inf")), np.array([object()], dtype=object),
                      np.array([(1,)], dtype=[("value", "i4")]), np.array([1 + 2j]),
                      np.array([b"bytes"]), np.datetime64("2026-11-07")):
            with self.subTest(dtype=str(value.dtype)):
                with self.assertRaises(PassiveEvidenceGap):
                    passive_value(value)
        if np.dtype(np.longdouble).char not in {"e", "f", "d"}:
            with self.assertRaisesRegex(PassiveEvidenceGap, "lossless"):
                passive_value(np.array([np.longdouble("1.0000000000000000001")]))

    def test_capture_detaches_records_and_shared_mutable_values(self):
        fields = {"claims": [["subject", "predicate", "object"]]}
        record = RecordSnapshot("original", "Claims", "a" * 64, fields)
        terminal = dict(kind="terminal", terminated=True, truncated=False,
                        evidence=dict(records=record), response=dict(answer="before"))
        calls = [dict(unit_id="u", duration_ms=1.5)]
        snapshot = passive_evaluation_snapshot(terminal, calls)
        fields["claims"][0][0] = "changed"
        terminal["response"]["answer"] = "after"
        calls.clear()
        self.assertEqual(snapshot["terminal"]["evidence"]["records"]["fields"]["claims"][0][0], "subject")
        self.assertEqual(snapshot["terminal"]["response"]["answer"], "before")
        self.assertEqual(len(snapshot["calls"]), 1)
        json.dumps(snapshot, allow_nan=False)

    def test_live_objects_are_rejected_without_running_hooks(self):
        invoked = []
        class Owner:
            def __deepcopy__(self, memo):
                invoked.append("deepcopy")
                return {}
            def __iter__(self):
                invoked.append("iter")
                return iter(())
            def __repr__(self):
                invoked.append("repr")
                return "owner"
        with self.assertRaises(PassiveEvidenceGap):
            passive_evaluation_snapshot(dict(evidence=dict(owner=Owner())), [])
        self.assertEqual(invoked, [])

    def test_cycles_nonfinite_values_and_subclass_hooks_are_not_passive(self):
        cyclic = []
        cyclic.append(cyclic)
        class Text(str):
            pass
        for value in (cyclic, float("nan"), Text("foreign")):
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaises(PassiveEvidenceGap):
                    passive_evaluation_snapshot(dict(evidence=dict(value=value)), [])

    def test_teacher_image_survives_owner_file_removal_and_detects_change(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "owner-image.png"
            content = b"\x89PNG\r\n\x1a\n" + b"retained original bytes"
            source.write_bytes(content)
            criterion = dict(teacher=dict(images=[dict(field="image", path=["input"], detail="high")]))
            terminal = dict(evidence=dict(image=dict(input=str(source))))
            snapshot = passive_evaluation_snapshot(terminal, [], criteria=[criterion],
                                                  destination=root / "retained")
            source.unlink()
            verify_passive_evaluation(snapshot)
            retained = Path(snapshot["terminal"]["evidence"]["image"]["input"]["value"])
            self.assertEqual(retained.read_bytes(), content)
            retained.write_bytes(content + b"changed")
            with self.assertRaises(PassiveEvidenceGap):
                verify_passive_evaluation(snapshot)

    def test_missing_image_keeps_capture_incomplete(self):
        criterion = dict(teacher=dict(images=[dict(field="image")]))
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(PassiveEvidenceGap):
                passive_evaluation_snapshot(dict(evidence=dict(image=str(Path(temporary) / "missing.png"))),
                                            [], criteria=[criterion], destination=Path(temporary) / "retained")


if __name__ == "__main__":
    unittest.main()
