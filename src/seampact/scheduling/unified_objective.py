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

SCHEMA = "capability-objective/2"
TERMS = ("quality", "delivery", "latency", "ram", "vram", "api")
SCALES = ("latency_s", "ram_byte_s", "vram_byte_s", "api_tokens")


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
    if not isinstance(value, dict) or set(value) - required - {"schema", "pareto"} or not required <= set(value):
        raise ValueError("统一目标须明确 weights、scales、quality_evaluator、deadline_s、resource_source")
    if value.get("schema", SCHEMA) != SCHEMA:
        raise ValueError("旧目标格式不能用于能力调度")
    if not isinstance(value["weights"], dict) or set(value["weights"]) != set(TERMS):
        raise ValueError("目标系数须明确包括 quality/delivery/latency/ram/vram/api")
    if not isinstance(value["scales"], dict) or set(value["scales"]) != set(SCALES):
        raise ValueError("目标尺度须明确包括 latency_s/ram_byte_s/vram_byte_s/api_tokens")
    value["weights"] = {key: number(value["weights"][key], key) for key in TERMS}
    value["scales"] = {key: number(value["scales"][key], key, positive=True) for key in SCALES}
    if not isinstance(value["quality_evaluator"], str) or not value["quality_evaluator"].strip():
        raise ValueError("须声明任务质量评价器的冻结身份及归一化规则")
    if value["resource_source"] not in {"measured", "reserved"}:
        raise ValueError("resource_source 须为 measured 或 reserved，不能混用")
    if value["deadline_s"] is not None:
        value["deadline_s"] = number(value["deadline_s"], "deadline_s", positive=True)
    if "pareto" in value:
        pareto = value["pareto"]
        if (not isinstance(pareto, dict) or not {"preference", "smoothing"} <= set(pareto)
                or set(pareto) - {"preference", "smoothing", "mode", "calibration"}):
            raise ValueError("帕累托目标须明确搜索方向 preference 与平滑参数 smoothing")
        preference = pareto["preference"]
        dimensions = ("quality", "latency", "ram", "vram", "api")
        if not isinstance(preference, dict) or set(preference) != set(dimensions):
            raise ValueError("帕累托方向须包括质量、时延、RAM、VRAM、token五项")
        pareto["preference"] = {key: number(preference[key], key + " preference", positive=True) for key in dimensions}
        if not math.isclose(sum(pareto["preference"].values()), 1.0, rel_tol=0, abs_tol=1e-9):
            raise ValueError("帕累托搜索方向各项之和须为1")
        pareto["smoothing"] = number(pareto["smoothing"], "smoothing", positive=True)
        mode = pareto.get("mode", "adaptive_stch")
        if mode not in {"adaptive_stch", "fixed_calibrated"}:
            raise ValueError("帕累托 mode 须为 adaptive_stch 或 fixed_calibrated")
        if mode == "fixed_calibrated":
            calibration = pareto.get("calibration")
            if not isinstance(calibration, dict) or set(calibration) != {"normalized_means", "source_id"}:
                raise ValueError("固定五头消融须提供校准 normalized_means 与 source_id")
            means, source_id = calibration["normalized_means"], calibration["source_id"]
            if not isinstance(means, dict) or set(means) != set(dimensions):
                raise ValueError("校准 normalized_means 须包括五项无量纲均值")
            calibration["normalized_means"] = {key: number(means[key], "calibration." + key) for key in dimensions}
            if calibration["normalized_means"]["quality"] > 1:
                raise ValueError("校准原生质量均值须位于 [0,1]")
            if (not isinstance(source_id, str) or not source_id.startswith("sha256:") or len(source_id) != 71
                    or any(char not in "0123456789abcdef" for char in source_id[7:])):
                raise ValueError("校准 source_id 须绑定冻结证据 sha256:<64位小写hex>")
        elif "calibration" in pareto:
            raise ValueError("adaptive_stch 不使用固定校准系数")
        if any(value["weights"][key] != preference.get(key, 0) for key in TERMS):
            raise ValueError("帕累托 weights 仅作为搜索方向的线性参照，须与 preference 相同，delivery 为0")
    value["schema"] = SCHEMA
    return value


class TaskLedger:
    """Hot state contains active requests and aggregates, not terminal history."""

    def __init__(self, objective=None, *, clock=time.monotonic):
        self.objective, self.clock = freeze_objective(objective), clock
        self.active = {}
        self.totals = dict(arrivals=0, finished=0, delivered=0, undelivered=0, quality_known=0,
                           quality_sum=0.0, latency_s=0.0, failures=0, api_known=0, api_observed_tokens=0)

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

    def terminal(self, task_id, *, finished_s=None):
        row = self.active[task_id]
        if finished_s is None and row.get("business_finished_s") is not None:
            return copy.deepcopy(row)
        finished_s = self.clock() if finished_s is None else number(finished_s, "business_finished_s")
        if not row["arrived_s"] <= finished_s <= self.clock():
            raise ValueError("业务终态时间须在本任务到达与当前单调时钟之间")
        previous = row.get("business_finished_s")
        if previous is not None and previous != finished_s:
            raise ValueError("已记录的业务终态不能改写")
        row["business_finished_s"] = finished_s
        return copy.deepcopy(row)

    def finish(self, task_id, *, delivered, quality=None, status="COMPLETED", metrics=None, business_finished_s=None):
        if type(delivered) is not bool:
            raise ValueError("delivered 须为布尔值")
        if quality is not None:
            quality = number(quality, "quality")
            if quality > 1:
                raise ValueError("quality 须由任务评价器归一化到 [0,1]")
        if metrics is not None and not isinstance(metrics, dict):
            raise ValueError("任务 metrics 须为对象")
        metrics = copy.deepcopy(metrics or {})
        api_tokens = metrics.setdefault("api_tokens", None)
        if api_tokens is not None and (type(api_tokens) is not int or api_tokens < 0):
            raise ValueError("api_tokens 须为真实非负整数或 None；未知用量不能补零")
        row = self.active[task_id]
        if business_finished_s is not None or "business_finished_s" not in row:
            self.terminal(task_id, finished_s=business_finished_s)
        ended = row["business_finished_s"]
        elapsed = max(0.0, ended - row["arrived_s"])
        on_time = row["deadline_s"] is None or elapsed <= row["deadline_s"]
        success = delivered and on_time and status == "COMPLETED"
        # A failure label does not supply a missing native quality measurement.
        row = dict(row, finished_s=ended, scored_s=self.clock(), elapsed_s=elapsed,
                   latency_s=min(elapsed, row["deadline_s"]) if row["deadline_s"] is not None else elapsed,
                   delivered=success, output_delivered=delivered, on_time=on_time,
                   quality=quality, status=status, metrics=metrics)
        del self.active[task_id]
        self.totals["finished"] += 1
        self.totals["delivered"] += int(success)
        self.totals["undelivered"] += int(not success)
        self.totals["failures"] += int(status != "COMPLETED")
        self.totals["latency_s"] += row["latency_s"]
        self.totals["quality_known"] += int(quality is not None)
        self.totals["quality_sum"] += quality if quality is not None else 0.0
        self.totals["api_known"] += int(api_tokens is not None)
        if api_tokens is not None:
            self.totals["api_observed_tokens"] += api_tokens
        return copy.deepcopy(row)

    def summary(self, resources=None):
        raw = copy.deepcopy(self.totals)
        n = raw["arrivals"]
        complete = n > 0 and raw["finished"] == n
        raw["quality_observed_sum"] = raw.pop("quality_sum")
        raw["quality_sum"] = raw["quality_observed_sum"] if complete and raw["quality_known"] == n else None
        raw["api_tokens"] = raw["api_observed_tokens"] if complete and raw["api_known"] == n else None
        raw["pending"] = len(self.active)
        result = dict(schema=SCHEMA, objective=copy.deepcopy(self.objective),
                      objective_id=digest(self.objective) if self.objective else None,
                      denominator=n, raw=raw, resources=copy.deepcopy(resources), utility=None,
                      contributions=None, training_ready=False, complete=complete, api_usage_unit="tokens")
        if self.objective is None:
            result["reason"] = "尚未配置任务评价器、统一目标和尺度；仅报告原始分项"
            return result
        costs = (resources or {}).get(self.objective["resource_source"], {})
        values = dict(quality=raw["quality_sum"], delivery=raw["undelivered"] if complete else None,
                      latency=raw["latency_s"] if complete else None,
                      ram=costs.get("ram_byte_s"), vram=costs.get("vram_byte_s"), api=raw["api_tokens"])
        if not complete or any(value is None for key, value in values.items()
                               if key != "api" or self.objective["weights"]["api"] > 0
                               or "pareto" in self.objective):
            result["reason"] = "未完成任务、质量、资源或启用的 API token 计量缺失；未知项不补零，不能训练"
            return result
        scale = self.objective["scales"]
        normalized = dict(quality=values["quality"] / n, delivery=values["delivery"] / n,
                          latency=values["latency"] / n / scale["latency_s"],
                          ram=values["ram"] / n / scale["ram_byte_s"],
                          vram=values["vram"] / n / scale["vram_byte_s"],
                          api=values["api"] / n / scale["api_tokens"] if values["api"] is not None else None)
        result["components"] = normalized
        result["contributions"] = {key: 0.0 if self.objective["weights"][key] == 0 else
                                   normalized[key] * self.objective["weights"][key] *
                                   (1 if key == "quality" else -1) for key in TERMS}
        result.update(utility=sum(result["contributions"].values()), training_ready=True, reason=None)
        if "pareto" in self.objective:
            # A changing multiplier has no single workload-wide scalar reward.
            # Keep this static reference explicitly separate from rollout utility.
            result["utility_kind"] = "pareto_linear_reference"
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
        self.interval_rows = None
        self.interval_lifetimes = {}

    def enable_intervals(self):
        """Keep real interval evidence for a bounded, explicit training stream."""
        self.interval_rows = []

    @staticmethod
    def interval_amount(row, metric, start_s, end_s):
        """Integrate a subrange only when real endpoints determine its shape."""
        left, right = max(start_s, row["start_s"]), min(end_s, row["end_s"])
        if right <= left:
            return 0.0
        amount = row.get(metric)
        if amount is None:
            return None
        if left == row["start_s"] and right == row["end_s"]:
            return amount
        field = metric.removesuffix("_byte_s") + "_bytes"
        endpoints = row.get("endpoints") or []
        if (len(endpoints) != 2 or any(point.get(field) is None for point in endpoints)
                or endpoints[0]["at_s"] > left or endpoints[1]["at_s"] < right):
            return None
        first, last = endpoints
        span = last["at_s"] - first["at_s"]
        if span <= 0:
            return None
        slope = (last[field] - first[field]) / span
        full_left = first[field] + slope * (row["start_s"] - first["at_s"])
        full_right = first[field] + slope * (row["end_s"] - first["at_s"])
        if not math.isclose((full_left + full_right) * (row["end_s"] - row["start_s"]) / 2,
                            amount, rel_tol=1e-7, abs_tol=1e-9):
            return None
        a = first[field] + slope * (left - first["at_s"])
        b = first[field] + slope * (right - first["at_s"])
        return (a + b) * (right - left) / 2

    def settlement(self, source, envelopes, cutoffs, *, end_s, final=False):
        """Return unconsumed evidence, leaving live trailing spans explicitly deferred.

        A missing startup or interior interval is unknown and blocks settlement.
        Only a still-live span beyond adjacent real samples may be deferred.
        The caller commits returned cutoffs only after a successful policy update.
        """
        if self.interval_rows is None:
            raise ValueError("未启用连续区间结算")
        pieces, coverage, unknown, following = [], [], [], dict(cutoffs)
        for identity, life in self.interval_lifetimes.items():
            start, end = life["start_s"], min(end_s, life["end_s"])
            if end <= start:
                continue
            cursor = cutoffs.get(identity, start)
            if life.get("start_observed") is False or life.get("end_observed") is False:
                unknown.append(dict(instance_id=identity[0], reason="unobserved_lifecycle"))
                continue
            if source == "reserved":
                envelope = envelopes.get(life["group_id"], {})
                if any(envelope.get(metric + "_bytes") is None for metric in ("ram", "vram")):
                    unknown.append(dict(instance_id=identity[0], reason="missing_reservation"))
                    continue
                target = end
                for name in ("ram", "vram"):
                    if target > cursor:
                        pieces.append(dict(instance_id=identity[0], generation=identity[1], metric=name + "_byte_s",
                            start_s=cursor, end_s=target, amount=envelope[name + "_bytes"] * (target - cursor),
                            rate=envelope[name + "_bytes"]))
            else:
                rows = [row for row in self.interval_rows
                        if (row["instance_id"], row["generation"]) == identity]
                limits = []
                for metric in ("ram_byte_s", "vram_byte_s"):
                    covered = cursor
                    for row in sorted((row for row in rows if row.get(metric) is not None),
                                      key=lambda row: row["start_s"]):
                        if row["end_s"] <= cursor:
                            continue
                        if row["start_s"] > covered + 1e-6:
                            unknown.append(dict(instance_id=identity[0], metric=metric, reason="measurement_gap",
                                                start_s=covered, end_s=row["start_s"]))
                            break
                        covered = max(covered, row["end_s"])
                    if covered == start and cursor == start:
                        unknown.append(dict(instance_id=identity[0], metric=metric, reason="no_measured_prefix"))
                    limits.append(covered)
                target = min(end, *limits)
                if (final or life.get("released")) and target < end - 1e-6:
                    unknown.append(dict(instance_id=identity[0], reason="uncovered_release_tail",
                                        start_s=target, end_s=end))
                for metric in ("ram_byte_s", "vram_byte_s"):
                    for row in rows:
                        if row.get(metric) is None:
                            continue
                        left, right = max(cursor, row["start_s"]), min(target, row["end_s"])
                        if right <= left:
                            continue
                        amount = self.interval_amount(row, metric, left, right)
                        if amount is None:
                            unknown.append(dict(instance_id=identity[0], metric=metric,
                                                reason="subrange_requires_real_endpoints"))
                        else:
                            pieces.append(dict(instance_id=identity[0], generation=identity[1], metric=metric,
                                               start_s=left, end_s=right, amount=amount, evidence=row))
            following[identity] = target
            coverage.append(dict(instance_id=identity[0], generation=identity[1], start_s=cursor,
                                 observed_through_s=target, held_through_s=end,
                                 deferred_s=max(0.0, end - target), released=life.get("released") is True))
        return dict(ready=not unknown, pieces=pieces, coverage=coverage, unknown=unknown, cutoffs=following,
                    totals={metric: sum(row["amount"] for row in pieces if row["metric"] == metric)
                            for metric in ("ram_byte_s", "vram_byte_s")})

    def commit_settlement(self, cutoffs):
        """Evict consumed hot evidence; persistent interval records keep its source."""
        self.interval_rows[:] = [row for row in self.interval_rows
            if row["end_s"] > cutoffs.get((row["instance_id"], row["generation"]), row["start_s"])]
        for identity, life in list(self.interval_lifetimes.items()):
            if life.get("released") and cutoffs.get(identity, life["start_s"]) >= life["end_s"] - 1e-6:
                del self.interval_lifetimes[identity]

    @staticmethod
    def _empty():
        totals = {source: dict(ram_byte_s=0.0, vram_byte_s=0.0, unknown=[], unknown_count=0)
                  for source in ("reserved", "measured")}
        totals["measured"].update(ram_observed_byte_s=0.0, vram_observed_byte_s=0.0,
                                  ram_covered_s=0.0, vram_covered_s=0.0, required_s=0.0,
                                  max_sample_span_s=None)
        return totals

    @staticmethod
    def _merge(target, addition):
        for source in ("reserved", "measured"):
            for key, value in addition[source].items():
                if key == "unknown":
                    target[source][key] = (target[source][key] + copy.deepcopy(value))[-20:]
                elif key == "max_sample_span_s":
                    observed = [span for span in (target[source][key], value) if span is not None]
                    target[source][key] = max(observed) if observed else None
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
            if row.get("sample_span_s") is not None:
                row["sample_span_s"] = number(row["sample_span_s"], "sample_span_s")
                if row["sample_span_s"] < end - start:
                    raise ValueError("采样端点间隔不能短于其积分区间")
            for name in ("ram_byte_s", "vram_byte_s"):
                row[name] = None if row.get(name) is None else number(row[name], name)
            identity = (row["instance_id"], row["generation"])
            previous = self.active.get(identity)
            if previous and row == previous["last"]:
                continue
            for metric in ("ram_byte_s", "vram_byte_s"):
                metric_end = previous.get(metric + "_end_s") if previous else None
                if row[metric] is not None and metric_end is not None and start < metric_end:
                    raise ValueError("同一实例代次同一指标的实测区间重叠或乱序，拒绝重复计费")
            if previous is None:
                previous = self.active[identity] = dict(start_s=start, end_s=end, last=None,
                    ram_byte_s=0.0, vram_byte_s=0.0, ram_covered_s=0.0, vram_covered_s=0.0, intervals=0,
                    max_sample_span_s=None)
            previous.update(start_s=min(start, previous["start_s"]), end_s=max(end, previous["end_s"]), last=row)
            spans = [span for span in (previous["max_sample_span_s"], row.get("sample_span_s")) if span is not None]
            previous["max_sample_span_s"] = max(spans) if spans else None
            previous["intervals"] += 1
            for metric in ("ram_byte_s", "vram_byte_s"):
                if row[metric] is not None:
                    previous[metric] += row[metric]
                    previous[metric.removesuffix("_byte_s") + "_covered_s"] += end - start
                    previous[metric + "_end_s"] = end
            if self.interval_rows is not None:
                self.interval_rows.append(row)

    def _totals(self, lifetimes, envelopes, *, start_s, end_s):
        totals = self._empty()
        instances, seen = [], set()
        for life in lifetimes:
            identity = (life["instance_id"], life.get("generation"))
            if identity in seen:
                continue
            seen.add(identity)
            if self.interval_rows is not None:
                self.interval_lifetimes[identity] = copy.deepcopy(life)
            duration = life["resident_ms"] / 1000.0
            totals["measured"]["required_s"] += duration
            envelope = envelopes.get(life["group_id"], {})
            row = dict(instance_id=identity[0], generation=identity[1], group_id=life["group_id"],
                       resident_s=duration, release_confirmed=life.get("released") is True)
            measured_instance = self.active.get(identity)
            span = measured_instance.get("max_sample_span_s") if measured_instance else None
            spans = [item for item in (totals["measured"]["max_sample_span_s"], span) if item is not None]
            totals["measured"]["max_sample_span_s"] = max(spans) if spans else None
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
