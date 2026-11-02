"""Opt-in, local resource recording; experiment window observers remain independent."""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import re
import shutil
import sys
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from .resource_observation import ATTRIBUTION, DEVICE_QUERY, gpu_uuid, number, query, utc_now
from .resource_process import process_sample

METRICS = ("platform_cpu", "platform_memory", "container_cpu", "container_memory",
           "gpu_memory", "gpu_utilization", "gpu_power", "workload")


class ResourceMonitor:
    def __init__(self, platform):
        self.platform = platform
        self.folder = Path(platform.home) / "runtime" / "resource-monitor"
        self.folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = threading.RLock()
        self.current = None
        self.latest = deque(maxlen=60)
        self.stop_event = None
        self.thread = None
        self.previous = {}
        self.summary = None
        for path in self.folder.glob("*.meta.json"):
            try:
                meta = json.loads(path.read_text(encoding="utf-8"))
                if meta.get("status") in {"RUNNING", "STOPPING"}:
                    meta.update(status="INTERRUPTED", ended_at=utc_now(), reason="PLATFORM_RESTART",
                                sample_count=None, summary=None, summary_status="UNAVAILABLE_AFTER_INTERRUPT")
                    self._save(meta)
            except (OSError, ValueError):
                continue

    def _save(self, meta):
        path = self.folder / (meta["id"] + ".meta.json")
        temp = path.with_suffix(".tmp")
        with os.fdopen(os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, allow_nan=False)
        os.replace(temp, path)

    def _configured_gpus(self):
        values = [self.platform.deployments.backend().get("gpus", "")]
        values.extend((item.get("docker") or {}).get("gpus", "")
                      for item in self.platform.store.list("managed_service"))
        ids = set()
        for value in values:
            value = str(value).strip().strip('"')
            if value.startswith("device="):
                for item in value[7:].split(","):
                    try:
                        ids.add(gpu_uuid(item.strip()))
                    except Exception:
                        pass
        return sorted(ids)

    def capabilities(self):
        labels = {
            "platform_cpu": ("process", "CPU cumulative seconds; platform process only"),
            "platform_memory": ("process", "RSS/working set; platform process only"),
            "container_cpu": ("container cgroup", "CPU cumulative seconds; sampling overhead included"),
            "container_memory": ("container cgroup", "memory.current includes cache and descendants"),
            "gpu_memory": ("device", "whole device, shared use cannot be attributed"),
            "gpu_utilization": ("device", "whole device, shared use cannot be attributed"),
            "gpu_power": ("device", "whole device power, not process energy"),
            "workload": ("platform state", "task and call counts")}
        units = {"platform_cpu": "CPU s", "platform_memory": "bytes", "container_cpu": "CPU s",
                 "container_memory": "bytes", "gpu_memory": "MiB", "gpu_utilization": "%",
                 "gpu_power": "W", "workload": "count"}
        names = {"platform_cpu": "平台进程 CPU", "platform_memory": "平台进程内存",
                 "container_cpu": "受管容器 CPU", "container_memory": "受管容器内存",
                 "gpu_memory": "GPU 整卡显存", "gpu_utilization": "GPU 整卡利用率",
                 "gpu_power": "GPU 整卡功率", "workload": "任务与调用"}
        backend = self.platform.deployments.backend().get("kind")
        service_specs = self.platform.store.list("managed_service")
        container_available = backend == "docker" or bool(service_specs)
        process_available = sys.platform.startswith("linux") or sys.platform == "win32" or importlib.util.find_spec("psutil") is not None
        gpu_ids = self._configured_gpus()
        gpu_available = bool(gpu_ids) and bool(shutil.which("nvidia-smi"))
        rows = []
        for key in METRICS:
            available = (process_available if key.startswith("platform_") else
                         container_available if key.startswith("container_") else
                         gpu_available if key.startswith("gpu_") else True)
            if available:
                reason = "实际权限和读数以采样结果为准"
            elif key.startswith("container_"):
                reason = "当前没有 Docker 后端或受管服务；K8S 容器计数暂不支持"
            elif key.startswith("gpu_"):
                reason = "未配置 GPU UUID" if not gpu_ids else "未找到 nvidia-smi"
            else:
                reason = "当前平台没有可用的进程采样后端"
            rows.append({"id": key, "label": names[key], "unit": units[key],
                         "scope": labels[key][0], "meaning": labels[key][1],
                         "status": "UNKNOWN_UNTIL_SAMPLED" if available else "UNSUPPORTED",
                         "available": available, "reason": reason})
        return {"metrics": rows,
                "gpu_uuids": gpu_ids, "attribution": ATTRIBUTION,
                "unknown": "unavailable readings are null with status/reason; no scope totals are combined"}

    def start(self, body):
        if not isinstance(body, dict) or set(body) - {"metrics", "interval_s", "gpu_uuids", "label"}:
            raise ValueError("资源记录配置无效")
        metrics = body.get("metrics")
        if not isinstance(metrics, list) or not metrics or any(x not in METRICS for x in metrics):
            raise ValueError("请选择至少一项可用指标")
        metrics = list(dict.fromkeys(metrics))
        interval = body.get("interval_s", 2)
        if isinstance(interval, bool) or not isinstance(interval, (int, float)) or not 0.5 <= interval <= 3600:
            raise ValueError("采样间隔须在 0.5 至 3600 秒之间")
        available = {item["id"]: item for item in self.capabilities()["metrics"]}
        unsupported = [key for key in metrics if not available[key]["available"]]
        if unsupported:
            raise ValueError("当前不可用的指标：" + ", ".join(unsupported))
        selected = body.get("gpu_uuids", [])
        if not isinstance(selected, list) or any(not isinstance(x, str) for x in selected):
            raise ValueError("GPU 选择格式无效")
        selected = list(dict.fromkeys(selected))
        if any(key.startswith("gpu_") for key in metrics) and not selected:
            raise ValueError("请选择至少一张已配置的 GPU")
        if set(selected) - set(self._configured_gpus()):
            raise ValueError("只能选择平台配置中的 GPU")
        label = body.get("label", "")
        if not isinstance(label, str) or len(label) > 120:
            raise ValueError("记录名称须为不超过 120 个字符的文字")
        with self.lock:
            if self.current:
                raise ValueError("当前已有资源记录，请先结束")
            collector = {name: hashlib.sha256((Path(__file__).parent / name).read_bytes()).hexdigest()
                         for name in ("monitoring.py", "resource_process.py", "resource_observation.py", "deployment.py", "managed_services.py")}
            meta = dict(id=uuid.uuid4().hex, label=label, status="RUNNING", started_at=utc_now(),
                        ended_at=None, started_monotonic_s=time.monotonic(), ended_monotonic_s=None, metrics=metrics, interval_s=interval, gpu_uuids=selected,
                        sample_count=0, sampling_error=None, collector_sha256=collector,
                        probe_config={"interval_s": interval, "metrics": metrics, "gpu_uuids": selected,
                                      "platform_scope": "current_process", "container_scope": "platform_owned_instances",
                                      "gpu_scope": "device", "max_integrated_gap_s": 2.5 * interval},
                        observation_overhead="parallel recording adds sampling overhead; experiment observers remain independent")
            self._save(meta)
            path = self.folder / (meta["id"] + ".jsonl")
            with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as stream:
                stream.write(json.dumps({"type": "header", "schema": "seampact.resource-monitor/1", **meta}, ensure_ascii=False) + "\n")
            self.current = meta
            self.latest.clear()
            self.previous = {}
            self.summary = {"sample_count": 0, "series": {}, "sampling_errors": 0}
            self.stop_event = threading.Event()
            self.thread = threading.Thread(target=self._run, name="seampact-resource-monitor", daemon=True)
            self.thread.start()
            return copy.deepcopy(meta)

    def _capture(self, meta):
        start = time.monotonic()
        metrics = set(meta["metrics"])
        record = dict(type="sample", utc=utc_now(), monotonic_s=start, processes=[], containers=[], devices=[], workload=None)
        cancelled = lambda: self.stop_event is not None and self.stop_event.is_set()
        if metrics & {"platform_cpu", "platform_memory"} and not cancelled():
            row = process_sample(os.getpid())
            if "platform_cpu" not in metrics: row.pop("cpu_seconds", None)
            if "platform_memory" not in metrics: row.pop("rss_bytes", None)
            record["processes"] = [row]
        if metrics & {"container_cpu", "container_memory"} and not cancelled():
            try:
                groups = [x["id"] for x in self.platform.managed_services.groups()]
                rows = self.platform.managed_services.sample_resources(groups) if not cancelled() else []
                if not cancelled(): rows += self.platform.deployments.sample_resources()
                allowed = {"scope", "instance_id", "group_id", "generation", "identity", "monotonic_s", "status", "reason", "cpu_seconds", "memory_current_bytes", "memory_semantics"}
                record["containers"] = [{k: v for k, v in row.items() if k in allowed and
                    (k != "cpu_seconds" or "container_cpu" in metrics) and
                    (k != "memory_current_bytes" or "container_memory" in metrics)} for row in rows]
            except Exception:
                record["container_status"] = "UNKNOWN_SAMPLING_FAILED"
        if metrics & {"gpu_memory", "gpu_utilization", "gpu_power"} and not cancelled():
            rows, error = query(DEVICE_QUERY, meta["gpu_uuids"])
            found = {r[0].strip(): r for r in rows or [] if len(r) == 6 and r[0].strip() in meta["gpu_uuids"]}
            if not meta["gpu_uuids"]:
                record["gpu_status"] = "UNKNOWN_NO_CONFIGURED_UUID_SELECTED"
            for device in meta["gpu_uuids"]:
                row = found.get(device)
                value = dict(scope="device", gpu_uuid=device, monotonic_s=time.monotonic(), status="OBSERVED" if row else "UNKNOWN",
                             reason=error or (None if row else "UUID_MISSING_OR_INVALID_ROW"), attribution=ATTRIBUTION)
                for key, index, metric in (("memory_total_mib", 2, "gpu_memory"), ("memory_used_mib", 3, "gpu_memory"),
                                           ("utilization_gpu_percent", 4, "gpu_utilization"), ("power_draw_w", 5, "gpu_power")):
                    if metric in metrics: value[key] = number(row[index]) if row else None
                value["metric_status"] = {key: ("OBSERVED" if value.get(key) is not None else "UNKNOWN")
                                          for key in ("memory_total_mib", "memory_used_mib", "utilization_gpu_percent", "power_draw_w") if key in value}
                record["devices"].append(value)
        if "workload" in metrics and not cancelled():
            try:
                snap = self.platform.runtime.snapshot()
                calls = list(snap["calls"].values())
                tasks = self.platform.store.list("task") if not cancelled() else []
                def parse_stamp(value):
                    try:
                        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
                    except (AttributeError, ValueError, TypeError):
                        return None
                started = parse_stamp(meta["started_at"])
                finished = [x for x in tasks if started and (stamp := parse_stamp(x.get("finished_at") or x.get("ended_at"))) and stamp >= started
                            and x.get("status") in {"COMPLETED", "FAILED", "INTERRUPTED", "CANCELLED", "CANCELED", "TIMEOUT", "TIMED_OUT"}]
                latencies = sorted(x["latency_ms"] for x in finished
                                   if isinstance(x.get("latency_ms"), (int, float)) and not isinstance(x["latency_ms"], bool)
                                   and 0 <= x["latency_ms"] < float("inf"))
                def percentile(q):
                    if not latencies: return None
                    return latencies[min(len(latencies) - 1, max(0, int((len(latencies) - 1) * q + 0.5)))]
                record["workload"] = dict(status="OBSERVED", calls_active=sum(x.get("physical") in {"AUTHORIZED", "STARTED"} for x in calls),
                    calls_waiting=sum(x.get("physical") in {"ENABLED", "RESERVED"} for x in calls),
                    tasks_running=sum(x.get("status") == "RUNNING" for x in tasks), tasks_total=len(tasks),
                    slots_used=snap.get("used"), slots_quota=snap.get("quota"),
                    finished_scope="tasks finished since session start; unfinished excluded",
                    tasks_finished=len(finished), tasks_completed=sum(x.get("status") == "COMPLETED" for x in finished),
                    tasks_failed=sum(x.get("status") == "FAILED" for x in finished),
                    tasks_interrupted=sum(x.get("status") == "INTERRUPTED" for x in finished),
                    tasks_cancelled=sum(x.get("status") in {"CANCELLED", "CANCELED"} for x in finished),
                    tasks_timed_out=sum(x.get("status") in {"TIMEOUT", "TIMED_OUT"} for x in finished),
                    latency_sample_count=len(latencies), latency_p50_ms=percentile(.50), latency_p95_ms=percentile(.95))
            except Exception:
                record["workload"] = {"status": "UNKNOWN", "reason": "STATE_UNAVAILABLE"}
        record["sampling_duration_s"] = time.monotonic() - start
        return record

    def _update_summary(self, sample, interval):
        self.summary["sample_count"] += 1
        seen = set()
        for row in sample["processes"] + sample["containers"] + sample["devices"]:
            if "cpu_seconds" in row: row["cpu_percent"] = None
            key = (row.get("scope"), row.get("instance_id", row.get("pid", row.get("gpu_uuid"))), row.get("generation"))
            seen.add(key)
            name = ":".join(str(x) for x in key if x is not None)
            if name not in self.summary["series"] and len(self.summary["series"]) >= 1000:
                self.summary["series_limit_reached"] = True
                continue
            series = self.summary["series"].setdefault(name, {"scope": key[0], "instance_id": row.get("instance_id"),
                "generation": row.get("generation"), "gpu_uuid": row.get("gpu_uuid"), "sample_count": 0, "unknown_count": 0,
                "cpu_seconds_delta": None, "cpu_valid_time_s": 0, "peaks": {}, "integrals": {}})
            series["sample_count"] += 1
            series["unknown_count"] += row.get("status") != "OBSERVED"
            prev = self.previous.get(key)
            if prev and row.get("identity") is not None and row.get("identity") == prev.get("identity"):
                a, b = prev.get("cpu_seconds"), row.get("cpu_seconds")
                dt = row.get("monotonic_s", 0) - prev.get("monotonic_s", 0)
                if a is not None and b is not None and b >= a and 0 < dt <= 2.5 * interval:
                    series["cpu_seconds_delta"] = (series["cpu_seconds_delta"] or 0) + b - a
                    series["cpu_valid_time_s"] += dt
                    row["cpu_percent"] = (b - a) / dt * 100
            for field in ("rss_bytes", "memory_current_bytes", "memory_used_mib", "utilization_gpu_percent", "power_draw_w"):
                dt = row.get("monotonic_s", 0) - (prev or {}).get("monotonic_s", 0)
                same = row.get("scope") == "device" or (row.get("identity") is not None and row.get("identity") == (prev or {}).get("identity"))
                a, b = (prev or {}).get(field), row.get(field)
                if same and a is not None and b is not None and 0 < dt <= 2.5 * interval:
                    stat = series["integrals"].setdefault(field, {"value_seconds": 0.0, "valid_time_s": 0.0})
                    stat["value_seconds"] += dt * (a + b) / 2
                    stat["valid_time_s"] += dt
                if row.get(field) is not None:
                    series["peaks"][field] = max(row[field], series["peaks"].get(field, row[field]))
            self.previous[key] = row
        self.previous = {key: value for key, value in self.previous.items() if key in seen}

    def _append_record(self, identity, record):
        """Append one complete record, rolling back a partial write if possible."""
        data = (json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
        with (self.folder / (identity + ".jsonl")).open("r+b") as stream:
            stream.seek(0, os.SEEK_END)
            offset = stream.tell()
            try:
                stream.write(data)
                stream.flush()
            except Exception:
                stream.seek(offset)
                stream.truncate()
                raise

    def _finish_summary(self, meta):
        meta["ended_monotonic_s"] = time.monotonic()
        elapsed = meta["ended_monotonic_s"] - meta["started_monotonic_s"]
        self.summary["elapsed_s"] = elapsed if elapsed >= 0 else None
        for series in self.summary["series"].values():
            series["cpu_time_coverage"] = (min(1.0, series["cpu_valid_time_s"] / elapsed)
                if elapsed > 0 and series["cpu_seconds_delta"] is not None else None)
            for stat in series["integrals"].values():
                stat["valid_time_coverage"] = (min(1.0, stat["valid_time_s"] / elapsed)
                    if elapsed > 0 and stat["valid_time_s"] > 0 else None)

    def _run(self):
        event = self.stop_event
        with self.lock:
            identity = self.current["id"]
        failed = False
        try:
            while not event.is_set():
                write_failed = False
                with self.lock:
                    if not self.current or self.current["id"] != identity:
                        return
                    meta = copy.deepcopy(self.current)
                try:
                    sample = self._capture(meta)
                    with self.lock:
                        if self.current is None or self.current["id"] != identity:
                            return
                        previous_summary = copy.deepcopy(self.summary)
                        previous_rows = copy.deepcopy(self.previous)
                        try:
                            self._update_summary(sample, meta["interval_s"])
                            self._append_record(identity, sample)
                        except Exception:
                            self.summary = previous_summary
                            self.previous = previous_rows
                            write_failed = True
                            raise
                        self.latest.append(sample)
                        self.current["sample_count"] += 1
                        self.current["last_sample"] = sample
                except Exception:
                    with self.lock:
                        if self.current and self.current["id"] == identity:
                            self.current["sampling_error"] = "SAMPLING_FAILED"
                            self.summary["sampling_errors"] += 1
                    if write_failed:
                        failed = True
                        break
                if event.wait(meta["interval_s"]):
                    break
        finally:
            with self.lock:
                if self.current and self.current["id"] == identity:
                    meta = self.current
                    meta.update(status="FAILED" if failed else "COMPLETED", ended_at=utc_now())
                    meta["finished_at"] = meta["ended_at"]
                    self._finish_summary(meta)
                    meta["summary"] = self.summary
                    try:
                        self._append_record(identity, {"type": "summary", **self.summary})
                    except Exception:
                        meta.update(status="FAILED", sampling_error="SUMMARY_WRITE_FAILED")
                    try:
                        self._save(meta)
                    finally:
                        self.current = None

    def stop(self, session_id):
        with self.lock:
            if not self.current or self.current["id"] != session_id:
                raise ValueError("该资源记录未在运行")
            self.current["status"] = "STOPPING"
            self._save(self.current)
            self.stop_event.set()
            thread = self.thread
        thread.join(timeout=1)
        with self.lock:
            return self._meta(session_id)

    def _meta(self, identity):
        if not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{32}", identity):
            raise ValueError("资源记录编号无效")
        if self.current and self.current["id"] == identity: return copy.deepcopy(self.current)
        try: return json.loads((self.folder / (identity + ".meta.json")).read_text(encoding="utf-8"))
        except (OSError, ValueError): raise ValueError("找不到该资源记录") from None

    def status(self):
        with self.lock:
            sessions = []
            for path in sorted(self.folder.glob("*.meta.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:30]:
                try:
                    item = self._meta(path.name[:32]); item.pop("summary", None); sessions.append(item)
                except ValueError: pass
            return dict(capabilities=self.capabilities(), current=copy.deepcopy(self.current),
                        latest=copy.deepcopy(list(self.latest)), sessions=sessions)

    def detail(self, session_id, offset=None, limit=100):
        if offset is not None and (type(offset) is not int or offset < 0): raise ValueError("记录起始位置无效")
        if type(limit) is not int or not 1 <= limit <= 500: raise ValueError("记录条数须在 1 至 500 之间")
        with self.lock:
            meta = self._meta(session_id)
            live = self.current and self.current["id"] == session_id
            summary = copy.deepcopy(self.summary) if live else meta.get("summary")
            if live and offset is None and limit <= self.latest.maxlen:
                samples = copy.deepcopy(list(self.latest)[-limit:])
                total = meta["sample_count"]
                meta.pop("summary", None)
                return dict(session=meta, samples=samples, summary=summary, next_offset=None, total=total)
        selected = deque(maxlen=limit) if offset is None else []
        count = 0
        try:
            with (self.folder / (session_id + ".jsonl")).open(encoding="utf-8") as f:
                for line in f:
                    # A concurrent append can expose its incomplete last line.
                    if not line.endswith("\n"):
                        break
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if row.get("type") != "sample": continue
                    if offset is None: selected.append(row)
                    elif offset <= count < offset + limit: selected.append(row)
                    count += 1
        except FileNotFoundError: pass
        begin = max(0, count - limit) if offset is None else offset
        meta.pop("summary", None)
        return dict(session=meta, samples=list(selected), summary=summary,
                    next_offset=begin + len(selected) if begin + len(selected) < count else None, total=count)

    def export_path(self, session_id):
        """Validated local raw path for a streaming download; never expose path to clients."""
        self._meta(session_id)
        path = self.folder / (session_id + ".jsonl")
        if not path.is_file():
            raise ValueError("资源记录文件不可用")
        return path

    def export(self, session_id, format="jsonl"):
        with self.lock:
            meta = self._meta(session_id)
            if format == "jsonl": return self.export_path(session_id)
            if format == "json": return dict(session=meta, summary=copy.deepcopy(self.summary) if self.current and self.current["id"] == session_id else meta.get("summary"))
            raise ValueError("导出格式须为 JSONL 或 JSON")

    def close(self):
        with self.lock:
            thread = self.thread
            if self.current:
                self.current["status"] = "STOPPING"
                self._save(self.current)
                self.stop_event.set()
        # Dependencies must remain open until any in-flight probe and its
        # persistence have finished. Probe subprocesses have their own timeouts.
        if thread and thread.is_alive():
            thread.join()
