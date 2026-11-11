"""All-arrivals accounting and actual package calls through the new scheduler.

The HTTP-worker checks reuse the package fixture's explicitly reported image;
they verify execution and close, not a Docker build or measured GPU release.
"""

import copy
import importlib.util
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from seampact.capability_contract import digest
from seampact.scheduling.capability_scheduler import CapabilityScheduler, freeze_config, load_policy, policy_config_id
from seampact.scheduling.unified_objective import ResourceLedger, TaskLedger, freeze_objective
import test_capability_package as package_fixture

wait_for = package_fixture.wait_for


OBJECTIVE = dict(weights=dict(quality=1, delivery=1, latency=0.1, ram=0.2, vram=0.3),
                 scales=dict(latency_s=10, ram_byte_s=100, vram_byte_s=200),
                 quality_evaluator="fixture-score/1: already normalized [0,1]",
                 deadline_s=10, resource_source="reserved")


class ObjectiveAccounting(unittest.TestCase):
    def test_failure_and_late_delivery_remain_in_denominator(self):
        stamp = [100.0]
        ledger = TaskLedger(OBJECTIVE, clock=lambda: stamp[0])
        for task in ("success", "failure", "late"):
            ledger.begin(task)
        stamp[0] = 102.0
        ledger.finish("success", delivered=True, quality=0.9)
        ledger.finish("failure", delivered=False, status="FAILED")
        stamp[0] = 112.0
        late = ledger.finish("late", delivered=True, quality=1)
        self.assertFalse(late["delivered"])
        resources = dict(reserved=dict(ram_byte_s=30, vram_byte_s=40))
        result = ledger.summary(resources)
        self.assertEqual(result["denominator"], 3)
        self.assertEqual(result["raw"]["undelivered"], 2)
        self.assertEqual(result["raw"]["quality_sum"], 0.9)
        self.assertEqual(result["raw"]["latency_s"], 14)
        expected = 0.9 / 3 - 2 / 3 - 0.1 * 14 / 30 - 0.2 * 30 / 300 - 0.3 * 40 / 600
        self.assertAlmostEqual(result["utility"], expected)

    def test_unknown_quality_and_memory_do_not_become_zero(self):
        ledger = TaskLedger(OBJECTIVE)
        ledger.begin("ungraded")
        ledger.finish("ungraded", delivered=True)
        result = ledger.summary(dict(reserved=dict(ram_byte_s=None, vram_byte_s=0)))
        self.assertEqual(result["denominator"], 1)
        self.assertIsNone(result["raw"]["quality_sum"])
        self.assertIsNone(result["utility"])
        self.assertFalse(result["training_ready"])
        self.assertEqual(ledger.active, {})

    def test_shared_lifecycle_once_release_tail_and_partial_measurement(self):
        ledger = ResourceLedger()
        life = dict(instance_id="shared", generation=2, group_id="g", resident_ms=10000,
                    start_s=2, end_s=12, released=True, end_observed=True)
        envelope = dict(g=dict(ram_bytes=100, vram_bytes=200, provenance="fixture"))
        measured = dict(source="measured", attribution="instance", instance_id="shared", generation=2,
                        provenance="fixture cgroup and process attribution", start_s=2, end_s=7,
                        ram_byte_s=400, vram_byte_s=None)
        ledger.record([measured, measured])
        summary = ledger.summary([life, copy.deepcopy(life)], envelope, start_s=0, end_s=12)
        self.assertEqual(summary["reserved"]["ram_byte_s"], 1000)
        self.assertEqual(summary["reserved"]["vram_byte_s"], 2000)
        self.assertEqual(len(summary["instances"]), 1)
        self.assertIsNone(summary["measured"]["ram_byte_s"])
        ledger.record([dict(measured, start_s=7, end_s=12, ram_byte_s=600)])
        summary = ledger.summary([life], envelope, start_s=0, end_s=12)
        self.assertEqual(summary["measured"]["ram_byte_s"], 1000)
        self.assertIsNone(summary["measured"]["vram_byte_s"])
        with self.assertRaisesRegex(ValueError, "重叠"):
            ledger.record([dict(measured, start_s=6, end_s=8)])

    def test_config_identity_is_detached_and_old_objectives_rejected(self):
        config = freeze_config(dict(objective=OBJECTIVE))
        identity = digest(config)
        OBJECTIVE["weights"]["quality"] = 2
        try:
            self.assertEqual(digest(config), identity)
        finally:
            OBJECTIVE["weights"]["quality"] = 1
        with self.assertRaises(ValueError):
            freeze_config(dict(preference="ram"))
        with self.assertRaises(ValueError):
            freeze_objective(dict(OBJECTIVE, schema="joint/1"))

    def test_resource_history_is_accumulated_and_released_identity_is_evicted(self):
        ledger = ResourceLedger()
        for index in range(1000):
            ledger.record([dict(source="measured", attribution="instance", instance_id="one", generation=1,
                provenance="fixture", start_s=index, end_s=index + 1, ram_byte_s=2, vram_byte_s=3)])
        self.assertEqual(len(ledger.active), 1)
        self.assertEqual(ledger.active[("one", 1)]["intervals"], 1000)
        self.assertNotIn("records", ledger.active[("one", 1)])
        life = dict(instance_id="one", generation=1, group_id="g", resident_ms=1000000,
                    start_s=0, end_s=1000, released=True)
        envelopes = dict(g=dict(ram_bytes=2, vram_bytes=3, provenance="fixture"))
        ledger.freeze(life, envelopes, start_s=0, end_s=1000)
        self.assertEqual(ledger.active, {})
        for _ in range(5):
            result = ledger.summary([], envelopes, start_s=0, end_s=2000)
            self.assertEqual(result["measured"]["ram_byte_s"], 2000)
            self.assertEqual(result["reserved"]["vram_byte_s"], 3000)
            self.assertEqual(result["released_instances"], 1)


class ScopedMeasurement(unittest.TestCase):
    def test_vram_excludes_other_container_pids_and_rejects_unstable_members(self):
        from seampact.scheduling.capability_measurement import _gpu_memory
        instance = dict(handle=dict(container="owned"), gpu_devices=["GPU-selected"],
                        gpu_allocation=dict(status="VERIFIED"))
        replies = [SimpleNamespace(returncode=0, stdout="PID\n12\n13\n"),
                   SimpleNamespace(returncode=0, stdout="GPU-selected,12,100\nGPU-selected,999,99999\n"),
                   SimpleNamespace(returncode=0, stdout="PID\n12\n13\n")]
        with patch("seampact.scheduling.capability_measurement._run", side_effect=replies) as run:
            amount, basis = _gpu_memory(instance)
            self.assertEqual(amount, 100 * 2**20)
            self.assertIn("container_host_pids", basis)
            self.assertIn("--id=GPU-selected", run.call_args_list[1].args[0])
        replies[2] = SimpleNamespace(returncode=0, stdout="PID\n12\n14\n")
        with patch("seampact.scheduling.capability_measurement._run", side_effect=replies):
            self.assertIsNone(_gpu_memory(instance)[0])

    def test_ram_sampler_selects_only_given_instance_and_rechecks_backend_identity(self):
        from seampact.scheduling.capability_measurement import sample_instances
        selected = dict(id="owned", group_id="g", generation=1, released=False,
                        handle=dict(kind="docker", container="container"),
                        gpu_devices=[], gpu_allocation=dict(status="VERIFIED"))
        class Controller:
            lock = threading.RLock()
            instances = dict(owned=selected, foreign=dict(selected, id="foreign"))
            def sample_resources(self, instance_ids=None):
                self.selected = instance_ids
                return [dict(instance_id="owned", identity="sha256:container:1", status="OBSERVED",
                             memory_current_bytes=1024, memory_semantics="private_cgroup_v2")]
        controller = Controller()
        with patch("seampact.scheduling.capability_measurement._run",
                   return_value=SimpleNamespace(returncode=0, stdout="sha256:container owned")):
            samples = sample_instances(controller, {"owned"})
        self.assertEqual(controller.selected, {"owned"})
        self.assertEqual(samples[0]["ram_bytes"], 1024)
        self.assertEqual(samples[0]["vram_bytes"], 0)
        with patch("seampact.scheduling.capability_measurement._run",
                   return_value=SimpleNamespace(returncode=0, stdout="sha256:other owned")):
            self.assertIsNone(sample_instances(controller, {"owned"})[0]["ram_bytes"])


@unittest.skipUnless(importlib.util.find_spec("torch"), "optional PyTorch dependency")
class OptionalPPO(unittest.TestCase):
    def test_finite_workload_update_atomic_checkpoint_and_old_schema_rejection(self):
        from seampact.scheduling.capability_ppo import CandidatePPO
        policy = CandidatePPO("frozen-config", OBJECTIVE, training=True,
                              config=dict(hidden_sizes=[8], epochs=1, batch_size=1))
        candidates = [dict(id="one", features=[1.0] + [0.0] * 11),
                      dict(id="two", features=[0.0, 1.0] + [0.0] * 10)]
        decision = dict(config_id="frozen-config", feature_schema="capability-event-features/1", session="workload",
                        observation=[2.0, 0.0, 1.0, 4.0], candidates=candidates)
        decision["selection"] = policy.choose(decision["observation"], candidates)
        summary = dict(training_ready=True, complete=True, objective_id=policy.identity["objective_id"], utility=0.8,
                       policy_config_id="frozen-config", session="workload")
        result = policy.update_workload([decision], dict(before_first=-0.1, after_decisions=[0.9]), summary)
        self.assertTrue(result["updated"])
        self.assertEqual(policy.learner.config["gamma"], 1)
        with tempfile.TemporaryDirectory() as directory:
            current, old = Path(directory) / "current.pt", Path(directory) / "old.pt"
            policy.save(current)
            self.assertEqual(list(Path(directory).glob("*.partial")), [])
            policy.load(current, optimizer=True)
            policy.learner.save(old, checkpoint_metadata=dict(schema="joint/1"))
            with self.assertRaisesRegex(ValueError, "拒绝"):
                policy.load(old)
        with self.assertRaisesRegex(ValueError, "不能更新"):
            policy.update_workload([], dict(before_first=0, after_decisions=[]), dict(summary, training_ready=False))

    def test_unknown_quality_and_discounted_objective_cannot_start_training(self):
        from seampact.scheduling.capability_ppo import CandidatePPO
        with self.assertRaises(ValueError):
            CandidatePPO("config", None, training=True)
        with self.assertRaises(ValueError):
            CandidatePPO("config", OBJECTIVE, config=dict(gamma=0.99))

    def test_frozen_policy_is_loadable_from_scheduler_configuration(self):
        from seampact.scheduling.capability_ppo import CandidatePPO
        config = freeze_config(dict(objective=OBJECTIVE))
        settings = dict(hidden_sizes=[8], epochs=1, batch_size=1)
        policy = CandidatePPO(policy_config_id(config), OBJECTIVE, config=settings)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "frozen.pt"
            policy.save(path)
            config = freeze_config(dict(objective=OBJECTIVE,
                policy=dict(kind="ppo", checkpoint=str(path), learner_config=settings)))
            loaded = load_policy(config)
            selection = loaded.choose([2, 0, 0, 4], [dict(features=[0.0] * 12), dict(features=[1.0] * 12)])
            self.assertIn(selection["index"], (0, 1))
            self.assertFalse(loaded.training)
            with self.assertRaises(ValueError):
                load_policy(freeze_config(dict(config, idle_timeout_s=61)))


class RealWorkerScheduler(unittest.TestCase):
    setUp = package_fixture.PackageLifecycle.setUp
    tearDown = package_fixture.PackageLifecycle.tearDown
    install = package_fixture.PackageLifecycle.install
    reported_image = package_fixture.PackageLifecycle.reported_image
    start_worker = package_fixture.PackageLifecycle.start_worker
    units = package_fixture.PackageLifecycle.units

    def scheduler(self, **config):
        scheduler = CapabilityScheduler(self.platform.capabilities, self.platform.deployments,
                                         store=self.platform.store, config=config)
        self.addCleanup(scheduler.close)
        return scheduler

    def prepared(self, *, auto_start=False):
        self.install()
        self.platform.capabilities.bind(self.group, dict(greeting=dict(form="value", value="hi")))
        artifact = self.reported_image()
        if auto_start:
            original = self.platform.deployments._start
            def start(group_id, run_id, *args, **kwargs):
                started = time.time()
                worker, record = self.start_worker(artifact)
                self.platform.deployments.instances[record["id"]]["run_id"] = run_id
                self.platform.deployments.instances[record["id"]]["start_wall"] = started
                return self.platform.deployments.instances[record["id"]]
            self.platform.deployments._start = start
            self.addCleanup(setattr, self.platform.deployments, "_start", original)
        else:
            self.start_worker(artifact)
        return self.units()

    def test_real_call_failure_and_forced_action_do_not_invoke_policy(self):
        units = self.prepared()
        scheduler = self.scheduler()
        class NeverCalled:
            def choose(self, *args):
                raise AssertionError("single legal action must bypass policy")
        scheduler.policy = NeverCalled()
        scheduler.begin("task")
        result = scheduler.invoke(units["count_words"]["id"], dict(text="alpha beta"), run_id="task")
        self.assertEqual(result["output"]["words"], 2)
        with self.assertRaisesRegex(Exception, "requested failure"):
            scheduler.invoke(units["count_words"]["id"], dict(text="alpha", fail=True), run_id="task")
        scheduler.cancel("task")
        with self.assertRaisesRegex(RuntimeError, "取消"):
            scheduler.invoke(units["count_words"]["id"], dict(text="late"), run_id="task")
        scheduler.finish("task", delivered=False, status="FAILED")
        self.assertNotIn("task", scheduler.cancelled)
        with self.assertRaisesRegex(RuntimeError, "结束"):
            scheduler.invoke(units["count_words"]["id"], dict(text="late"), run_id="task")
        state = scheduler.status()
        self.assertEqual(state["objective"]["denominator"], 1)
        self.assertEqual(state["objective"]["raw"]["quality_sum"], 0)
        self.assertEqual(state["counts"]["forced_actions"], 2)
        self.assertEqual(state["recent_decisions"], [])
        self.assertEqual(scheduler.tasks.active, {})

    def test_on_demand_start_idle_close_and_unconfirmed_release_tail(self):
        units = self.prepared(auto_start=True)
        scheduler = self.scheduler(idle_timeout_s=0.05)
        result = scheduler.invoke(units["count_words"]["id"], dict(text="alpha"), run_id="first")
        first_id = result["call"]["instance_id"]
        wait_for(lambda: self.workers[0][0].closed, timeout=5)
        # This fixture has no Docker object handle. Worker close must not be
        # reported as confirmed physical release or a complete training return.
        instance = self.platform.deployments.instances[first_id]
        wait_for(lambda: instance.get("pending_stop", {}).get("blocked"), timeout=5)
        state = scheduler.status()
        self.assertFalse(instance["released"])
        self.assertGreater(state["objective"]["resources"]["instances"][0]["resident_s"], 0)
        self.assertFalse(state["objective"]["training_ready"])
        self.assertGreaterEqual(state["counts"]["idle_drains"], 1)

    def test_shared_legal_queue_and_cancellation(self):
        units = self.prepared()
        scheduler = self.scheduler(max_in_flight=1)
        entered, release = threading.Event(), threading.Event()
        original = self.platform.capabilities.invoke
        calls, errors = [], []
        def invoke(unit_id, arguments, **kwargs):
            calls.append(arguments)
            if arguments.get("text") == "hold":
                entered.set()
                release.wait(5)
            return original(unit_id, arguments, **kwargs)
        self.platform.capabilities.invoke = invoke
        self.addCleanup(setattr, self.platform.capabilities, "invoke", original)
        def call(text, run_id):
            try:
                scheduler.invoke(units["count_words"]["id"], dict(text=text), run_id=run_id)
            except Exception as error:
                errors.append(error)
        threads = [threading.Thread(target=call, args=(text, text)) for text in ("hold", "queued", "cancel")]
        threads[0].start()
        self.assertTrue(entered.wait(5))
        for thread in threads[1:]:
            thread.start()
        wait_for(lambda: scheduler.status()["queued_calls"] == 2, timeout=5)
        self.assertEqual(scheduler.cancel("cancel")["queued_cancelled"], 1)
        with self.assertRaisesRegex(ValueError, "收尾"):
            scheduler.configure(dict(idle_timeout_s=2))
        release.set()
        for thread in threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertEqual([call["text"] for call in calls], ["hold", "queued"])

    @unittest.skipUnless(importlib.util.find_spec("torch"), "optional PyTorch dependency")
    def test_api_loads_frozen_ppo_configuration(self):
        from seampact.scheduling.capability_ppo import CandidatePPO
        config = freeze_config(dict(objective=OBJECTIVE))
        settings = dict(hidden_sizes=[8], epochs=1, batch_size=1)
        policy = CandidatePPO(policy_config_id(config), OBJECTIVE, config=settings)
        checkpoint = self.root / "policy.pt"
        policy.save(checkpoint)
        body = dict(objective=OBJECTIVE,
                    policy=dict(kind="ppo", checkpoint=str(checkpoint), learner_config=settings))
        request = Request(self.coordinator + "/api/execution/config", data=json.dumps(body).encode(),
                          headers={"Content-Type": "application/json"})
        with urlopen(request) as response:
            result = json.load(response)
        self.assertEqual(result["policy"], "CandidatePPO")
        with urlopen(self.coordinator + "/api/execution") as response:
            state = json.load(response)
        self.assertEqual(state["config_id"], result["config_id"])
        self.assertFalse(self.platform.execution.policy.training)
        checkpoint.unlink()
        restored = CapabilityScheduler(self.platform.capabilities, self.platform.deployments, store=self.platform.store)
        self.addCleanup(restored.close)
        self.assertIsNone(restored.policy)
        self.assertTrue(restored.status()["policy_error"])
        self.assertEqual(restored.configure({})["policy"], "RulePolicy")


if __name__ == "__main__":
    unittest.main()
