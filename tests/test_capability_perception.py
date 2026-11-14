"""Group timing reaches real scheduler decisions without future or fallback costs."""

import math
import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from seampact.capability_contract import WORKER_PROFILE
from seampact.hosting.capability_hosting import CapabilityError
from seampact.scheduling.capability_perception import GroupTiming, METRICS, FEATURE_DIM, features
from seampact.scheduling.capability_scheduler import CapabilityScheduler, CANDIDATE_DIM, FEATURE_SCHEMA


IDENTITY = dict(package_digest="package", contract_digest="contract", installation_id="installation")


class TimingEvidence(unittest.TestCase):
    def setUp(self):
        self.timing = GroupTiming()

    def request(self, at, *, group="g", identity=None):
        return dict(group_id=group, identity=identity or IDENTITY, queued_s=at)

    def snapshot(self, now=10, **kwargs):
        return self.timing.snapshot("g", IDENTITY, now=now, wall=100 + now,
            queue=kwargs.get("queue", []), running=kwargs.get("running", []), instances=kwargs.get("instances", []))

    def test_arrival_speed_uses_only_observed_group_intervals_and_stale_window_expires(self):
        self.timing.arrive(self.request(1))
        self.assertIsNone(self.snapshot()["metrics"]["arrival_rate_per_s"])
        self.timing.arrive(self.request(3))
        self.timing.arrive(self.request(30))
        self.timing.arrive(self.request(2, group="other"))
        seen = self.snapshot()["metrics"]
        self.assertEqual(seen["arrival_sample_count"], 2)
        self.assertEqual(seen["arrival_rate_per_s"], .5)
        self.assertEqual(seen["last_arrival_age_s"], 7)
        expired = self.snapshot(61)["metrics"]
        self.assertEqual(expired["arrival_sample_count"], 0)
        self.assertIsNone(expired["arrival_rate_per_s"])

    def test_queue_live_age_and_completion_samples_do_not_include_future_or_new_identity(self):
        waiting = [self.request(2), self.request(6), self.request(12)]
        running = [dict(self.request(1), execution_started_s=7)]
        self.timing.sample("g", IDENTITY, "queue_wait", 2, 8)
        self.timing.sample("g", IDENTITY, "queue_wait", 900, 11)
        self.timing.sample("g", IDENTITY, "execution", .25, 8)
        self.timing.sample("g", IDENTITY, "execution", 900, 9, at_wall=111)
        seen = self.snapshot(queue=waiting, running=running)
        self.assertEqual(seen["metrics"]["queue_depth"], 2)
        self.assertEqual(seen["metrics"]["oldest_wait_s"], 8)
        self.assertEqual(seen["metrics"]["mean_wait_s"], 6)
        self.assertEqual(seen["metrics"]["completed_queue_wait_s"], 2)
        self.assertEqual(seen["metrics"]["execution_s"], .25)
        self.assertEqual(seen["metrics"]["running_call_age_s"], 3)
        changed = dict(IDENTITY, installation_id="new")
        self.timing.arrive(self.request(9, identity=changed))
        self.timing.sample("g", IDENTITY, "execution", 99, 9)
        self.assertIsNone(self.snapshot()["metrics"]["execution_s"])
        self.assertEqual(self.timing.groups["g"]["identity"], changed)

    def test_missing_costs_and_measured_zero_have_distinct_features(self):
        empty = self.snapshot()
        self.assertIsNone(empty["metrics"]["startup_to_ready_s"])
        self.assertIsNone(empty["metrics"]["execution_s"])
        self.timing.sample("g", IDENTITY, "execution", 0, 8)
        measured = self.snapshot()
        offset = METRICS.index("execution_s") * 2
        self.assertEqual(features(empty)[offset:offset + 2], [0, 0])
        self.assertEqual(features(measured)[offset:offset + 2], [0, 1])
        self.assertEqual(len(features(measured)), FEATURE_DIM)
        self.assertTrue(all(math.isfinite(value) for value in features(measured)))
        unknown = self.timing.snapshot("g", None, now=10, wall=110, queue=[], running=[], instances=[dict(group_id="g")])
        self.assertTrue(all(value is None for value in unknown["metrics"].values()))

    def test_start_to_first_ready_is_attributed_deduplicated_and_not_replaced_by_prewarm(self):
        event = dict(instance_id="one", generation=1, group_id="g", wall=102)
        self.timing.start(event, IDENTITY, 101)
        self.timing.ready(dict(event, wall=105, start_to_ready_ms=4000), 6, 106)
        self.timing.ready(dict(event, wall=109, start_to_ready_ms=8000), 9, 109)
        self.timing.ready(dict(event, instance_id="operator", start_to_ready_ms=0), 9, 109)
        seen = self.snapshot()
        self.assertEqual(seen["metrics"]["startup_to_ready_s"], 4)
        self.assertEqual(seen["samples"]["startup"], 1)
        self.timing.start(dict(event, instance_id="future"), IDENTITY, 108)
        self.timing.ready(dict(event, instance_id="future", wall=112, start_to_ready_ms=4000), 12, 112)
        self.assertEqual(self.snapshot()["samples"]["startup"], 1)


class DecisionPath(unittest.TestCase):
    def test_related_live_scalars_are_discovered_once_and_reused_for_same_group_candidates(self):
        class NoCopy:
            def __deepcopy__(self, memo):
                raise AssertionError("decision copied historical instance payload")

        class Instances(dict):
            scans = 0

            def values(self):
                self.scans += 1
                return super().values()

        group = dict(package_digest="package", installation_id="installation", contract=dict(digest="contract"))
        instances = Instances({str(i): dict(id=str(i), group_id="old", state="STOPPED", events=NoCopy())
                               for i in range(1000)})
        instances["live"] = dict(id="live", group_id="g", generation=1, state="READY", events=NoCopy())
        instances["other"] = dict(id="other", group_id="other", generation=1, state="READY", events=NoCopy())
        deployments = SimpleNamespace(lock=threading.RLock(), instances=instances, affinity={})
        catalog = SimpleNamespace(_group=lambda _: group)
        scheduler = CapabilityScheduler(catalog, deployments, config={})
        self.addCleanup(scheduler.close)
        candidates = [dict(kind="continue", id="continue"),
                      dict(kind="run_hook", id="one", group_id="g", unit_id="u1"),
                      dict(kind="run_hook", id="two", group_id="g", unit_id="u2")]
        with patch.object(catalog, "_group", wraps=catalog._group) as group_read, patch.object(
                scheduler.group_timing, "snapshot", wraps=scheduler.group_timing.snapshot) as snapshot:
            scheduler.decide("before_tool", candidates)
            self.assertEqual(group_read.call_count, 1)
            self.assertEqual(snapshot.call_count, 2)  # One group and the no-group continue action.
            self.assertEqual(instances.scans, 1)
            saved = scheduler.decisions[-1]["candidates"]
            self.assertIs(saved[1]["perception"], saved[2]["perception"])
            self.assertEqual(set(scheduler.live_instance_ids["g"]), {"live"})
            group_read.reset_mock()
            snapshot.reset_mock()
            instances["new"] = dict(id="new", group_id="g", generation=2, state="READY", events=NoCopy())
            scheduler.instance_event(dict(kind="registered", instance_id="new", generation=2,
                                          group_id="g", profile=WORKER_PROFILE))
            scheduler.decide("before_tool", candidates)
            self.assertEqual(instances.scans, 1)
            self.assertEqual(group_read.call_count, 1)
            self.assertEqual(snapshot.call_count, 2)
            self.assertEqual(scheduler.decisions[-1]["candidates"][1]["perception"]["metrics"]["ready_replicas"], 2)
            instances["live"].update(state="STOPPED", released=True)
            scheduler.instance_event(dict(kind="stopped", instance_id="live", generation=1,
                                          group_id="g", profile=WORKER_PROFILE))
            scheduler.decide("before_tool", candidates)
            self.assertEqual(instances.scans, 1)
            self.assertEqual(scheduler.live_instance_ids["g"], {"new"})

    def test_controller_records_wait_worker_execution_and_first_ready_for_all_group_candidates(self):
        group = dict(id="g", **{key: value for key, value in IDENTITY.items() if key != "contract_digest"},
            contract=dict(digest="contract", concurrency=1, replicas=dict(replicable=True),
                          operations=dict(prewarm=dict(status="SUPPORTED"), add_replica=dict(status="SUPPORTED"))))
        deployments = SimpleNamespace(lock=threading.RLock(), instances={}, affinity={},
            runtime_fact=lambda *args: dict(ready_instance=True, cold_startable=False, warm=True))
        scheduler = None

        def invoke(unit, arguments, on_event=None, run_id=None):
            if not deployments.instances:
                start = time.time()
                deployments.instances["one"] = dict(id="one", generation=1, group_id="g", state="READY",
                    accepting=True, released=False, start_wall=start, run_id=run_id, units=["u1", "u2"])
                event = dict(instance_id="one", generation=1, group_id="g", run_id=run_id,
                             profile=WORKER_PROFILE, wall=start, kind="start_requested")
                scheduler.instance_event(event)
                scheduler.instance_event(dict(event, kind="registered", wall=time.time(), start_to_ready_ms=0))
            on_event(dict(kind="call_started", instance_id="one"))
            # Separate arrivals even on hosts with coarse monotonic clock ticks.
            threading.Event().wait(.02)
            facts = dict(execution="REMOTE", execute_ms=250 if unit == "u1" else 125,
                         round_trip_ms=9000, initialization_ms=8000)
            if arguments.get("fail"):
                raise CapabilityError("measured failure", kind="Fixture", facts=facts)
            if arguments.get("missing"):
                facts.pop("execute_ms")
            return dict(output="done", call=facts)

        catalog = SimpleNamespace(_group=lambda _: group, unit=lambda _: dict(runtime_group="g"),
                                  validate_input=lambda *args: None, invoke=invoke)
        scheduler = CapabilityScheduler(catalog, deployments, config={})
        self.addCleanup(scheduler.close)
        scheduler.invoke("u1", {})
        with self.assertRaises(CapabilityError):
            scheduler.invoke("u2", dict(fail=True))
        scheduler.invoke("u2", dict(missing=True))
        choice = scheduler.choose_optional(None, "before_tool", [dict(name="first", unit_id="u1"),
                                                                  dict(name="second", unit_id="u2")])
        decision = scheduler.decisions[-1]
        perceptions = [row["perception"] for row in decision["candidates"] if row["kind"] == "run_hook"]
        self.assertEqual(perceptions[0], perceptions[1])
        self.assertEqual(perceptions[0]["samples"], dict(queue_wait=3, execution=2, startup=1))
        self.assertEqual(perceptions[0]["metrics"]["execution_s"], .1875)
        self.assertEqual(perceptions[0]["metrics"]["startup_to_ready_s"], 0)
        self.assertGreaterEqual(perceptions[0]["metrics"]["completed_queue_wait_s"], 0)
        self.assertGreater(perceptions[0]["metrics"]["arrival_rate_per_s"], 0)
        self.assertEqual(perceptions[0]["monotonic_s"], decision["monotonic_s"])
        self.assertEqual(perceptions[0]["at_wall"], decision["at"])
        self.assertEqual(len(choice["features"]), CANDIDATE_DIM)
        self.assertEqual(FEATURE_SCHEMA, "capability-event-features/7")
        # A new session cannot consume the previous session's timing evidence.
        with scheduler.condition:
            scheduler._reset_ledger()
        fresh = scheduler._perception(dict(group_id="g"), time.time(), time.monotonic())
        self.assertIsNone(fresh["metrics"]["execution_s"])


if __name__ == "__main__":
    unittest.main()
