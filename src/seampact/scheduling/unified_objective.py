"""One frozen objective and an all-arrivals ledger for capability workloads.

Quality is supplied by the workload's named evaluator. Resource intervals are
attributed to instance generations, never to the number of calls sharing them.
Absent measurements remain absent; no task or failed window is discarded.
"""

import copy
import math
import time
from collections import deque

from seampact.capability_contract import digest

SCHEMA = "capability-objective/1"
TERMS = ("quality", "delivery", "latency", "ram", "vram")
SCALES = ("latency_s", "ram_byte_s", "vram_byte_s")


def number(value, name, *, positive=False):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0 or (positive and value == 0):
        raise ValueError(name + " 须为有限" + ("正数" if positive else "非负数"))
    return float(value)


def freeze_objective(value):
    """No default research weights, scales, evaluator or deadline are invented."""
    if value is None:
        return None
    value = copy.deepcopy(value)
    required = {"weights", "scales", "quality_evaluator", "deadline_s", "resource_source"}
    if not isinstance(value, dict) or set(value) - required - {"schema"} or not required <= set(value):
        raise ValueError("统一目标须明确 weights、scales、quality_evaluator、deadline_s、resource_source")
    if value.get("schema", SCHEMA) != SCHEMA:
        raise ValueError("旧目标格式不能用于能力调度")
    if not isinstance(value["weights"], dict) or set(value["weights"]) != set(TERMS):
        raise ValueError("目标系数须包括 quality/delivery/latency/ram/vram")
    if not isinstance(value["scales"], dict) or set(value["scales"]) != set(SCALES):
        raise ValueError("目标尺度须包括 latency_s/ram_byte_s/vram_byte_s")
    value["weights"] = {key: number(value["weights"][key], key) for key in TERMS}
    value["scales"] = {key: number(value["scales"][key], key, positive=True) for key in SCALES}
    if not isinstance(value["quality_evaluator"], str) or not value["quality_evaluator"].strip():
        raise ValueError("须声明任务质量评价器的冻结身份及归一化规则")
    if value["resource_source"] not in {"measured", "reserved"}:
        raise ValueError("resource_source 须为 measured 或 reserved，不能混用")
    value["deadline_s"] = number(value["deadline_s"], "deadline_s", positive=True)
    value["schema"] = SCHEMA
    return value


class TaskLedger:
    """Hot state contains active requests and aggregates, not terminal history."""

    def __init__(self, objective=None, *, clock=time.monotonic):
        self.objective, self.clock = freeze_objective(objective), clock
        self.active = {}
        self.totals = dict(arrivals=0, finished=0, delivered=0, undelivered=0, quality_known=0,
                           quality_sum=0.0, latency_s=0.0, failures=0)

    def begin(self, task_id, *, deadline_s=None, metadata=None):
        if task_id in self.active:
            raise ValueError("任务已登记")
        if deadline_s is None and self.objective:
            deadline_s = self.objective["deadline_s"]
        if deadline_s is not None:
            deadline_s = number(deadline_s, "deadline_s", positive=True)
        row = dict(task_id=task_id, arrived_s=self.clock(), deadline_s=deadline_s,
                   metadata=copy.deepcopy(metadata or {}))
        self.active[task_id] = row
        self.totals["arrivals"] += 1
        return copy.deepcopy(row)

    def finish(self, task_id, *, delivered, quality=None, status="COMPLETED", metrics=None):
        if type(delivered) is not bool:
            raise ValueError("delivered 须为布尔值")
        if quality is not None:
            quality = number(quality, "quality")
            if quality > 1:
                raise ValueError("quality 须由任务评价器归一化到 [0,1]")
        row = self.active[task_id]
        elapsed = max(0.0, self.clock() - row["arrived_s"])
        on_time = row["deadline_s"] is None or elapsed <= row["deadline_s"]
        success = delivered and on_time and status == "COMPLETED"
        quality = quality if success else 0.0
        row = dict(row, finished_s=self.clock(), elapsed_s=elapsed,
                   latency_s=min(elapsed, row["deadline_s"]) if row["deadline_s"] is not None else elapsed,
                   delivered=success, output_delivered=delivered, on_time=on_time,
                   quality=quality, status=status, metrics=copy.deepcopy(metrics or {}))
        del self.active[task_id]
        self.totals["finished"] += 1
        self.totals["delivered"] += int(success)
        self.totals["undelivered"] += int(not success)
        self.totals["failures"] += int(status != "COMPLETED")
        self.totals["latency_s"] += row["latency_s"]
        self.totals["quality_known"] += int(quality is not None)
        self.totals["quality_sum"] += quality if quality is not None else 0.0
        return copy.deepcopy(row)

    def summary(self, resources=None):
        raw = copy.deepcopy(self.totals)
        n = raw["arrivals"]
        complete = n > 0 and raw["finished"] == n
        raw["quality_observed_sum"] = raw.pop("quality_sum")
        raw["quality_sum"] = raw["quality_observed_sum"] if complete and raw["quality_known"] == n else None
        raw["pending"] = len(self.active)
        result = dict(schema=SCHEMA, objective=copy.deepcopy(self.objective),
                      objective_id=digest(self.objective) if self.objective else None,
                      denominator=n, raw=raw, resources=copy.deepcopy(resources), utility=None,
                      contributions=None, training_ready=False, complete=complete)
        if self.objective is None:
            result["reason"] = "尚未配置任务评价器、统一目标和尺度；仅报告原始分项"
            return result
        costs = (resources or {}).get(self.objective["resource_source"], {})
        values = dict(quality=raw["quality_sum"], delivery=raw["undelivered"] if complete else None,
                      latency=raw["latency_s"] if complete else None,
                      ram=costs.get("ram_byte_s"), vram=costs.get("vram_byte_s"))
        if not complete or any(value is None for value in values.values()):
            result["reason"] = "未完成任务、质量或资源计量缺失；未知项不补零，不能训练"
            return result
        scale = self.objective["scales"]
        normalized = dict(quality=values["quality"] / n, delivery=values["delivery"] / n,
                          latency=values["latency"] / n / scale["latency_s"],
                          ram=values["ram"] / n / scale["ram_byte_s"],
                          vram=values["vram"] / n / scale["vram_byte_s"])
        result["contributions"] = {key: normalized[key] * self.objective["weights"][key] *
                                   (1 if key == "quality" else -1) for key in TERMS}
        result.update(utility=sum(result["contributions"].values()), training_ready=True, reason=None)
        return result


class ResourceLedger:
    """Explicit, nonoverlapping instance intervals; byte-seconds stay in SI units.

    Measured intervals require already verified instance attribution. A cgroup
    RAM counter cannot imply known VRAM. Reservation envelopes are separately
    integrated over the controller's STARTING-to-confirmed-release lifetimes.
    """

    def __init__(self):
        self.active = {}
        self.closed_totals = self._empty()
        self.closed_count = 0
        self.recent_closed = deque(maxlen=20)

    @staticmethod
    def _empty():
        totals = {source: dict(ram_byte_s=0.0, vram_byte_s=0.0, unknown=[], unknown_count=0)
                  for source in ("reserved", "measured")}
        totals["measured"].update(ram_observed_byte_s=0.0, vram_observed_byte_s=0.0,
                                  ram_covered_s=0.0, vram_covered_s=0.0, required_s=0.0)
        return totals

    @staticmethod
    def _merge(target, addition):
        for source in ("reserved", "measured"):
            for key, value in addition[source].items():
                if key == "unknown":
                    target[source][key] = (target[source][key] + copy.deepcopy(value))[-20:]
                elif key not in {"ram_coverage", "vram_coverage"}:
                    before = target[source][key]
                    target[source][key] = None if before is None or value is None else before + value

    def record(self, records):
        for supplied in records:
            row = copy.deepcopy(supplied)
            if row.get("source") != "measured" or row.get("attribution") != "instance":
                raise ValueError("实测资源须提供 source=measured 和 attribution=instance")
            if not row.get("instance_id") or row.get("generation") is None or not row.get("provenance"):
                raise ValueError("资源计量须标识实例、代次和来源")
            start, end = (number(row.get(key), key) for key in ("start_s", "end_s"))
            if end <= start:
                raise ValueError("资源计量区间须为正且不重叠")
            row.update(start_s=start, end_s=end)
            for name in ("ram_byte_s", "vram_byte_s"):
                row[name] = None if row.get(name) is None else number(row[name], name)
            identity = (row["instance_id"], row["generation"])
            previous = self.active.get(identity)
            if previous and row == previous["last"]:
                continue
            if previous and start < previous["end_s"]:
                raise ValueError("同一实例代次的实测区间重叠或乱序，拒绝重复计费")
            if previous is None:
                previous = self.active[identity] = dict(start_s=start, end_s=end, last=None,
                    ram_byte_s=0.0, vram_byte_s=0.0, ram_covered_s=0.0, vram_covered_s=0.0, intervals=0)
            previous.update(end_s=end, last=row)
            previous["intervals"] += 1
            for metric in ("ram_byte_s", "vram_byte_s"):
                if row[metric] is not None:
                    previous[metric] += row[metric]
                    previous[metric.removesuffix("_byte_s") + "_covered_s"] += end - start

    def _totals(self, lifetimes, envelopes, *, start_s, end_s):
        totals = self._empty()
        instances, seen = [], set()
        for life in lifetimes:
            identity = (life["instance_id"], life.get("generation"))
            if identity in seen:
                continue
            seen.add(identity)
            duration = life["resident_ms"] / 1000.0
            totals["measured"]["required_s"] += duration
            envelope = envelopes.get(life["group_id"], {})
            row = dict(instance_id=identity[0], generation=identity[1], group_id=life["group_id"],
                       resident_s=duration, release_confirmed=life.get("released") is True)
            for metric, capacity in (("ram_byte_s", "ram_bytes"), ("vram_byte_s", "vram_bytes")):
                reserved = envelope.get(capacity)
                if reserved is None or life.get("start_observed") is False:
                    totals["reserved"][metric] = None
                    totals["reserved"]["unknown"].append(dict(instance_id=identity[0], metric=metric))
                    totals["reserved"]["unknown_count"] += 1
                elif totals["reserved"][metric] is not None:
                    totals["reserved"][metric] += reserved * duration
                name = metric.removesuffix("_byte_s")
                measured = self.active.get(identity)
                # Hot accounting supports the current complete workload scope.
                # Historical subranges are reconstructed from persisted rows.
                in_scope = not measured or (measured["start_s"] >= max(start_s, life.get("start_s", start_s))
                    and measured["end_s"] <= min(end_s, life.get("end_s", end_s)))
                covered = measured[name + "_covered_s"] if measured and in_scope else 0.0
                amount = measured[metric] if measured and in_scope else 0.0
                totals["measured"][name + "_observed_byte_s"] += amount
                totals["measured"][name + "_covered_s"] += covered
                if (abs(covered - duration) > 1e-6 or life.get("end_observed") is False
                        or life.get("start_observed") is False or not in_scope):
                    totals["measured"][metric] = None
                    totals["measured"]["unknown_count"] += 1
                    totals["measured"]["unknown"].append(dict(instance_id=identity[0], metric=metric,
                                                              measured_s=covered, required_s=duration))
                elif totals["measured"][metric] is not None:
                    totals["measured"][metric] += amount
            instances.append(row)
        return totals, instances

    def freeze(self, lifetime, envelopes, *, start_s, end_s):
        """Caller freezes a confirmed release once, persists detail, evicts identity."""
        if lifetime.get("released") is not True:
            raise ValueError("资源尚未确认释放，不能移出活动账本")
        totals, instances = self._totals([lifetime], envelopes, start_s=start_s, end_s=end_s)
        self._merge(self.closed_totals, totals)
        self.closed_count += 1
        self.recent_closed.extend(instances)
        self.active.pop((lifetime["instance_id"], lifetime.get("generation")), None)
        return dict(instance=instances[0], totals=totals)

    def summary(self, lifetimes, envelopes, *, start_s, end_s):
        totals, instances = self._totals(lifetimes, envelopes, start_s=start_s, end_s=end_s)
        self._merge(totals, self.closed_totals)
        for metric in ("ram", "vram"):
            required = totals["measured"]["required_s"]
            totals["measured"][metric + "_coverage"] = totals["measured"][metric + "_covered_s"] / required if required else None
        return dict(schema="capability-resource-ledger/1", scope="managed_capability_instances",
                    start_s=start_s, end_s=end_s, **totals, instances=instances,
                    released_instances=self.closed_count, recent_released=list(self.recent_closed),
                    semantics="shared instance once; startup, busy, idle and unreleased tail included; "
                              "reserved is declared capacity, measured requires attributed interval coverage")
