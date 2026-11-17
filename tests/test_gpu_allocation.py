"""Explicit worker device selection reaches Docker and freezes research identity."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import threading
import urllib.request
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from seampact.hosting import backends
from seampact.service import Platform
from seampact.server import make_server

A = "GPU-12345678-1234-1234-1234-123456789abc"
B = "GPU-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


def identity_rows(fields, selectors):
    rows = []
    for selector in selectors:
        if selector in ("0", A):
            rows.append(["0", A, "Example A", "80000"])
        elif selector in ("1", B):
            rows.append(["1", B, "Example B", "40000"])
        else:
            return None, "COMMAND_FAILED"
    return rows, None


class GPUAllocationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.platform = Platform(self.root / "var")
        self.addCleanup(self.platform.close)
        self.controller = self.platform.deployments
        # No controller polling or image preparation is part of these checks.
        self.controller.close()
        self.platform.images = None
        self.group = "capability-group"
        self.platform.store.save("runtime_group", self.group, dict(id=self.group, kind="capability-package",
            contract=dict(digest="contract", operations=dict(start=dict(status="SUPPORTED")),
                replicas=dict(replicable=False), host_profile="fixture"), units=[], resources=[], bindings={}))
        self.platform.store.save("deployment", self.group, dict(id=self.group, image="example:local",
            folder=str(self.root), binding_fingerprint="binding", build=dict(image_id="sha256:" + "a" * 64)))
        self.controller.configure_backend(dict(kind="docker", coordinator_url="http://coordinator.invalid",
            docker_network="example-network"))
        self.enterContext(patch("seampact.observability.resource_observation.query", side_effect=identity_rows))
        self.enterContext(patch.object(self.controller, "_start_gap", return_value=None))
        self.enterContext(patch("seampact.hosting.backends.docker_available", return_value=None))
        self.commands = []
        def run(argv, timeout, **kwargs):
            self.commands.append(argv)
            return dict(argv=argv, returncode=0, duration_ms=1, stdout="container", stderr="", output="container")
        self.enterContext(patch("seampact.hosting.backends.run", side_effect=run))

    def save(self, devices):
        return self.controller.configure_gpus(dict(group_id=self.group, devices=devices))

    def test_group_assignment_is_canonical_persistent_and_polling_does_not_probe(self):
        saved = self.save([0, A, 1])
        self.assertEqual(saved["devices"], [A, B])
        with patch("seampact.hosting.deployment.resolve_gpu_devices", side_effect=AssertionError("polling must not probe")):
            self.assertEqual(self.controller.gpu_allocation(self.group)["devices"], [A, B])
        self.assertEqual(self.platform.store.get("runtime_group_gpu", self.group)["devices"], [A, B])
        with self.assertRaisesRegex(ValueError, "无法识别"):
            self.save([A, 99])
        self.assertEqual(self.controller.gpu_allocation(self.group)["devices"], [A, B])

    def test_selected_multi_gpu_ids_reach_real_command_builder_and_instance_evidence(self):
        self.save([1, 0])
        record = self.controller.start(self.group, {})
        self.assertEqual(record["gpu_devices"], [B, A])
        argv = self.commands[-1]
        self.assertEqual(argv[argv.index("--gpus") + 1], '"device=' + B + ',' + A + '"')
        self.assertIn("NVIDIA_VISIBLE_DEVICES=" + B + "," + A, argv)
        self.assertEqual(record["state"], "STARTING")
        with self.assertRaisesRegex(ValueError, "实例"):
            self.save([A])

    def test_explicit_start_selection_updates_cold_default_and_empty_selection_omits_gpus(self):
        first = self.controller.start(self.group, dict(gpu_devices=[1]))
        self.assertEqual(first["gpu_devices"], [B])
        self.assertEqual(self.controller.gpu_allocation(self.group)["devices"], [B])
        self.controller.instances[first["id"]].update(state="STOPPED", released=True)
        second = self.controller.start(self.group, dict(gpu_devices=[]))
        self.assertEqual(second["gpu_devices"], [])
        self.assertNotIn("--gpus", self.commands[-1])
        self.assertIn("NVIDIA_VISIBLE_DEVICES=void", self.commands[-1])

    def test_frozen_catalog_and_run_lock_reject_changed_devices_before_dispatch(self):
        self.save([0])
        placement = dict(group_id=self.group, gpu_devices=[A])
        self.controller.lock_groups("run", {self.group: placement}, {})
        with self.assertRaisesRegex(ValueError, "运行"):
            self.save([1])
        changed = copy.deepcopy(self.platform.store.get("runtime_group_gpu", self.group))
        changed["devices"] = [B]
        self.platform.store.save("runtime_group_gpu", self.group, changed)
        with self.assertRaisesRegex(ValueError, "GPU 分配已变化"):
            self.controller.check_gpu_placement(placement)
        with self.assertRaisesRegex(ValueError, "GPU 分配已变化"):
            self.controller.invoke_placed(dict(placement, run_id="run"), dict(id="unit"), {})
        self.assertEqual(self.commands, [])

    def test_unreleased_failure_and_frozen_image_scope_block_reconfiguration(self):
        self.save([A])
        self.controller.instances["unknown"] = dict(id="unknown", group_id=self.group, state="FAILED", released=False,
            handle=dict(kind="docker", container="unreleased"))
        with self.assertRaisesRegex(ValueError, "实例"):
            self.save([B])
        self.controller.instances.clear()
        self.platform.images = SimpleNamespace(guard_artifact=lambda _: (_ for _ in ()).throw(ValueError("image scope frozen")))
        with self.assertRaisesRegex(ValueError, "image scope frozen"):
            self.save([B])
        self.assertEqual(self.controller.gpu_allocation(self.group)["devices"], [A])

    def test_global_device_config_is_exact_and_cannot_change_while_held(self):
        config = dict(self.controller.backend(), gpus='"device=0,1"')
        saved = self.controller.configure_backend(config)
        self.assertEqual(saved["gpus"], "device=" + A + "," + B)
        for selection in ["all", "2"]:
            with self.subTest(selection=selection), self.assertRaises(ValueError):
                self.controller.configure_backend(dict(config, gpus=selection))
        self.controller.instances["held"] = dict(id="held", group_id=self.group, state="UNKNOWN", released=False)
        with self.assertRaisesRegex(ValueError, "释放实例"):
            self.controller.configure_backend(dict(config, gpus="device=0"))



    def test_http_probe_save_and_explicit_start_share_identity_resolution(self):
        server = make_server(self.platform, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = "http://127.0.0.1:" + str(server.server_port)
        def request(path, body):
            req = urllib.request.Request(base + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=5) as response:
                return json.load(response)
        try:
            probed = request("/api/gpus/probe", dict(devices=["0"]))
            self.assertEqual(probed["gpu_uuids"], [A])
            self.assertEqual(self.platform.monitor.gpu_settings["devices"], [])
            saved = request("/api/deployments/gpus", dict(group_id=self.group, devices=["0"]))
            self.assertEqual(saved["devices"], [A])
            started = request("/api/deployments/start", dict(group_id=self.group, gpu_devices=["1"]))
            self.assertEqual(started["gpu_devices"], [B])
            self.assertIn(B, self.commands[-1][self.commands[-1].index("--gpus") + 1])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)


if __name__ == "__main__":
    unittest.main()
