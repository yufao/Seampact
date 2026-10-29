"""Explicit PID and configured cgroup counters; no discovery or child traversal."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import time


def _unknown(scope, reason, **identity):
    return {"scope": scope, "monotonic_s": time.monotonic(), "status": "UNKNOWN",
            "reason": reason, "identity": None, "cpu_seconds": None,
            "memory_current_bytes" if scope == "cgroup" else "rss_bytes": None, **identity}


def process_sample(pid):
    """Read only the selected PID; identity changes invalidate counter edges."""
    try:
        if sys.platform.startswith("linux"):
            return _linux_process(pid)
        if sys.platform == "win32":
            return _windows_process(pid)
        # Optional portability support; never required to install psutil.
        import psutil
        process = psutil.Process(pid)
        identity = process.create_time()
        cpu = process.cpu_times()
        rss = process.memory_info().rss
        if identity != process.create_time():
            return _unknown("process", "IDENTITY_CHANGED_DURING_SAMPLE", pid=pid)
        return {"scope": "process", "pid": pid, "identity": str(identity),
                "cpu_seconds": cpu.user + cpu.system, "rss_bytes": rss,
                "monotonic_s": time.monotonic(), "status": "OBSERVED", "reason": None,
                "backend": "psutil"}
    except (OSError, ValueError, IndexError, ImportError):
        return _unknown("process", "PROCESS_UNAVAILABLE_OR_UNSUPPORTED", pid=pid)
    except Exception as error:
        # psutil exceptions may expose command lines and local paths in their text.
        if error.__class__.__module__.startswith("psutil"):
            return _unknown("process", "PROCESS_UNAVAILABLE_OR_UNSUPPORTED", pid=pid)
        raise


def _linux_process(pid):
    def fields():
        raw = (Path("/proc") / str(pid) / "stat").read_text(encoding="utf-8")
        return raw[raw.rfind(")") + 2:].split()
    values = fields()
    identity = values[19]  # starttime, field 22; comm can contain spaces and ')'.
    cpu = (int(values[11]) + int(values[12])) / os.sysconf("SC_CLK_TCK")
    rss = int(values[21]) * os.sysconf("SC_PAGE_SIZE")
    if identity != fields()[19]:
        return _unknown("process", "IDENTITY_CHANGED_DURING_SAMPLE", pid=pid)
    return {"scope": "process", "pid": pid, "identity": identity,
            "cpu_seconds": cpu, "rss_bytes": rss if rss >= 0 else None,
            "monotonic_s": time.monotonic(), "status": "OBSERVED", "reason": None,
            "backend": "linux_proc_stat"}


def _windows_process(pid):
    import ctypes
    from ctypes import wintypes

    class MemoryCounters(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    memory_api = ctypes.WinDLL("psapi", use_last_error=True).GetProcessMemoryInfo
    memory_api.argtypes = [wintypes.HANDLE, ctypes.POINTER(MemoryCounters), wintypes.DWORD]
    handle = kernel.OpenProcess(0x0400 | 0x0010, False, pid)  # QUERY_INFORMATION | VM_READ
    if not handle:
        return _unknown("process", "PROCESS_UNAVAILABLE_OR_ACCESS_DENIED", pid=pid)
    try:
        creation, exit_time, kernel_time, user_time = (wintypes.FILETIME() for _ in range(4))
        if not kernel.GetProcessTimes(handle, creation, exit_time, kernel_time, user_time):
            return _unknown("process", "CPU_COUNTER_UNAVAILABLE", pid=pid)
        ticks = lambda value: (value.dwHighDateTime << 32) | value.dwLowDateTime
        counters = MemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        memory_ok = memory_api(handle, ctypes.byref(counters), counters.cb)
        return {"scope": "process", "pid": pid, "identity": str(ticks(creation)),
                "cpu_seconds": (ticks(kernel_time) + ticks(user_time)) / 10_000_000,
                "rss_bytes": counters.WorkingSetSize if memory_ok else None,
                "monotonic_s": time.monotonic(), "status": "OBSERVED",
                "reason": None if memory_ok else "RSS_COUNTER_UNAVAILABLE",
                "backend": "windows_process_handle"}
    finally:
        kernel.CloseHandle(handle)


def cgroup_sample(path, index):
    """Read only cpu.stat and memory.current in an explicitly configured v2 scope."""
    base = {"scope": "cgroup", "cgroup_index": index}
    if not sys.platform.startswith("linux"):
        return _unknown("cgroup", "CGROUP_V2_REQUIRES_LINUX", cgroup_index=index)
    try:
        before = path.stat()
        identity = f"{before.st_dev}:{before.st_ino}"
        cpu = rss = None
        reasons = []
        try:
            stats = dict(line.split() for line in (path / "cpu.stat").read_text().splitlines())
            cpu = int(stats["usage_usec"]) / 1_000_000
            if cpu < 0:
                cpu = None
        except (OSError, ValueError, KeyError):
            reasons.append("CPU_COUNTER_UNAVAILABLE")
        try:
            rss = int((path / "memory.current").read_text().strip())
            if rss < 0:
                rss = None
        except (OSError, ValueError):
            reasons.append("MEMORY_COUNTER_UNAVAILABLE")
        after = path.stat()
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            return _unknown("cgroup", "IDENTITY_CHANGED_DURING_SAMPLE", cgroup_index=index)
        return {**base, "identity": identity, "cpu_seconds": cpu, "memory_current_bytes": rss,
                "memory_semantics": "cgroup_memory_current_includes_cache_and_descendants",
                "monotonic_s": time.monotonic(), "status": "OBSERVED" if cpu is not None or rss is not None else "UNKNOWN",
                "reason": ";".join(reasons) or None, "backend": "linux_cgroup_v2"}
    except OSError:
        return _unknown("cgroup", "CGROUP_UNAVAILABLE", cgroup_index=index)


def counter_summary(records, elapsed, gap_limit, *, cgroup=False):
    """Integrate only adjacent valid readings from the same process/group identity."""
    memory_key = "memory_current_bytes" if cgroup else "rss_bytes"
    cpu = memory_integral = cpu_covered = memory_covered = 0.0
    cpu_edges = 0
    identities = {row["identity"] for row in records if row.get("identity") is not None}
    for left, right in zip(records, records[1:]):
        dt = right["monotonic_s"] - left["monotonic_s"]
        if not (0 < dt <= gap_limit and left.get("identity") is not None
                and left.get("identity") == right.get("identity")):
            continue
        a, b = left.get("cpu_seconds"), right.get("cpu_seconds")
        if a is not None and b is not None and b >= a:
            cpu += b - a
            cpu_covered += dt
            cpu_edges += 1
        a, b = left.get(memory_key), right.get(memory_key)
        if a is not None and b is not None:
            memory_integral += dt * (a + b) / 2
            memory_covered += dt
    memories = [row[memory_key] for row in records if row.get(memory_key) is not None]
    result = {"measurement_kind": "MEASURED_COUNTERS_WITH_SAMPLED_MEMORY", "identity_count": len(identities),
            "identity_changed": len(identities) > 1, "sample_count": len(records),
            "cpu_seconds_delta": cpu if cpu_edges else None,
            "cpu_valid_time_s": cpu_covered,
            "cpu_time_coverage": min(1.0, cpu_covered / elapsed) if elapsed else None,
            "peak_sampled_rss_bytes": max(memories) if memories else None,
            "rss_byte_s": memory_integral if memory_covered else None,
            "rss_valid_time_s": memory_covered,
            "rss_time_coverage": min(1.0, memory_covered / elapsed) if elapsed else None}
    if cgroup:
        for key in ("peak_sampled_rss_bytes", "rss_byte_s", "rss_valid_time_s", "rss_time_coverage"):
            result[key.replace("rss", "memory")] = result.pop(key)
    return result
