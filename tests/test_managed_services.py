"""Minimal service-control checks: fake Docker, real local HTTP health/models."""

import copy
import json
import os
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from seampact.hosting.managed_services import ManagedServices
from seampact.store import Store
from seampact.providers import ProviderError

IMAGE = "sha256:" + "a" * 64
GPU_A = "GPU-12345678-1234-1234-1234-123456789abc"
GPU_B = "GPU-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
GPU_C = "GPU-bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


class FakeDocker:
    def __init__(self):
        self.commands = []
        self.containers = {}
        self.image = IMAGE
        self.unavailable = False
        self.keep_deleted = False
        self.counter_output = "0::/\nusage_usec 2500000\nuser_usec 2000000\nsystem_usec 500000\n1048576\n"
        self.change_pid_during_sample = False

    def __call__(self, argv, timeout):
        self.commands.append(list(argv))
        code, output = 0, ""
        if self.unavailable:
            return dict(returncode=1, stdout="", stderr="private-engine-output")
        if argv[:3] == ["docker", "image", "inspect"]:
            output = json.dumps(dict(Id=self.image, RepoDigests=[]))
        elif argv[:2] == ["docker", "run"]:
            name = argv[argv.index("--name") + 1]
            if name in self.containers:
                code = 1
            else:
                labels = dict(argv[i + 1].split("=", 1) for i, arg in enumerate(argv) if arg == "--label")
                instance = labels["seampact.instance"]
                identifier = (instance * 2)[:64]
                self.containers[name] = dict(Id=identifier, Image=IMAGE, instance=instance,
                    spec=labels["seampact.spec"], State=dict(Running=True, Pid=42, StartedAt="2026-01-01T00:00:00Z"))
                output = identifier
        elif argv[:2] == ["docker", "inspect"]:
            match = next((value for name, value in self.containers.items() if argv[-1] in (name, value["Id"])), None)
            if match:
                output = json.dumps(match)
            else:
                code = 1
        elif argv[:2] == ["docker", "ps"]:
            name = argv[argv.index("--filter") + 1].removeprefix("name=^/").removesuffix("$")
            output = self.containers.get(name, {}).get("Id", "")
        elif argv[:2] == ["docker", "stop"]:
            for value in self.containers.values():
                if value["Id"] == argv[-1]:
                    value["State"]["Running"] = False
        elif argv[:2] == ["docker", "rm"]:
            if not self.keep_deleted:
                self.containers = {name: value for name, value in self.containers.items() if value["Id"] != argv[-1]}
        elif argv[:2] == ["docker", "exec"]:
            output = self.counter_output
            if self.change_pid_during_sample:
                for value in self.containers.values():
                    value["State"]["Pid"] += 1
        else:
            raise AssertionError(argv)
        return dict(returncode=code, stdout=output, stderr="private-engine-output")


class Handler(BaseHTTPRequestHandler):
    model = "test-model"
    health = 200
    paths = []
    redirect = None

    def do_GET(self):
        type(self).paths.append(self.path)
        if self.redirect:
            self.send_response(302)
            self.send_header("Location", self.redirect)
            self.end_headers()
            return
        self.send_response(self.health if self.path == "/health" else 200)
        self.end_headers()
        self.wfile.write(json.dumps(dict(data=[dict(id=self.model)]) if self.path == "/v1/models" else {}).encode())

    def log_message(self, *args):
        pass


class ServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "state.sqlite")
        Handler.model, Handler.health, Handler.paths, Handler.redirect = "test-model", 200, [], None
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        deployments = SimpleNamespace(lock=threading.RLock(), instances={}, run_locks={}, event_log=[],
            listeners=[], environment_changes={}, session="session")
        def guard(identity, run_id):
            owner = deployments.run_locks.get(identity)
            if owner and owner["run_id"] != run_id:
                raise ValueError("another run owns service")
        def event(record, kind, **facts):
            item = dict(kind=kind, wall=1.0, group_id=record["group_id"], instance_id=record["id"], **facts)
            deployments.event_log.append(item)
            self.store.save("runtime_instance", record["id"], record)
            for listener in deployments.listeners:
                listener(item)
        deployments._guard, deployments._event = guard, event
        deployments.backend = lambda: dict(kind="docker")
        self.platform = SimpleNamespace(store=self.store, deployments=deployments)
        self.docker = FakeDocker()
        self.manager = ManagedServices(self.platform, runner=self.docker)
        port = self.http.server_address[1]
        self.spec = dict(id="model-a", model="test-model", model_identity="public-model@revision", weight_identity="sha256:weights",
            image="engine:local", argv=["--model", "/weights/model", "--port", "8000"],
            endpoint="http://127.0.0.1:" + str(port) + "/v1", docker=dict(container_port=8000, publish_port=port,
                mounts=[dict(source="/weights-source", target="/weights", read_only=True)], pass_env=["MODEL_SERVICE_KEY"]),
            contract=dict(exclusive_control=True, request_isolation=True, rebuildable=True, background_work=False,
                          control_owner="experiment", max_concurrency=2),
            resource_envelope=dict(host_memory_mb=512, devices={}, basis="test upper bound"), start_timeout_s=0.2)
        self.manager.configure(self.spec)

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join()
        self.store.close()
        self.tmp.cleanup()

    def test_image_frozen_argv_and_real_model_probe(self):
        self.docker.image = IMAGE
        record = self.manager.start("service:model-a", {})
        self.assertEqual((record["state"], record["model_state"]), ("READY", "MODEL_READY"))
        argv = next(argv for argv in self.docker.commands if argv[:2] == ["docker", "run"])
        self.assertEqual(argv[argv.index(IMAGE) + 1:], self.spec["argv"])
        self.assertIn("NVIDIA_VISIBLE_DEVICES=void", argv)
        self.assertNotIn("/invoke", argv)
        self.assertIn("/health", Handler.paths)
        self.assertIn("/v1/models", Handler.paths)
        self.assertNotIn("private-engine-output", json.dumps(self.manager.public()))

    def test_concurrent_start_one_container(self):
        with ThreadPoolExecutor(max_workers=8) as executor:
            records = list(executor.map(lambda _: self.manager.start("model-a", {}, run_id="r1"), range(8)))
        self.assertEqual(len({record["id"] for record in records}), 1)
        self.assertEqual(sum(argv[:2] == ["docker", "run"] for argv in self.docker.commands), 1)

    def test_two_users_prevent_stop_until_both_end(self):
        self.manager.acquire("model-a", "task", run_id="r1")
        self.manager.acquire("model-a", "teacher", run_id="r1")
        record = self.manager.start("model-a", {}, run_id="r1")
        self.manager.drain_stop("model-a", {}, run_id="r1")
        self.manager.release_lease("task")
        self.manager.refresh()
        self.assertFalse(self.manager.instance_fact(record["id"])["released"])
        self.manager.release_lease("teacher")
        self.manager.refresh()
        self.assertTrue(self.manager.instance_fact(record["id"])["released"])

    def test_unknown_request_retains_lease_and_capacity(self):
        self.manager.start("model-a", {}, run_id="r1")
        self.manager.acquire("model-a", "task", run_id="r1")
        with self.assertRaises(TimeoutError):
            with self.manager.request_scope(["service:model-a"], "task", run_id="r1"):
                raise TimeoutError("private network information")
        self.manager.release_lease("task")
        with self.assertRaises(ValueError):
            self.manager.release_run("model-a", "r1")
        self.manager.drain_stop("model-a", dict(release_unknown=True), run_id="r1")
        self.assertEqual(self.manager.inflight("model-a"), 1)
        self.assertTrue(self.manager.live_instances("model-a"))

    def test_wrong_model_and_health_never_ready(self):
        Handler.model = "another-model"
        record = self.manager.start("model-a", {})
        self.assertEqual(record["state"], "STARTING")
        self.assertFalse(record["accepting"])
        Handler.model = "test-model"
        Handler.health = 503
        self.manager.refresh()
        self.assertNotEqual(self.manager.instance_fact(record["id"])["state"], "READY")

    def test_backend_disconnected_is_not_release(self):
        record = self.manager.start("model-a", {})
        self.docker.unavailable = True
        self.manager.refresh()
        fact = self.manager.instance_fact(record["id"])
        self.assertEqual(fact["state"], "UNKNOWN")
        self.assertFalse(fact["released"])

    def test_rm_success_without_confirmed_deletion_is_held(self):
        record = self.manager.start("model-a", {})
        self.docker.keep_deleted = True
        self.manager.drain_stop("model-a", {})
        fact = self.manager.instance_fact(record["id"])
        self.assertFalse(fact["released"])
        self.assertEqual(fact["state"], "UNKNOWN")

    def test_restart_stable_endpoint_new_generation(self):
        first = self.manager.start("model-a", {})
        self.manager.drain_stop("model-a", {})
        second = self.manager.start("model-a", {})
        self.assertEqual(first["endpoint"], second["endpoint"])
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(second["generation"], first["generation"] + 1)

    def test_run_exclusive_and_config_immutable_while_held(self):
        self.manager.lock_groups("r1", {"service:model-a": self.manager.placement("model-a")}, {})
        with self.assertRaises(ValueError):
            self.manager.start("model-a", {}, run_id="r2")
        with self.assertRaises(ValueError):
            self.manager.configure(self.spec)
        self.manager.start("model-a", {}, run_id="r1")
        with self.assertRaises(ValueError):
            self.manager.acquire("model-a", "intruder", run_id="r2")

    def test_private_argv_and_wrong_connection_rejected(self):
        bad = copy.deepcopy(self.spec)
        bad["argv"] += ["--api-key", "private-value"]
        with self.assertRaises(ValueError):
            self.manager.configure(bad)
        with self.assertRaises(ValueError):
            self.manager.validate_connection(dict(managed_service_id="model-a", base_url=self.spec["endpoint"], model="other"))
        self.assertEqual(self.manager.get("model-a")["image_id"], IMAGE)

    def test_active_request_limits_and_ordinary_error_releases(self):
        self.manager.start("model-a", {})
        with self.assertRaisesRegex(ValueError, "business"):
            with self.manager.request_scope(["model-a"], "direct"):
                self.assertEqual(self.manager.inflight("model-a"), 1)
                raise ValueError("business")
        self.assertEqual(self.manager.inflight("model-a"), 0)
        self.assertFalse(self.manager.leases["service:model-a"])

    def test_wrapped_provider_error_retains_unknown(self):
        record = self.manager.start("model-a", {})
        with self.assertRaises(ProviderError):
            with self.manager.request_scope(["model-a"], "direct"):
                raise ProviderError("private detail", kind="response_incomplete")
        self.assertEqual(self.manager.instance_fact(record["id"])["unknown_requests"], 1)
        self.assertTrue(self.manager.leases["service:model-a"]["direct"]["unknown"])

    def test_partial_scope_admission_never_marks_not_sent_request_unknown(self):
        record = self.manager.start("model-a", {})
        with self.assertRaises(ValueError):
            with self.manager.request_scope(["model-a", "missing-service"], "direct"):
                self.fail("scope with missing dependency cannot invoke")
        self.assertEqual(self.manager.instance_fact(record["id"])["unknown_requests"], 0)
        self.assertEqual(self.manager.inflight("model-a"), 0)

    def test_request_slot_wait_releases_lock_and_dispatches_after_end(self):
        self.manager.start("model-a", {})
        entered, finish, next_entered = threading.Event(), threading.Event(), threading.Event()
        def first():
            with self.manager.request_scope(["model-a"], "first"):
                entered.set()
                finish.wait(3)
        def second():
            with self.manager.request_scope(["model-a"], "second"):
                next_entered.set()
        self.manager.get("model-a")
        with self.manager.lock:
            # Configure limit was 2; use both slots in the first owner's scope.
            self.manager.acquire("model-a", "blocker")
            blocker = self.manager.begin_request("model-a", "blocker")
        with ThreadPoolExecutor(max_workers=2) as executor:
            one = executor.submit(first)
            self.assertTrue(entered.wait(2))
            two = executor.submit(second)
            self.assertFalse(next_entered.wait(0.1))
            # This requires the same controller lock held by request_scope.
            self.manager.end_request(blocker, "blocker")
            self.assertTrue(next_entered.wait(2))
            finish.set()
            one.result(2)
            two.result(2)
        self.manager.release_lease("blocker")

    def test_cgroup_counter_sample_has_generation_identity(self):
        record = self.manager.start("model-a", {})
        sample = self.manager.sample_resources(["service:model-a"])[0]
        self.assertEqual(sample["status"], "OBSERVED")
        self.assertEqual(sample["cpu_seconds"], 2.5)
        self.assertEqual(sample["memory_current_bytes"], 1048576)
        self.assertEqual(sample["generation"], record["generation"])
        self.assertIsNone(sample["gpu_memory_bytes"])
        command = next(argv for argv in self.docker.commands if argv[:2] == ["docker", "exec"])
        self.assertEqual(command[3:], ["cat", "/proc/self/cgroup", "/sys/fs/cgroup/cpu.stat", "/sys/fs/cgroup/memory.current"])

    def test_cgroup_unconfirmed_namespace_or_changed_identity_unknown(self):
        self.manager.start("model-a", {})
        self.docker.counter_output = "0::/host/path\nusage_usec 1\n1024\n"
        self.assertEqual(self.manager.sample_resources(["model-a"])[0]["status"], "UNKNOWN")
        self.docker.change_pid_during_sample = True
        self.assertEqual(self.manager.sample_resources(["model-a"])[0]["reason"], "IDENTITY_CHANGED_DURING_SAMPLE")

    def test_probe_redirect_never_contacts_target_or_forwards_credential(self):
        received = []
        class Target(BaseHTTPRequestHandler):
            def do_GET(self):
                received.append(self.headers.get("Authorization"))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")
            def log_message(self, *args):
                pass
        target = ThreadingHTTPServer(("127.0.0.1", 0), Target)
        thread = threading.Thread(target=target.serve_forever, daemon=True)
        thread.start()
        try:
            Handler.redirect = "http://127.0.0.1:" + str(target.server_address[1]) + "/destination"
            self.spec["key_env"] = "MODEL_SERVICE_KEY"
            self.manager.configure(self.spec)
            with patch.dict(os.environ, {"MODEL_SERVICE_KEY": "test-only-probe-value"}):
                record = self.manager.start("model-a", {})
            self.assertNotEqual(record["state"], "READY")
            self.assertEqual(received, [])
            self.assertNotIn("test-only-probe-value", json.dumps(self.manager.public()))
        finally:
            target.shutdown()
            target.server_close()
            thread.join()

    def test_overlapping_request_scopes_record_one_union_interval(self):
        record = self.manager.start("model-a", {})
        self.manager.acquire("model-a", "one")
        self.manager.acquire("model-a", "two")
        first = self.manager.begin_request("model-a", "one")
        opening = self.manager.instance_fact(record["id"])["busy_scope_started_wall"]
        second = self.manager.begin_request("model-a", "two")
        self.manager.end_request(first, "one")
        active = self.manager.instance_fact(record["id"])
        self.assertEqual(active["busy_scope_started_wall"], opening)
        self.assertEqual(active["busy_scope_intervals"], [])
        self.manager.end_request(second, "two")
        complete = self.manager.instance_fact(record["id"])
        self.assertEqual(len(complete["busy_scope_intervals"]), 1)
        self.assertEqual(complete["busy_scope_intervals"][0][0], opening)
        self.assertIsNone(complete["busy_scope_started_wall"])
        self.assertFalse(complete["busy_scope_unknown"])
        self.manager.begin_request("model-a", "one")
        self.manager.end_request(first, "one", unknown=True)
        self.assertTrue(self.manager.instance_fact(record["id"])["busy_scope_unknown"])

    def test_distinct_service_ids_cannot_alias_one_network_target(self):
        second = copy.deepcopy(self.spec)
        second["id"] = "model-b"
        second["endpoint"] = second["endpoint"].replace("127.0.0.1", "localhost") + "/another-base"
        second["docker"]["publish_host"] = "localhost"
        with self.assertRaisesRegex(ValueError, "同一个 service id"):
            self.manager.configure(second)
        self.assertEqual(len(self.manager.groups()), 1)

    def gpu_probe(self):
        def query(fields, selected):
            self.assertEqual(fields, "index,uuid,name,memory.total")
            self.assertEqual(len(selected), 1)
            identities = {"0": GPU_A, "1": GPU_B, "2": GPU_C}
            identity = identities.get(str(selected[0]), selected[0])
            if identity not in identities.values():
                return None, "COMMAND_FAILED"
            index = next(index for index, device in identities.items() if device == identity)
            return [[index, identity, "Example GPU", "80000"]], None
        return patch("seampact.observability.resource_observation.query", side_effect=query)

    def test_full_gpu_spec_normalizes_indices_with_original_per_device_demands(self):
        spec = copy.deepcopy(self.spec)
        spec["docker"]["gpus"] = '"device=0,1"'
        spec["resource_envelope"]["devices"] = {"0": dict(memory_mb=512), "1": dict(memory_mb=1024)}
        with self.gpu_probe():
            saved = self.manager.configure(spec)
        self.assertEqual(saved["docker"]["gpus"], '"device=' + GPU_A + ',' + GPU_B + '"')
        self.assertEqual(saved["resource_envelope"]["devices"], {GPU_A: dict(memory_mb=512), GPU_B: dict(memory_mb=1024)})
        self.assertEqual(saved["argv"], self.spec["argv"])
        self.assertEqual(saved["resource_envelope"]["host_memory_mb"], 512)

    def test_explicit_service_gpu_assignment_is_frozen_in_real_start_argv(self):
        original = self.manager.get("model-a")
        with self.gpu_probe():
            saved = self.manager.set_gpu_devices(dict(service_id="model-a", devices=[2],
                memory_mb={GPU_C: 512}, basis="explicit original model upper bound"))
            record = self.manager.start("model-a", dict(gpu_devices=[2]))
        self.assertNotEqual(saved["spec_digest"], original["spec_digest"])
        for key in ("argv", "model", "model_identity", "weight_identity", "contract"):
            self.assertEqual(saved[key], original[key])
        self.assertEqual(saved["resource_envelope"]["devices"], {GPU_C: dict(memory_mb=512)})
        self.assertEqual(record["gpu_devices"], [GPU_C])
        self.assertEqual(record["service_spec"]["spec_digest"], saved["spec_digest"])
        argv = next(argv for argv in self.docker.commands if argv[:2] == ["docker", "run"])
        self.assertEqual(argv[argv.index("--gpus") + 1], "device=" + GPU_C)
        self.assertIn("NVIDIA_VISIBLE_DEVICES=" + GPU_C, argv)

    def test_equal_gpu_demand_moves_without_substituting_whole_card_capacity(self):
        with self.gpu_probe():
            self.manager.set_gpu_devices(dict(service_id="model-a", devices=[GPU_A], memory_mb={GPU_A: 768}, basis="model upper bound"))
            moved = self.manager.set_gpu_devices(dict(service_id="model-a", devices=[GPU_B]))
        self.assertEqual(moved["resource_envelope"]["devices"], {GPU_B: dict(memory_mb=768)})
        self.assertEqual(moved["resource_envelope"]["basis"], "model upper bound")

    def test_heterogeneous_or_resized_gpu_demand_needs_explicit_mapping(self):
        with self.gpu_probe():
            original = self.manager.set_gpu_devices(dict(service_id="model-a", devices=[GPU_A, GPU_B],
                memory_mb={GPU_A: 512, GPU_B: 1024}, basis="per-device model upper bounds"))
            for devices in ([GPU_B, GPU_C], [GPU_A], []):
                with self.subTest(devices=devices), self.assertRaises(ValueError):
                    self.manager.set_gpu_devices(dict(service_id="model-a", devices=devices))
                self.assertEqual(self.manager.get("model-a"), original)
            moved = self.manager.set_gpu_devices(dict(service_id="model-a", devices=[GPU_B, GPU_C],
                memory_mb={GPU_B: 1024, GPU_C: 512}, basis="explicit per-device transfer"))
        self.assertEqual(moved["resource_envelope"]["devices"], {GPU_B: dict(memory_mb=1024), GPU_C: dict(memory_mb=512)})

    def test_invalid_gpu_assignment_keeps_the_complete_previous_spec(self):
        previous = self.manager.get("model-a")
        invalid = [dict(devices=["9"], memory_mb={GPU_A: 512}, basis="explicit"),
            dict(devices=[GPU_A], memory_mb={GPU_A: True}, basis="explicit"),
            dict(devices=[GPU_A], memory_mb={GPU_B: 512}, basis="explicit"),
            dict(devices=[GPU_A], memory_mb={GPU_A: 512}),
            dict(devices=[GPU_A]), dict(devices="0")]
        with self.gpu_probe():
            for body in invalid:
                with self.subTest(body=body), self.assertRaises(ValueError):
                    self.manager.set_gpu_devices(dict(service_id="model-a", **body))
                self.assertEqual(self.manager.get("model-a"), previous)
        with self.gpu_probe(), self.assertRaisesRegex(ValueError, "同一 GPU"):
            spec = copy.deepcopy(self.spec)
            spec["docker"]["gpus"] = "device=0," + GPU_A
            spec["resource_envelope"]["devices"] = {"0": dict(memory_mb=512), GPU_A: dict(memory_mb=512)}
            self.manager.configure(spec)
        self.assertEqual(self.manager.get("model-a"), previous)

    def test_service_gpu_assertion_cannot_override_frozen_spec_or_hot_swap(self):
        with self.gpu_probe():
            saved = self.manager.set_gpu_devices(dict(service_id="model-a", devices=[GPU_A], memory_mb={GPU_A: 512}, basis="explicit"))
            with self.assertRaisesRegex(ValueError, "冷态保存"):
                self.manager.start("model-a", dict(gpu_devices=[GPU_B]))
            self.assertFalse(any(argv[:2] == ["docker", "run"] for argv in self.docker.commands))
            record = self.manager.start("model-a", dict(gpu_devices=[0]))
            with self.assertRaisesRegex(ValueError, "冷态保存"):
                self.manager.set_gpu_devices(dict(service_id="model-a", devices=[GPU_B]))
        self.assertEqual(record["gpu_devices"], [GPU_A])
        self.assertEqual(self.manager.get("model-a"), saved)

    def test_service_gpu_update_is_blocked_by_lease_run_or_image_scope(self):
        body = dict(service_id="model-a", devices=[GPU_A], memory_mb={GPU_A: 512}, basis="explicit")
        previous = self.manager.get("model-a")
        self.manager.acquire("model-a", "task")
        with patch("seampact.hosting.managed_services.resolve_gpu_devices") as probe:
            with self.assertRaisesRegex(ValueError, "仍被运行"):
                self.manager.set_gpu_devices(body)
            with self.assertRaisesRegex(ValueError, "仍被运行"):
                self.manager.configure(self.spec)
        probe.assert_not_called()
        self.manager.release_lease("task")
        self.platform.deployments.run_locks["service:model-a"] = dict(run_id="research")
        with self.assertRaisesRegex(ValueError, "仍被运行"):
            self.manager.set_gpu_devices(body)
        self.platform.deployments.run_locks.clear()
        self.platform.images = SimpleNamespace(guard_artifact=lambda _: (_ for _ in ()).throw(ValueError("frozen image scope")))
        with self.assertRaisesRegex(ValueError, "frozen image scope"):
            self.manager.set_gpu_devices(body)
        self.assertEqual(self.manager.get("model-a"), previous)

    def test_legacy_numeric_service_spec_must_be_saved_before_start(self):
        spec = self.manager.get("model-a")
        spec["docker"]["gpus"] = "device=0"
        spec["resource_envelope"]["devices"] = {"0": dict(memory_mb=512)}
        self.store.save("managed_service", "model-a", spec)
        with self.gpu_probe(), self.assertRaisesRegex(ValueError, "冷态保存"):
            self.manager.start("model-a", {})
        self.assertFalse(any(argv[:2] == ["docker", "run"] for argv in self.docker.commands))


if __name__ == "__main__":
    unittest.main()
