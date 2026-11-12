"""Sample memory of explicitly selected managed capability instances only."""

import copy
import csv
import io
import math
import os
import subprocess
import time
from pathlib import Path


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
        backend_id = (instance.get("handle") or {}).get("container_id") or instance.get("resource_backend_identity")
        same_kernel = False
        if os.name == "posix" and isinstance(backend_id, str) and len(backend_id) == 64:
            try:
                same_kernel = all(backend_id in Path("/proc", str(pid), "cgroup").read_text() for pid in before)
            except OSError:
                pass
        # A controller with host PID visibility proves that Docker and NVML
        # share the exact kernel. Remote/private-PID controllers query the
        # backend worker instead and never infer zero from unmatched PIDs.
        command = ["nvidia-smi"] if same_kernel else ["docker", "exec", container, "nvidia-smi"]
        result = _run(command + ["--id=" + ",".join(devices),
                       "--query-compute-apps=gpu_uuid,pid,used_gpu_memory", "--format=csv,noheader,nounits"])
        after = members()
        if result.returncode or before != after:
            return None, "GPU_COUNTER_OR_PID_IDENTITY_UNCONFIRMED"
        amount, matched = 0.0, 0
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
                matched += 1
        if not matched:
            return ((0.0, "same_kernel_cgroup_verified_full_nvml_table_has_no_container_cuda_process") if same_kernel else
                    (None, "NO_VERIFIED_MATCHING_BACKEND_GPU_PROCESS_COUNTER"))
        return amount, "configured_gpu_process_memory_for_container_host_pids"
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None, "GPU_PROCESS_ACCOUNTING_UNAVAILABLE"


def sample_instances(deployments, instance_ids, *, vram_source="nvml_process", lifecycle_boundary=False):
    """Reuse verified private-cgroup RAM readings; never sample the whole host."""
    with deployments.lock:
        selected = {identity: copy.deepcopy(deployments.instances[identity]) for identity in instance_ids
                    if identity in deployments.instances and deployments.instances[identity].get("released") is not True
                    and (lifecycle_boundary or ("started" in deployments.instances[identity].get("resource_boundaries", {})
                         and identity not in getattr(deployments, "stops_in_progress", set())))
                    and (deployments.instances[identity].get("handle") or {}).get("kind") == "docker"}
    if not selected:
        return []
    ram = {row["instance_id"]: row for row in deployments.sample_resources(instance_ids=set(selected))}
    samples = []
    for identity, instance in selected.items():
        row = ram.get(identity, {})
        instance["resource_backend_identity"] = (row.get("identity") or "").rsplit(":", 1)[0]
        if not row.get("identity") and (instance.get("handle") or {}).get("container_id"):
            from seampact.observability.resource_observation import docker_zero_boundary
            created = docker_zero_boundary(instance, "created")
            if created is not None:
                samples.append(dict(created, vram_source=vram_source))
                continue
        if row.get("identity") and vram_source == "torch_allocator_reserved":
            try:
                health = deployments.resource_health(instance)
                allocator = (health.get("resources") or {}).get("torch_allocator") or {}
                gpu = allocator.get("reserved_bytes") if allocator.get("status") == "OBSERVED" else None
                if gpu is not None and (type(gpu) is not int or gpu < 0):
                    gpu = None
                gpu_basis = "worker_main_process_pytorch_allocator_reserved_excludes_context_other_allocators_children"
            except Exception:
                gpu, gpu_basis = None, "WORKER_ALLOCATOR_COUNTER_UNAVAILABLE"
        else:
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
            # Start/stop own their forced lifecycle endpoints. A periodic probe
            # crossing either handoff has no stable process to observe.
            if not lifecycle_boundary and ("started" not in current.get("resource_boundaries", {})
                    or identity in getattr(deployments, "stops_in_progress", set()) or current.get("released") is True):
                continue
            unchanged = (confirmed and current.get("generation") == instance["generation"] and
                         current.get("handle") == instance.get("handle") and current.get("released") is not True)
        samples.append(dict(instance_id=identity, generation=instance["generation"],
            identity=row.get("identity") if unchanged else None, at_s=time.time(),
            ram_bytes=row.get("memory_current_bytes") if unchanged and row.get("status") == "OBSERVED" else None,
            vram_bytes=gpu if unchanged else None,
            vram_source=vram_source,
            ram_bytes_blocked=not bool(instance.get("pending_stop")),
            vram_bytes_blocked=(vram_source == "nvml_process" or
                                bool(instance.get("process_nonce")) and not bool(instance.get("pending_stop"))),
            ram_provenance=row.get("memory_semantics", row.get("reason", "COUNTERS_UNAVAILABLE")),
            vram_provenance=gpu_basis))
    return samples
