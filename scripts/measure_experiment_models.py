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
                # A monitor restart leaves an unknown interval between files.
                # Keep that interval uncovered even when the files are close in time.
                if left.get("_source") != right.get("_source"):
                    continue
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


def option_value(argv, option):
    for index, value in enumerate(argv):
        if value == option:
            if index + 1 >= len(argv):
                raise ValueError(f"{option} needs a value")
            return argv[index + 1]
        if value.startswith(option + "="):
            return value.split("=", 1)[1]
    return None


def matching_attach(output, run_id):
    matches = []
    for folder in output.glob("attach-*"):
        preflight = folder / "attach-preflight.json"
        if preflight.is_file() and json.loads(preflight.read_bytes()).get("run_id") == run_id:
            matches.append(folder)
    if len(matches) != 1:
        raise ValueError(f"Expected one attach folder for live run {run_id}; found {len(matches)}")
    return matches[0]


def read_samples(paths):
    samples = []
    seen = set()
    for source_id, path in enumerate(paths):
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        for line in path.read_text(encoding="utf-8").splitlines():
            sample_row = json.loads(line)
            sample_row["_source"] = source_id
            samples.append(sample_row)
    return sorted(samples, key=lambda row: row["at_s"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="fresh pilot output directory created by the wrapped command")
    parser.add_argument("--business-unit", required=True)
    parser.add_argument("--guard-unit", required=True)
    parser.add_argument("--attached-run", type=Path,
                        help="original RUN directory whose endpoint samples should be combined with this attach attempt")
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
    attach_run_id = None
    attachment = None
    sample_paths = [path]
    workload_dirs = [args.output]
    if args.attached_run:
        attach_run_id = option_value(args.command, "--attach-training")
        if not attach_run_id:
            raise ValueError("--attached-run requires the wrapped command's --attach-training RUN_ID")
        attachment = matching_attach(args.attached_run, attach_run_id)
        original_samples = args.attached_run.parent / (args.attached_run.name + "-endpoint-samples.jsonl")
        sample_paths.insert(0, original_samples)
        workload_dirs = [attachment]
    samples = read_samples(sample_paths)
    sessions = {}
    for folder in workload_dirs:
        files = sorted([*folder.glob("*-drain.json"), *folder.glob("*-finish.json")])
        for file in files:
            result = json.loads(file.read_bytes())
            if attach_run_id and result.get("id") != attach_run_id:
                continue
            workload = result.get("workload", {})
            if workload.get("summary"):
                sessions[workload["session"]] = summarize(samples, workload)
    report = dict(scope="Managed capability workers plus the two named model service cgroups; host, tools, graders and proxies excluded.",
        units=dict(business=args.business_unit, guard=args.guard_unit), interval_s=INTERVAL,
        gpu_source="NVML compute processes with stable host cgroup membership before and after sampling",
        missing="Incomplete observed intervals are null", sessions=list(sessions.values()))
    if args.attached_run:
        report.update(attached_run=str(args.attached_run.resolve()), attached_run_id=attach_run_id,
                      attachment=str(attachment.resolve()), sample_files=[str(item.resolve()) for item in sample_paths
                                                                          if item.is_file()])
    (path.parent/(args.output.name+"-resource-scopes.json")).write_text(json.dumps(report, indent=2)+"\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
