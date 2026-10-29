"""Explicit resource observations for real windows and manual device sampling.

WindowObserver measures only configured PIDs, cgroups and GPU UUIDs. Device power
is not process energy; GPU utilization is not active GPU-seconds. Remote API
billing and service resources require separately supplied evidence.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
from pathlib import Path
import re
import subprocess
import time
from datetime import datetime, timezone
from dataclasses import dataclass
import os
import threading
import uuid as uuid_module

from .resource_process import process_sample, cgroup_sample, counter_summary


ATTRIBUTION = "UNVERIFIED_SHARED_DEVICE"
METRICS = ("memory_total_mib", "memory_used_mib", "utilization_gpu_percent", "power_draw_w")
DEVICE_QUERY = "uuid,name,memory.total,memory.used,utilization.gpu,power.draw"
PROCESS_QUERY = "gpu_uuid,pid,used_gpu_memory"


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def number(value):
    """Unsupported, absent, and nonfinite readings remain unknown, including N/A."""
    try:
        result = float(value.strip())
        return result if math.isfinite(result) and result >= 0 else None
    except (ValueError, TypeError, AttributeError):
        return None


def query(fields, uuids):
    # Both queries are restricted at the source; filtering below is a second guard.
    if not uuids:
        return [], None
    kind = "--query-compute-apps=" if fields == PROCESS_QUERY else "--query-gpu="
    try:
        result = subprocess.run(
            ["nvidia-smi", "--id=" + ",".join(uuids), kind + fields,
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None, "COMMAND_UNAVAILABLE_OR_TIMEOUT"
    if result.returncode:
        return None, "COMMAND_FAILED"
    try:
        return list(csv.reader(io.StringIO(result.stdout), skipinitialspace=True)), None
    except csv.Error:
        return None, "INVALID_CSV"


def sample(uuids, expected_pids=()):
    stamp = {"monotonic_s": time.monotonic(), "utc": utc_now(), "scope": "device"}
    rows, gpu_error = query(DEVICE_QUERY, uuids)
    devices = {}
    for row in rows or []:
        if len(row) == 6 and row[0].strip() in uuids:
            devices[row[0].strip()] = row
    gpu_finished = {"monotonic_s": time.monotonic(), "utc": utc_now()}
    process_rows, process_error = query(PROCESS_QUERY, uuids)
    processes = {uuid: [] for uuid in uuids}
    process_unknown = set()
    for row in process_rows or []:
        if not row or not any(cell.strip() for cell in row):
            continue
        uuid = row[0].strip()
        if uuid not in processes:
            continue
        if len(row) != 3:
            process_unknown.add(uuid)
            continue
        pid_value = number(row[1])
        pid = int(pid_value) if pid_value is not None and pid_value.is_integer() and pid_value > 0 else None
        relation = "unknown" if pid is None or not expected_pids else (
            "observed" if pid in expected_pids else "unexpected")
        processes[uuid].append({"pid": pid, "used_gpu_memory_mib": number(row[2]),
                                "pid_relation": relation})
    process_finished = {"monotonic_s": time.monotonic(), "utc": utc_now()}
    observations = []
    for uuid in uuids:
        row = devices.get(uuid)
        values = [number(cell) for cell in row[2:]] if row else [None] * len(METRICS)
        device = {**stamp, "gpu_uuid": uuid, "name": row[1].strip() or None if row else None,
                  "status": "OBSERVED" if row else "UNKNOWN",
                  "reason": gpu_error or (None if row else "UUID_MISSING_OR_INVALID_ROW"),
                  **dict(zip(METRICS, values)),
                  "gpu_query_finished": gpu_finished,
                  "process_observation": {
                      "scope": "device", **process_finished,
                      "status": "UNKNOWN" if process_error or uuid in process_unknown else "OK",
                      "reason": process_error or ("INVALID_CSV_ROW" if uuid in process_unknown else None),
                      "processes": processes[uuid],
                      "expected_pids": [{"pid": pid, "status": "observed" if any(
                          item["pid"] == pid for item in processes[uuid]) else "unknown"}
                                        for pid in expected_pids],
                  }}
        observations.append(device)
    return {"type": "sample", **stamp, "devices": observations}


def summarize(samples, uuids, started, ended, interval_s, status):
    elapsed = max(0.0, ended - started)
    gap_limit = 2.5 * interval_s
    summaries = []
    for uuid in uuids:
        records = [device for item in samples for device in item["devices"] if device["gpu_uuid"] == uuid]
        metrics = {}
        for metric in METRICS:
            covered = 0.0
            integral = 0.0
            valid = [item[metric] for item in records if item[metric] is not None]
            for left, right in zip(records, records[1:]):
                dt = right["monotonic_s"] - left["monotonic_s"]
                if (0 < dt <= gap_limit and left[metric] is not None and right[metric] is not None):
                    covered += dt
                    integral += dt * (left[metric] + right[metric]) / 2
            metrics[metric] = {"valid_samples": len(valid), "valid_time_s": covered,
                               "valid_time_coverage": covered / elapsed if elapsed else None}
            if metric in ("memory_used_mib", "power_draw_w"):
                metrics[metric]["trapezoidal_integral"] = integral if covered else None
        memories = [item["memory_used_mib"] for item in records if item["memory_used_mib"] is not None]
        summaries.append({"gpu_uuid": uuid, "scope": "device", "attribution": ATTRIBUTION,
                          "peak_sampled_memory_mib": max(memories) if memories else None,
                          "memory_mib_s": metrics["memory_used_mib"]["trapezoidal_integral"],
                          "device_energy_j_estimate": metrics["power_draw_w"]["trapezoidal_integral"],
                          "metrics": metrics})
    return {"type": "summary", "scope": "device", "monotonic_s": ended, "utc": utc_now(),
            "status": status, "attribution": ATTRIBUTION, "elapsed_s": elapsed,
            "sample_count": len(samples), "max_integrated_gap_s": gap_limit, "devices": summaries}


def positive(value):
    parsed = number(value)
    if parsed is None or parsed <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number")
    return parsed


def interval(value):
    parsed = positive(value)
    if not 0.1 <= parsed <= 3600:
        raise argparse.ArgumentTypeError("must be between 0.1 and 3600 seconds")
    return parsed


def gpu_uuid(value):
    if not re.fullmatch(r"(?:GPU-|MIG-)[A-Za-z0-9/-]+", value):
        raise argparse.ArgumentTypeError("must be an explicit GPU- or MIG- UUID")
    return value


def positive_pid(value):
    try:
        pid = int(value)
        if pid > 0:
            return pid
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("must be a positive integer PID")


@dataclass(frozen=True)
class ObservationConfig:
    """No implicit PID, GPU, cgroup or environment-based selection.

    cgroup_paths identify whole cgroup-v2 accounting scopes, including descendants;
    they must never be added to the process totals. Dynamic PID selection is owned
    by the window runner and supplied to WindowObserver explicitly.
    """

    interval_s: float = 0.5
    gpu_uuids: tuple[str, ...] = ()
    process_ids: tuple[int, ...] = ()
    include_current_process: bool = False
    cgroup_paths: tuple[Path, ...] = ()

    def __post_init__(self):
        if isinstance(self.interval_s, bool):
            raise ValueError("interval_s must be between 0.1 and 3600 seconds")
        try:
            object.__setattr__(self, "interval_s", interval(str(self.interval_s)))
            if not isinstance(self.include_current_process, bool):
                raise ValueError("include_current_process must be a boolean")
            if isinstance(self.gpu_uuids, (str, bytes)) or isinstance(self.process_ids, (str, bytes)):
                raise ValueError("gpu_uuids and process_ids must be lists")
            uuids = tuple(dict.fromkeys(gpu_uuid(item) for item in self.gpu_uuids))
            pids = tuple(dict.fromkeys(_validated_pid(item) for item in self.process_ids))
            if isinstance(self.cgroup_paths, (str, bytes, Path)):
                raise ValueError("cgroup_paths must be a list of absolute directories")
            paths = tuple(dict.fromkeys(Path(item) for item in self.cgroup_paths))
            for path in paths:
                if not path.is_absolute() or ".." in path.parts:
                    raise ValueError("cgroup_paths must be absolute directories without '..'")
            object.__setattr__(self, "gpu_uuids", uuids)
            object.__setattr__(self, "process_ids", pids)
            object.__setattr__(self, "cgroup_paths", paths)
        except (argparse.ArgumentTypeError, TypeError) as error:
            raise ValueError("invalid resource observation configuration") from error

    @classmethod
    def from_mapping(cls, value):
        if value is None:
            return None
        if isinstance(value, cls):
            return value
        if not isinstance(value, dict):
            raise ValueError("resource observation configuration must be an object")
        allowed = {"interval_s", "gpu_uuids", "process_ids", "include_current_process", "cgroup_paths"}
        if set(value) - allowed:
            raise ValueError("unknown resource observation configuration field")
        return cls(**value)


def _validated_pid(value):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("process IDs must be positive integers")
    return value


class WindowObserver:
    """Start before preparation, finish after release, retaining private raw data.

    The resolver returns only runner-owned explicit PIDs, never recursively found
    children. Newly selected/exited processes leave unknown intervals; PID reuse
    cannot join two CPU counters. No observers are enabled when config is None.
    """

    def __init__(self, config=None, *, run_id, arm, runtime_dir, pid_resolver=None):
        self.config = ObservationConfig.from_mapping(config)
        if not str(run_id).strip() or not str(arm).strip():
            raise ValueError("run_id and arm must be nonempty")
        self.run_id, self.arm = str(run_id), str(arm)
        self.runtime_dir = Path(runtime_dir)
        self.pid_resolver = pid_resolver
        if pid_resolver is not None and not callable(pid_resolver):
            raise ValueError("pid_resolver must be callable")
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._finish_lock = threading.Lock()
        self._closed = False
        self._phases = []
        self._phase = None
        self._samples = []
        self._seen_pids = set()
        self._started = None
        self._summary = None
        self._stream = None
        self._thread = None
        self._worker_error = None

    def _write(self, item):
        self._stream.write(json.dumps(item, ensure_ascii=False, allow_nan=False) + "\n")
        self._stream.flush()

    def start(self):
        if self._started is not None:
            raise RuntimeError("observer already started")
        if self.config is None:
            self._started = time.monotonic()
            return self
        # Runtime evidence contains local PIDs/device identifiers. Do not put it
        # under docs, user-selected shared files, or a source directory.
        private_parts = {"var", "runtime", ".local-private"}
        if not private_parts.intersection(part.lower() for part in self.runtime_dir.parts):
            raise ValueError("resource records require a private var/runtime/.local-private directory")
        directory = self.runtime_dir / "resource-observations"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        stem = uuid_module.uuid4().hex
        self.raw_path = directory / (stem + ".jsonl")
        self.summary_path = directory / (stem + ".summary.json")
        fd = os.open(self.raw_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        self._stream = os.fdopen(fd, "w", encoding="utf-8")
        self._started = time.monotonic()
        self._write({"type": "header", "schema": "seampact.window-resource-observation/1",
                     "run_id": self.run_id, "arm": self.arm, "utc": utc_now(),
                     "monotonic_s": self._started, "interval_s": self.config.interval_s,
                     "gpu_uuids": list(self.config.gpu_uuids),
                     "process_ids": list(self.config.process_ids),
                     "include_current_process": self.config.include_current_process,
                     "dynamic_explicit_pid_resolver": self.pid_resolver is not None,
                     "cgroup_paths": [str(path) for path in self.config.cgroup_paths],
                     "pairing": "AUTOMATIC_WINDOW_PREPARATION_THROUGH_RELEASE",
                     "measurement_kind": "MEASURED_RESOURCE_OBSERVATION",
                     "aggregation": "SCOPES_SEPARATE_DO_NOT_SUM_PROCESS_CGROUP_DEVICE",
                     "attribution": ATTRIBUTION,
                     "excluded_costs": ["remote_API_billing", "unselected_processes", "other_servers"],
                     "collector_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()})
        try:
            self._capture()
        except BaseException:
            self._stream.close()
            raise
        self._thread = threading.Thread(target=self._run, name="seampact-resource-observer", daemon=True)
        self._thread.start()
        return self

    def mark_phase(self, name):
        """Record a runner phase boundary without triggering a resource query."""
        if not isinstance(name, str) or not name.strip():
            raise ValueError("phase name must be nonempty")
        if self._started is None:
            raise RuntimeError("observer has not started")
        with self._lock:
            if self._summary is not None or self._closed:
                raise RuntimeError("observer has finished")
            if self.config is None:
                return None
            stamp = time.monotonic()
            item = {"type": "phase", "name": name, "monotonic_s": stamp,
                    "elapsed_s": max(0.0, stamp - self._started), "utc": utc_now()}
            self._phase = name
            self._phases.append(item)
            self._write(item)
            return item

    def _capture(self):
        # Queries run outside the persistence lock so a stuck resolver does not
        # prevent finish from saving evidence already collected.
        with self._lock:
            if self._closed:
                return
            phase = self._phase
        pids = set(self.config.process_ids)
        if self.config.include_current_process:
            pids.add(os.getpid())
        resolver_status = "NOT_CONFIGURED"
        if self.pid_resolver is not None:
            try:
                resolved = tuple(self.pid_resolver())
                pids.update(_validated_pid(pid) for pid in resolved)
                resolver_status = "OK"
            except Exception:
                resolver_status = "UNKNOWN_RESOLVER_FAILED"
        with self._lock:
            if self._closed:
                return
            self._seen_pids.update(pids)
            seen_pids = sorted(self._seen_pids)
        processes = []
        for pid in seen_pids:
            if pid in pids:
                processes.append(process_sample(pid))
            else:
                processes.append({"scope": "process", "pid": pid, "identity": None,
                                  "monotonic_s": time.monotonic(), "cpu_seconds": None,
                                  "rss_bytes": None, "status": "UNKNOWN", "reason": "NO_LONGER_SELECTED"})
        groups = [cgroup_sample(path, index) for index, path in enumerate(self.config.cgroup_paths)]
        item = sample(self.config.gpu_uuids, sorted(pids)) if self.config.gpu_uuids else {
            "type": "sample", "monotonic_s": time.monotonic(), "utc": utc_now(), "devices": []}
        item.update({"scope": "window", "processes": processes, "cgroups": groups,
                     "pid_resolver_status": resolver_status})
        item["phase"] = phase
        with self._lock:
            if not self._closed:
                self._samples.append(item)
                self._write(item)

    def _run(self):
        while not self._stop.wait(self.config.interval_s):
            try:
                if not self._stop.is_set():
                    self._capture()
            except Exception:
                # Preserve the completed records and report the hole, without
                # exposing exceptions that may contain local machine details.
                self._worker_error = "SAMPLING_FAILED"
                self._stop.set()

    def finish(self, status="COMPLETED"):
        with self._finish_lock:
            return self._finish(status)

    def _finish(self, status):
        if self._summary is not None:
            return self._summary
        if self._started is None:
            raise RuntimeError("observer has not started")
        if self.config is None:
            self._summary = {"status": "NOT_CONFIGURED", "measurement_kind": "NOT_MEASURED",
                             "processes": [], "cgroups": [], "devices": []}
            return self._summary
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        worker_running = self._thread is not None and self._thread.is_alive()
        try:
            if worker_running:
                self._worker_error = "SAMPLING_STOP_TIMEOUT"
            else:
                try:
                    self._capture()
                except Exception:
                    self._worker_error = "FINAL_SAMPLING_FAILED"
            # No late worker result may append after the summary or closed file.
            with self._lock:
                self._closed = True
            ended = time.monotonic()
            result = summarize(self._samples, self.config.gpu_uuids, self._started, ended,
                               self.config.interval_s, status)
            result.update({"schema": "seampact.window-resource-observation/1", "scope": "window",
                           "run_id": self.run_id, "arm": self.arm,
                           "measurement_kind": "MEASURED_RESOURCE_OBSERVATION",
                           "sampling_error": self._worker_error,
                           "phases": list(self._phases),
                           "aggregation": "SCOPES_SEPARATE_DO_NOT_SUM_PROCESS_CGROUP_DEVICE",
                           "window_boundary": "PREPARATION_THROUGH_RELEASE",
                           "observer_collection_overhead_included": True,
                           "raw_path": str(self.raw_path), "summary_path": str(self.summary_path)})
            elapsed = result["elapsed_s"]
            gap = result["max_integrated_gap_s"]
            result["processes"] = [{"scope": "process", "pid": pid,
                **counter_summary([row for item in self._samples for row in item["processes"]
                                   if row["pid"] == pid], elapsed, gap)} for pid in sorted(self._seen_pids)]
            result["cgroups"] = [{"scope": "cgroup", "cgroup_index": index,
                "memory_semantics": "memory_current_includes_cache_and_descendants",
                **counter_summary([row for item in self._samples for row in item["cgroups"]
                                   if row["cgroup_index"] == index], elapsed, gap, cgroup=True)}
                                  for index in range(len(self.config.cgroup_paths))]
            self._summary = result
            self._write(result)
            with os.fdopen(os.open(self.summary_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600),
                           "w", encoding="utf-8") as stream:
                json.dump(result, stream, ensure_ascii=False, allow_nan=False, indent=2)
            self._summary = result
            return result
        finally:
            self._stream.close()

    def __enter__(self):
        return self.start()

    def __exit__(self, exc_type, exc_value, traceback):
        self.finish("FAILED" if exc_type is not None else "COMPLETED")
        return False


def parser():
    result = argparse.ArgumentParser(description=__doc__, epilog=(
        "Pair run_id and arm manually; this is not automatically connected to the training window. "
        "No CPU, billing, or other servers' API costs are measured. All readings describe the "
        "selected devices, with UNVERIFIED_SHARED_DEVICE attribution. Expected PIDs do not "
        "establish exclusive use. Output contains local device information; keep it private."))
    result.add_argument("--run-id", required=True)
    result.add_argument("--arm", required=True)
    result.add_argument("--gpu-uuid", type=gpu_uuid, action="append", required=True)
    result.add_argument("--duration-s", type=positive, required=True)
    result.add_argument("--interval-s", type=interval, required=True)
    result.add_argument("--output", type=Path, required=True)
    result.add_argument("--expected-pid", type=positive_pid, action="append", default=[])
    return result


def main(argv=None):
    arguments = parser().parse_args(argv)
    if not arguments.run_id.strip() or not arguments.arm.strip():
        parser().error("run-id and arm must be nonempty")
    uuids = list(dict.fromkeys(arguments.gpu_uuid))
    expected_pids = list(dict.fromkeys(arguments.expected_pid))
    output = arguments.output
    try:
        stream = output.open("x", encoding="utf-8")
    except OSError:
        print(f"{output}: OUTPUT_UNAVAILABLE_OR_EXISTS")
        return 2
    samples = []
    status = "COMPLETED"
    started = time.monotonic()
    with stream:
        def write(item):
            stream.write(json.dumps(item, ensure_ascii=False, allow_nan=False) + "\n")
            stream.flush()

        write({"type": "header", "schema": "seampact.device-observation/1",
               "collector_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
               "run_id": arguments.run_id, "arm": arguments.arm,
               "gpu_uuids": uuids, "expected_pids": expected_pids,
               "monotonic_s": started, "utc": utc_now(), "scope": "device",
               "duration_s": arguments.duration_s, "interval_s": arguments.interval_s,
               "max_integrated_gap_s": 2.5 * arguments.interval_s,
               "attribution": ATTRIBUTION, "pairing": "MANUAL_RUN_ID_NO_AUTOMATIC_TRAINING_WINDOW",
               "excluded_costs": ["CPU", "billing", "other_servers_API"],
               "interpretation": "Device power is not process energy; GPU utilization is not active GPU-seconds. "
                                 "Expected PIDs and an empty process list cannot establish exclusive use.",
               "reference": "https://docs.nvidia.com/deploy/nvidia-smi/index.html"})
        try:
            deadline = started + arguments.duration_s
            target = started
            while True:
                delay = target - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                observation = sample(uuids, expected_pids)
                samples.append(observation)
                write(observation)
                now = time.monotonic()
                if now >= deadline:
                    break
                target = min(deadline, max(target + arguments.interval_s, now))
        except KeyboardInterrupt:
            status = "INTERRUPTED"
        ended = time.monotonic()
        write(summarize(samples, uuids, started, ended, arguments.interval_s, status))
    print(f"{output}: {status}")
    return 130 if status == "INTERRUPTED" else 0


if __name__ == "__main__":
    raise SystemExit(main())
