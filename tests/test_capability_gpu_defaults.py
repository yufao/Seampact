"""Capability device declarations control defaults without guessing from weights."""
import sys
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from seampact.hosting.deployment import Deployments


class Store:
    def __init__(self, group):
        self.data = {("runtime_group", "cap"): group}

    def get(self, kind, identity):
        return self.data.get((kind, identity))

    def save(self, kind, identity, value):
        self.data[kind, identity] = value


class CapabilityGPUDefaults(unittest.TestCase):
    def test_cpu_gpu_unknown_and_explicit_override(self):
        group = dict(kind="capability-package", resources=[], bindings={})
        store = Store(group)
        controller = object.__new__(Deployments)
        controller.platform = SimpleNamespace(store=store)
        controller.backend = lambda: dict(kind="docker", gpus="device=GPU-example")
        with patch("seampact.hosting.deployment.resolve_gpu_devices", side_effect=lambda values: [dict(gpu_uuid=v) for v in values]):
            self.assertEqual(controller.gpu_allocation("cap", probe=True)["devices"], [])
            group["resources"] = [dict(id="weights", kind="weight"), dict(id="device", kind="parameter")]
            group["bindings"] = {"device": dict(form="value", value="cpu")}
            self.assertEqual(controller.gpu_allocation("cap", probe=True)["devices"], [])
            group["bindings"]["device"]["value"] = "cuda:0"
            self.assertEqual(controller.gpu_allocation("cap", probe=True)["devices"], ["GPU-example"])
            group["resources"] = [dict(id="weights", kind="weight")]
            group["bindings"] = {}
            self.assertEqual(controller.gpu_allocation("cap", probe=True)["devices"], ["GPU-example"])
            store.save("runtime_group_gpu", "cap", dict(devices=[], source="group", status="VERIFIED"))
            self.assertEqual(controller.gpu_allocation("cap", probe=True)["devices"], [])


if __name__ == "__main__":
    unittest.main()
