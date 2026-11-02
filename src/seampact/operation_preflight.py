"""Read-only activity observation before an update or source environment change."""

from .store import now


_LIVE = {"UNDEPLOYED", "STARTING", "READY", "DRAINING", "UNKNOWN"}


def preflight(platform, operation, source_id=None):
    if operation not in {"update", "source_environment"}:
        raise ValueError("不支持的操作")
    if operation == "source_environment" and not source_id:
        raise ValueError("来源环境操作需要 source_id")
    source = platform.store.get("harness", source_id) if source_id else None
    if operation == "source_environment" and not source:
        raise ValueError("Harness 尚未接入")
    items = []

    def add(kind, status, reason, next_step, **facts):
        items.append(dict(kind=kind, title={"target_prepare": "目标解释器准备", "analysis": "分析与修订", "task": "任务", "research_run": "研究运行", "worker_build": "镜像构建", "worker_publish": "镜像发布", "runtime_observation": "运行组观察", "run_lock": "研究运行锁", "runtime_instance": "运行实例", "managed_service": "受管服务", "service_lease": "服务使用租约", "source_calls": "来源调用", "call": "运行调用", "runtime_resources": "运行资源", "source": "来源状态", "monitor": "资源记录", "source_observation": "来源操作", "root": "任务根请求"}.get(kind, kind), status=status, reason=reason, next_step=next_step, **facts))

    source_lock = getattr(platform, "source_lock", None)
    if source_lock is not None:
        if source_lock.acquire(blocking=False):
            source_lock.release()
        else:
            add("source_observation", "UNKNOWN", "来源或修订操作正在进行，暂无法确认其状态", "等待操作结束后刷新")
    if source and source.get("status") == "DETACHED":
        add("source", "BLOCKED", "来源已摘除", "先重新接入来源", source_id=source_id)
    sources = platform.store.list("harness") if operation == "update" else [source]
    groups = platform.store.list("runtime_group")
    if operation == "source_environment":
        groups = [g for g in groups if g.get("source_id") == source_id]
    group_ids = {g["id"] for g in groups}
    for row in sources:
        if (row.get("target_runtime") or {}).get("status") == "PREPARING":
            add("target_prepare", "BLOCKED", "目标解释器正在准备", "等待准备完成", source_id=row["id"])
    for job in platform.store.list("analysis"):
        if job.get("status") not in {"RUNNING", "CANCELLING"}:
            continue
        owner = job.get("harness_id") or (platform.store.get("request", job.get("request_id")) or {}).get("source_id")
        if operation == "update" or owner == source_id:
            add("analysis", "BLOCKED", "分析或修订正在进行", "等待分析或修订结束", id=job.get("id"), source_id=owner)
    if operation == "update":
        for task in platform.store.list("task"):
            if task.get("status") in {"RUNNING", "CANCELLING"}:
                add("task", "BLOCKED", "任务正在运行或结束中", "等待任务结束", task_id=task.get("id"))
    if operation == "update" and getattr(platform, "monitor", None) is not None:
        try:
            current = platform.monitor.status().get("current") or {}
            if current.get("status") in {"RUNNING", "STOPPING"}:
                add("monitor", "DRAIN", "资源记录正在进行，更新会中断记录", "先停止资源记录；这不会停止任务", id=current.get("id"))
        except (OSError, ValueError, RuntimeError):
            add("monitor", "UNKNOWN", "资源记录状态暂时无法确认", "检查记录状态后刷新")
    for run in platform.store.list("scheduler_run"):
        if run.get("status") in {"RUNNING", "QUEUED"} and (operation == "update" or source_id in run.get("source_ids", [])):
            add("research_run", "BLOCKED" if operation == "update" else "DRAIN", "研究运行正在执行或排队", "等待研究运行结束并释放运行组", run_id=run.get("id"))
    for artifact in platform.store.list("deployment"):
        if operation != "update" and artifact.get("id") not in group_ids:
            continue
        for phase in ("build", "publish"):
            if (artifact.get(phase) or {}).get("status") == "RUNNING":
                add("worker_" + phase, "BLOCKED", "运行组镜像正在构建或发布", "等待操作完成", group_id=artifact.get("id"))

    # The controller and service share this lock. Never wait for a long-running
    # operation or invoke its mutating reconciliation from this observation.
    lock = platform.deployments.lock
    if not lock.acquire(blocking=False):
        add("runtime_observation", "UNKNOWN", "运行组状态正在变化，暂时无法观察", "稍后刷新")
    else:
        try:
            if platform.deployments.controller_error:
                add("runtime_observation", "UNKNOWN", "运行控制器报告观察异常", "检查控制器状态后刷新")
            for group_id, owner in platform.deployments.run_locks.items():
                if operation == "update" or group_id in group_ids:
                    add("run_lock", "BLOCKED", "研究运行占用运行组", "等待研究运行结束释放锁", group_id=group_id, run_id=owner.get("run_id"))
            for instance in platform.deployments.instances.values():
                group_id = instance.get("group_id")
                if instance.get("target_kind") == "model-service":
                    continue
                if operation != "update" and group_id not in group_ids:
                    continue
                state = instance.get("state")
                held = state in _LIVE or (state == "FAILED" and bool(instance.get("handle") or instance.get("endpoint")) and not instance.get("released"))
                if held:
                    status = "UNKNOWN" if state == "UNKNOWN" else "DRAIN"
                    add("runtime_instance", status, "实例状态未确认" if status == "UNKNOWN" else "运行实例尚未释放", "确认实例状态并按需排空、停止" if status == "UNKNOWN" else "先排空并停止实例", group_id=group_id, id=instance.get("id"), state=state)
            if operation == "update":
                for instance in platform.managed_services.instances.values():
                    if instance.get("target_kind") != "model-service" or instance.get("released") is True:
                        continue
                    state = instance.get("state")
                    add("managed_service", "UNKNOWN" if state == "UNKNOWN" else "DRAIN", "受管服务实例尚未确认或释放", "确认请求已结束，排空并停止服务", group_id=instance.get("group_id"), id=instance.get("id"), state=state)
                for group_id, leases in platform.managed_services.leases.items():
                    for owner, lease in leases.items():
                        add("service_lease", "UNKNOWN" if lease.get("unknown") else "DRAIN", "受管服务仍有使用租约", "等待请求和租约结束；未知状态需先确认", group_id=group_id, owner=owner)
        finally:
            lock.release()

    # Includes local calls and UNKNOWN physical execution, which cannot be
    # inferred solely from task records. Do not treat saved images as activity.
    if operation == "source_environment":
        if not platform.catalog.lock.acquire(blocking=False):
            add("source_calls", "UNKNOWN", "来源调用状态正在变化", "稍后刷新")
        else:
            try:
                if platform.catalog.source_running(source_id):
                    add("source_calls", "BLOCKED", "来源仍有调用执行或状态未知", "等待调用结束并确认状态", source_id=source_id)
            finally:
                platform.catalog.lock.release()
    else:
        snapshot = platform.runtime.snapshot()
        for root in snapshot.get("roots", {}).values():
            state = root.get("state")
            if state in {"ENABLED", "ACTIVE", "UNKNOWN"}:
                unknown = state == "UNKNOWN" or bool(root.get("outcome_unknown"))
                add("root", "UNKNOWN" if unknown else "BLOCKED",
                    "任务根请求的执行结果尚未确认" if unknown else ("任务根请求正在等待调度" if state == "ENABLED" else "任务根请求正在执行"),
                    "确认请求及其运行环境状态后再更新" if unknown else "等待任务根请求结束",
                    id=root.get("id"), task_id=root.get("task_id"), run_id=root.get("run_id"), state=state)
        for call in snapshot.get("calls", {}).values():
            physical = call.get("physical")
            if physical in {"AUTHORIZED", "STARTED", "ENABLED", "RESERVED", "UNKNOWN"}:
                add("call", "UNKNOWN" if physical == "UNKNOWN" else "BLOCKED", "调用正在执行、等待或状态未知", "等待调用结束；未知状态需先确认", id=call.get("id"), physical=physical)
        if snapshot.get("used"):
            add("runtime_resources", "BLOCKED", "运行时仍有资源占用", "等待调用及资源释放")
    status = ("BLOCKED" if any(i["status"] == "BLOCKED" for i in items) else
              "UNKNOWN" if any(i["status"] == "UNKNOWN" for i in items) else
              "DRAIN" if items else "READY")
    return dict(operation=operation, source_id=source_id, status=status, observed_at=now(), items=items,
                note="只读即时观察；实际操作仍以执行时的检查为准。更新脚本还会检查容器、挂载及部署锁。")
