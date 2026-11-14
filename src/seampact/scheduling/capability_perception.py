"""Causal, session-local timing evidence for each frozen capability group."""

import math
from collections import deque

SCHEMA = "capability-group-perception/1"
ARRIVAL_WINDOW_S = 30.0
METRICS = ("arrival_rate_per_s", "arrival_sample_count", "last_arrival_age_s", "queue_depth",
           "oldest_wait_s", "mean_wait_s", "completed_queue_wait_s", "execution_s",
           "startup_to_ready_s", "starting_age_s", "running_call_age_s",
           "ready_replicas", "starting_replicas", "running_calls")
SAMPLE_KINDS = ("queue_wait", "execution", "startup")
FEATURE_DIM = 2 * len(METRICS) + len(SAMPLE_KINDS)


def finite(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


class GroupTiming:
    """Bounded histories; callers serialize mutations with the scheduler lock.

    Arrivals and queue waits use the coordinator monotonic clock. Execution
    durations come only from worker execute_ms. Startup uses the controller's
    recorded start-to-first-ready interval, never a prewarm or call round trip.
    """

    def __init__(self):
        self.groups, self.starts = {}, {}

    def _record(self, group_id, identity):
        row = self.groups.get(group_id)
        if row is None or row["identity"] != identity:
            row = self.groups[group_id] = dict(identity=identity, arrivals=deque(maxlen=64),
                **{kind: deque(maxlen=32) for kind in SAMPLE_KINDS})
        return row

    def arrive(self, request):
        self._record(request["group_id"], request["identity"])["arrivals"].append(request["queued_s"])

    def sample(self, group_id, identity, kind, value, at_s, *, at_wall=None):
        current = self.groups.get(group_id)
        if current is not None and current["identity"] != identity:
            return
        if finite(value) and finite(at_s) and (at_wall is None or finite(at_wall)):
            self._record(group_id, identity)[kind].append(dict(value=float(value), at_s=at_s, at_wall=at_wall))

    def start(self, event, identity, start_wall):
        if finite(start_wall):
            self.starts.setdefault((event["instance_id"], event["generation"]),
                dict(group_id=event["group_id"], identity=identity, start_wall=start_wall, completed=False))
            if len(self.starts) > 512:
                del self.starts[next(iter(self.starts))]

    def ready(self, event, at_s, at_wall):
        start = self.starts.get((event["instance_id"], event["generation"]))
        if not start or start["completed"]:
            return
        # Only the first registration can establish launch-to-availability.
        start["completed"] = True
        milliseconds = event.get("start_to_ready_ms")
        wall = event.get("wall")
        if (finite(milliseconds) and finite(wall) and wall <= at_wall
                and start["start_wall"] + milliseconds / 1000 <= wall + 1e-6):
            self.sample(start["group_id"], start["identity"], "startup", milliseconds / 1000,
                        at_s, at_wall=wall)

    def snapshot(self, group_id, identity, *, now, wall, queue, running, instances):
        metrics = dict.fromkeys(METRICS)
        samples = dict.fromkeys(SAMPLE_KINDS, 0)
        result = dict(schema=SCHEMA, group_id=group_id, identity=identity, monotonic_s=now,
                      at_wall=wall, arrival_window_s=ARRIVAL_WINDOW_S, arrival_history_limit=64,
                      duration_history_limit=32, metrics=metrics, samples=samples)
        if not group_id or not isinstance(identity, dict):
            return result
        same = lambda row: row.get("group_id") == group_id and row.get("identity") == identity
        waiting = [now - row["queued_s"] for row in queue if same(row)
                   and finite(row.get("queued_s")) and row["queued_s"] <= now]
        active = [row for row in running if same(row)]
        executing = [now - row["execution_started_s"] for row in active
                     if finite(row.get("execution_started_s")) and row["execution_started_s"] <= now]
        live = [row for row in instances if row.get("group_id") == group_id
                and row.get("contract_digest", identity["contract_digest"]) == identity["contract_digest"]
                and ((row.get("id"), row.get("generation")) not in self.starts or
                     self.starts[(row.get("id"), row.get("generation"))]["identity"] == identity)
                and row.get("released") is not True and row.get("state") not in {"STOPPED", "FAILED"}]
        starting = [row for row in live if row.get("state") in {"UNDEPLOYED", "STARTING"}]
        ages = [wall - row["start_wall"] for row in starting
                if finite(row.get("start_wall")) and row["start_wall"] <= wall]
        metrics.update(queue_depth=len(waiting), oldest_wait_s=max(waiting, default=0.0),
            mean_wait_s=sum(waiting) / len(waiting) if waiting else 0.0,
            running_calls=len(active), running_call_age_s=max(executing) if executing else None,
            ready_replicas=sum(row.get("state") == "READY" and row.get("accepting", True)
                               and not row.get("pending_stop") for row in live),
            starting_replicas=len(starting), starting_age_s=max(ages) if ages else None)
        history = self.groups.get(group_id)
        if not history or history["identity"] != identity:
            return result
        arrivals = [at for at in history["arrivals"] if finite(at) and now - ARRIVAL_WINDOW_S <= at <= now]
        past = [at for at in history["arrivals"] if finite(at) and at <= now]
        if past:
            metrics["arrival_sample_count"] = len(arrivals)
            metrics["last_arrival_age_s"] = now - max(past)
        if len(arrivals) >= 2 and max(arrivals) > min(arrivals):
            metrics["arrival_rate_per_s"] = (len(arrivals) - 1) / (max(arrivals) - min(arrivals))
        for kind, metric in zip(SAMPLE_KINDS, ("completed_queue_wait_s", "execution_s", "startup_to_ready_s")):
            values = [row["value"] for row in history[kind] if row["at_s"] <= now
                      and (row["at_wall"] is None or row["at_wall"] <= wall)]
            samples[kind] = len(values)
            if values:
                metrics[metric] = sum(values) / len(values)
        return result


def features(perception):
    values = perception["metrics"]
    return [part for name in METRICS for part in
            (math.log1p(values[name]) if values[name] is not None else 0.0, float(values[name] is not None))] + [
                math.log1p(perception["samples"][kind]) for kind in SAMPLE_KINDS]
