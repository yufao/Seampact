"""Public plan results reuse exact passive NumPy projection at native leaves."""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from seampact.chat import public_result

try:
    import numpy as np
except ImportError:
    np = None


@unittest.skipIf(np is None, "NumPy is not installed")
class PublicNumpyResultTests(unittest.TestCase):
    def test_original_numpy_probability_leaves_and_arrays_keep_their_values(self):
        score = np.float32(.1)
        raw = dict(labels=[1], scores=[score], chunks=np.array([[.5, .875]], dtype=np.float32))
        value = public_result(raw)
        self.assertEqual(value, dict(labels=[1], scores=[float(score)], chunks=[[.5, .875]]))
        self.assertIs(type(value["scores"][0]), float)
        raw["chunks"][:] = 0
        self.assertEqual(value["chunks"], [[.5, .875]])

    def test_unsafe_native_leaves_keep_opaque_metadata_without_executing_hooks(self):
        invoked = []
        class Array(np.ndarray):
            def tolist(self):
                invoked.append("tolist")
                return [1]
            def __array__(self, *args, **kwargs):
                invoked.append("array")
                return np.array([1])
        class Opaque:
            @property
            def __class__(self):
                invoked.append("class")
                return np.ndarray
            def __array__(self, *args, **kwargs):
                invoked.append("opaque array")
                return np.array([1])
        for raw in (np.array([1]).view(Array), Opaque(), np.array([float("nan")]), np.array([object()], dtype=object)):
            value = public_result(raw)
            self.assertEqual(value["serialization"], "NOT_CONVERTED")
            self.assertTrue(value["native_object"])
        self.assertEqual(invoked, [])
        cyclic = []
        cyclic.append(cyclic)
        self.assertEqual(public_result(cyclic)[0]["serialization"], "CYCLIC_REFERENCE")


if __name__ == "__main__":
    unittest.main()
