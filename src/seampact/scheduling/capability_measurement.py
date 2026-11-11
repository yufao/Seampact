"""Sample memory of explicitly selected managed capability instances only."""

import copy
import csv
import io
import math
import subprocess
import time


def _run(arguments):
    return subprocess.run(arguments, capture_output=True, text=True, timeout=10, check=False)


def _gpu_memory(instance):
    """Sum GPU process memory only for host PIDs in this exact container.

    Empty verified GPU assignment means zero accessible VRAM. Unsupported PID
    attribution, N/A counters, and changed identities remain unknown.
    """
    allocation = instance.get("gpu_allocation") or {}
    devices = instance.get("gpu_devices")
    if devices == [] and allocation.get("status") == "VERIFIED":
        return 0.0, "verified_no_gpu_assignment"
    if not devices or allocation.get("status") != "VERIFIED":
        return None, "GPU_ASSIGNMENT_UNVERIFIED"
    container = (instance.get("handle") or {}).get("container")
    if not container:
        return None, "CONTAINER_IDENTITY_UNAVAILABLE"
    try:
        def members():
            result = _run(["docker", "top", container, "-eo", "pid"])
            if result.returncode:
                return None
            rows = result.stdout.splitlines()[1:]
            return {int(row.strip()) for row in rows if row.strip().isdigit()}
        before = members()
        if not before:
            return None, "CONTAINER_HOST_PIDS_UNAVAILABLE"
        result = _run(["nvidia-smi", "--id=" + ",".join(devices),
                       "--query-compute-apps=gpu_uuid,pid,used_gpu_memory", "--format=csv,noheader,nounits"])
        after = members()
        if result.returncode or before != after:
            return None, "GPU_COUNTER_OR_PID_IDENTITY_UNCONFIRMED"
        amount = 0.0
        for fields in csv.reader(io.StringIO(result.stdout), skipinitialspace=True):
            if not fields or not any(field.strip() for field in fields):
                continue
            if len(fields) != 3 or fields[0].strip() not in devices:
                return None, "GPU_COUNTER_SCOPE_INVALID"
            pid = fields[1].strip()
            if not pid.isdigit():
                return None, "GPU_PID_UNAVAILABLE"
            if int(pid) in before:
                memory = float(fields[2].strip())
                if memory < 0 or not math.isfinite(memory):
                    return None, "GPU_MEMORY_COUNTER_UNAVAILABLE"
                amount += memory * 2**20
        return amount, "configured_gpu_process_memory_for_container_host_pids"
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None, "GPU_PROCESS_ACCOUNTING_UNAVAILABLE"


def sample_instances(deployments, instance_ids):
    """Reuse verified private-cgroup RAM readings; never sample the whole host."""
    with deployments.lock:
        selected = {identity: copy.deepcopy(deployments.instances[identity]) for identity in instance_ids
                    if identity in deployments.instances and deployments.instances[identity].get("released") is not True
                    and (deployments.instances[identity].get("handle") or {}).get("kind") == "docker"}
    if not selected:
        return []
    ram = {row["instance_id"]: row for row in deployments.sample_resources(instance_ids=set(selected))}
    samples = []
    for identity, instance in selected.items():
        row = ram.get(identity, {})
        gpu, gpu_basis = _gpu_memory(instance) if row.get("identity") else (None, "CONTAINER_IDENTITY_UNCONFIRMED")
        confirmed = False
        try:
            inspected = _run(["docker", "inspect", "--format", '{{.Id}} {{index .Config.Labels "seampact.instance"}}',
                              instance["handle"]["container"]])
            values = inspected.stdout.strip().split()
            confirmed = inspected.returncode == 0 and len(values) == 2 and values[1] == identity and (
                values[0] + ":" + str(instance["generation"]) == row.get("identity"))
        except (OSError, subprocess.TimeoutExpired):
            pass
        # Verify the generation still owns the same backend object after both probes.
        with deployments.lock:
            current = deployments.instances.get(identity) or {}
            unchanged = (confirmed and current.get("generation") == instance["generation"] and
                         current.get("handle") == instance.get("handle") and current.get("released") is not True)
        samples.append(dict(instance_id=identity, generation=instance["generation"],
            identity=row.get("identity") if unchanged else None, at_s=time.time(),
            ram_bytes=row.get("memory_current_bytes") if unchanged and row.get("status") == "OBSERVED" else None,
            vram_bytes=gpu if unchanged else None,
            ram_provenance=row.get("memory_semantics", row.get("reason", "COUNTERS_UNAVAILABLE")),
            vram_provenance=gpu_basis))
    return samples
