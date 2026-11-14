"""Explicit capability diagnostics with project leases, separate from workloads."""

from contextlib import nullcontext
import time
import uuid

from seampact.hosting.capability_hosting import project_scope
from seampact.guard_contract import verification_error
from seampact.store import now


def invoke(platform, body):
    if set(body) - {"unit_id", "input", "project_id"}:
        raise ValueError("能力诊断只接收能力、输入和项目标识")
    workload = getattr(platform.execution, "workload", None)
    if workload and workload["status"] in {"RUNNING", "DRAINING"}:
        raise ValueError("冻结工作负载运行中，诊断调用不能混入其资源范围")
    catalog, router = platform.capabilities, platform.deployments
    unit = catalog.unit(body["unit_id"])
    group = catalog._group(unit["runtime_group"])
    owners = {b["project_id"] for b in group.get("bindings", {}).values() if b.get("form") == "project"}
    project_id = body.get("project_id")
    if owners and (len(owners) != 1 or project_id not in owners):
        raise ValueError("项目能力诊断需要明确匹配其资源绑定的项目")
    with router.lock:
        leased = set(router.affinity.values())
        if any(row.get("in_flight") or row["id"] in leased for row in router.live_instances(group["id"])):
            raise ValueError("能力实例正在被其他任务使用，请待收尾后诊断")
    identity = uuid.uuid4().hex
    record = dict(id=identity, status="RUNNING", kind="capability_diagnostic", unit_id=unit["id"],
                  project_id=project_id, started_at=now(), input=body["input"], model_usage=[])
    lease = None
    if project_id is not None:
        with platform.projects.lock:
            platform.projects.require_idle(project_id)
            lease = platform.projects.begin_task(project_id, project_id + "_diagnostics", identity)
    platform.store.save("capability_diagnostic", identity, record)
    def observe(event):
        if event.get("kind") == "model_usage":
            record["model_usage"].append(event)
    try:
        # The explicit project scope permits diagnostics; the lease prevents
        # corpus changes or cleanup until this physical call has ended.
        with project_scope(project_id) if project_id is not None else nullcontext():
            result = catalog.invoke(unit["id"], body["input"], run_id=identity, on_event=observe)
        reason = verification_error(unit["capability"], body["input"], result)
        record.update(status="FAILED" if reason else "COMPLETED", result=result,
                      verification=dict(status="FAILED" if reason else "SUCCEEDED", reason=reason))
    except Exception as error:
        record.update(status="FAILED", error=str(error))
        raise
    finally:
        router.release_task(identity)
        try:
            instances = router.live_instances(group["id"])
            with router.lock:
                leased = set(router.affinity.values())
            for instance in instances:
                if instance["id"] not in leased and not instance.get("in_flight"):
                    router.drain_stop(group["id"], dict(instance_id=instance["id"]),
                                      run_id=identity, reason="capability_diagnostic_finished")
            deadline = time.monotonic() + 30
            while router.live_instances(group["id"]) and time.monotonic() < deadline:
                time.sleep(0.1)
            drained = not router.live_instances(group["id"])
            record["release"] = "RELEASED" if drained else "UNCONFIRMED"
        except Exception as error:
            drained = False
            record.update(release="UNCONFIRMED", release_error=str(error))
        if lease is not None:
            record["project_lease"] = platform.projects.finish_task(identity, drained=drained)
        record["finished_at"] = now()
        platform.store.save("capability_diagnostic", identity, record)
    return dict(result, diagnostic={key: record.get(key) for key in
                ("id", "status", "verification", "release", "release_error", "project_lease", "model_usage")})
