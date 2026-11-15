"""All-arrivals accounting and actual package calls through the new scheduler.

The HTTP-worker checks reuse the package fixture's explicitly reported image;
they verify execution and close, not a Docker build or measured GPU release.
"""

import copy
from contextlib import closing
import importlib.util
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from queue import Queue, SimpleQueue
from types import SimpleNamespace
from unittest.mock import patch
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from seampact.capability_contract import digest
from seampact.scheduling.capability_scheduler import (
    CANDIDATE_DIM, FEATURE_SCHEMA, OBSERVATION_DIM, CapabilityScheduler, freeze_config, load_policy, policy_config_id)
from seampact.scheduling.unified_objective import ResourceLedger, TaskLedger, freeze_objective
from seampact.store import Store
import test_capability_package as package_fixture

wait_for = package_fixture.wait_for


OBJECTIVE = dict(weights=dict(quality=1, delivery=1, latency=0.1, ram=0.2, vram=0.3, api=0),
                 scales=dict(latency_s=10, ram_byte_s=100, vram_byte_s=200, api_tokens=1),
                 quality_evaluator="fixture-score/1: already normalized [0,1]",
                 deadline_s=10, resource_source="reserved")


class ElasticCandidateTests(unittest.TestCase):
    def setUp(self):
        self.group = dict(id="group", package_digest="package", installation_id="install",
            contract=dict(digest="contract", concurrency=1, replicas=dict(replicable=True),
                          operations=dict(prewarm=dict(status="SUPPORTED"), add_replica=dict(status="SUPPORTED"))))
        self.deployments = SimpleNamespace(lock=threading.RLock(), instances={}, affinity={},
            runtime_fact=lambda group, unit: dict(ready_instance=bool(self.deployments.instances),
                cold_startable=not self.deployments.instances, warm=False), backend=lambda: dict(start_timeout_s=1))
        s = self.scheduler = CapabilityScheduler.__new__(CapabilityScheduler)
        s._config = freeze_config(dict(elastic_actions=True, max_replicas_per_group=2, max_in_flight=4))
        s.catalog = SimpleNamespace(_group=lambda group: self.group)
        s.deployments = self.deployments
        s.condition = threading.Condition(threading.RLock())
        s.queue, s.running, s.owned = {}, {}, {}
        s.groups, s.tracked = {"group"}, set()
        s.elastic_active, s.elastic_runs, s.elastic_attempted, s.elastic_waited = {}, __import__("collections").Counter(), set(), {}
        s.retention_due, s.force_drain, s.closed = {}, False, False
        s.instance_events, s.measure_wake = SimpleQueue(), threading.Event()
        s.counts, s.store, s.workload, s.session = __import__("collections").Counter(), None, None, "test-session"
        self.request = dict(id="waiting", unit_id="unit", group_id="group", run_id="task",
            execution_run_id="task", concurrency=1, queued_wall=time.time(),
            identity=dict(package_digest="package", contract_digest="contract", installation_id="install"))

    def instance(self, identity="first", *, inflight=0):
        self.deployments.instances[identity] = dict(id=identity, group_id="group", generation=1,
            state="READY", accepting=True, released=False, units=["unit"], in_flight=inflight,
            ready_wall=time.time(), last_activity_wall=time.time())
        return self.deployments.instances[identity]

    def test_cold_prewarm_only_with_real_demand_and_rule_still_dispatches(self):
        s = self.scheduler
        self.assertFalse(any(c["kind"] == "prewarm" for c in s._candidates()[1]))
        s.queue["waiting"] = self.request
        choices = s._candidates()[1]
        self.assertEqual({c["kind"] for c in choices}, {"dispatch", "prewarm"})
        from seampact.scheduling.capability_scheduler import RulePolicy
        self.assertEqual(choices[RulePolicy().choose([], choices)["index"]]["kind"], "dispatch")
        s.elastic_attempted.add(("prewarm", "waiting"))
        self.assertEqual([c["kind"] for c in s._candidates()[1]], ["dispatch"])

    def test_overload_replica_and_wait_are_bounded_and_respect_affinity(self):
        s = self.scheduler
        self.instance(inflight=1)
        s.queue["waiting"] = self.request
        s.running["active"] = dict(id="active", group_id="group")
        choices = s._candidates()[1]
        self.assertEqual({c["kind"] for c in choices}, {"wait", "add_replica"})
        from seampact.scheduling.capability_scheduler import RulePolicy
        self.assertEqual(choices[RulePolicy().choose([], choices)["index"]]["kind"], "wait")
        pressure = RulePolicy(dict(kind="rule", scale_when_saturated=True))
        self.assertEqual(choices[pressure.choose([], choices)["index"]]["kind"], "add_replica")
        s.elastic_active.update({str(i): dict(kind="prewarm") for i in range(3)})
        self.assertEqual(s._candidates()[1], [])
        s.elastic_active.clear()
        wait = next(c for c in choices if c["kind"] == "wait")
        s.elastic_waited["group"] = wait["wait_token"]
        self.assertEqual(s._candidates()[1], [])
        s.elastic_waited.clear()
        self.deployments.affinity[("task", "group")] = "first"
        self.assertEqual(s._candidates()[1], [])
        self.deployments.affinity.clear()
        self.instance("second")
        self.assertEqual([c["kind"] for c in s._candidates()[1]], ["dispatch"])
        self.group["contract"]["replicas"]["replicable"] = False
        self.assertEqual(s._candidates()[1], [])

    def test_busy_group_offers_only_proven_idle_owned_replica(self):
        s = self.scheduler
        first, second = self.instance("first", inflight=1), self.instance("second")
        s.owned["second"] = dict(group_id="group", run_id="owner")
        s.running["active"] = dict(id="active", group_id="group", instance_id="first")
        self.deployments.affinity[("task", "group")] = "first"

        def release_choices():
            return [c for c in s._candidates()[1] if c.get("instance_id") == "second"]

        choices = release_choices()
        self.assertEqual({c["kind"] for c in choices}, {"retain", "drain_stop"})
        self.assertTrue(all(c["idle_replica"] for c in choices))
        cases = [
            (lambda: s.queue.update(waiting=self.request), lambda: s.queue.clear()),
            (lambda: s.elastic_active.update(group=dict(kind="add_replica")), lambda: s.elastic_active.clear()),
            (lambda: s.running["active"].pop("instance_id"), lambda: s.running["active"].update(instance_id="first")),
            (lambda: s.running["active"].update(instance_id="second"), lambda: s.running["active"].update(instance_id="first")),
            (lambda: first.update(in_flight=0), lambda: first.update(in_flight=1)),
            (lambda: first.update(state="DRAINING"), lambda: first.update(state="READY")),
            (lambda: first.update(state="STARTING"), lambda: first.update(state="READY")),
            (lambda: second.update(in_flight=1), lambda: second.update(in_flight=0)),
            (lambda: self.deployments.affinity.update({("other", "group"): "second"}),
             lambda: self.deployments.affinity.pop(("other", "group"))),
            (lambda: self.group["contract"]["replicas"].update(replicable=False),
             lambda: self.group["contract"]["replicas"].update(replicable=True)),
            (lambda: s.owned.pop("second"), lambda: s.owned.update(second=dict(group_id="group", run_id="owner"))),
        ]
        for change, restore in cases:
            with self.subTest(change=change):
                change()
                self.assertEqual(release_choices(), [])
                restore()

    def test_elastic_start_tracks_actual_instance_and_aborted_work_does_not_start(self):
        s = self.scheduler
        s.queue["waiting"] = self.request
        starts, warms = [], []
        def start(group, body, run_id=None, replica=False):
            starts.append((group, run_id, replica))
            return self.instance("new")
        def prewarm(group, body, run_id=None):
            warms.append((group, body, run_id))
            return dict(status="PREPARED", initialization_ready=True)
        self.deployments.start, self.deployments.prewarm = start, prewarm
        action = dict(kind="prewarm", group_id="group", unit_id="unit", request_id="waiting", instance_id=None)
        s.elastic_active["group"] = dict(kind="prewarm")
        s.elastic_runs["task"] += 1
        s._elastic_action(action, "task")
        self.assertEqual(starts, [("group", "task", False)])
        self.assertEqual(warms[0][1]["instance_id"], "new")
        self.assertIn("new", s.tracked)
        self.assertEqual(s.owned["new"]["run_id"], "task")
        self.assertFalse(s.elastic_active)
        s.force_drain = True
        s.elastic_active["group"] = dict(kind="prewarm")
        s._elastic_action(action, "task")
        self.assertEqual(len(starts), 1)
        self.assertFalse(s.elastic_active)

    def test_replica_start_and_disappearing_demand_keep_ownership(self):
        s = self.scheduler
        self.instance(inflight=1)
        s.queue["waiting"] = self.request
        starts = []
        def start(group, body, run_id=None, replica=False):
            starts.append((group, run_id, replica))
            record = self.instance("replica")
            # The request can be cancelled while a real launch is in progress.
            s.queue.pop("waiting")
            record.update(state="STARTING", accepting=False)
            return record
        self.deployments.start = start
        action = dict(kind="add_replica", group_id="group", unit_id="unit", request_id="waiting")
        s.elastic_active["group"] = dict(kind="add_replica")
        s.elastic_runs["task"] += 1
        s._elastic_action(action, "task")
        self.assertEqual(starts, [("group", "task", True)])
        self.assertIn("replica", s.tracked)
        self.assertEqual(s.owned["replica"]["run_id"], "task")
        self.assertFalse(s.elastic_active)
        self.assertEqual(s.counts["elastic_cancelled"], 1)

    def test_workload_cannot_finish_while_elastic_action_is_active(self):
        s = self.scheduler
        s.tasks = SimpleNamespace(active={})
        s.workload = dict(id="workload", status="RUNNING", finished=["task"], task_ids=["task"],
                          training=False, updated=False)
        s.workload_decision_ids = []
        s._status_locked = lambda: dict(objective={"complete": True})
        s._has_held = lambda: False
        s.elastic_active["group"] = dict(kind="prewarm")
        self.assertEqual(s.finish_workload()["status"], "DRAINING")
        self.assertTrue(s.force_drain)
        s.elastic_active.clear()
        self.assertEqual(s.finish_workload()["status"], "COMPLETE")

    def test_pressure_rule_expands_only_under_real_queued_demand(self):
        first_entered, release_first, second_done = threading.Event(), threading.Event(), threading.Event()
        instances = {"first": dict(id="first", group_id="group", generation=1, state="READY", accepting=True,
            released=False, units=["unit"], in_flight=0, initialization={"unit": "BOUND"},
            last_activity_wall=time.time())}
        deploy = SimpleNamespace(lock=threading.RLock(), instances=instances, affinity={},
            backend=lambda: dict(start_timeout_s=2), release_task=lambda task: None)
        def runtime_fact(group, unit):
            with deploy.lock:
                return dict(ready_instance=True, cold_startable=False, warm=True)
        deploy.runtime_fact = runtime_fact
        starts = []
        def start(group, body, run_id=None, replica=False):
            self.assertTrue(replica)
            with deploy.lock:
                record = dict(id="second", group_id=group, generation=2, state="READY", accepting=True,
                    released=False, units=["unit"], in_flight=0, initialization={"unit": "BOUND"},
                    run_id=run_id, last_activity_wall=time.time())
                instances["second"] = record
                starts.append(record["id"])
                return dict(record)
        deploy.start = start
        deploy.drain_stop = lambda group, body, **kwargs: instances[body["instance_id"]].update(released=True, state="STOPPED")
        group = self.group
        def invoke(unit, arguments, on_event=None, run_id=None):
            with deploy.lock:
                available = [row for row in instances.values() if row["state"] == "READY" and row["in_flight"] == 0]
                if not available:
                    raise AssertionError("dispatch exceeded available replica capacity")
                row = min(available, key=lambda item: item["generation"])
                row["in_flight"] += 1
            try:
                if arguments["call"] == "first":
                    first_entered.set()
                    if not release_first.wait(4):
                        raise AssertionError("test failed to release first call")
                else:
                    second_done.set()
                return dict(call=dict(outcome="result"))
            finally:
                with deploy.lock:
                    row["in_flight"] -= 1
        catalog = SimpleNamespace(store=None, _group=lambda identity: group,
            unit=lambda identity: dict(id=identity, runtime_group="group"),
            validate_input=lambda *args: None, invoke=invoke)
        scheduler = CapabilityScheduler(catalog, deploy, config=dict(elastic_actions=True,
            max_replicas_per_group=2, policy=dict(kind="rule", scale_when_saturated=True)))
        errors = []
        def call(name):
            try:
                scheduler.invoke("unit", dict(call=name), run_id=name)
            except Exception as exc:
                errors.append(exc)
        first = threading.Thread(target=call, args=("first",))
        second = threading.Thread(target=call, args=("second",))
        try:
            first.start()
            self.assertTrue(first_entered.wait(2))
            self.assertEqual(starts, [])
            second.start()
            self.assertTrue(second_done.wait(3), "queued request did not use added capacity")
            self.assertEqual(starts, ["second"])
            self.assertTrue(first.is_alive())
            decisions = list(scheduler.decisions)
            self.assertTrue(any(d["candidates"][d["selection"]["index"]]["kind"] == "add_replica" for d in decisions))
            self.assertIn("second", scheduler.tracked)
        finally:
            release_first.set()
            first.join(3)
            if second.ident is not None:
                second.join(3)
            scheduler.close()
        self.assertEqual(errors, [])


class ObjectiveAccounting(unittest.TestCase):
    def test_delayed_quality_settlement_preserves_frozen_costs_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as folder, closing(Store(Path(folder) / "state.sqlite3")) as store:
            stamp = [10.0]
            tasks = TaskLedger(OBJECTIVE, clock=lambda: stamp[0])
            for identity in ("first", "second"):
                tasks.begin(identity)
            stamp[0] = 12.0
            tasks.terminal("first")
            tasks.terminal("second")
            stamp[0] = 15.0
            first = tasks.finish("first", delivered=True, metrics=dict(api_tokens=21))
            second = tasks.finish("second", delivered=False, status="FAILED", metrics=dict(api_tokens=8))
            for row in (first, second):
                row["session"] = "session"
                store.save("capability_task_result", row["task_id"], row)
            resources = ResourceLedger()
            life = dict(instance_id="worker", generation=1, group_id="g", resident_ms=5000,
                        start_s=10, end_s=15, released=True, end_observed=True)
            envelopes = dict(g=dict(ram_bytes=10, vram_bytes=20, provenance="fixture"))
            resources.freeze(life, envelopes, start_s=10, end_s=15)
            frozen_resources = resources.summary([], envelopes, start_s=10, end_s=15)
            scheduler = CapabilityScheduler.__new__(CapabilityScheduler)
            scheduler.condition, scheduler.store, scheduler.tasks = threading.RLock(), store, tasks
            scheduler.session = "session"
            scheduler.workload = dict(id="workload", status="COMPLETE", updated=False,
                                      finished=["first", "second"])
            scheduler._status_locked = lambda: dict(objective=tasks.summary(copy.deepcopy(frozen_resources)))
            before = scheduler._status_locked()["objective"]
            before.update(config_id="config", policy_config_id="policy", session="session")
            scheduler.workload["summary"] = copy.deepcopy(before)
            before_first = copy.deepcopy(first)
            receipt = scheduler.settle_quality("first", quality=True, metrics=dict(native_hard=True), evidence_id="e1")
            after = scheduler.workload["summary"]
            self.assertEqual(after["denominator"], before["denominator"])
            for field in ("latency_s", "api_tokens", "api_observed_tokens", "undelivered"):
                self.assertEqual(after["raw"][field], before["raw"][field])
            self.assertEqual(after["resources"], before["resources"])
            self.assertEqual(resources.summary([], envelopes, start_s=10, end_s=15), frozen_resources)
            for field in ("arrived_s", "finished_s", "scored_s", "latency_s", "delivered", "status"):
                self.assertEqual(receipt[field], before_first[field])
            self.assertEqual(tasks.totals["quality_known"], 1)
            self.assertEqual(tasks.totals["quality_sum"], 1)
            self.assertEqual(scheduler.settle_quality("first", quality=True, metrics=dict(native_hard=True),
                                                      evidence_id="e1"), receipt)
            self.assertEqual(tasks.totals["quality_known"], 1)
            self.assertEqual(tasks.totals["quality_sum"], 1)
            with self.assertRaisesRegex(ValueError, "不能覆盖"):
                scheduler.settle_quality("first", quality=False, metrics=dict(native_hard=False), evidence_id="e2")
            scheduler.workload["updated"] = True
            with self.assertRaisesRegex(ValueError, "未更新"):
                scheduler.settle_quality("second", quality=False, evidence_id="e3")
            self.assertIsNone(store.get("capability_task_result", "second")["quality"])

    def test_failure_and_late_delivery_remain_in_denominator(self):
        stamp = [100.0]
        ledger = TaskLedger(OBJECTIVE, clock=lambda: stamp[0])
        for task in ("success", "failure", "late"):
            ledger.begin(task)
        stamp[0] = 102.0
        ledger.finish("success", delivered=True, quality=0.9)
        ledger.finish("failure", delivered=False, status="FAILED", quality=0)
        stamp[0] = 112.0
        late = ledger.finish("late", delivered=True, quality=0)
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

    def test_business_terminal_excludes_scoring_and_unknown_failure_stays_unknown(self):
        stamp = [10.0]
        ledger = TaskLedger(OBJECTIVE, clock=lambda: stamp[0])
        ledger.begin("task")
        stamp[0] = 12.0
        ledger.terminal("task")
        stamp[0] = 20.0
        ledger.terminal("task")  # Idempotent notification cannot move the end.
        row = ledger.finish("task", delivered=False, status="FAILED")
        self.assertEqual(row["latency_s"], 2)
        self.assertEqual(row["finished_s"], 12)
        self.assertEqual(row["scored_s"], 20)
        self.assertIsNone(row["quality"])
        self.assertFalse(ledger.summary(dict(reserved=dict(ram_byte_s=0, vram_byte_s=0)))["training_ready"])

    def test_api_tokens_have_their_own_coefficient_and_unknown_is_not_zero(self):
        objective = copy.deepcopy(OBJECTIVE)
        objective["weights"]["api"] = 0.7  # Synthetic arithmetic fixture, not experiment coefficients.
        objective["scales"]["api_tokens"] = 100
        ledger = TaskLedger(objective, clock=lambda: 10.0)
        ledger.begin("known")
        ledger.finish("known", delivered=True, quality=1, metrics=dict(api_tokens=200))
        resources = dict(reserved=dict(ram_byte_s=0, vram_byte_s=0))
        summary = ledger.summary(resources)
        self.assertEqual(summary["schema"], "capability-objective/2")
        self.assertEqual(summary["api_usage_unit"], "tokens")
        self.assertEqual(summary["raw"]["api_tokens"], 200)
        self.assertAlmostEqual(summary["contributions"]["api"], -1.4)
        self.assertTrue(summary["training_ready"])
        ledger.begin("unknown-failed-request")
        ledger.finish("unknown-failed-request", delivered=False, status="FAILED", quality=0,
                      metrics=dict(api_tokens=None))
        summary = ledger.summary(resources)
        self.assertIsNone(summary["raw"]["api_tokens"])
        self.assertEqual(summary["raw"]["api_observed_tokens"], 200)
        self.assertEqual(summary["raw"]["api_known"], 1)
        self.assertFalse(summary["training_ready"])
        disabled = TaskLedger(OBJECTIVE, clock=lambda: 10.0)
        disabled.begin("task")
        disabled.finish("task", delivered=True, quality=1)
        summary = disabled.summary(resources)
        self.assertIsNone(summary["raw"]["api_tokens"])
        self.assertEqual(summary["contributions"]["api"], 0)
        self.assertTrue(summary["training_ready"])

    def test_old_api_objective_and_noninteger_usage_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "旧目标"):
            freeze_objective(dict(OBJECTIVE, schema="capability-objective/1"))
        old = copy.deepcopy(OBJECTIVE)
        del old["weights"]["api"]
        del old["scales"]["api_tokens"]
        with self.assertRaises(ValueError):
            freeze_objective(old)
        ledger = TaskLedger()
        ledger.begin("task")
        for value in (-1, 1.0, True, "1"):
            with self.assertRaisesRegex(ValueError, "api_tokens"):
                ledger.finish("task", delivered=True, metrics=dict(api_tokens=value))
        self.assertIn("task", ledger.active)

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

    def test_continuous_measured_cutoffs_telescope_and_missing_ranges_block(self):
        ledger = ResourceLedger()
        ledger.enable_intervals()
        life = dict(instance_id="shared", generation=1, group_id="g", resident_ms=6000,
                    start_s=0, end_s=6, released=False, start_observed=True, end_observed=True)
        def interval(start, end, a, b):
            return dict(source="measured", attribution="instance", instance_id="shared", generation=1,
                        provenance="fixture real endpoints", start_s=start, end_s=end,
                        ram_byte_s=(a + b) * (end - start) / 2, vram_byte_s=0,
                        endpoints=[dict(at_s=start, ram_bytes=a, vram_bytes=0),
                                   dict(at_s=end, ram_bytes=b, vram_bytes=0)])
        first_row = interval(0, 4, 0, 8)
        ledger.record([first_row])
        ledger.summary([life], {}, start_s=0, end_s=6)
        first = ledger.settlement("measured", {}, {}, end_s=6)
        self.assertTrue(first["ready"])
        self.assertEqual(first["coverage"][0]["deferred_s"], 2)
        self.assertEqual(first["totals"]["ram_byte_s"], 16)
        # A split uses the original slope, not the interval's average rate.
        self.assertEqual(ledger.interval_amount(first_row, "ram_byte_s", 0, 1), 1)
        self.assertEqual(ledger.interval_amount(first_row, "ram_byte_s", 1, 4), 15)
        ledger.commit_settlement(first["cutoffs"])
        self.assertEqual(ledger.interval_rows, [])
        ledger.record([interval(4, 6, 8, 4)])
        life.update(end_s=8, resident_ms=8000)
        ledger.summary([life], {}, start_s=0, end_s=8)
        second = ledger.settlement("measured", {}, first["cutoffs"], end_s=8)
        self.assertTrue(second["ready"])
        self.assertEqual(second["totals"]["ram_byte_s"], 12)
        ledger.commit_settlement(second["cutoffs"])
        ledger.record([interval(6, 8, 4, 0)])
        life.update(released=True)
        ledger.freeze(life, {}, start_s=0, end_s=8)
        final = ledger.settlement("measured", {}, second["cutoffs"], end_s=8, final=True)
        self.assertTrue(final["ready"])
        self.assertEqual(sum(row["totals"]["ram_byte_s"] for row in (first, second, final)),
                         ledger.summary([], {}, start_s=0, end_s=8)["measured"]["ram_byte_s"])
        ledger.commit_settlement(final["cutoffs"])
        self.assertEqual(ledger.interval_lifetimes, {})
        missing = ResourceLedger()
        missing.enable_intervals()
        missing.record([interval(1, 4, 2, 8)])
        missing.summary([dict(life, released=False)], {}, start_s=0, end_s=8)
        self.assertFalse(missing.settlement("measured", {}, {}, end_s=8)["ready"])


class ScopedMeasurement(unittest.TestCase):
    def test_candidate_memory_uses_only_fresh_identified_instance_samples(self):
        scheduler = CapabilityScheduler.__new__(CapabilityScheduler)
        scheduler._config = dict(measurement_interval_s=1.0, vram_source="nvml_process")
        instance = dict(id="one", group_id="g", generation=2, state="READY", released=False,
                        handle=dict(container_id="backend"))
        scheduler.deployments = SimpleNamespace(lock=threading.RLock(), instances={"one": instance})
        scheduler.tracked = {"one"}
        sample = dict(identity="backend:2", at_s=100.0, ram_bytes=1024, vram_bytes=0,
                      vram_source="nvml_process")
        scheduler.measure_samples = {("one", 2): sample}
        group_action = dict(group_id="g")
        instance_action = dict(group_id="g", instance_id="one")
        self.assertEqual(scheduler._candidate_memory(group_action, 101.0), (1024, 0))
        self.assertEqual(scheduler._candidate_memory(instance_action, 101.0), (1024, 0))
        self.assertEqual(scheduler._candidate_memory(group_action, 103.0), (None, None))
        scheduler.deployments.instances["two"] = dict(id="two", group_id="g", generation=1,
                                                       state="READY", released=False)
        self.assertEqual(scheduler._candidate_memory(group_action, 101.0), (None, None))
        self.assertEqual(scheduler._candidate_memory(instance_action, 101.0), (1024, 0))
        sample["vram_source"] = "different_source"
        self.assertEqual(scheduler._candidate_memory(instance_action, 101.0), (1024, None))
        sample["identity"] = "old-backend:2"
        self.assertEqual(scheduler._candidate_memory(instance_action, 101.0), (None, None))
        sample.update(identity="backend:2", boundary="created")
        self.assertEqual(scheduler._candidate_memory(instance_action, 101.0), (None, None))

    def test_lifecycle_owns_start_stop_and_preserves_failed_boundary_probe(self):
        from seampact.scheduling.capability_measurement import sample_instances
        backend = "a" * 64
        selected = dict(id="owned", group_id="g", generation=1, released=False,
            handle=dict(kind="docker", container="worker", container_id=backend),
            gpu_devices=["GPU-owned"], gpu_allocation=dict(status="VERIFIED"), resource_boundaries={})

        class Controller:
            lock = threading.RLock()
            stops_in_progress = set()
            instances = {"owned": selected}
            def sample_resources(self, instance_ids=None):
                return [dict(instance_id="owned", identity=backend + ":1", status="OBSERVED",
                    memory_current_bytes=1024, memory_semantics="private_cgroup_v2")]

        controller = Controller()
        with patch("seampact.scheduling.capability_measurement._gpu_memory", return_value=(0, "fixture")), patch(
                "seampact.scheduling.capability_measurement._run",
                return_value=SimpleNamespace(returncode=0, stdout=backend + " owned")) as run:
            self.assertEqual(sample_instances(controller, {"owned"}), [])
            run.assert_not_called()
            started = sample_instances(controller, {"owned"}, lifecycle_boundary=True)[0]
        self.assertEqual(started["ram_bytes"], 1024)
        selected["resource_boundaries"]["started"] = started
        for stop_finished in (False, True):
            controller.stops_in_progress.clear()
            selected["released"] = False
            def crosses_stop(instance):
                if stop_finished:
                    selected["released"] = True
                else:
                    controller.stops_in_progress.add("owned")
                return None, "CONTAINER_HOST_PIDS_UNAVAILABLE"
            with patch("seampact.scheduling.capability_measurement._gpu_memory", side_effect=crosses_stop), patch(
                    "seampact.scheduling.capability_measurement._run",
                    return_value=SimpleNamespace(returncode=0, stdout=backend + " owned")):
                self.assertEqual(sample_instances(controller, {"owned"}), [])

        selected["released"] = False
        controller.stops_in_progress.add("owned")
        with patch("seampact.scheduling.capability_measurement._gpu_memory",
                   return_value=(None, "GPU_MEMORY_COUNTER_UNAVAILABLE")), patch(
                "seampact.scheduling.capability_measurement._run",
                return_value=SimpleNamespace(returncode=0, stdout=backend + " owned")) as run:
            self.assertEqual(sample_instances(controller, {"owned"}), [])
            run.assert_not_called()
            boundary = sample_instances(controller, {"owned"}, lifecycle_boundary=True)[0]
        self.assertEqual(boundary["ram_bytes"], 1024)
        self.assertIsNone(boundary["vram_bytes"])
        self.assertTrue(boundary["vram_bytes_blocked"])
        self.assertEqual(boundary["vram_provenance"], "GPU_MEMORY_COUNTER_UNAVAILABLE")
        # A mandatory started probe exception must record UNKNOWN, mark the
        # lifecycle handoff complete, and allow later periodic observations.
        from seampact.hosting.deployment import Deployments
        selected.update(profile="capability-package-worker/1", backend="docker")
        owner = Deployments.__new__(Deployments)
        owner.lock, owner.instances = controller.lock, controller.instances
        owner.platform = SimpleNamespace(execution=None)
        events = []
        owner._event = lambda record, kind, **value: events.append(value["sample"])
        with patch("seampact.scheduling.capability_measurement.sample_instances", side_effect=RuntimeError("fixture")):
            owner._resource_boundary(selected, "started")
        self.assertIsNone(events[-1]["ram_bytes"])
        self.assertIsNone(events[-1]["vram_bytes"])
        self.assertTrue(events[-1]["vram_bytes_blocked"])
        self.assertIs(selected["resource_boundaries"]["started"], events[-1])

    def test_release_boundary_arriving_during_snapshot_is_consumed_before_freeze(self):
        scheduler = CapabilityScheduler.__new__(CapabilityScheduler)
        scheduler.condition = threading.Condition(threading.RLock())
        scheduler.tracked, scheduler.groups, scheduler.owned = {"owned"}, {"g"}, {}
        scheduler.measure_samples, scheduler.metric_samples = {}, {}
        scheduler.started_wall, scheduler.session, scheduler.store = 100.0, "fixture", None
        scheduler.config_id = "fixture"
        scheduler._config = dict(measurement_interval_s=1.0, vram_source="nvml_process", resource_envelopes={})
        scheduler.instance_events = Queue()
        scheduler.resources = ResourceLedger()
        scheduler.resources.record([dict(source="measured", attribution="instance", instance_id="owned",
            generation=1, provenance="fixture", start_s=100.0, end_s=101.0, ram_byte_s=10, vram_byte_s=20)])
        before = dict(instance_id="owned", generation=1, at_s=101.0, identity="backend:1",
                      ram_bytes=10, vram_bytes=20, boundary="before_stop")
        scheduler.measure_samples[("owned", 1)] = before
        for field in ("ram_bytes", "vram_bytes"):
            scheduler.metric_samples[("owned", 1, field)] = before
        released = dict(before, at_s=102.0, ram_bytes=0, vram_bytes=0, boundary="released")
        event = dict(group_id="g", instance_id="owned", generation=1, kind="resource_sample", sample=released)

        class StopLock:
            lock, pending = threading.RLock(), True
            def __enter__(self):
                self.lock.acquire()
                if self.pending:
                    self.pending = False
                    scheduler.instance_events.put(event)
            def __exit__(self, *args):
                self.lock.release()

        scheduler.deployments = SimpleNamespace(lock=StopLock(), _held=lambda row: not row["released"],
            instances={"owned": dict(id="owned", group_id="g", generation=1, start_wall=100.0,
                state="STOPPED", resource_release_observed_s=102.0, released=True)})
        scheduler.record_resources = scheduler.resources.record
        with scheduler.condition:
            result = scheduler._resource_summary()
        self.assertEqual(result["released_instances"], 1)
        self.assertEqual(result["measured"]["unknown_count"], 0)
        self.assertEqual(result["measured"]["ram_coverage"], 1.0)
        self.assertEqual(result["measured"]["vram_coverage"], 1.0)

    def test_real_adjacent_endpoints_allow_slow_probes_and_keep_unknown_barrier(self):
        scheduler = CapabilityScheduler.__new__(CapabilityScheduler)
        scheduler.tracked = {"owned"}
        scheduler.measure_samples, scheduler.metric_samples = {}, {}
        scheduler.started_wall = 100.0
        scheduler._config = dict(measurement_interval_s=1.0, vram_source="nvml_process")
        scheduler.deployments = SimpleNamespace(lock=threading.RLock(), instances={"owned": dict(
            generation=1, start_wall=100.0, resource_release_observed_s=108.2, released=True)})
        rows = []
        scheduler.record_resources = rows.extend

        def point(at_s, ram, vram, *, identity="backend:1", boundary=None):
            scheduler._ingest_resource_sample(dict(instance_id="owned", generation=1,
                at_s=at_s, identity=identity, boundary=boundary, ram_bytes=ram, vram_bytes=vram,
                vram_bytes_blocked=True))

        point(99.9, 0, 0, identity="unallocated:owned:1", boundary="before_create")
        point(100.2, 0, 0, boundary="created")
        point(103.4, 10, 20)
        point(104.0, 10, None)
        point(105.0, 20, 20)
        point(108.2, 0, 0, boundary="released")
        ledger = ResourceLedger()
        ledger.record(rows)
        life = dict(instance_id="owned", generation=1, group_id="g", resident_ms=8200,
                    start_s=100.0, end_s=108.2, released=True)
        measured = ledger.summary([life], {}, start_s=100.0, end_s=108.2)["measured"]
        self.assertAlmostEqual(measured["ram_coverage"], 1.0)
        self.assertIsNotNone(measured["ram_byte_s"])
        self.assertIsNone(measured["vram_byte_s"])
        self.assertLess(measured["vram_coverage"], 1.0)
        self.assertAlmostEqual(measured["max_sample_span_s"], 3.2)
        self.assertEqual(rows[0]["endpoints"][0]["at_s"], 99.9)
        self.assertEqual(rows[0]["start_s"], 100.0)

    def test_unmatched_or_unavailable_gpu_counters_are_not_zero(self):
        from seampact.scheduling.capability_measurement import _gpu_memory
        instance = dict(handle=dict(container="owned"), gpu_devices=["GPU-selected"],
                        gpu_allocation=dict(status="VERIFIED"))
        for counter in ("GPU-selected,999,100\n", "GPU-selected,12,[N/A]\n"):
            replies = [SimpleNamespace(returncode=0, stdout="PID\n12\n"),
                       SimpleNamespace(returncode=0, stdout=counter),
                       SimpleNamespace(returncode=0, stdout="PID\n12\n")]
            with patch("seampact.scheduling.capability_measurement._run", side_effect=replies) as run:
                amount, _ = _gpu_memory(instance)
                self.assertIsNone(amount)
                self.assertEqual(run.call_args_list[1].args[0][:3], ["docker", "exec", "owned"])

    def test_zero_boundary_requires_actual_created_pid_zero(self):
        from seampact.observability.resource_observation import docker_zero_boundary
        container = "a" * 64
        instance = dict(id="owner", name="worker", generation=3, state="STARTING",
                        handle=dict(container="worker", container_id=container))
        observed = dict(id=container, running=True, pid=19, status="running", instance="owner")
        with patch("seampact.observability.resource_observation.subprocess.run",
                   return_value=SimpleNamespace(returncode=0, stdout=json.dumps(observed))):
            self.assertIsNone(docker_zero_boundary(instance, "created"))
        observed.update(running=False, pid=0, status="created")
        with patch("seampact.observability.resource_observation.subprocess.run",
                   return_value=SimpleNamespace(returncode=0, stdout=json.dumps(observed))):
            point = docker_zero_boundary(instance, "created")
            self.assertEqual(point["ram_bytes"], 0)
            self.assertEqual(point["identity"], container + ":3")
            self.assertEqual(point["proof"]["kind"], "created_container_verified_no_process")

    def test_optional_allocator_snapshot_never_initializes_cuda(self):
        from seampact.capability_worker import allocator_snapshot
        fake = SimpleNamespace(cuda=SimpleNamespace(is_initialized=lambda: False,
            memory_reserved=lambda *a: self.fail("allocator query initialized CUDA")))
        with patch.dict(sys.modules, {"torch": fake}):
            point = allocator_snapshot()
        self.assertEqual(point["reserved_bytes"], 0)
        self.assertFalse(point["initialized"])
        self.assertFalse(point["includes_cuda_context"])

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
                        resource_boundaries=dict(started=dict(at_s=0)),
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
    def test_continuous_interval_bootstraps_the_actual_next_choice(self):
        from seampact.scheduling.capability_ppo import CandidatePPO
        policy = CandidatePPO("config", OBJECTIVE, training=True,
                              config=dict(hidden_sizes=[4], epochs=1, batch_size=1))
        with policy.learner.torch.no_grad():
            for parameter in policy.learner.networks["value"].parameters():
                parameter.zero_()
            policy.learner.networks["value"][-1].bias.fill_(3)
        candidates = [dict(features=[0.0] * CANDIDATE_DIM), dict(features=[1.0] * CANDIDATE_DIM)]
        decision = dict(config_id="config", feature_schema=FEATURE_SCHEMA, session="stream",
                        observation=[0.0] * OBSERVATION_DIM, candidates=candidates)
        decision["selection"] = policy.choose(decision["observation"], candidates)
        bootstrap = dict(observation=[1.0] * OBSERVATION_DIM, candidates=candidates)
        summary = dict(training_ready=True, objective_id=policy.identity["objective_id"], utility=2,
                       policy_config_id="config", session="stream", interval_id="one")
        observed, original = [], policy.learner.observe
        def capture(row):
            observed.append(copy.deepcopy(row))
            original(row)
        with patch.object(policy.learner, "observe", side_effect=capture):
            result = policy.update_interval([decision], dict(before_first=0, after_decisions=[2]), summary,
                                            bootstrap=bootstrap)
        self.assertFalse(observed[-1]["terminated"])
        self.assertTrue(observed[-1]["truncated"])
        self.assertEqual(observed[-1]["next_state"], bootstrap["observation"])
        self.assertAlmostEqual(result["value_loss"], 4.0, places=5)  # (V=3 - (r=2 + Vnext=3))²
        self.assertEqual(policy.choose(bootstrap["observation"], candidates)["policy_version"], 1)
        with self.assertRaisesRegex(ValueError, "重复"):
            policy.update_interval([decision], dict(before_first=0, after_decisions=[2]), summary, bootstrap=bootstrap)

    def test_continuous_updates_preserve_instances_and_final_utility(self):
        with tempfile.TemporaryDirectory() as folder, closing(Store(Path(folder) / "state.sqlite3")) as store:
            deployment = SimpleNamespace(lock=threading.RLock(), instances={}, affinity={},
                                         _held=lambda row: row.get("released") is not True,
                                         release_task=lambda task_id: None)
            scheduler = CapabilityScheduler(SimpleNamespace(store=store), deployment, store=store,
                config=dict(objective=OBJECTIVE,
                            resource_envelopes=dict(g=dict(ram_bytes=10, vram_bytes=20, provenance="fixture"))))
            scheduler.close()  # Deterministic accounting below needs no background probes.
            scheduler.closed = False
            scheduler.start_workload("stream", ["first", "second", "third"], training=True, continuous=True,
                                     learner_config=dict(hidden_sizes=[4], epochs=1, batch_size=1))
            scheduler.begin("first")
            scheduler.choose_optional("first", "input_received", [dict(name="optional")])
            first = scheduler.finish("first", delivered=True, metrics=dict(api_tokens=3))
            start = time.time()
            deployment.instances["held"] = dict(generation=1, group_id="g", start_wall=start,
                                                 released=False, state="READY")
            scheduler.tracked.add("held")
            self.assertFalse(scheduler.request_update(minimum_tasks=1)["pending"])
            receipt = scheduler.settle_quality("first", quality=True, evidence_id="native-first")
            self.assertEqual(receipt["finished_s"], first["finished_s"])
            self.assertTrue(scheduler.request_update(minimum_tasks=1)["pending"])
            self.assertFalse(scheduler.force_drain)
            scheduler.begin("second")
            with patch.object(scheduler.resources, "settlement", return_value=dict(ready=False, unknown=["gap"])):
                scheduler.choose_optional("second", "input_received", [dict(name="optional")])
            # Once a pending update was deferred, this active task must finish
            # and supply its real score before any of its choices are consumed.
            scheduler.choose_optional("second", "before_tool", [dict(name="optional")])
            self.assertEqual(scheduler.workload["updates"], [])
            self.assertEqual(scheduler.policy.learner.policy_version, 0)
            scheduler.finish("second", delivered=False, quality=0, metrics=dict(api_tokens=4))
            scheduler.begin("third")
            scheduler.choose_optional("third", "input_received", [dict(name="optional")])
            self.assertEqual(scheduler.workload["updated_tasks"], ["first", "second"])
            self.assertTrue(scheduler.workload["updates"][0]["updated"])
            self.assertEqual(scheduler.decisions[-1]["selection"]["policy_version"], 1)
            self.assertFalse(deployment.instances["held"]["released"])
            self.assertFalse(scheduler.force_drain)
            self.assertEqual(scheduler.settle_quality("first", quality=True, evidence_id="native-first"), receipt)
            scheduler.finish("third", delivered=True, quality=1, metrics=dict(api_tokens=5))
            release = time.time()
            deployment.instances["held"].update(released=True, state="STOPPED", stopped_wall=release,
                                                resource_release_observed_s=release)
            finished = scheduler.finish_workload(update=True)
            self.assertEqual(finished["status"], "COMPLETE")
            self.assertEqual(finished["updated_tasks"], ["first", "second", "third"])
            self.assertAlmostEqual(sum(item["utility"] for item in finished["updates"]),
                                   finished["summary"]["utility"])
            for metric in ("ram_byte_s", "vram_byte_s"):
                self.assertAlmostEqual(sum(item["resource_totals"][metric] for item in finished["updates"]),
                                       finished["summary"]["resources"]["reserved"][metric])
            self.assertTrue(finished["updates"][-1]["terminated"])
            with self.assertRaisesRegex(ValueError, "重复"):
                scheduler.finish_workload(update=True)

    def test_finite_workload_update_atomic_checkpoint_and_old_schema_rejection(self):
        from seampact.scheduling.capability_ppo import CandidatePPO
        policy = CandidatePPO("frozen-config", OBJECTIVE, training=True,
                              config=dict(hidden_sizes=[8], epochs=1, batch_size=1))
        candidates = [dict(id="one", features=[1.0] + [0.0] * (CANDIDATE_DIM - 1)),
                      dict(id="two", features=[0.0, 1.0] + [0.0] * (CANDIDATE_DIM - 2))]
        decision = dict(config_id="frozen-config", feature_schema=FEATURE_SCHEMA, session="workload",
                        observation=[2.0, 0.0, 1.0, 4.0] + [0.0] * (OBSERVATION_DIM - 4), candidates=candidates)
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
            policy.learner.save(old, checkpoint_metadata=dict(policy.identity, feature_schema="capability-event-features/6"))
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
            selection = loaded.choose([2, 0, 0, 4] + [0] * (OBSERVATION_DIM - 4),
                                      [dict(features=[0.0] * CANDIDATE_DIM), dict(features=[1.0] * CANDIDATE_DIM)])
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

    def test_remote_worker_startup_uses_actual_controller_start_and_registration_events(self):
        self.install(replicable=True)
        self.platform.capabilities.bind(self.group, dict(greeting=dict(form="value", value="hi")))
        self.reported_image()
        deployments = self.platform.deployments
        scheduler = self.scheduler(policy=dict(kind="rule", retention="keep"))

        def local_backend(record, reference, folder, config, **kwargs):
            # Replace Docker creation only; use the real remote worker, attach,
            # controller launch timestamps and registration event callback.
            worker = package_fixture.CapabilityWorker(Path(folder) / "worker.json", self.coordinator,
                instance_id=record["id"], generation=record["generation"])
            server = package_fixture.make_worker_server(worker, "127.0.0.1", 0)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.workers.append((worker, server))
            deployments.attach("http://127.0.0.1:" + str(server.server_port))
            return dict(status="APPLIED", steps=[])

        with patch.object(self.platform, "execution", scheduler), patch.object(
                deployments, "_start", side_effect=deployments._start_unprotected), patch(
                "seampact.hosting.deployment.backends.start", side_effect=local_backend):
            result = scheduler.invoke(self.units()["count_words"]["id"], dict(text="actual worker"))
        instance = deployments.instances[result["call"]["instance_id"]]
        row = scheduler._perception(dict(group_id=self.group), time.time(), time.monotonic())
        self.assertEqual(instance["profile"], package_fixture.capability_hosting.WORKER_PROFILE)
        self.assertEqual(row["samples"]["startup"], 1)
        self.assertAlmostEqual(row["metrics"]["startup_to_ready_s"], instance["ready_wall"] - instance["start_wall"])
        self.assertGreater(row["metrics"]["startup_to_ready_s"], 0)

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
        self.assertIsNone(state["objective"]["raw"]["quality_sum"])
        self.assertEqual(state["counts"]["forced_actions"], 2)
        self.assertEqual(state["recent_decisions"], [])
        self.assertEqual(scheduler.tasks.active, {})

    def test_same_decide_selects_actual_hook_call_retain_and_final_drain(self):
        units = self.prepared(auto_start=True)
        scheduler = self.scheduler(policy=dict(kind="rule", optional_hooks={"before_tool": ["count"]}, retention="keep"))
        scheduler.start_workload("hooks-and-retain", ["task"])
        scheduler.begin("task")
        options = [dict(name="required", required=True), dict(name="count", hook_family="fact_support",
                                                               unit_id=units["count_words"]["id"])]
        choice = scheduler.choose_optional("task", "before_tool", options, dict(text_chars=10))
        self.assertEqual(choice["hook"], "count")
        self.assertEqual(choice["features"][28:30], [0.0, 0.0])
        self.assertEqual(choice["features"][19:21], [1.0, 0.0])
        invoke = scheduler.catalog.invoke
        def without_timing(*args, **kwargs):
            result = invoke(*args, **kwargs)
            result["call"].pop("round_trip_ms", None)
            return result
        with patch.object(scheduler.catalog, "invoke", side_effect=without_timing):
            scheduler.invoke(choice["unit_id"], dict(text="alpha beta"), run_id="task")
        self.assertEqual(scheduler._features(choice)[28:30], [0.0, 0.0])
        result = scheduler.invoke(choice["unit_id"], dict(text="alpha beta"), run_id="task")
        self.assertEqual(result["output"]["words"], 2)
        next_choice = scheduler.choose_optional("task", "before_tool", options, dict(text_chars=10))
        self.assertEqual(next_choice["features"][29], 1.0)
        self.assertGreaterEqual(next_choice["features"][28], 0.0)
        self.assertEqual(scheduler._features(dict(kind="run_hook", group_id=choice["group_id"],
                                                  unit_id="another-unit"))[28:30], [0.0, 0.0])
        current_group = copy.deepcopy(scheduler.catalog._group(choice["group_id"]))
        current_group["installation_id"] = "different-installation"
        with patch.object(scheduler.catalog, "_group", return_value=current_group):
            self.assertEqual(scheduler._features(choice)[28:30], [0.0, 0.0])
        scheduler.business_terminal("task")
        scheduler.finish("task", delivered=True, quality=1)
        wait_for(lambda: scheduler.status()["counts"].get("retains", 0) > 0, timeout=5)
        snapshot = scheduler.status()
        self.assertTrue(any(row["event"] == "before_tool" for row in snapshot["recent_decisions"]))
        self.assertTrue(any(row["event"] == "resource" for row in snapshot["recent_decisions"]))
        self.assertFalse(any(candidate.get("hook") == "required" for row in snapshot["recent_decisions"] for candidate in row["candidates"]))
        before = snapshot["counts"]["decisions"]
        scheduler.finish_workload()
        wait_for(lambda: self.workers[0][0].closed, timeout=5)
        self.assertEqual(scheduler.status()["counts"]["decisions"], before)
        # The HTTP fixture cannot confirm Docker release; it must remain untrainable.
        self.assertFalse(scheduler.status()["objective"]["training_ready"])

    def test_interactive_chat_after_evaluation_preserves_frozen_results(self):
        scheduler = self.scheduler()
        scheduler.recent_call_costs["old"] = dict(identity=(), samples=[1])
        scheduler.start_workload("frozen-eval", ["test-case"])
        self.assertEqual(scheduler.recent_call_costs, {})
        with self.assertRaises(ValueError):
            scheduler.begin("online")
        scheduler.begin("test-case")
        scheduler.finish("test-case", delivered=True, quality=1)
        frozen = scheduler.finish_workload()
        self.assertEqual(frozen["status"], "COMPLETE")
        scheduler.recent_call_costs["evaluation"] = dict(identity=(), samples=[2])
        scheduler.begin("online")
        self.assertEqual(scheduler.recent_call_costs, {})
        self.assertNotEqual(scheduler.session, frozen["session"])
        self.assertEqual(scheduler.store.get("capability_workload", "frozen-eval"), frozen)
        self.assertEqual(scheduler.status()["objective"]["denominator"], 1)

    def test_choose_next_observes_updated_feedback_without_executing_hooks(self):
        scheduler = self.scheduler(policy=dict(kind="rule", optional_hooks={"before_tool": ["light", "heavy"]}))
        scheduler.begin("task")
        observed = []
        def observe(event, payload):
            observed.append(copy.deepcopy(payload))
            return dict(has_evidence=payload.get("checked", False), checks_run=int(payload.get("checked", False)),
                        risk_signal=0.8 if payload.get("checked") else 0)
        selector = scheduler.optional_selector("task", observe=observe)
        light, heavy = (SimpleNamespace(name=name, required=False) for name in ("light", "heavy"))
        required = SimpleNamespace(name="required", required=True)
        self.assertEqual(selector.choose_next("before_tool", {}, [required, light, heavy])["hook"], "light")
        self.assertEqual(selector.choose_next("before_tool", dict(checked=True), [heavy])["hook"], "heavy")
        self.assertEqual(observed, [{}, dict(checked=True)])
        decisions = scheduler.status()["recent_decisions"]
        self.assertEqual([row["observation"][10] for row in decisions], [0, 1])
        self.assertEqual([row["observation"][12:14] for row in decisions], [[0, 0], [1, 0.8]])
        self.assertFalse(any(candidate.get("hook") == "required" for row in decisions for candidate in row["candidates"]))
        scheduler.finish("task", delivered=True)

    def test_semantic_context_distinguishes_unknown_and_known_observations(self):
        scheduler = self.scheduler()
        scheduler.begin("task", deadline_s=60)
        options = [dict(name="first", hook_family="fact_support"),
                   dict(name="different-name", hook_family="fact_support")]
        scheduler.choose_optional("task", "before_tool", options, dict(
            linked_receipt=False, linked_source=True, missing_evidence=False, finding_valid=True, evidence_partial=True,
            family_results={"fact_support": False}, fact_findings=["unconfirmed"], support_score=0.4,
            revision_remaining=0, operation_effect="send", operation_destination="external",
            operation_phase="pending"))
        row = scheduler.status()["recent_decisions"][-1]
        self.assertEqual(row["feature_schema"], FEATURE_SCHEMA)
        self.assertEqual(len(row["observation"]), OBSERVATION_DIM)
        self.assertEqual(row["observation"][14:26], [0, 1, 1, 1, 0, 1, 1, 1, 1, 1, 0, 1])
        self.assertEqual(row["observation"][26:32], [0, 0, 1, 0, 0, 1])
        self.assertEqual(row["observation"][32:37], [0, 1, 0, 0, 1])
        self.assertEqual(row["observation"][37:42], [0, 1, 0, 0, 1])
        self.assertEqual(row["observation"][-6:], [0, 1, 0, 0, 0.4, 1])
        self.assertEqual(row["candidates"][1]["features"][19:28],
                         row["candidates"][2]["features"][19:28])
        self.assertEqual(len(row["candidates"][1]["features"]), CANDIDATE_DIM)
        first, second = row["candidates"][1:]
        self.assertEqual(first["deadline_wall"], second["deadline_wall"])
        self.assertGreater(first["features"][3], 0)
        self.assertEqual(first["features"][15], 1)
        self.assertEqual(first["features"][40:42], [1, 0])
        self.assertEqual(first["features"][4:], second["features"][4:])
        scheduler.choose_optional("task", "before_tool", options, {})
        unknown = scheduler.status()["recent_decisions"][-1]["observation"]
        self.assertEqual(unknown[14:], [0] * (OBSERVATION_DIM - 14))
        scheduler.finish("task", delivered=True)

    @unittest.skipUnless(importlib.util.find_spec("torch"), "optional PyTorch dependency")
    def test_explicit_fresh_workload_settles_real_decision_once(self):
        scheduler = self.scheduler(objective=OBJECTIVE)
        scheduler.start_workload("fresh", ["task"], training=True,
                                 learner_config=dict(hidden_sizes=[8], epochs=1, batch_size=1))
        scheduler.begin("task")
        scheduler.choose_optional("task", "input_received", [dict(name="optional")])
        scheduler.business_terminal("task")
        scheduler.finish("task", delivered=True, quality=1)
        complete = scheduler.finish_workload()
        self.assertEqual(complete["status"], "COMPLETE")
        self.assertTrue(complete["summary"]["training_ready"])
        # No managed instance was allocated: its measured reservation sum is truly zero.
        utility = complete["summary"]["utility"]
        settled = scheduler.finish_workload(settlements=dict(before_first=0, after_decisions=[utility]), update=True)
        self.assertTrue(settled["update"]["updated"])
        self.assertEqual(settled["update"]["decisions"], 1)
        with self.assertRaisesRegex(ValueError, "重复"):
            scheduler.finish_workload(settlements=dict(before_first=0, after_decisions=[utility]), update=True)

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
        wait_for(lambda: scheduler.status()["counts"].get("idle_drains", 0) >= 1, timeout=5)
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
