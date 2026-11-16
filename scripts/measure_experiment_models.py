"""Measure dedicated Linux model services during a pilot without changing PPO's worker-only reward."""
import argparse
import csv
import json
from pathlib import Path
import subprocess
import threading
import time


INTERVAL = 4
METRICS = ("ram_byte_s", "vram_byte_s")


def unit_cgroup(unit):
    path = subprocess.check_output(["systemctl", "--user", "show", unit, "-p", "ControlGroup", "--value"],
                                   text=True).strip()
    root = Path("/sys/fs/cgroup")
    resolved = (root / path.lstrip("/")).resolve()
    if not path or resolved == root or not resolved.is_relative_to(root) or not (resolved/"memory.current").is_file():
        raise ValueError(f"No active private memory cgroup for {unit}")
    return resolved


def members(group):
    files = list(group.rglob("cgroup.procs"))
    if not files:
        raise OSError("Model cgroup disappeared")
    return {int(pid) for path in files for pid in path.read_text().split()}


def sample(groups):
    row = {"at_s": time.time()}
    before = {}
    for role, group in groups.items():
        row[role] = dict(ram_bytes=None, vram_bytes=None)
        try:
            before[role] = members(group)
            row[role]["ram_bytes"] = int((group / "memory.current").read_text())
        except (ValueError, OSError):
            continue
    try:
        raw = subprocess.check_output(["nvidia-smi", "--query-compute-apps=pid,used_gpu_memory",
                                       "--format=csv,noheader,nounits"], text=True, timeout=5, stderr=subprocess.DEVNULL)
        process_rows = [(int(pid), amount.strip()) for pid, amount in csv.reader(raw.splitlines())]
        for role, pids in before.items():
            try:
                if pids and members(groups[role]) == pids:
                    row[role]["vram_bytes"] = sum(int(amount) * 1024**2 for pid, amount in process_rows if pid in pids)
            except (ValueError, OSError):
                continue
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return row


def summarize(samples, workload):
    resource = workload["summary"]["resources"]
    start, end = resource["start_s"], resource["end_s"]
    scopes = {"managed_workers": {key: resource["measured"].get(key) for key in METRICS}}
    coverage, observed = {}, {}
    for role in ("business", "guard"):
        scopes[role], coverage[role], observed[role] = {}, {}, {}
        for metric in METRICS:
            field = metric.replace("_byte_s", "_bytes")
            covered = area = 0.0
            for left, right in zip(samples, samples[1:]):
                t0, t1 = left["at_s"], right["at_s"]
                a, b = max(t0, start), min(t1, end)
                v0, v1 = left[role][field], right[role][field]
                if b <= a or t1-t0 > INTERVAL*3 or v0 is None or v1 is None:
                    continue
                mean = v0 + (v1-v0) * ((a+b)/2-t0) / (t1-t0)
                area += mean * (b-a)
                covered += b-a
            coverage[role][metric] = covered
            observed[role][metric] = area if covered else None
            scopes[role][metric] = area if abs(covered-(end-start)) < 0.01 else None
    for name, roles in (("excluding_business", ("managed_workers", "guard")),
                        ("including_business", ("managed_workers", "guard", "business"))):
        scopes[name] = {}
        for key in METRICS:
            values = [scopes[role][key] for role in roles]
            scopes[name][key] = None if any(v is None for v in values) else sum(values)
    return dict(session=workload["session"], start_s=start, end_s=end, duration_s=end-start,
                byte_seconds=scopes, endpoint_coverage_s=coverage, endpoint_observed_byte_seconds=observed)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="fresh pilot output directory created by the wrapped command")
    parser.add_argument("--business-unit", required=True)
    parser.add_argument("--guard-unit", required=True)
    parser.add_argument("command", nargs="+")
    args = parser.parse_args()
    groups = {role: unit_cgroup(unit) for role, unit in (("business", args.business_unit), ("guard", args.guard_unit))}
    if groups["business"] == groups["guard"]:
        raise ValueError("Declared endpoint units share a cgroup")
    # The pilot owns creation of its output directory. Sibling samples survive a failed driver.
    path = args.output.parent / (args.output.name + "-endpoint-samples.jsonl")
    stopped = threading.Event()
    with path.open("x", encoding="utf-8", buffering=1) as stream:
        def record():
            stream.write(json.dumps(sample(groups)) + "\n")
        def monitor():
            while not stopped.wait(INTERVAL):
                record()
        record()
        thread = threading.Thread(target=monitor, daemon=True)
        thread.start()
        try:
            code = subprocess.call(args.command)
        finally:
            stopped.set()
            thread.join()
            record()
    samples = [json.loads(line) for line in path.read_text().splitlines()]
    sessions = {}
    for file in sorted([*args.output.glob("*-drain.json"), *args.output.glob("*-finish.json")]):
        workload = json.loads(file.read_bytes()).get("workload", {})
        if workload.get("summary"):
            sessions[workload["session"]] = summarize(samples, workload)
    report = dict(scope="Managed capability workers plus the two named model service cgroups; host, tools, graders and proxies excluded.",
        units=dict(business=args.business_unit, guard=args.guard_unit), interval_s=INTERVAL,
        gpu_source="NVML compute processes with stable host cgroup membership before and after sampling",
        missing="Incomplete observed intervals are null", sessions=list(sessions.values()))
    (path.parent/(args.output.name+"-resource-scopes.json")).write_text(json.dumps(report, indent=2)+"\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
