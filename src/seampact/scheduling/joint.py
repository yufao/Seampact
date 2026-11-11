"""joint/1 workload windows: rules build legal sets, a policy selects, the executor re-checks.

Original dependencies, owner/session order, protection preconditions and resource
limits are kept; the cross-task global arrival order is not. A business decision
submits an approved request without blocking; dispatch chooses among enabled root
requests and checked internal boundaries; resource decisions prepare, add or release instances. A running call is
never preempted. Passive evidence is frozen at task completion and quality is
backfilled after business resources are released; time-integral costs accrue
between adjacent events. Vertical, node scaling and live migration
are outside joint/1.
"""

import copy
import contextlib
import math
import queue
import threading
import time
import uuid
from dataclasses import asdict

from seampact.capabilities import VERSION_2, freeze_joint, joint_candidate, joint_observation
from seampact.scheduling import budget_policy
from seampact.scheduling.costs import episode_costs, summarize_usage, envelope_integral
from seampact.evaluation import evaluate
from seampact.runtime import ProtocolError
from seampact.scenes.scenarios import SceneSession, task_unit_ids, scene_task_criteria, route_metric_applicability, route_metric_report
from seampact.scenes.evaluation_evidence import passive_evaluation_snapshot, verify_passive_evaluation, PassiveEvidenceGap
from seampact.scheduling.residency import freeze_working_sets, WorkingSets, dependency_closure
from seampact.observability.resource_observation import ObservationConfig, WindowObserver

MODE = "joint/1"
DISPATCH = ("fifo/1", "shortest_estimated/1", "edf/1", "learned_ready/1")
RESOURCE = ("fixed/1", "queue_threshold/1", "dependency_cost/1", "shared_lru/1", "learned/1")
FIELDS = {"functional_policy", "dispatch_policy", "resource_policy", "retention", "queue_threshold", "age_guard_ms",
          "max_consecutive_resource_ops", "review_interval_ms", "capacity", "workload", "costs", "scales",
          "initial_state", "start_timeout_s", "dispatch_scope", "boundary_slots", "warm_groups", "observation",
          "budget_policy"}
TERMINAL = {"DONE", "FAILED", "UNFINISHED", "NOT_ARRIVED"}
HELD = {"ACTIVE", "UNKNOWN"}


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _positive(value, label):
    if not _number(value) or value <= 0:
        raise ValueError(label + " 须为有限正数")
    return value


def _count(value, label, default=None):
    value = default if value is None else value
    if type(value) is not int or value < 1:
        raise ValueError(label + " 须为正整数")
    return value


def _arrival_offsets(arrival, size):
    if isinstance(arrival, dict) and set(arrival) == {"interval_ms"}:
        if not _number(arrival["interval_ms"]) or arrival["interval_ms"] < 0:
            raise ValueError("arrival.interval_ms 须为非负有限数")
        return [arrival["interval_ms"] * index for index in range(size)]
    if isinstance(arrival, dict) and set(arrival) == {"offsets_ms"}:
        offsets = arrival["offsets_ms"]
        if (not isinstance(offsets, list) or len(offsets) != size or any(not _number(v) or v < 0 for v in offsets)
                or offsets != sorted(offsets)):
            raise ValueError("arrival.offsets_ms 须为与窗口任务数等长、非负且不减的到达偏移")
        return list(offsets)
    raise ValueError("workload.arrival 须为 {interval_ms} 或 {offsets_ms}")


def _freeze_workload(value, task_ids, episodes):
    if (not isinstance(value, dict)
            or set(value) - {"windows", "tasks_per_window", "arrival", "horizon_ms", "unfinished_reward"}):
        raise ValueError("workload 仅接收 windows、tasks_per_window、arrival、horizon_ms、unfinished_reward")
    if (not isinstance(task_ids, list) or not task_ids
            or any(not isinstance(identity, str) or not identity for identity in task_ids)
            or len(set(task_ids)) != len(task_ids)):
        raise ValueError("联合运行 task_ids 须为非空且不重复的任务 ID 清单")
    episodes = _count(episodes, "episodes")
    windows = []
    if "windows" in value:
        if "tasks_per_window" in value or "arrival" in value:
            raise ValueError("显式 windows 不同时接收 tasks_per_window 或公共 arrival")
        rows = value["windows"]
        if not isinstance(rows, list) or not rows or len(rows) != episodes:
            raise ValueError("workload.windows 须为与 episodes 等长的非空窗口清单")
        for row in rows:
            if not isinstance(row, dict) or set(row) != {"task_ids", "arrival"}:
                raise ValueError("每个显式窗口须声明 task_ids 与 arrival")
            members = row["task_ids"]
            if (not isinstance(members, list) or not members
                    or any(not isinstance(identity, str) or identity not in task_ids for identity in members)
                    or len(set(members)) != len(members)):
                raise ValueError("窗口 task_ids 须非空、不重复且属于本次运行任务清单")
            windows.append(dict(task_ids=list(members), offsets_ms=_arrival_offsets(row["arrival"], len(members))))
    else:
        size = _count(value.get("tasks_per_window"), "workload.tasks_per_window")
        if size > len(task_ids):
            raise ValueError("一个窗口的任务数不能超过任务清单")
        offsets = _arrival_offsets(value.get("arrival"), size)
        blocks = [task_ids[start:start + size] for start in range(0, len(task_ids), size)]
        for number in range(episodes):
            members = blocks[number % len(blocks)]
            windows.append(dict(task_ids=list(members), offsets_ms=offsets[:len(members)]))
    frozen = dict(windows=windows)
    horizon = value.get("horizon_ms")
    if horizon is not None:
        _positive(horizon, "workload.horizon_ms")
        if any(horizon < window["offsets_ms"][-1] for window in windows):
            raise ValueError("horizon_ms 不能早于窗口内最后到达")
        if not _number(value.get("unfinished_reward")):
            raise ValueError("设置收尾时限时须预先声明未完成任务的 unfinished_reward")
        frozen.update(horizon_ms=horizon, unfinished_reward=value["unfinished_reward"])
    elif "unfinished_reward" in value:
        raise ValueError("unfinished_reward 只用于设置了 horizon_ms 的窗口")
    return frozen


def validate_worker_gpu_envelopes(groups, resources, demands):
    """Check declared device budgets against frozen assignment, never card size."""
    if not resources:
        return  # Slot-only development runs retain unknown GPU reservation costs.
    gpu_resources = {name[len("gpu:"):-len(":memory_mb")]: name for name in resources
                     if name.startswith("gpu:") and name.endswith(":memory_mb")}
    for group, placement in groups.items():
        if group.startswith("service:"):
            continue  # Managed-service envelopes are checked separately below.
        devices = set(placement.get("gpu_devices", []))
        requested = {device for device, name in gpu_resources.items() if demands[group][name] > 0}
        if requested - devices:
            raise ValueError("worker GPU 驻留需求必须对应冻结分配的 UUID：" + group)
        if devices - set(gpu_resources) or devices - requested:
            raise ValueError("worker 已分配 GPU 须声明同 UUID 的显存配额与正值驻留需求：" + group)


def configure(config, *, scene, tasks, task_ids, algorithm, criteria, contracts, groups, deployments,
              learning, references, mode, placements=None, service_dependencies=None, shared_services=None,
              online_services=None, scoring_services=None):
    """Validate and freeze one joint/1 run; every compared policy shares these values."""
    value = copy.deepcopy(config.get("joint") or {})
    if not isinstance(value, dict) or set(value) - FIELDS:
        raise ValueError("joint 仅接收：" + "、".join(sorted(FIELDS)))
    if (scene.get("representation") or {}).get("version") != VERSION_2:
        raise ValueError("联合模式的业务候选需要 " + VERSION_2 + " 共同表示")
    if config.get("resource_control"):
        raise ValueError("联合模式的初态、副本与回收由 joint 配置冻结，不再接收 resource_control")
    if (config.get("feedback_protocol") or {}).get("reward_timing", "sparse_terminal") != "sparse_terminal":
        raise ValueError("联合模式按终局证据结算质量、按事件区间累计时间成本；event_module/1 局部奖励未定义")
    if mode == "rejudge":
        raise ValueError("联合窗口的旧经历重评未定义；请采集新经历")
    # Resource experiments preserve the declared functional reference unless
    # functional learning is explicitly selected in a separate experiment.
    functional = value.get("functional_policy", "required_only" if algorithm in learning else algorithm)
    dispatch = value.get("dispatch_policy", "fifo/1")
    resource = value.get("resource_policy", "fixed/1")
    if functional not in {"learned", *references}:
        raise ValueError("functional_policy 须为 learned 或固定参照：" + "、".join(sorted(references)))
    if dispatch not in DISPATCH:
        raise ValueError("dispatch_policy 须为：" + "、".join(DISPATCH))
    if resource not in RESOURCE:
        raise ValueError("resource_policy 须为：" + "、".join(RESOURCE))
    learned = [kind for kind, flag in (("business", functional == "learned"), ("dispatch", dispatch == "learned_ready/1"),
                                       ("resource", resource == "learned/1")) if flag]
    if learned and algorithm not in learning:
        raise ValueError("交给学习器的决策类型需要学习器：" + "、".join(learned))
    if not learned and (algorithm in learning or algorithm != functional):
        raise ValueError("没有决策类型交给学习器时，algorithm 须等于固定功能参照")
    budget = budget_policy.validate(value.get("budget_policy"), algorithm, mode, config.get("episodes", len(task_ids)))
    if budget and config.get("parameters", {}).get("gamma", 1.0) != 1.0:
        raise ValueError("budget_policy requires gamma=1")
    if budget and functional == "learned" and not budget.get("quality_budgets"):
        raise ValueError("learned optional intervention requires explicit quality budgets and frozen cohorts")

    retention = value.get("retention")
    if resource == "learned/1":
        if retention is not None:
            raise ValueError("learned/1 的保留与回收由策略选择，不接收 retention")
    else:
        retention = copy.deepcopy(retention or {"mode": "fixed_resident"})
        if not isinstance(retention, dict) or retention.get("mode") not in {"fixed_resident", "idle_retire/1"}:
            raise ValueError("retention.mode 须为 fixed_resident 或 idle_retire/1")
        if retention["mode"] == "idle_retire/1":
            if set(retention) != {"mode", "idle_ms"}:
                raise ValueError("idle_retire/1 只接收 mode 与 idle_ms")
            _positive(retention.get("idle_ms"), "retention.idle_ms")
        elif set(retention) != {"mode"}:
            raise ValueError("fixed_resident 不接收其他字段")
        if resource in {"dependency_cost/1", "shared_lru/1"} and retention["mode"] != "idle_retire/1":
            raise ValueError(resource + " 须显式冻结 retention={mode: idle_retire/1, idle_ms: 调参阈值}")
    threshold = value.get("queue_threshold")
    if resource == "queue_threshold/1":
        threshold = _count(threshold, "queue_threshold")
    elif threshold is not None:
        raise ValueError("queue_threshold 只用于 queue_threshold/1")
    review = value.get("review_interval_ms")
    if review is not None:
        _positive(review, "review_interval_ms")

    capacity = copy.deepcopy(value.get("capacity") or {})
    if not isinstance(capacity, dict) or set(capacity) - {"call_slots", "replica_cap", "resources", "demands"}:
        raise ValueError("capacity 仅接收 call_slots、replica_cap、resources、demands")
    capacity["call_slots"] = _count(capacity.get("call_slots"), "capacity.call_slots", 1)
    capacity["replica_cap"] = _count(capacity.get("replica_cap"), "capacity.replica_cap", 2)
    resources, demands = capacity.setdefault("resources", {}), capacity.setdefault("demands", {})
    services = [g for g in groups if g.startswith("service:")]
    if services and not resources:
        raise ValueError("受管服务调度须显式声明完整容量与资源包络需求")
    if not isinstance(resources, dict) or not isinstance(demands, dict):
        raise ValueError("capacity.resources 与 capacity.demands 须为对象")
    for name, quota in resources.items():
        _positive(quota, "资源配额 " + str(name))
    if budget and {key for keys in budget["resource_keys"].values() for key in keys} - set(resources):
        raise ValueError("budget_policy.resource_keys 须对应完整声明的RAM与VRAM容量资源")
    if set(demands) - set(groups):
        raise ValueError("驻留需求只能声明本次 worker 与模型服务分配单元")
    for group_id in groups if resources else []:
        row = demands.get(group_id)
        # Unknown residency demands are not filled with zero.
        if not isinstance(row, dict) or set(row) != set(resources) or any(not _number(v) or v < 0 for v in row.values()):
            raise ValueError("声明资源配额后，每个远程运行组须给出每项资源的实例驻留需求：" + group_id)
    observation = ObservationConfig.from_mapping(value.get("observation"))
    validate_worker_gpu_envelopes(groups, resources, demands)
    for group_id in services:
        spec = deployments.platform.managed_services.get(group_id)
        envelope = spec["resource_envelope"]
        required = {"host_memory_mb": envelope["host_memory_mb"]}
        required.update({"gpu:" + device + ":memory_mb": values["memory_mb"]
                         for device, values in envelope["devices"].items()})
        if set(required) - set(resources):
            raise ValueError("受管服务容量须覆盖冻结包络 host_memory_mb 及 gpu:<device>:memory_mb：" + group_id)
        if any(demands[group_id][name] < amount for name, amount in required.items()):
            raise ValueError("服务驻留需求不得小于冻结资源包络（含加载与并发上界）：" + group_id)
    reclaimable = [g for g in groups if deployments._idle_rebuild_allowed(
        deployments.platform.store.get("runtime_group", g))]
    placement = placements or {u: dict(kind="LOCAL") for u in scene.get("unit_ids", [])}
    if groups and placements is None:
        raise ValueError("联合驻留准入需要实际冻结的 Unit 放置映射")
    if shared_services and online_services is None:
        raise ValueError("联合模式须区分业务模型与离线评分服务，不能把共享角色并集当成业务依赖")
    online_services, scoring_services = list(online_services or []), list(scoring_services or [])
    if (set(online_services) | set(scoring_services)) - set(services):
        raise ValueError("业务与评分角色依赖须来自冻结的受管服务分配")
    unit_services = {group for target in placement.values() for group in target.get("resource_dependencies", [])}
    scoring_only = set(scoring_services) - set(online_services) - unit_services
    initial = value.get("initial_state", "cold")
    if initial not in {"cold", "warm"}:
        raise ValueError("initial_state 须为 cold 或 warm")
    warm_groups = value.get("warm_groups", sorted(set(groups) - scoring_only) if initial == "warm" else [])
    if (not isinstance(warm_groups, list) or len(set(warm_groups)) != len(warm_groups)
            or set(warm_groups) - set(groups) or (initial == "cold" and warm_groups)):
        raise ValueError("warm_groups 必须是暖态初始驻留组的明确子集")
    if set(warm_groups) & scoring_only:
        raise ValueError("纯离线评分服务不能进入业务窗口暖态初始组")
    for name, quota in resources.items():
        if sum(demands[g][name] for g in warm_groups) > quota:
            raise ValueError("暖态初始驻留组超过容量，请选择可容纳的 warm_groups：" + name)
    initial_allocations = {g for g in groups if deployments.live_instances(g)} | set(warm_groups)
    working_sets = freeze_working_sets(scene, [t for t in tasks if t["id"] in task_ids], placement,
        resources, demands, reclaimable, (retention or {}).get("mode"), service_dependencies, online_services,
        offline_allocations=scoring_only, initial_allocations=initial_allocations)
    scalable = [g for g in groups if contracts[g]["replicas"].get("replicable") is True
                and contracts[g]["operations"].get("add_replica") == "SUPPORTED"]
    if resource != "fixed/1" and capacity["replica_cap"] >= 2 and scalable and capacity["call_slots"] < 2:
        raise ValueError("扩缩容实验须按实际资源配置至少两个根调用槽；call_slots=1 仅作串行对照")

    workload = _freeze_workload(value.get("workload"), task_ids, config.get("episodes", len(task_ids)))
    by_id = {task["id"]: task for task in tasks}
    quality_metrics = budget_policy.validate_cohorts(budget, task_ids,
        workload["windows"],
        lambda identity: scene_task_criteria(scene, by_id[identity], criteria))
    if dispatch == "edf/1":
        by_id = {task["id"]: task for task in tasks}
        if any(not _number(by_id[t].get("deadline_ms")) or by_id[t]["deadline_ms"] <= 0 for t in task_ids):
            raise ValueError("edf/1 仅在每个任务确有 deadline_ms 时使用")

    costs = value.get("costs")
    second_costs = isinstance(costs, dict) and costs.get("schema") == "joint-costs/2"
    if budget and not second_costs:
        raise ValueError("多资源预算PPO须使用 joint-costs/2 完整逐维资源积分账本")
    expected = {"schema", "task_seconds", "instance_seconds", "resource_seconds"} if second_costs else {"task_seconds", "instance_seconds"}
    if not isinstance(costs, dict) or set(costs) != expected or any(
            not _number(costs[k]) or costs[k] < 0 for k in ("task_seconds", "instance_seconds")):
        raise ValueError("joint.costs 须显式给出 task_seconds 与 instance_seconds 的非负权重")
    if services and not second_costs:
        raise ValueError("模型服务须显式采用 joint-costs/2 与完整 resource_seconds 权重；旧策略不能沿用")
    if second_costs and (not isinstance(costs["resource_seconds"], dict)
            or set(costs["resource_seconds"]) != set(resources)
            or any(not _number(v) or v < 0 for v in costs["resource_seconds"].values())):
        raise ValueError("joint-costs/2.resource_seconds 须显式给出每项冻结资源的非负积分权重")
    if costs["task_seconds"] > 0:
        specs = [criteria, *[r["criteria"] for r in scene.get("routes", {}).values() if r.get("criteria")]]
        doubled = sorted({d["id"] for spec in specs for d in spec["dimensions"] if d["provider"] == "program"
                   and d.get("binding") == "latency_ms" and spec["objective"]["weights"].get(d["id"], 0) > 0})
        if doubled:
            raise ValueError("已用任务数×时间积分计响应代价，判据不再重复加每任务完整时延：" + ", ".join(doubled))
    dispatch_scope = value.get("dispatch_scope", "roots_and_boundaries")
    if dispatch_scope not in {"roots", "roots_and_boundaries"}:
        raise ValueError("dispatch_scope 须为 roots 或 roots_and_boundaries")
    timeout = _count(value.get("start_timeout_s"), "start_timeout_s", 900)
    frozen = dict(version=MODE, functional_policy=functional, dispatch_policy=dispatch, resource_policy=resource,
        learned=learned, retention=retention, queue_threshold=threshold,
        age_guard_ms=_positive(value.get("age_guard_ms"), "age_guard_ms"),
        max_consecutive_resource_ops=_count(value.get("max_consecutive_resource_ops"), "max_consecutive_resource_ops"),
        review_interval_ms=review, capacity=capacity, workload=workload, costs=dict(costs),
        representation=freeze_joint(value.get("scales"), resources), initial_state=initial, start_timeout_s=timeout,
        working_sets=working_sets, online_services=online_services, scoring_services=scoring_services,
        warm_groups=warm_groups, dispatch_scope=dispatch_scope,
        boundary_slots=_count(value.get("boundary_slots"), "boundary_slots", capacity["call_slots"]),
        observation=dict(asdict(observation), cgroup_paths=[str(p) for p in observation.cgroup_paths]) if observation else None,
        rules=dict(
            decision_order="at each event: business decisions in arrival order, deterministic on-demand starts, "
                           "capacity-pressure releases, resource decisions (at most max_consecutive_resource_ops "
                           "non-yield operations), root dispatch, then checked internal boundary dispatch",
            single_candidate="executed and recorded; not a learning step",
            dispatch="legal roots require a free host and root slot; roots_and_boundaries additionally chooses "
                     "among original checked, enabled internal calls under active roots and boundary_slots; "
                     "root host ownership is retained until return",
            age_guard="age = time since the root or boundary was enabled; the oldest legal aged candidate goes first; "
                      "host, owner, dependency, slot and placement conditions remain mandatory",
            shortest_estimated="historical mean root duration of the same Unit from completed calls earlier in this "
                               "run; if any candidate lacks an estimate the decision falls back to FIFO",
            edf="earliest task arrival + declared deadline_ms; ties FIFO",
            on_demand="a waiting root of a group with no held instance starts one deterministically; prepare/scale "
                      "candidates for that group are withheld while the start is pending",
            scale_in="READY, accepting, no in-flight or prewarm, no active or waiting root that could use it, no "
                     "unfinished task lease, reclaim allowed by the group contract",
            capacity="allowed candidates do not reserve resources; after business selection the complete selected "
                     "region is admitted atomically with pinned, retained-owner claims and actual occupancy; "
                     "without checked stage-release evidence selected closures remain until business task exit, "
                     "not each root return; new resource additions under retained owners require an immediately "
                     "feasible logical reservation, otherwise they are blocked rather than creating hold-and-wait; "
                     "root slots = call_slots; held instances "
                     "(UNDEPLOYED/STARTING/READY/DRAINING/unreleased) and "
                     "starts submitted but not yet registered count toward replica_cap and declared residency quotas; "
                     "the reservation is made before submission and returned on confirmed failure or release",
            outcomes="each resource operation is RUNNING, DONE (READY / prewarmed / released), FAILED (refused, start "
                     "failed or timed out, worker answered not ready) or UNKNOWN (identity or response lost, release "
                     "blocked); FAILED stays in the trajectory with its real cost and the same action on the same "
                     "group state is withheld; UNKNOWN makes the window ineligible; a failed on-demand start fails the "
                     "waiting roots as in functional mode",
            idle="an idle_ms deadline or review point wakes the resource stage",
            stop="a stop request closes the current window at the next event",
            deadline="at horizon_ms each task's status is frozen; a task unfinished then gets unfinished_reward and "
                     "its later quality is recorded, not credited",
            evaluation="freeze passive terminal evidence before owner cleanup; score only after all business "
                       "windows in the collection block and their physical release have completed; scoring "
                       "services hold separate leases and costs; unsafe snapshots retain their owner and block isolation",
            controller="this window is the only replica controller; HPA is not active in the same experiment"))
    if budget:
        frozen["budget_policy"] = budget
        frozen["budget_quality_metrics"] = quality_metrics
    return dict(scheduling_mode=MODE, joint=frozen, lower_policy=dispatch,
        resource_control=dict(mode=MODE, initial_state=initial, start_timeout_s=timeout,
            replicas="cap %d per group, frozen in this run" % capacity["replica_cap"],
            run_window="each window starts from the frozen initial state; instances it started are drained and stopped at its end"),
        feedback_protocol=dict(reward_timing="joint_window/1", local_weights={},
            terminal="freeze task terminal evidence and its open transition; score after the full business block "
                     "and trusted physical release, then backfill that original transition once",
            interval="-(task_seconds * arrived unfinished tasks + instance_seconds * held instances) * seconds between "
                     "events; task time is charged up to horizon_ms when set and measured until every task ends; "
                     "instance time, including drain and release after the last task, is charged up to the common "
                     "end point where every instance of the window is released",
            unfinished="declared unfinished_reward per task whose status at horizon_ms is unfinished"),
        reward_timing="joint_window/1",
        cost_window=dict(task="each task ledger charges its own measured calls, root waits and start waits",
                         window="task-seconds, instance-seconds and residency are window totals (UNALLOCATED)",
                         allocation="NONE"))


def window_tasks(manifest, number):
    return list(manifest["joint"]["workload"]["windows"][number]["task_ids"])


def service_summary(tasks, span_seconds):
    arrived = [t for t in tasks if t["arrived_ms"] is not None]
    completed = [t for t in arrived if t["status"] == "DONE" and t["deadline_status"] != "UNFINISHED"]
    times = sorted(t["response_ms"] for t in completed if t["response_ms"] is not None)
    return dict(arrived=len(arrived), completed=len(completed), failed=sum(t["status"] == "FAILED" for t in arrived),
        unfinished=len(arrived) - len(completed) - sum(t["status"] == "FAILED" for t in arrived),
        completion_rate=len(completed) / len(arrived) if arrived else None,
        throughput_per_s=len(completed) / span_seconds if span_seconds else None,
        response_mean_ms=sum(times) / len(times) if times else None,
        response_p95_ms=times[max(0, math.ceil(.95 * len(times)) - 1)] if times else None,
        response_denominator=len(times), latency_scope="completed within deadline; all arrivals retained separately")


class TaskRun:
    def __init__(self, index, task, context, session, offset_ms):
        self.index, self.task, self.context, self.session, self.offset_ms = index, task, context, session, offset_ms
        self.inbox = queue.Queue()
        self.status, self.event, self.thread = "PENDING", None, None
        self.arrived = self.done = self.ended = self.deadline_status = None
        self.terminal = self.error = self.feedback = self.independent = self.program = None
        self.evaluating, self.evaluation_error, self.decisions, self.roots_done = False, None, [], 0
        self.quality_credit_index = None
        self.pending_action, self.pending_units, self.committed_units = None, [], set()
        self.pending_condition_groups = set()
        self.pending_condition_signatures = None
        self.pending_imports, self.pending_import_ops = [], {}
        self.evaluation_snapshot, self.evaluation_pending, self.evidence_blocked = None, False, False
        self.session_closed = False


class Window:
    def __init__(self, jobs, rid, manifest, catalog, bound, learner, number, tasks, estimates,
                 block_budget_index=None, block_multipliers=None):
        self.jobs, self.platform = jobs, jobs.platform
        self.runtime, self.deployments = jobs.platform.runtime, jobs.platform.deployments
        self.rid, self.manifest, self.config = rid, manifest, manifest["joint"]
        self.catalog, self.bound, self.learner = catalog, bound, learner
        self.number, self.wid = number, rid + ":w" + str(number)
        self.training = manifest["mode"] != "freeze"
        self.budget_policy = self.config.get("budget_policy")
        if self.budget_policy:
            self.budget_index = (budget_policy.budget_index(self.budget_policy, manifest["seed"], number)
                                 if block_budget_index is None else block_budget_index)
            self.block_multipliers = (list(block_multipliers) if block_multipliers is not None else
                [self.budget_policy["initial_multipliers"][name] for name in budget_policy.dimensions(self.budget_policy)])
        self.learned = set(self.config["learned"])
        self.scales = self.config["representation"]["scales"]
        self.cards = manifest["scene"]["representation"]["cards"]
        self.groups = catalog.remote_groups()
        self.service_groups = {g for g in self.groups if g.startswith("service:")}
        self.members = {g: [u for u, p in catalog.placement.items() if p.get("group_id") == g] for g in self.groups}
        self.events = queue.Queue()
        self.estimates = estimates
        if [task["id"] for task in tasks] != window_tasks(manifest, number):
            raise ValueError("窗口执行任务与冻结成员清单不一致")
        offsets = self.config["workload"]["windows"][number]["offsets_ms"]
        self.tasks = [TaskRun(index, task, self.wid + ":" + str(index) + ":" + task["id"], None, offsets[index])
                      for index, task in enumerate(tasks)]
        self.by_context = {task.context: task for task in self.tasks}
        self.trajectory, self.records, self.ops, self.on_demand = [], [], [], []
        self.counters = {}
        self.state_snapshot_ms, self.state_snapshot_calls = 0.0, 0
        self.pending_groups, self.stopping, self.seen_roots = set(), set(), set()
        # Values stay in the window, not in persisted coordinator root metadata.
        self.call_conditions, self.call_conditions_lock = {}, threading.Lock()
        # op id -> (group, held instance ids at submission): a start not yet registered still holds budget.
        self.reserved, self.op_walls, self.withheld, self.idle_seen = {}, {}, {}, set()
        self.unknown_outcomes = []
        self.unattributed = dict(quality=0.0, task_time=0.0, instance_time=0.0, resource_time=0.0, unfinished=0.0)
        self.ledger = dict(task_seconds=0.0, charged_task_seconds=0.0, instance_seconds=0.0, release_tail_seconds=0.0, root_seconds=0.0,
                           unknown_root_seconds=0.0, parallel_seconds=0.0, max_active_roots=0,
                           max_unknown_roots=0, max_held_instances=0, hosts_used=[])
        self.levels = (0, 0, 0, 0)
        self.closing = self.execution_error = None
        self.environment_changes, self.missing_feedback, self.truncated = [], [], []
        self.setup, self.release, self.residency = {}, {}, None
        self.start = self.clock = self.done_at = self.horizon_at = self.end_at = None
        self.resource_due, self.consecutive = True, 0
        self.working = WorkingSets(self.config["working_sets"], self.config["capacity"]["resources"],
                                   self.config["capacity"]["demands"])
        self.observation = None
        self.boundaries_seen = set()
        self.resource_charged = {name: 0.0 for name in self.config["capacity"]["resources"]}
        self.scoring = dict(mode="passive_after_business_release", status="PENDING", services={}, release={},
                            left={}, wall_ms=0.0, resource_envelope=None)

    def _candidate(self, action, scales, **facts):
        return joint_candidate(action, scales, resource_schema=self.config["representation"].get("resources", []), **facts)

    def _charge_resources(self, integral):
        if self.config["costs"].get("schema") != "joint-costs/2" or integral["status"] != "KNOWN":
            return
        current = integral["resource_seconds"]
        charge = sum(self.config["costs"]["resource_seconds"][name] *
                     (value - self.resource_charged.get(name, 0.0)) for name, value in current.items())
        self._credit("resource_time", -charge)
        self.resource_charged = dict(current)

    def _services(self, task):
        return set(self.working.claims.get(task.context, ())) & self.service_groups

    def _services_ready(self, task, views):
        return all(any(i["state"] == "READY" and i["accepting"] and not i["pending_stop"]
                       for i in views[g]["instances"]) for g in self._services(task))

    def _load_history(self, group):
        expected = self.groups[group].get("contract_digest")
        with self.deployments.lock:
            samples = [(i["ready_wall"] - i["start_wall"]) * 1000
                for i in self.deployments.instances.values() if i["group_id"] == group
                # Independent budget windows do not inherit action-dependent
                # histories. Other protocols may use prior windows of this run.
                and i.get("run_id") is not None and i["run_id"] == self.rid
                and (not getattr(self, "budget_policy", None)
                     or i.get("start_wall", 0) >= self.accounting_start)
                and i.get("ready_wall") and i.get("start_wall")
                and not i.get("preparation_failure")
                and (not expected or i.get("spec_digest", i.get("contract_digest")) == expected)]
        return dict(load_ms=sum(samples) / len(samples) if samples else None,
                    load_samples=len(samples), load_uncertainty=1 / math.sqrt(len(samples)) if samples else 1.0)

    def _release_order(self):
        dependencies = self.config["working_sets"].get("dependencies", {})
        visited, ordered = set(), []
        def visit(group):
            if group in visited:
                return
            visited.add(group)
            for dependency in dependencies.get(group, []):
                visit(dependency)
            if group in self.groups:
                ordered.append(group)
        for group in sorted(self.service_groups):
            visit(group)
        return sorted(set(self.groups) - self.service_groups) + list(reversed(ordered))

    def _release_task(self, task, unknown=False):
        task.lease_unknown = getattr(task, "lease_unknown", False) or unknown or task.evidence_blocked
        unknown = task.lease_unknown
        if self._services(task):
            self.platform.managed_services.release_lease(task.context, unknown=unknown)
        if not unknown:
            self.deployments.release_task(task.context)
            self.working.release(task.context)

    # Window lifecycle.

    def run(self, defer_evaluation=False):
        def allocations():
            # Both host kinds have an existing identity-checked sampler. Keep
            # only this window's actual groups; unrelated session workers must
            # not silently enter its measured allocation totals.
            rows = [row for row in self.deployments.sample_resources()
                    if row.get("group_id") in self.groups] if self.groups else []
            if self.service_groups:
                rows.extend(self.platform.managed_services.sample_resources(self.service_groups))
            return rows

        observer = WindowObserver(self.config.get("observation"), run_id=self.wid,
            arm=self.manifest["algorithm"], runtime_dir=self.jobs.directory / "runtime" / self.rid,
            allocation_sampler=allocations if self.groups or self.service_groups else None)
        self.observer = observer
        try:
            observer.start()
            self.accounting_start = time.time()
            observer.mark_phase("preparation")
            self._setup()
            observer.mark_phase("workload")
            self._loop()
        except Exception as error:
            if self.execution_error is None:
                self.execution_error = dict(type=type(error).__name__, message=str(error))
            self._close("execution_error")
            self._drain_after_error()
        finally:
            try:
                self._teardown()
            finally:
                if observer._started is not None:
                    self.observation = observer.finish("FAILED" if self.execution_error else "COMPLETED")
        if not defer_evaluation:
            return self.evaluate_pending()
        return self._item()

    def _setup(self):
        deployments = self.deployments
        for group_id in self.groups:
            if deployments.live_instances(group_id):
                raise ValueError("窗口初态要求运行组没有存活实例：" + group_id)
        with deployments.lock:
            self.changes_seen = len(deployments.environment_changes.get(self.rid, []))
        if self.config["initial_state"] == "warm":
            for group_id in self.config["warm_groups"]:
                placement = self.groups[group_id]
                if not self._operation_allowed(group_id, "prepare"):
                    raise ValueError("暖态启动缺少操作契约资格：" + group_id)
                started = time.perf_counter()
                record = deployments.ensure_ready(placement, None if group_id in self.service_groups else self.members[group_id][0])
                required = self.members[group_id]
                setup = dict(instance_id=record["id"], start_to_ready_ms=(time.perf_counter() - started) * 1000,
                             process_ready=record["state"] == "READY", required_units=required)
                self.setup[group_id] = setup
                if group_id in self.service_groups:
                    setup.update(status="PREPARED", initialization_ready=record["state"] == "READY")
                    continue
                if set(required) - set(record["units"]) or set(required) - set(self._preparable(group_id)):
                    setup["status"] = "FAILED"
                    raise ValueError("暖态准备要求全部所需成员可用且契约允许提前初始化：" + group_id)
                if not self._operation_allowed(group_id, "prewarm", required):
                    raise ValueError("暖态预热缺少操作契约资格：" + group_id)
                started = time.perf_counter()
                setup["prewarm"] = deployments.prewarm(group_id, dict(instance_id=record["id"], units=required), run_id=self.rid)
                setup["prewarm_ms"] = (time.perf_counter() - started) * 1000
                setup["initialization_ready"] = setup["prewarm"].get("initialization_ready") is True
                setup["status"] = "PREPARED" if setup["initialization_ready"] else "FAILED"
                if not setup["initialization_ready"]:
                    raise ValueError("所需成员预热未成功；不进入暖态窗口：" + group_id)
        for task in self.tasks:
            task.session = SceneSession(self.manifest["scene"], self.platform, task.context,
                                        profiles=self.bound, catalog=self.catalog)
            task.session.context.admission = self._admission(task)
        self.runtime.call("configure_roots", dict(run_id=self.wid, call_slots=self.config["capacity"]["call_slots"],
                                                  boundaries=self.config["dispatch_scope"] == "roots_and_boundaries",
                                                  boundary_slots=self.config["boundary_slots"],
                                                  notify=lambda: self.events.put(("root",))))
        self.roots_configured = True
        with deployments.lock:
            deployments.listeners.append(self._on_instance_event)

    def _on_instance_event(self, event):
        if event["group_id"] in self.groups:
            self.events.put(("instance", event["kind"], event["instance_id"]))

    def _admission(self, task):
        @contextlib.contextmanager
        def admit(unit_id):
            if unit_id not in task_unit_ids(self.manifest["scene"], task.task):
                raise ValueError("调用超出当前任务冻结工作集")
            self.catalog.require_operation(unit_id, "dispatch")
            current = task.session.context.call_arguments
            if current is None or current[0] != unit_id:
                raise ValueError("缺少该次原调用的实际参数")
            self.catalog.require_call_conditions(unit_id, current[1], current[2])
            if not self.working.covers(task.context, task.task["id"], [unit_id]):
                raise ValueError("原调用尚未取得完整资源闭包预约")
            placement = self.catalog.placement[unit_id]
            target = dict(kind="LOCAL") if placement["kind"] == "LOCAL" else dict(
                kind="REMOTE", group_id=placement["group_id"],
                instance_id=self.deployments.bound(task.context, placement["group_id"]))
            with self.call_conditions_lock:
                self.call_conditions[task.context] = current
                try:
                    identity = self.runtime.call("enable_root", task.context, unit_id, target)
                except BaseException:
                    self.call_conditions.pop(task.context, None)
                    raise
            try:
                grant = self.runtime.call("wait_root", identity)
                receipt = "RETURN"
                try:
                    yield dict(grant)
                except BaseException as error:
                    receipt = type(error).__name__
                    raise
                finally:
                    self.runtime.call("finish_root", identity, receipt)
            finally:
                with self.call_conditions_lock:
                    self.call_conditions.pop(task.context, None)
        return admit

    def _require_root_conditions(self, root, host=None):
        with self.call_conditions_lock:
            current = self.call_conditions.get(root["task_id"])
        if current is None or current[0] != root["unit_id"]:
            raise ValueError("缺少该次原调用的实际参数")
        result = self.catalog.require_call_conditions(current[0], current[1], current[2])
        if host is not None and root["target"]["kind"] == "REMOTE" and result["status"] != "NOT_REQUIRED":
            with self.deployments.lock:
                records = [item for item in self.deployments._instances(root["target"]["group_id"])
                           if item["id"] == host and item["state"] == "READY"]
                if len(records) != 1:
                    raise ValueError("目标实例身份已改变")
                identity = self.deployments._identity(records[0])
            self.catalog.require_call_conditions(current[0], current[1], current[2], instance_identity=identity)

    def _task_body(self, task):
        try:
            with self.runtime.context(task.context):
                event = task.session.reset(task.task, self.manifest["seed"] + self.number * len(self.tasks) + task.index)
            while True:
                self.events.put(("task_event", task, event, time.time()))
                if event["kind"] == "terminal":
                    return
                index = task.inbox.get()
                if index is None:
                    self.events.put(("task_abandoned", task, time.time()))
                    return
                if isinstance(index, dict) and index.get("condition_rejected"):
                    raise ValueError("已选动作的逐次调用条件未能确认或已失效")
                with self.runtime.context(task.context):
                    event = task.session.step(index)
        except Exception as error:
            self.events.put(("task_error", task, error, time.time()))

    def _loop(self):
        self.start = self.clock = time.time()
        workload = self.config["workload"]
        arrivals = [(self.start + task.offset_ms / 1000, task) for task in self.tasks]
        horizon = self.start + workload["horizon_ms"] / 1000 if workload.get("horizon_ms") is not None else None
        self.horizon_at = horizon
        review = self.config["review_interval_ms"]
        next_review = self.start + review / 1000 if review else None
        self._refresh()
        while True:
            # A closed window drains existing work; expired timers must not cause a busy loop.
            deadlines = [] if self.closing else [at for at, _ in arrivals] + [
                d for d in (next_review, horizon) if d is not None]
            deadlines += [d for d in list(self._idle_deadlines().values()) + list(self._start_deadlines().values())
                          if d > time.time()]
            timeout = max(0.0, min([d - time.time() for d in deadlines] + [1.0]))
            batch = []
            try:
                batch.append(self.events.get(timeout=timeout))
                while True:
                    batch.append(self.events.get_nowait())
            except queue.Empty:
                pass
            now = time.time()
            self._advance(now)
            # Arrival belongs to the frozen workload clock, even if this event
            # loop wakes after the horizon. Never lose queued tasks from counts.
            due = min(now, horizon) if horizon is not None else now
            while arrivals and arrivals[0][0] <= due and not self.closing:
                at, task = arrivals.pop(0)
                task.arrived, task.status = at, "WAITING_CAPACITY"
                self.resource_due = True
            if not self.closing and (self.jobs.stops[self.rid].is_set() or self.jobs.closed):
                self._close("stopped")
            if horizon is not None and now >= horizon and not self.closing:
                self._close("horizon")
            if self.closing:
                arrivals.clear()
            for event in batch:
                self._handle(event)
            self._charge_task_time(now)
            if next_review is not None and now >= next_review:
                next_review += review / 1000
                self.resource_due = True
            idle = [key for key, at in self._idle_deadlines().items() if at <= now and key not in self.idle_seen]
            if idle:
                self.idle_seen.update(idle)
                self.resource_due = True
            self._track_ops(now)
            if not self.closing and (self.jobs.stops[self.rid].is_set() or self.jobs.closed):
                self._close("stopped")
            with self.deployments.lock:
                changes = list(self.deployments.environment_changes.get(self.rid, []))
            if len(changes) > self.changes_seen:
                self.environment_changes.extend(copy.deepcopy(changes[self.changes_seen:]))
                self.changes_seen = len(changes)
                self._close("environment_changed")
            if horizon is not None and now >= horizon and not self.closing:
                self._close("horizon")
            if self.done_at is None and all(task.status in TERMINAL for task in self.tasks) and not arrivals:
                self.done_at = now
            # Evaluation and release continue in teardown; instance time keeps accruing until release ends.
            if self.done_at is not None and not self._submitting():
                return
            if self.closing:
                self._cancel_waiting("窗口已收尾：" + self.closing)
                self._boundary_stage(draining=True)
            elif self.done_at is None:
                self._admit_tasks()
                self._decide()
            self._refresh()

    def _handle(self, event):
        kind = event[0]
        if kind == "task_event":
            _, task, scene_event, at = event
            if scene_event["kind"] == "terminal":
                self._complete(task, scene_event, at)
            elif self.closing:
                task.inbox.put(None)
                task.status = "ABANDONING"
            else:
                task.event, task.status, task.event_at = scene_event, "DECISION", at
        elif kind == "task_abandoned":
            event[1].status = "UNFINISHED"
            event[1].ended = event[2]
            self._capture_failure(event[1], reason="unfinished")
            self._finish_business_owner(event[1])
            self._release_task(event[1])
        elif kind == "task_error":
            _, task, error, at = event
            task.ended = at
            task.error = dict(type=type(error).__name__, message=str(error))
            if self.closing:
                task.status = "UNFINISHED"
            else:
                task.status = "FAILED"
                self._fail(dict(type=type(error).__name__, message=str(error), task_id=task.task["id"]))
            self._capture_failure(task)
            self._finish_business_owner(task)
            self._release_task(task, unknown=type(error).__name__ in {"TimeoutError", "ConnectionError"})
        elif kind == "root":
            self.resource_due = True
        elif kind == "instance":
            if event[1] in {"registered", "stopped", "stop_blocked", "start_failed", "unknown", "process_replaced",
                            "apply", "prewarm"}:
                self.resource_due = True
        elif kind == "op_submitted":
            _, op, outcome = event
            if outcome.get("preparation_failure"):
                self._fail(dict(type="PreparationFailed", message=outcome["preparation_failure"]))
            self.pending_groups.discard(op["group_id"])
            self.reserved.pop(op["id"], None)
            self.resource_due = True
            if op["status"] != "RUNNING":
                op["late_submission"] = outcome
                return
            status, reason = outcome.pop("status", None), outcome.pop("reason", None)
            op.update(outcome)
            if status is not None:
                self._settle(op, status, reason)
            self._track_ops(time.time())
        elif kind == "cleanup":
            _, op, result = event
            op["cleanup"] = result
            self.resource_due = True
        elif kind == "evaluated":
            _, task, feedback, independent, index, error = event
            task.evaluating, task.feedback, task.independent = False, feedback, independent
            task.quality_credit_index = index
            self.resource_due = True
            # Quality after the horizon is recorded, not credited: unfinished_reward already settled that task.
            late = task.deadline_status == "UNFINISHED"
            if error is not None:
                task.evaluation_error = error
                if not late:
                    self.missing_feedback.append(task.task["id"])
            elif feedback["eligible"] and feedback["reward"] is not None:
                if not late:
                    self._credit("quality", feedback["reward"], index)
            elif not late:
                self.missing_feedback.append(task.task["id"])

    def _capture_failure(self, task, reason="execution_error"):
        if task.session is None or task.evaluation_snapshot is not None or task.evidence_blocked:
            return
        try:
            task.evaluation_snapshot = task.session.context.freeze_failure_evidence(
                criteria=[scene_task_criteria(self.manifest["scene"], task.task, self.manifest[scope], scope)
                          for scope in ("criteria", "evaluation_criteria")],
                destination=self.jobs.directory / "runtime" / self.rid / "evaluation-evidence" /
                            ("window-" + str(self.number)) / ("task-" + str(task.index)),
                interrupted_event=task.event, reason=reason)
            task.terminal = task.evaluation_snapshot["terminal"]
        except PassiveEvidenceGap:
            task.evidence_blocked = True
            task.session.context.evidence_blocked = True
            task.active_evaluation_terminal = task.session.context.active_failure_terminal
            task.evaluation_error = task.session.context.evaluation_error
            task.terminal = dict(kind="terminal", terminated=False, truncated=True,
                reason="passive_evaluation_evidence_unavailable", evidence={})
            self.scoring.update(status="BLOCKED", mode="owner_dependent_research_loop")

    def _complete(self, task, event, at):
        # Use the completion timestamp, not the order in which the queue is drained.
        # A terminal event can cross the horizon while the decision thread is busy.
        if self.horizon_at is not None:
            if at > self.horizon_at:
                task.deadline_status = "UNFINISHED"
            elif self.closing == "horizon":
                task.deadline_status = "DONE"
        task.status, task.done, task.ended, task.terminal = "DONE", at, at, event
        if event.get("truncated") and task.deadline_status != "UNFINISHED":
            self.truncated.append(task.task["id"])
        task.quality_credit_index = len(self.trajectory) - 1 if self.trajectory else None
        try:
            criteria = [scene_task_criteria(self.manifest["scene"], task.task, self.manifest["criteria"]),
                        scene_task_criteria(self.manifest["scene"], task.task,
                            self.manifest["evaluation_criteria"], "evaluation_criteria")]
            task.evaluation_snapshot = passive_evaluation_snapshot(event, task.session.context.calls,
                task.session.context.model_calls, criteria=criteria,
                destination=self.jobs.directory / "runtime" / self.rid / "evaluation-evidence" /
                            ("window-" + str(self.number)) / ("task-" + str(task.index)))
            task.terminal = task.evaluation_snapshot["terminal"]
            calls = task.evaluation_snapshot["calls"]
            task.program = dict(task.terminal.get("program_metrics", {}), latency_ms=(task.done - task.arrived) * 1000,
                module_calls=len(calls), module_duration_ms=sum(c["duration_ms"] for c in calls),
                root_wait_ms=sum((c.get("root") or {}).get("wait_ms") or 0 for c in calls))
            task.evaluation_pending = True
        except PassiveEvidenceGap as error:
            # Keep the original object with its live owner in memory. Only an
            # explicit gap record crosses the persisted passive boundary.
            task.active_evaluation_terminal = event
            task.terminal = dict(kind="terminal", terminated=True, truncated=False, response=None,
                                 evidence={}, reason="passive_evaluation_evidence_unavailable")
            task.evidence_blocked = True
            task.session.context.evidence_blocked = True
            task.evaluation_error = dict(type="PassiveEvidenceGap", message=str(error))
            self.missing_feedback.append(task.task["id"])
            self.scoring.update(status="BLOCKED", mode="owner_dependent_research_loop")
        self._finish_business_owner(task)
        self._release_task(task)
        self.resource_due = True

    def _finish_business_owner(self, task):
        if task.evidence_blocked or task.session_closed or task.session is None:
            return
        if task.thread is not None and task.thread is not threading.current_thread():
            task.thread.join(30)
            if task.thread.is_alive():
                task.evidence_blocked = True
                task.error = dict(type="ThreadStillRunning", message="业务线程尚未退出，保留原owner租约")
                self._fail(task.error)
                return
        try:
            task.session.close()
            task.session_closed = True
        except Exception as error:
            task.evidence_blocked = True
            task.error = task.error or dict(type=type(error).__name__, message="场景关闭失败：" + str(error))
            self._fail(task.error)

    def _evaluate(self, task, index):
        from seampact.scheduling.training import task_criteria
        with self.runtime.context(task.context + ":evaluation"):
            try:
                verify_passive_evaluation(task.evaluation_snapshot)
                event, calls = task.evaluation_snapshot["terminal"], task.evaluation_snapshot["calls"]
                program = task.program
                applicability = route_metric_applicability(self.manifest["scene"], task.task, event)
                called = {c["unit_id"] for c in calls}
                criterion = task_criteria(scene_task_criteria(self.manifest["scene"], task.task,
                    self.manifest["criteria"]), applicability, called)
                final = task_criteria(scene_task_criteria(self.manifest["scene"], task.task,
                    self.manifest["evaluation_criteria"], "evaluation_criteria"), applicability, called)
                evidence = event.get("evidence", {})
                feedback = evaluate(self.bound, criterion, evidence, program)
                final_program = dict(program)
                final_program.update(event.get("evaluation_metrics", {}))
                final_program.update({key: program[key] for key in ("latency_ms", "module_calls", "module_duration_ms")})
                independent = evaluate(self.bound, final, evidence, final_program) if final else None
                self.events.put(("evaluated", task, feedback, independent, index, None))
            except Exception as error:
                self.events.put(("evaluated", task, None, None, index, dict(type=type(error).__name__, message=str(error))))

    def _fail(self, error):
        if self.execution_error is None:
            self.execution_error = error
        self._close("execution_error")

    def _close(self, reason):
        if self.closing:
            return
        self.closing = reason
        if reason == "horizon":
            for task in self.tasks:
                task.deadline_status = ("DONE" if task.done is not None and task.done <= self.horizon_at else
                    "NOT_ARRIVED" if task.status == "PENDING" and
                    self.start + task.offset_ms / 1000 > self.horizon_at else "UNFINISHED")
        for task in self.tasks:
            if task.status == "PENDING":
                task.status = "NOT_ARRIVED"
            elif task.status in {"WAITING_CAPACITY", "WAITING_RESOURCES", "WAITING_CALL_CAPACITY", "WAITING_CALL_RESOURCES"}:
                if task.thread is not None:
                    task.inbox.put(None)
                task.status = "UNFINISHED"
                task.ended = self.horizon_at if reason == "horizon" else time.time()
            elif task.status == "DECISION":
                task.inbox.put(None)
                task.status = "ABANDONING"
        if getattr(self, "roots_configured", False):
            self._cancel_waiting("窗口已收尾：" + reason)

    def _drain_after_error(self):
        # Paid calls in flight are not preempted; wait for their tasks before release.
        deadline = time.time() + self.config["start_timeout_s"]
        while time.time() < deadline and (any(task.status not in TERMINAL for task in self.tasks) or self._submitting()):
            try:
                self._handle(self.events.get(timeout=1.0))
            except queue.Empty:
                pass
            if getattr(self, "roots_configured", False):
                self._cancel_waiting("窗口已收尾：" + self.closing)
                self._boundary_stage(draining=True)
        if self.done_at is None:
            self.done_at = time.time()

    def _teardown(self):
        deployments = self.deployments
        self.observer.mark_phase("release")
        with deployments.lock:
            if self._on_instance_event in deployments.listeners:
                deployments.listeners.remove(self._on_instance_event)
        if getattr(self, "roots_configured", False):
            # Stop fresh permits before joining owners. The coordinator retains
            # the closed window's limits until its possible work has retired.
            self.runtime.call("configure_roots", None)
        for task in self.tasks:
            if task.thread is not None:
                task.thread.join(30)
                if task.thread.is_alive():
                    task.error = task.error or dict(type="ThreadStillRunning", message="任务线程未在收尾时限内结束")
                    self.execution_error = self.execution_error or task.error
                    task.evidence_blocked = True
                    task.session.context.evidence_blocked = True
                    # A task that can still execute retains its owner lease.
                    continue
            if task.status != "DONE":
                self._capture_failure(task, reason="unfinished" if task.status == "UNFINISHED" else "execution_error")
            self._finish_business_owner(task)
            self._release_task(task)
        end = self.done_at or time.time()
        if self.start is not None:
            self._track_ops(time.time())
        for op in self.ops:
            if op["status"] == "RUNNING" and op["phase"] == "awaiting" and op["action"] != "scale_in":
                # The window ends before this start resolved; release stops it, which is not a start failure.
                self._settle(op, "CANCELLED", "窗口收尾时撤回未就绪的启动")
        released_at = time.time()
        retained = any(task.evidence_blocked for task in self.tasks)
        if retained:
            # Keep the Window itself: it owns sessions, their raw evidence and
            # resource claims. Persist only the explicit UNKNOWN release state.
            if not hasattr(self.jobs, "retained_owners"):
                self.jobs.retained_owners = {}
            self.jobs.retained_owners.setdefault(self.rid, []).append(self)
        for group_id in self._release_order():
            if retained:
                self.release[group_id] = dict(status="UNKNOWN", reason="passive_evaluation_evidence_unavailable")
                continue
            try:
                self.release[group_id] = deployments.release_run(group_id, self.rid)
            except Exception as error:
                self.release[group_id] = dict(error=str(error))
        deadline = time.time() + self.config["start_timeout_s"]
        while not retained and time.time() < deadline:
            self._track_ops(time.time())
            held = [item for g in self.groups for item in deployments.live_instances(g)]
            # A blocked stop needs operator confirmation; waiting longer does not release it.
            if not held or all((item.get("pending_stop") or {}).get("blocked") for item in held):
                break
            time.sleep(0.5)
        for op in self.ops:
            if op["status"] == "RUNNING":
                self._settle(op, "UNKNOWN", "收尾时限内未确认释放")
        left = {g: [item["id"] for item in deployments.live_instances(g)] for g in self.groups}
        self.end_at = time.time()
        self.release_ms = (self.end_at - released_at) * 1000
        self.release_left = {g: ids for g, ids in left.items() if ids}
        self.observer.mark_phase("release_unconfirmed" if self.release_left else "resources_released")
        if self.start is not None and self.groups:
            # Occupancy after the last event, drain and release included, up to the common end point.
            tail = deployments.residency(set(self.groups), self.clock, self.end_at)["resident_ms"] / 1000
            self.ledger["instance_seconds"] += tail
            self.ledger["release_tail_seconds"] = tail
            self._credit("instance_time", -self.config["costs"]["instance_seconds"] * tail)
            self.clock = self.end_at
            self.residency = deployments.residency(set(self.groups), self.start, self.end_at)
        complete = deployments.residency(set(self.groups), getattr(self, "accounting_start", self.start) or self.end_at,
                                         self.end_at)
        self.envelope = envelope_integral(complete, self.config["capacity"]["demands"],
                                         self.config["capacity"]["resources"])
        if self.config["costs"].get("schema") == "joint-costs/2":
            # Include warm-state loading and asynchronous lifecycle changes from
            # the physical ledger rather than the event-loop sample count.
            correction = complete["resident_ms"] / 1000 - self.ledger["instance_seconds"]
            self.ledger["instance_seconds"] += correction
            self._credit("instance_time", -self.config["costs"]["instance_seconds"] * correction)
            if self.envelope["status"] != "KNOWN":
                self.unknown_outcomes.append("resource_envelope_unknown")
            else:
                self._charge_resources(self.envelope)
        self.window_end = end

    def _scoring_groups(self, task):
        groups = set()
        for scope in ("criteria", "evaluation_criteria"):
            criterion = scene_task_criteria(self.manifest["scene"], task.task, self.manifest[scope], scope)
            if not criterion or not any(d["provider"] == "teacher" for d in criterion["dimensions"]):
                continue
            profile = self.bound.role_profile(criterion["teacher"]["role"])
            if profile.get("resource_kind") == "managed":
                groups.add("service:" + profile["managed_service_id"])
        result = dependency_closure(groups, self.config["working_sets"]["dependencies"])
        if result - set(self.config.get("scoring_services", [])):
            raise ValueError("离线评分服务不在冻结评分闭包内")
        for name, quota in self.config["capacity"]["resources"].items():
            if sum(self.config["capacity"]["demands"][g][name] for g in result) > quota:
                raise ValueError("离线评分完整服务闭包超过容量：" + name)
        return result

    def evaluate_pending(self):
        """Score frozen evidence only after the caller's full business block ends.

        Collection code must finish every deferred window before entering here.
        The local check additionally refuses any actual held business instance,
        retained owner or unresolved start, including another deferred window.
        """
        pending = [task for task in self.tasks if task.evaluation_pending]
        if not pending:
            if self.scoring["status"] == "PENDING":
                self.scoring["status"] = "COMPLETED"
            return self._item()
        held = {g: [item["id"] for item in self.deployments.live_instances(g)] for g in self.groups}
        if (self.end_at is None or any(held.values()) or self.working.claims
                or any(task.evidence_blocked for task in self.tasks)):
            self.scoring.update(status="BLOCKED", reason="business_release_unconfirmed")
            for task in pending:
                if task.task["id"] not in self.missing_feedback:
                    self.missing_feedback.append(task.task["id"])
            return self._item()
        started, used_groups = time.time(), set()
        self.scoring.update(status="RUNNING", started_after_business_release=True)
        for task in pending:
            groups, owner = set(), task.context + ":evaluation"
            try:
                groups = self._scoring_groups(task)
                used_groups.update(groups)
                for group in self._release_order()[::-1]:
                    if group not in groups:
                        continue
                    placement = self.catalog.service_placements[group]
                    if not self._operation_allowed(group, "prepare"):
                        raise ValueError("离线评分服务缺少准备契约资格：" + group)
                    record = self.deployments.ensure_ready(placement, None)
                    self.scoring["services"][group] = dict(instance_id=record["id"], state=record["state"])
                    if record["state"] != "READY":
                        raise ValueError("离线评分服务未就绪：" + group)
                for group in groups:
                    self.platform.managed_services.acquire(group, owner, run_id=self.rid)
                task.evaluating = True
                self._evaluate(task, task.quality_credit_index)
                # _evaluate is synchronous and places exactly one feedback event.
                while task.evaluating:
                    self._handle(self.events.get(timeout=1.0))
                task.evaluation_pending = False
            except Exception as error:
                task.evaluating, task.evaluation_pending = False, False
                task.evaluation_error = dict(type=type(error).__name__, message=str(error))
                if task.task["id"] not in self.missing_feedback:
                    self.missing_feedback.append(task.task["id"])
                self.scoring.update(status="BLOCKED", reason="scoring_execution_failed")
            finally:
                if groups:
                    self.platform.managed_services.release_lease(owner)
                for group in self._release_order():
                    if group not in groups:
                        continue
                    try:
                        self.scoring["release"][group] = self.deployments.release_run(group, self.rid)
                    except Exception as error:
                        self.scoring["release"][group] = dict(error=str(error))
                deadline = time.time() + self.config["start_timeout_s"]
                while groups and time.time() < deadline:
                    left = [item for group in groups for item in self.deployments.live_instances(group)]
                    if not left or all((item.get("pending_stop") or {}).get("blocked") for item in left):
                        break
                    time.sleep(0.5)
                left = {g: [item["id"] for item in self.deployments.live_instances(g)] for g in groups}
                self.scoring["left"].update({g: ids for g, ids in left.items() if ids})
            if self.scoring["left"]:
                self.scoring.update(status="BLOCKED", reason="scoring_release_unconfirmed")
                break
        ended = time.time()
        self.scoring["wall_ms"] += (ended - started) * 1000
        self.scoring["resource_envelope"] = envelope_integral(
            self.deployments.residency(used_groups, started, ended), self.config["capacity"]["demands"],
            self.config["capacity"]["resources"])
        if self.scoring["status"] == "RUNNING":
            self.scoring["status"] = "COMPLETED"
        return self._item()

    # Observable state.

    def _state(self):
        started = time.perf_counter()
        views = self.deployments.group_views({g: p.get("image") for g, p in self.groups.items()})
        roots = self.runtime.call("root_view", self.wid)
        self.state_snapshot_ms += (time.perf_counter() - started) * 1000
        self.state_snapshot_calls += 1
        return views, roots

    def _refresh(self):
        images = getattr(self.platform, "images", None)
        scope_id = (self.platform.store.get("scheduler_run", self.rid) or {}).get("retention_scope_id")
        failure = images.get(scope_id).get("failure") if images and scope_id else None
        if failure:
            self._fail(dict(type="PreparationFailed", message=failure["reason"]))
        views, roots = self._state()
        # Parallelism counts roots known to be executing; UNKNOWN roots still hold a slot but are reported apart.
        active = [r for r in roots if r["state"] == "ACTIVE"]
        unknown = sum(r["state"] == "UNKNOWN" for r in roots)
        held = sum(len(view["instances"]) for view in views.values())
        tasks = sum(task.status not in TERMINAL and task.status != "PENDING" for task in self.tasks)
        self.levels = (tasks, held, len(active), unknown)
        self.ledger["max_active_roots"] = max(self.ledger["max_active_roots"], len(active))
        self.ledger["max_unknown_roots"] = max(self.ledger["max_unknown_roots"], unknown)
        self.ledger["max_held_instances"] = max(self.ledger["max_held_instances"], held)
        hosts = set(self.ledger["hosts_used"]) | {r["host"] for r in active}
        self.ledger["hosts_used"] = sorted(hosts)
        for r in roots:
            if r["state"] in {"RETIRED", "UNKNOWN"} and r["id"] not in self.seen_roots:
                self.seen_roots.add(r["id"])
                task = self.by_context.get(r["task_id"])
                if task:
                    task.roots_done += 1
                if r["state"] == "RETIRED" and r["receipt"] == "RETURN":
                    self.estimates.setdefault(r["unit_id"], []).append(r["duration_ms"])
        for call in self.runtime.call("retired_boundaries", self.wid, self.boundaries_seen):
            if (call.get("task_id") in self.by_context and call.get("root_id")
                    and call["physical"] == "RETIRED" and call["id"] not in self.boundaries_seen):
                self.boundaries_seen.add(call["id"])
                if call["receipt"] == "RETURN" and call.get("started_at") is not None:
                    elapsed = max(0, call["latency_ms"] - (call["started_at"] - call["enabled_at"]) * 1000)
                    self.estimates.setdefault("boundary:" + str(call["semantic_key"]), []).append(elapsed)
        self.views = views

    def _advance(self, now):
        self._charge_task_time(now)
        span = now - self.clock
        if span <= 0:
            return
        _, held, roots, unknown = self.levels
        self.ledger["instance_seconds"] += held * span
        self.ledger["root_seconds"] += roots * span
        self.ledger["unknown_root_seconds"] += unknown * span
        if roots >= 2:
            self.ledger["parallel_seconds"] += span
        weights = self.config["costs"]
        self._credit("instance_time", -weights["instance_seconds"] * held * span)
        if weights.get("schema") == "joint-costs/2":
            lifetime = self.deployments.residency(set(self.groups), self.accounting_start, now)
            self._charge_resources(envelope_integral(lifetime, self.config["capacity"]["demands"],
                                                     self.config["capacity"]["resources"]))
        self.clock = now

    def _charge_task_time(self, now):
        # Queue handling can lag arrival/completion. Reconcile from event endpoints,
        # including negative corrections for a completion learned after an advance.
        # With gamma=1 the differences telescope to the same window return.
        measured = charged = 0.0
        for task in self.tasks:
            if task.arrived is None:
                continue
            endpoint = task.done if task.done is not None else task.ended
            end = now if endpoint is None else min(now, endpoint)
            measured += max(0.0, end - task.arrived)
            charged_end = end if self.horizon_at is None else min(end, self.horizon_at)
            charged += max(0.0, charged_end - task.arrived)
        delta = charged - self.ledger["charged_task_seconds"]
        self.ledger["task_seconds"] = measured
        self.ledger["charged_task_seconds"] = charged
        self._credit("task_time", -self.config["costs"]["task_seconds"] * delta)

    def _credit(self, component, value, index=-1):
        if not value:
            return
        if index is None or not self.trajectory:
            self.unattributed[component] += value
            return
        stored = self.trajectory[index]
        stored["transition"]["reward"] += value
        stored["components"][component] += value

    def _idle_deadlines(self):
        """(instance, last activity) -> the time its idle_ms elapses; each is due once."""
        retention = self.config.get("retention") or {}
        if retention.get("mode") != "idle_retire/1":
            return {}
        result = {}
        for group, view in getattr(self, "views", {}).items():
            if not view["reclaim"]:
                continue
            for item in view["instances"]:
                if (item["state"] == "READY" and not item["in_flight"] and item.get("last_activity_wall")
                        and item["id"] not in self.stopping):
                    loading = (self._load_history(group)["load_ms"] or 0) if self.config["resource_policy"] == "dependency_cost/1" else 0
                    result[(item["id"], item["last_activity_wall"])] = item["last_activity_wall"] + (retention["idle_ms"] + loading) / 1000
        return result

    def _start_deadlines(self):
        limit = self.config["start_timeout_s"]
        return {item["id"]: item["start_wall"] + limit for view in getattr(self, "views", {}).values()
                for item in view["instances"] if item["state"] in {"UNDEPLOYED", "STARTING"} and item.get("start_wall")}

    def _submitting(self):
        return any(op["status"] == "RUNNING" and op["phase"] == "submitting" for op in self.ops)

    def _occupancy(self, views):
        """Held instances plus submitted starts whose instance is not registered yet."""
        result = {g: len(view["instances"]) for g, view in views.items()}
        for group_id, before in self.reserved.values():
            if not {item["id"] for item in views[group_id]["instances"]} - before:
                result[group_id] += 1
        return result

    def _workload(self, views, roots, now):
        waiting = [r for r in roots if r["state"] == "ENABLED"]
        active = [r for r in roots if r["state"] in HELD]
        busy = {r["host"] for r in active}
        instances = [item for view in views.values() for item in view["instances"]]
        resources = self.config["capacity"]["resources"]
        remaining = None
        if resources:
            used = self._resource_use(views)
            remaining = min((quota - used[name]) / quota for name, quota in resources.items())
        cap = self.config["capacity"]["replica_cap"]
        occupancy = self._occupancy(views)
        reserved = sum(occupancy.values()) - len(instances)
        return dict(tasks_active=sum(t.status not in TERMINAL and t.status != "PENDING" for t in self.tasks),
            tasks_at_decision=sum(t.status == "DECISION" for t in self.tasks),
            tasks_done=sum(t.status == "DONE" for t in self.tasks), roots_waiting=len(waiting),
            roots_dispatchable=len(self._dispatchable(views, roots)), roots_active=len(active),
            slots_free=self.config["capacity"]["call_slots"] - len(active),
            oldest_wait_ms=max(((now - r["enabled_at"]) * 1000 for r in waiting), default=0.0),
            instances_held=len(instances) + reserved, instances_ready=sum(i["state"] == "READY" for i in instances),
            instances_starting=sum(i["state"] in {"UNDEPLOYED", "STARTING"} for i in instances) + reserved,
            instances_draining=sum(i["state"] == "DRAINING" for i in instances),
            instances_idle=sum(i["state"] == "READY" and not i["in_flight"] and i["id"] not in busy for i in instances),
            replicas_remaining=sum(cap - occupancy[g] for g in views),
            resource_remaining=remaining,
            resource_ops_in_row=self.consecutive / self.config["max_consecutive_resource_ops"],
            window_elapsed_ms=(now - self.start) * 1000)

    def _resource_use(self, views):
        return self.working.used(self._occupancy(views))

    def _budget_after(self, group_id, views):
        resources = self.config["capacity"]["resources"]
        if not resources:
            return None
        return self.working.after_start(group_id, self._occupancy(views))

    def _admit_tasks(self):
        # Waiting time starts at the supplied arrival, including capacity queueing.
        # No future task or task label is exposed to the learned policy here.
        for task in sorted((t for t in self.tasks if t.status == "WAITING_CAPACITY"),
                           key=lambda t: (t.arrived, t.index)):
            views, _ = self._state()
            needed = self.working.proposed(task.context, task.task["id"]) & self.service_groups
            if any(any(i.get("pending_stop") for i in views[g]["instances"]) for g in needed):
                continue
            if not self.working.admit(task.context, task.task["id"], self._occupancy(views)):
                break
            task.status = "WAITING_RESOURCES"
            task.admitted_at = time.time()
            for group in self._services(task):
                self.platform.managed_services.acquire(group, task.context, run_id=self.rid)
        views, _ = self._state()
        for task in self.tasks:
            if task.status == "WAITING_CALL_CAPACITY":
                candidate = task.event["candidates"][task.pending_action]
                admissible, condition_groups, _ = self._candidate_intent(task, candidate)
                signatures = getattr(task, "pending_condition_signatures", None)
                if admissible and signatures is not None:
                    current = task.session.candidate_intent(candidate)
                    admissible = current["allowed"] and current["signatures"] == signatures
                if not admissible:
                    # Fail the selected action before reservation. Do not send
                    # its index: readiness could change again before the task
                    # thread receives it, bypassing the resource admission.
                    task.status = "RUNNING"
                    task.inbox.put(dict(condition_rejected=True))
                    task.pending_action, task.pending_units = None, []
                    continue
                needed = self.working.proposed(task.context, task.task["id"], task.pending_units) & self.service_groups
                if any(any(i.get("pending_stop") for i in views[g]["instances"]) for g in needed):
                    continue
                if not self.working.admit(task.context, task.task["id"], self._occupancy(views), task.pending_units):
                    continue
                task.committed_units.update(task.pending_units)
                task.pending_condition_groups = condition_groups
                task.pending_imports, task.pending_import_ops = [], {}
                task.status = "WAITING_CALL_RESOURCES"
                for group in self._services(task):
                    self.platform.managed_services.acquire(group, task.context, run_id=self.rid)
            if task.status not in {"WAITING_RESOURCES", "WAITING_CALL_RESOURCES"} or not self._services_ready(task, views):
                continue
            if task.status == "WAITING_CALL_RESOURCES":
                groups = getattr(task, "pending_condition_groups", set())
                if any(not any(i["state"] == "READY" for i in views[g]["instances"]) for g in groups):
                    continue
                candidate = task.event["candidates"][task.pending_action]
                signatures = getattr(task, "pending_condition_signatures", None)
                current = task.session.candidate_intent(candidate) if signatures is not None else None
                legal_now = task.session.candidate_legal(candidate)
                consistent = (current is None or (current["allowed"] and not current["groups"] and
                                                    current["signatures"] == signatures))
                imports = current.get("imports", []) if current is not None else []
                if not legal_now and consistent and imports:
                    attempted = getattr(task, "pending_import_ops", {})
                    keys = [(item["unit"], item["instance_identity"]["instance_id"], item["plan_digest"])
                            for item in imports]
                    if any(key in attempted and attempted[key]["status"] != "RUNNING" for key in keys):
                        consistent = False
                    else:
                        task.pending_imports = imports
                        continue
                if not legal_now or not consistent:
                    task.status = "RUNNING"
                    task.inbox.put(dict(condition_rejected=True))
                    task.pending_action, task.pending_units = None, []
                    task.pending_condition_groups = set()
                    task.pending_condition_signatures = None
                    task.pending_imports = []
                    continue
                task.status = "RUNNING"
                task.inbox.put(task.pending_action)
                task.pending_action, task.pending_units = None, []
                task.pending_condition_groups = set()
                task.pending_condition_signatures = None
                task.pending_imports = []
            else:
                task.status = "STARTING"
                task.thread = threading.Thread(target=self._task_body, args=(task,), daemon=True,
                                               name="seampact-joint-task")
                task.thread.start()

    def _pressure_release(self):
        # Claims can replace idle residency only after the backend confirms
        # release. Same rule for every policy, never kill an active owner.
        if not self.config["capacity"]["resources"]:
            return
        views, roots = self._state()
        used = self.working.used(self._occupancy(views))
        waiting = sorted((t for t in self.tasks if t.status in {"WAITING_CAPACITY", "WAITING_CALL_CAPACITY"}),
                         key=lambda t: (t.arrived, t.index))
        proposed = self.working.proposed(waiting[0].context, waiting[0].task["id"], waiting[0].pending_units) if waiting else set()
        pressure = any(used[n] > quota or sum(max(self._occupancy(views).get(g, 0),
            int(g in self.working.groups() | proposed)) * self.config["capacity"]["demands"][g][n]
            for g in set(views) | proposed) > quota
            for n, quota in self.config["capacity"]["resources"].items())
        if not pressure:
            return
        options = [o for o in self._resource_options(views, roots, time.time()) if o["action"] == "scale_in"]
        for option in sorted(options, key=lambda o: (-o["facts"]["idle_ms"], o["instance_id"])):
            group = option["group_id"]
            if (group in self.working.groups() and len(views[group]["instances"]) <= 1):
                continue
            op = self._execute(option, "capacity_pressure", views)
            self._record(kind="resource", choice="capacity_pressure", action="scale_in", group_id=group,
                         instance_id=option["instance_id"], op=op["id"], legal=[o["instance_id"] for o in options])
            # Reconsider after physical release, not after a stop was requested.
            return

    def _preparable(self, group_id):
        if group_id in self.service_groups:
            return []
        cache = self.__dict__.setdefault("preparable", {})
        if group_id not in cache:
            # The run lock and contract digest keep these rules fixed for the run.
            rules = self.platform.store.get("runtime_group", group_id)["contract"]["prepare"]
            cache[group_id] = [u for u in self.members[group_id]
                               if rules.get((self.catalog.unit(u) or {}).get("entry_id"), {}).get("status") == "SUPPORTED"]
        return cache[group_id]

    def _operation_allowed(self, group_id, operation, units=None):
        # Managed services retain their own service contract and lease guards.
        if group_id in self.service_groups:
            return True
        members = list(units if units is not None else self.members.get(group_id, []))
        if not members:
            return False
        try:
            for unit_id in members:
                self.catalog.require_operation(unit_id, operation)
        except ValueError:
            return False
        return True

    # Decisions.

    def _choose(self, kind, legal, build, rule):
        """Index, choice kind, representation and policy milliseconds; features are built only for learning."""
        indices = [index for index, allowed in enumerate(legal) if allowed]
        if len(indices) == 1:
            return indices[0], "single", 0.0, 0.0
        if kind not in self.learned:
            started = time.perf_counter()
            index = rule()
            return index, "rule", 0.0, (time.perf_counter() - started) * 1000
        started = time.perf_counter()
        observation, candidates = build()
        if self.budget_policy:
            observation = [*observation, *budget_policy.condition(self.budget_policy, self.budget_index,
                                                                  self.block_multipliers)]
        represented = (time.perf_counter() - started) * 1000
        started = time.perf_counter()
        decision = self.learner.act(observation, candidates, legal, training=self.training)
        policy = (time.perf_counter() - started) * 1000
        at_ms = (time.time() - self.start) * 1000
        row = dict(state=observation, candidates=candidates, legal=legal, index=decision["index"], reward=0.0,
                   terminated=False, truncated=False, next_state=None, next_candidates=[], next_legal=[],
                   policy_version=decision.get("policy_version"), old_log_prob=decision.get("log_prob"),
                   value=decision.get("value"), episode_id=self.wid, elapsed_ms=None)
        if self.budget_policy:
            current = envelope_integral(self.deployments.residency(set(self.groups), self.accounting_start,
                time.time()), self.config["capacity"]["demands"], self.config["capacity"]["resources"])
            row["cost_at_decision"] = dict(resource_seconds=current.get("resource_seconds"),
                resource_status=current["status"], charged_task_seconds=self.ledger["charged_task_seconds"],
                missed_arrivals=0)
        if self.trajectory:
            self.trajectory[-1]["transition"].update(next_state=observation, next_candidates=candidates,
                next_legal=legal, elapsed_ms=max(0.0, at_ms - self.trajectory[-1]["at_ms"]))
        self.trajectory.append(dict(transition=row, kind=kind, at_ms=at_ms,
            components=dict(quality=0.0, task_time=0.0, instance_time=0.0, resource_time=0.0, unfinished=0.0),
            explored=bool((decision.get("diagnostics") or {}).get("explored")),
            diagnostics=copy.deepcopy(decision.get("diagnostics"))))
        return decision["index"], "learned", represented, policy

    def _count(self, *key):
        name = "/".join(key)
        self.counters[name] = self.counters.get(name, 0) + 1

    def _record(self, **value):
        value.setdefault("at_ms", (time.time() - self.start) * 1000)
        # For non-business decisions representation_ms is the learned build time.
        value.setdefault("joint_observation_build_ms", value.get("representation_ms", 0.0))
        self.records.append(value)
        self._count(value["kind"], value["choice"])
        return value

    def _decide(self):
        for task in sorted(self.tasks, key=lambda item: (item.event_at if item.status == "DECISION" else 0, item.index)):
            if task.status == "DECISION":
                self._business(task)
        # Selection creates an intent. Only its full admitted region can wake
        # the original adapter and thereby create executable call roots.
        self._admit_tasks()
        self._pressure_release()
        self._on_demand()
        if self.resource_due:
            self.resource_due = False
            # fixed/1 with resident retention has nothing but yield to choose.
            if (self.config["resource_policy"] != "fixed/1"
                    or (self.config.get("retention") or {}).get("mode") == "idle_retire/1"):
                self._resource_stage()
        self._dispatch_stage()
        self._boundary_stage()

    def _boundary_stage(self, draining=False):
        if self.config.get("dispatch_scope") != "roots_and_boundaries":
            return
        refused = set()
        def check(call, roots):
            root = next((item for item in roots if item["id"] == call["root_id"]), None)
            gate = self.runtime.call("gate_view", call["gate"])
            if root is None or gate is None:
                raise ValueError("原位 gate 或所属根请求不存在")
            return self.catalog.require_boundary(root["unit_id"], call, root, gate)

        def reject(call, error):
            # The view only contains ENABLED calls; cancel wakes the original
            # waiter and never claims that an already started call retired.
            self.runtime.call("cancel_enabled", call["id"])
            refused.add(call["id"])
            self._record(kind="boundary", choice="contract_refused", call_id=call["id"],
                         root=call["root_id"], gate_id=call["gate"], reason=str(error))

        while True:
            pool = sorted((c for c in self.runtime.call("boundary_view", self.wid) if c["id"] not in refused),
                          key=lambda c: c["enabled_at"])
            if not pool:
                return
            views, roots = self._state()
            eligible = []
            for call in pool:
                try:
                    check(call, roots)
                    eligible.append(call)
                except ValueError as error:
                    reject(call, error)
            pool = eligible
            if not pool:
                return
            now = time.time()
            aged = [c for c in pool if (now - c["enabled_at"]) * 1000 >= self.config["age_guard_ms"]]
            pool = aged or pool
            views, roots = self._state()
            by_root = {r["id"]: r for r in roots}
            def estimate(call):
                return self._estimate("boundary:" + str(call["semantic_key"]))
            def facts(call):
                root = by_root[call["root_id"]]
                return dict(wait_ms=(now - call["enabled_at"]) * 1000, estimate_ms=estimate(call),
                            task_age_ms=(now - self.by_context[call["task_id"]].arrived) * 1000,
                            task_roots=self.by_context[call["task_id"]].roots_done,
                            remote=root["target"]["kind"] == "REMOTE", warm_target=True, aged=bool(aged), boundary=True)
            def build():
                return (joint_observation("dispatch", self.scales, self._workload(views, roots, now)),
                        [self._candidate("dispatch", self.scales,
                            cards=[self.cards.get((c.get("unit") or {}).get("id"),
                                                  self.cards[by_root[c["root_id"]]["unit_id"]])],
                            dispatch=facts(c)) for c in pool])
            def rule():
                estimates = [estimate(c) for c in pool]
                if self.config["dispatch_policy"] == "shortest_estimated/1" and all(v is not None for v in estimates):
                    return min(range(len(pool)), key=lambda i: estimates[i])
                if self.config["dispatch_policy"] == "edf/1":
                    return min(range(len(pool)), key=lambda i: self.by_context[pool[i]["task_id"]].arrived +
                               self.by_context[pool[i]["task_id"]].task["deadline_ms"] / 1000)
                return 0
            if draining or aged:
                index, choice, represented, policy_ms = 0, "drain" if draining else "age_guard", 0, 0
            else:
                index, choice, represented, policy_ms = self._choose("dispatch", [True] * len(pool), build, rule)
            call = pool[index]
            try:
                _, current_roots = self._state()
                evidence = check(call, current_roots)
            except ValueError as error:
                reject(call, error)
                continue
            try:
                self.runtime.call("authorize_boundary", call["id"], self.wid)
            except ProtocolError as error:
                refused.add(call["id"])
                self._record(kind="boundary", choice="recheck_refused", call_id=call["id"], root=call["root_id"],
                             reason=str(error), representation_ms=represented, policy_ms=policy_ms)
                continue
            self._record(kind="boundary", choice=choice, call_id=call["id"], root=call["root_id"],
                         task_id=self.by_context[call["task_id"]].task["id"], semantic_key=call["semantic_key"],
                         gate_id=call["gate"], contract_digest=evidence["contract_digest"],
                         boundary_scope=evidence.get("scope"), boundary_id=evidence.get("boundary_id"),
                         legal=len(pool), wait_ms=(now - call["enabled_at"]) * 1000,
                         representation_ms=represented, policy_ms=policy_ms)

    def _candidate_intent(self, task, candidate, *, observe=None):
        # A cold intent is not permission to execute. Only the precise
        # no-worker outcome can be resolved by starting an empty host.
        if task.session.candidate_legal(candidate):
            return True, set(), None
        if not hasattr(task.session, "candidate_intent"):
            return False, set(), None
        intent = task.session.candidate_intent(candidate)
        if not intent["allowed"]:
            return False, set(), None
        groups = set(intent["groups"])
        views, _ = observe() if observe is not None else self._state()
        occupancy = self._occupancy(views)
        for group in groups:
            view = views[group]
            if (view["instances"] or group in self.pending_groups or
                    occupancy[group] or view["operations"].get("start") != "SUPPORTED" or
                    view["start_gap"] is not None or self.config["capacity"]["replica_cap"] < 1 or
                    not self._operation_allowed(group, "prepare")):
                return False, set(), None
        return True, groups, intent["signatures"]

    def _business(self, task):
        event = task.event
        # A synchronous candidate pool shares descriptive state only.
        # Admission, preparation and dispatch obtain fresh state separately.
        observed = None
        def observe():
            nonlocal observed
            if observed is None:
                observed = self._state()
            return observed
        legal, intent_signatures = [], []
        for candidate in event["candidates"]:
            units = candidate.get("units", [])
            intent, _, signatures = self._candidate_intent(task, candidate, observe=observe)
            intent_signatures.append(signatures)
            candidate["decision_legal"] = task.session.candidate_legal(candidate)
            allowed = intent and self.working.fits(task.context, task.task["id"],
                units=units, include_other=False)
            # A retained owner may not take on a new region by holding some
            # resources while waiting for another retained owner's resources.
            # Full compound candidates were reserved before their first call.
            if allowed and task.committed_units and not self.working.covers(task.context, task.task["id"], units):
                allowed = self.working.fits(task.context, task.task["id"], units=units)
            legal.append(allowed)
        if not any(legal):
            raise ValueError("当前业务动作缺少可准入的完整闭包；保留原继续者，不能删减必做工作或部分占有后等待")
        policy = self.config["functional_policy"]
        def build():
            views, roots = observe()
            observation = joint_observation("business", self.scales, self._workload(views, roots, time.time()),
                                            event["observation"])
            return observation, [self._candidate(c["action_type"], self.scales, business=c["features"])
                                 for c in event["candidates"]]
        def reference():
            target = (event.get("references") or {}).get(policy)
            for index, candidate in enumerate(event["candidates"]):
                if candidate["id"] == target and legal[index]:
                    return index
            raise ValueError("场景未给出满足相同义务的合法参照动作：" + policy)
        index, choice, represented, policy_ms = self._choose("business", legal, build, reference)
        candidate = event["candidates"][index]
        record = self._record(kind="business", choice=choice, task=task.index, task_id=task.task["id"],
            candidate=candidate["id"], action=candidate["action_type"], units=list(candidate.get("units", [])),
            legal=[c["id"] for i, c in enumerate(event["candidates"]) if legal[i]],
            representation_ms=represented + event.get("representation_duration_ms", 0), policy_ms=policy_ms,
            joint_observation_build_ms=represented,
            scene_represent_ms=event.get("represent_duration_ms", 0.0),
            scene_dispatch_conditions_ms=event.get("dispatch_conditions_duration_ms", 0.0))
        task.decisions.append(record)
        task.pending_action, task.pending_units = index, list(candidate.get("units", []))
        task.pending_condition_signatures = intent_signatures[index]
        if task.pending_condition_signatures is not None:
            current = task.session.candidate_intent(candidate)
            if not current["allowed"] or current["signatures"] != task.pending_condition_signatures:
                task.status = "RUNNING"
                task.inbox.put(dict(condition_rejected=True))
                task.pending_action, task.pending_units = None, []
                task.pending_condition_signatures = None
                self._count("action", candidate["action_type"])
                return
        task.status = "WAITING_CALL_CAPACITY"
        self._count("action", candidate["action_type"])

    def _on_demand(self):
        views, roots = self._state()
        # Prepare only admitted business-role and selected-call service closures.
        for group_id in sorted(self.working.groups() & self.service_groups):
            view = views[group_id]
            if view["instances"] or group_id in self.pending_groups:
                continue
            budget = self._budget_after(group_id, views)
            if budget is not None and budget < 0:
                continue
            self._execute(dict(action="prepare", mode="start", group_id=group_id), "on_demand", views)
            views, roots = self._state()
        # Only selected and fully admitted cold intents can start a worker for
        # a pure condition check. This does not prepare or construct a Unit.
        needed = set().union(*(getattr(t, "pending_condition_groups", set()) for t in self.tasks
                               if t.status == "WAITING_CALL_RESOURCES"))
        for group_id in sorted(needed):
            view = views[group_id]
            if view["instances"] or group_id in self.pending_groups:
                continue
            budget = self._budget_after(group_id, views)
            if ((budget is not None and budget < 0) or view["operations"].get("start") != "SUPPORTED"
                    or view["start_gap"] is not None or not self._operation_allowed(group_id, "prepare")):
                continue
            self._execute(dict(action="prepare", mode="start", group_id=group_id), "on_demand", views)
            views, roots = self._state()
        # A selected, fully reserved call may pay for a separately proved
        # original import prefix. No owner or original method is prepared here.
        for task in self.tasks:
            if task.status != "WAITING_CALL_RESOURCES":
                continue
            for request in getattr(task, "pending_imports", []):
                group_id = request["group_id"]
                key = (request["unit"], request["instance_identity"]["instance_id"], request["plan_digest"])
                if key in task.pending_import_ops or group_id in self.pending_groups:
                    continue
                if (not self.working.covers(task.context, task.task["id"], task.pending_units) or
                        not self._operation_allowed(group_id, "prepare", [request["unit"]])):
                    task.status = "RUNNING"
                    task.inbox.put(dict(condition_rejected=True))
                    task.pending_action, task.pending_units = None, []
                    task.pending_imports = []
                    break
                option = dict(action="prepare", mode="import_only", group_id=group_id,
                              instance_id=request["instance_identity"]["instance_id"],
                              instance_identity=request["instance_identity"], units=[request["unit"]],
                              plan_digest=request["plan_digest"], task=task,
                              input_signatures=list(task.pending_condition_signatures))
                op = self._execute(option, "on_demand_import", views)
                task.pending_import_ops[key] = op
                views, roots = self._state()
        for r in sorted((r for r in roots if r["state"] == "ENABLED"), key=lambda r: r["enabled_at"]):
            if r["target"]["kind"] != "REMOTE":
                continue
            group_id = r["target"]["group_id"]
            view = views[group_id]
            bound = r["target"].get("instance_id") or self.deployments.bound(r["task_id"], group_id)
            gone = bound and not any(item["id"] == bound and item["state"] in {"READY", "DRAINING"} for item in view["instances"])
            pending = group_id in self.pending_groups or any(item["state"] in {"UNDEPLOYED", "STARTING"} for item in view["instances"])
            ready = [item for item in view["instances"] if item["state"] == "READY"]
            if gone:
                self.runtime.call("cancel_root", r["id"], "本任务绑定的实例已不可用；原 owner 状态不迁移，也不重放")
            elif ready and not pending and all(r["unit_id"] not in item["units"] for item in ready) and len(ready) == len(view["instances"]):
                self.runtime.call("cancel_root", r["id"], "实例宿主环境未满足该成员：" + r["unit_id"])
            elif not view["instances"] and not pending:
                budget = self._budget_after(group_id, views)
                if budget is not None and budget < 0:
                    self._count("on_demand", "blocked_by_budget")
                    continue
                op = self._execute(dict(action="prepare", mode="start", group_id=group_id), "on_demand", views)
                self.on_demand.append(dict(root=r["id"], task_id=self.by_context[r["task_id"]].task["id"],
                                           group_id=group_id, op=op["id"], at_ms=(time.time() - self.start) * 1000))
                views, roots = self._state()

    def _dispatchable(self, views, roots):
        active = [r for r in roots if r["state"] in HELD]
        if self.config["capacity"]["call_slots"] - len(active) <= 0:
            return []
        busy = {r["host"] for r in active}
        result = []
        for r in sorted((r for r in roots if r["state"] == "ENABLED"), key=lambda r: r["enabled_at"]):
            target = r["target"]
            try:
                self.catalog.require_operation(r["unit_id"], "dispatch")
                self._require_root_conditions(r)
            except ValueError as error:
                self.runtime.call("cancel_root", r["id"], "操作契约不再满足：" + str(error))
                continue
            if not self._services_ready(self.by_context[r["task_id"]], views):
                continue
            if target["kind"] == "LOCAL":
                host = None if "local" in busy else "local"
            else:
                usable = [item for item in views[target["group_id"]]["instances"]
                          if item["state"] == "READY" and item["accepting"] and not item["pending_stop"]
                          and r["unit_id"] in item["units"] and item["id"] not in busy and item["id"] not in self.stopping]
                bound = target.get("instance_id") or self.deployments.bound(r["task_id"], target["group_id"])
                if bound:
                    host = bound if any(item["id"] == bound for item in usable) else None
                else:
                    host = min(usable, key=lambda item: (item["in_flight"], item["generation"]))["id"] if usable else None
            if host:
                result.append((r, host))
        return result

    def _estimate(self, unit_id):
        values = self.estimates.get(unit_id)
        return sum(values) / len(values) if values else None

    def _dispatch_rule(self, pool):
        policy = self.config["dispatch_policy"]
        if policy == "shortest_estimated/1":
            estimates = [self._estimate(r["unit_id"]) for r, _ in pool]
            if any(value is None for value in estimates):
                self._count("dispatch", "estimate_fallback_fifo")
                return 0
            return min(range(len(pool)), key=lambda i: (estimates[i], pool[i][0]["enabled_at"]))
        if policy == "edf/1":
            def deadline(i):
                task = self.by_context[pool[i][0]["task_id"]]
                return task.arrived + task.task["deadline_ms"] / 1000
            return min(range(len(pool)), key=lambda i: (deadline(i), pool[i][0]["enabled_at"]))
        return 0

    def _dispatch_stage(self):
        refused = set()
        while True:
            views, roots = self._state()
            legal = [(r, host) for r, host in self._dispatchable(views, roots) if (r["id"], host) not in refused]
            if not legal:
                return
            now = time.time()
            aged = [(r, host) for r, host in legal if (now - r["enabled_at"]) * 1000 >= self.config["age_guard_ms"]]
            pool = aged or legal
            represented = policy_ms = 0.0
            if aged:
                index, choice = 0, "age_guard"
            else:
                def build():
                    observation = joint_observation("dispatch", self.scales, self._workload(views, roots, now))
                    return observation, [self._candidate("dispatch", self.scales, cards=[self.cards[r["unit_id"]]],
                                                         dispatch=self._dispatch_facts(r, host, views, now))
                                         for r, host in pool]
                index, choice, represented, policy_ms = self._choose("dispatch", [True] * len(pool), build,
                                                                     lambda: self._dispatch_rule(pool))
            root, host = pool[index]
            target = root["target"]
            assigned = False
            try:
                self.catalog.require_operation(root["unit_id"], "dispatch")
                self._require_root_conditions(root, host)
            except ValueError as error:
                self.runtime.call("cancel_root", root["id"], "操作契约不再满足：" + str(error))
                self._record(kind="dispatch", choice="recheck_refused", root=root["id"], host=host, reason=str(error))
                continue
            try:
                if target["kind"] == "REMOTE":
                    assigned = self.deployments.assign(root["task_id"], target["group_id"], host)
                self.runtime.call("authorize_root", root["id"], host)
            except ProtocolError as error:
                if assigned:
                    self.deployments.unassign(root["task_id"], target["group_id"])
                refused.add((root["id"], host))
                self._record(kind="dispatch", choice="recheck_refused", root=root["id"], host=host, reason=str(error))
                continue
            task = self.by_context[root["task_id"]]
            self._record(kind="dispatch", choice=choice, root=root["id"], task=task.index, task_id=task.task["id"],
                unit_id=root["unit_id"], host=host, wait_ms=(now - root["enabled_at"]) * 1000,
                estimate_ms=self._estimate(root["unit_id"]), legal=len(pool), legal_before_age_guard=len(legal),
                representation_ms=represented, policy_ms=policy_ms)
            self._count("action", "dispatch")

    def _dispatch_facts(self, root, host, views, now):
        task = self.by_context[root["task_id"]]
        remote = root["target"]["kind"] == "REMOTE"
        if remote:
            item = next(item for item in views[root["target"]["group_id"]]["instances"] if item["id"] == host)
            warm = (item.get("initialization") or {}).get(root["unit_id"]) == "BOUND"
        else:
            warm = bool(self.catalog.runtime_state([root["unit_id"]]).get("warm"))
        services = self._services(task)
        return dict(wait_ms=(now - root["enabled_at"]) * 1000, estimate_ms=self._estimate(root["unit_id"]),
                    task_age_ms=(now - task.arrived) * 1000, task_roots=task.roots_done, remote=remote,
                    warm_target=warm, aged=False, dependencies_ready=self._services_ready(task, views),
                    shared_users=sum(bool(services & set(v)) for v in self.working.claims.values()),
                    dependency_waiters=sum(bool(services & self._services(t)) and t.status == "WAITING_RESOURCES"
                                           for t in self.tasks), leases=len(services))

    def _resource_options(self, views, roots, now):
        options = []
        cap = self.config["capacity"]["replica_cap"]
        active = [r for r in roots if r["state"] in HELD]
        busy = {r["host"] for r in active}
        waiting = [r for r in roots if r["state"] == "ENABLED"]
        occupancy = self._occupancy(views)
        for group_id, view in views.items():
            if group_id in self.pending_groups:
                continue
            instances, ops = view["instances"], view["operations"]
            held = occupancy[group_id]
            ready = [item for item in instances if item["state"] == "READY"]
            preparable = self._preparable(group_id)
            queued = [r for r in waiting if r["target"].get("group_id") == group_id]
            budget = self._budget_after(group_id, views)
            facts = dict(held=held, ready=len(ready),
                starting=held - len(instances) + sum(item["state"] in {"UNDEPLOYED", "STARTING"} for item in instances),
                draining=sum(item["state"] == "DRAINING" for item in instances), cap_remaining=cap - held,
                waiting_roots=len(queued), cold=not instances, replicable=view["replicable"], budget_after=budget,
                warm=any(all((item.get("initialization") or {}).get(u) == "BOUND" for u in preparable)
                         for item in ready) if preparable else False)
            claimed = self.working.groups()
            users = sum(group_id in v for v in self.working.claims.values())
            facts.update(service_target=group_id in self.service_groups, shared_users=users,
                leases=users, dependency_waiters=sum(group_id in self._services(t) and
                    t.status == "WAITING_RESOURCES" for t in self.tasks),
                dependencies_ready=bool(ready),
                **self._load_history(group_id),
                marginal_resources={name: (0 if group_id in claimed or held else
                    self.config["capacity"]["demands"][group_id][name]) / quota
                    for name, quota in self.config["capacity"]["resources"].items()})
            cards = [self.cards[u] for u in self.members[group_id]]
            fits = budget is None or budget >= 0
            if not held:
                if ops.get("start") == "SUPPORTED" and view["start_gap"] is None and fits and cap >= 1:
                    options.append(dict(action="prepare", mode="start", group_id=group_id, facts=facts, cards=cards))
            else:
                for item in sorted(ready, key=lambda item: item["generation"]):
                    missing = [u for u in preparable if u in item["units"]
                               and (item.get("initialization") or {}).get(u) != "BOUND"]
                    if (missing and ops.get("prewarm") == "SUPPORTED" and item["accepting"] and not item["pending_stop"]
                            and not item["in_flight"] and item["id"] not in busy):
                        options.append(dict(action="prepare", mode="prewarm", group_id=group_id, instance_id=item["id"],
                                            units=missing, facts=facts, cards=cards))
                        break
                if (held < cap and view["replicable"] and ops.get("add_replica") == "SUPPORTED"
                        and view["start_gap"] is None and fits):
                    options.append(dict(action="scale_out", group_id=group_id, facts=facts, cards=cards))
            if view["reclaim"] and ops.get("drain_stop") == "SUPPORTED":
                for item in ready:
                    usable = [r for r in queued if (r["target"].get("instance_id") or
                              self.deployments.bound(r["task_id"], group_id)) in (None, item["id"])]
                    if (item["accepting"] and not item["pending_stop"] and not item["in_flight"]
                            and item["id"] not in busy and item["id"] not in self.stopping
                            and not view["leases"].get(item["id"]) and not usable
                            and not (group_id in claimed and held <= 1)):
                        idle = now - (item.get("last_activity_wall") or item.get("ready_wall") or now)
                        options.append(dict(action="scale_in", group_id=group_id, instance_id=item["id"],
                            facts=dict(facts, idle_ms=idle * 1000,
                                       warm=all((item.get("initialization") or {}).get(u) == "BOUND" for u in preparable)
                                       if preparable else False),
                            cards=cards))
        # A known failure is not repeated on the same group state; the policy learns from the attempt itself.
        signatures = {g: self._signature(view) for g, view in views.items()}
        for option in options:
            group = option["group_id"]
            facts = dict(option["facts"])
            current = max(occupancy[group], int(group in self.working.groups()))
            delta = -1 if option["action"] == "scale_in" else int(option.get("mode") == "start" or
                                                                          option["action"] == "scale_out")
            future = max(occupancy[group] + delta, int(group in self.working.groups()))
            facts["marginal_resources"] = {name: (future - current) *
                self.config["capacity"]["demands"][group][name] / quota
                for name, quota in self.config["capacity"]["resources"].items()}
            option["facts"] = facts
        return [o for o in options if self.withheld.get((o["action"], o.get("mode"), o["group_id"], o.get("instance_id")))
                != signatures[o["group_id"]] and self._operation_allowed(o["group_id"],
                    "prewarm" if o.get("mode") == "prewarm" else o["action"], o.get("units"))]

    def _resource_rule(self, options):
        policy, retention = self.config["resource_policy"], self.config.get("retention") or {}
        if policy in {"dependency_cost/1", "shared_lru/1"}:
            # All candidates already passed contract, full-closure budget and
            # business-owner lease guards. No future arrivals are consulted.
            needed = [i for i, o in enumerate(options) if o["action"] == "prepare" and
                      o["facts"]["waiting_roots"] + o["facts"].get("dependency_waiters", 0) > 0]
            if needed:
                return max(needed, key=lambda i: (options[i]["facts"]["waiting_roots"] +
                    options[i]["facts"].get("dependency_waiters", 0),
                    options[i]["facts"].get("load_ms") or 0, options[i]["group_id"]))
            retired = [i for i, o in enumerate(options) if o["action"] == "scale_in" and
                o["facts"]["idle_ms"] >= retention["idle_ms"] +
                ((o["facts"].get("load_ms") or 0) if policy == "dependency_cost/1" else 0)]
            if retired:
                if policy == "shared_lru/1":
                    return max(retired, key=lambda i: (options[i]["facts"]["idle_ms"], options[i]["instance_id"]))
                # Prefer useful reclaimed capacity with low observed reload cost;
                # unknown loading cost has no predicted value and ties use age.
                return max(retired, key=lambda i: (
                    sum(max(0, -v) for v in options[i]["facts"]["marginal_resources"].values()) /
                    (1 + (options[i]["facts"].get("load_ms") or retention["idle_ms"])),
                    options[i]["facts"]["idle_ms"], options[i]["instance_id"]))
            return len(options) - 1
        if policy == "queue_threshold/1":
            for index, option in enumerate(options):
                if option["action"] == "scale_out" and option["facts"]["waiting_roots"] >= self.config["queue_threshold"]:
                    return index
        if retention.get("mode") == "idle_retire/1":
            for index, option in enumerate(options):
                if option["action"] == "scale_in" and option["facts"]["idle_ms"] >= retention["idle_ms"]:
                    return index
        return len(options) - 1

    def _resource_stage(self):
        self.consecutive = 0
        while self.consecutive < self.config["max_consecutive_resource_ops"]:
            views, roots = self._state()
            now = time.time()
            options = self._resource_options(views, roots, now)
            if not options:
                return
            options.append(dict(action="yield_until_next_event"))
            def build():
                observation = joint_observation("resource", self.scales, self._workload(views, roots, now))
                return observation, [self._candidate(o["action"], self.scales, cards=o.get("cards"),
                                                     resource=o.get("facts")) for o in options]
            index, choice, represented, policy_ms = self._choose("resource", [True] * len(options), build,
                                                                 lambda: self._resource_rule(options))
            option = options[index]
            legal = [o["action"] + ":" + (o.get("instance_id") or o["group_id"]) for o in options[:-1]]
            if option["action"] == "yield_until_next_event":
                if choice == "learned":
                    self._record(kind="resource", choice=choice, action=option["action"], legal=legal,
                                 representation_ms=represented, policy_ms=policy_ms)
                else:
                    self._count("resource", "rule_yield")
                self._count("action", option["action"])
                return
            op = self._execute(option, choice, views)
            if choice == "learned":
                self.trajectory[-1]["op"] = op["id"]
            self._record(kind="resource", choice=choice, action=option["action"], group_id=option["group_id"],
                         instance_id=option.get("instance_id"), mode=option.get("mode"), op=op["id"], legal=legal,
                         representation_ms=represented, policy_ms=policy_ms)
            self._count("action", option["action"])
            self.consecutive += 1

    def _execute(self, option, origin, views):
        operation = "prewarm" if option.get("mode") == "prewarm" else option["action"]
        if not self._operation_allowed(option["group_id"], operation, option.get("units")):
            raise ValueError("资源操作契约不再满足；保留当前实例及原有清理流程")
        op = dict(id=uuid.uuid4().hex, action=option["action"], mode=option.get("mode"), group_id=option["group_id"],
                  instance_id=option.get("instance_id"), units=option.get("units"), origin=origin, status="RUNNING",
                  phase="submitting", requested_ms=(time.time() - self.start) * 1000)
        if option.get('mode') == 'import_only':
            op.update(task_id=option['task'].context, plan_digest=option['plan_digest'],
                      instance_identity=copy.deepcopy(option['instance_identity']),
                      input_signatures=copy.deepcopy(option['input_signatures']))
        self.ops.append(op)
        self.op_walls[op["id"]] = time.time()
        starting = option["action"] == "scale_out" or option.get("mode") == "start"
        before = {item["id"] for item in views[option["group_id"]]["instances"]}
        if option["action"] == "scale_in":
            self.stopping.add(option["instance_id"])
        else:
            self.pending_groups.add(option["group_id"])
        if starting:
            # Reserved on this loop thread before submission, so a concurrent decision cannot spend it again.
            self.reserved[op["id"]] = (option["group_id"], before)
        image = self.groups[option["group_id"]].get("image")
        def work():
            started = time.perf_counter()
            outcome = dict(phase="awaiting")
            try:
                if option["action"] == "scale_in":
                    result = self.deployments.drain_stop(option["group_id"], dict(instance_id=option["instance_id"]),
                                                         run_id=self.rid, reason="scale_in")
                    outcome["result"] = dict(instances=[item.get("id") for item in result.get("instances", [])])
                elif option.get("mode") == "prewarm":
                    result = self.deployments.prewarm(option["group_id"], dict(instance_id=option["instance_id"],
                                                      units=option["units"]), run_id=self.rid)
                    answers = result.get("results", [])
                    outcome["result"] = dict(initialization_ready=result.get("initialization_ready"),
                        answers=[dict(status=a.get("status"), unprepared_units=a.get("unprepared_units"),
                                      error=a.get("error")) for a in answers])
                    if result.get("initialization_ready") is True:
                        outcome["status"] = "DONE"
                    elif answers and all(a.get("answered", True) for a in answers):
                        outcome.update(status="FAILED", reason="工作进程应答，所需成员未完成提前初始化")
                    else:
                        outcome.update(status="UNKNOWN", reason="提前初始化的应答未取得或身份不符")
                elif option.get("mode") == "import_only":
                    task = option['task']
                    unit = option['units'][0]
                    def claim_check():
                        return (not self.closing and task.status == 'WAITING_CALL_RESOURCES'
                                and task.pending_action is not None and unit in task.pending_units
                                and task.pending_condition_signatures == option['input_signatures']
                                and self.working.covers(task.context, task.task['id'], task.pending_units)
                                and any(item['unit'] == unit and item['plan_digest'] == option['plan_digest']
                                        and item['instance_identity'] == option['instance_identity']
                                        for item in task.pending_imports))
                    result = self.deployments.import_only(option['group_id'], dict(instance_id=option['instance_id'],
                        instance_identity=option['instance_identity'], unit=unit,
                        plan_digest=option['plan_digest'], task_id=task.context),
                        run_id=self.rid, claim_check=claim_check)
                    outcome['result'] = dict(status=result.get('status'), reason=result.get('reason'))
                    if result.get('status') == 'IMPORTED':
                        outcome['status'] = 'DONE'
                    elif result.get('status') == 'FAILED':
                        outcome.update(status='FAILED', reason=result.get('reason') or '原导入前缀失败')
                    else:
                        outcome.update(status='UNKNOWN', reason=result.get('reason') or '原导入前缀结果未知')
                else:
                    result = self.deployments.start(option["group_id"], dict(expected_image=image), run_id=self.rid,
                                                    replica=option["action"] == "scale_out")
                    outcome.update(instance_id=result.get("id"), result=dict(state=result.get("state"),
                                   status=result.get("status"), reason=result.get("reason")))
                    if result.get("state") == "FAILED":
                        outcome.update(status="FAILED", reason="实例启动失败：" + str(result.get("reason")))
            except Exception as error:
                reason = type(error).__name__ + ": " + str(error)
                from seampact.hosting.images import PreparationFailed
                if isinstance(error, PreparationFailed):
                    outcome.update(status="PREPARATION_FAILED", reason=reason, preparation_failure=str(error))
                created = [item["id"] for item in self.deployments.live_instances(option["group_id"])
                           if item["id"] not in before] if starting else []
                if created:
                    # The record exists although submission raised; its own state decides the outcome.
                    outcome.update(instance_id=created[0], error=reason)
                elif not outcome.get("preparation_failure"):
                    outcome.update(status="FAILED", reason="执行器拒绝或提交失败：" + reason)
            finally:
                outcome["submit_ms"] = (time.perf_counter() - started) * 1000
                self.events.put(("op_submitted", op, outcome))
        threading.Thread(target=work, daemon=True, name="seampact-joint-op").start()
        return op

    @staticmethod
    def _signature(view):
        return tuple(sorted((item["id"], item["state"], bool(item["accepting"]), bool(item["pending_stop"]),
                             tuple(sorted((item.get("initialization") or {}).items()))) for item in view["instances"]))

    def _track_ops(self, now):
        """Resolve submitted operations from the instance they act on."""
        for op in self.ops:
            if op["status"] != "RUNNING" or op["phase"] != "awaiting":
                continue
            fact = self.deployments.instance_fact(op["instance_id"]) if op.get("instance_id") else None
            if op["action"] == "scale_in":
                if fact is None or not fact["held"]:
                    self._settle(op, "DONE", "实例已停止并释放")
                elif fact["state"] == "UNKNOWN":
                    self._settle(op, "UNKNOWN", "排空期间实例结果未知")
                elif (fact.get("pending_stop") or {}).get("blocked") or fact.get("drain_blocked"):
                    self._settle(op, "UNKNOWN", "释放受阻，无法确认资源已归还：" +
                                 str((fact.get("pending_stop") or {}).get("blocked") or fact.get("drain_blocked")))
            elif fact is None:
                self._settle(op, "UNKNOWN", "未取得新实例记录")
            elif fact["state"] == "READY":
                self._settle(op, "DONE", "实例就绪")
            elif fact["state"] == "UNKNOWN":
                self._settle(op, "UNKNOWN", "启动中实例结果未知")
            elif fact["state"] in {"FAILED", "STOPPED"} or not fact["held"]:
                self._settle(op, "FAILED", "实例未就绪即失败或停止：" + str(fact.get("reason")))
            elif fact.get("start_wall") and now - fact["start_wall"] > self.config["start_timeout_s"]:
                self._settle(op, "FAILED", "实例在 %d 秒内未就绪" % self.config["start_timeout_s"])

    def _settle(self, op, status, reason):
        op.update(status=status, outcome_reason=reason, phase="settled",
                  duration_ms=(time.time() - self.op_walls[op["id"]]) * 1000,
                  settled_ms=(time.time() - self.start) * 1000)
        self.reserved.pop(op["id"], None)
        self.stopping.discard(op["instance_id"] if op["action"] == "scale_in" else None)
        self._count("resource_outcome", op["action"], status)
        self.resource_due = True
        if status == "UNKNOWN":
            self.unknown_outcomes.append(op["id"])
            self._close("resource_outcome_unknown")
        if status != "FAILED":
            return
        if op["action"] != "scale_in" and op.get("mode") not in {"prewarm", "import_only"} and op.get("instance_id"):
            fact = self.deployments.instance_fact(op["instance_id"]) or {}
            if fact.get("held"):
                self._cleanup(op)
        if op["origin"] == "on_demand_import":
            for task in self.tasks:
                if task.status == "WAITING_CALL_RESOURCES" and op in getattr(task, "pending_import_ops", {}).values():
                    task.status = "RUNNING"
                    task.inbox.put(dict(condition_rejected=True))
                    task.pending_action, task.pending_units = None, []
                    task.pending_imports = []
        elif op["origin"] == "on_demand":
            for task in self.tasks:
                if (task.status == "WAITING_CALL_RESOURCES" and
                        op["group_id"] in getattr(task, "pending_condition_groups", set())):
                    task.status = "RUNNING"
                    task.inbox.put(dict(condition_rejected=True))
                    task.pending_action, task.pending_units = None, []
                    task.pending_condition_groups = set()
                    task.pending_condition_signatures = None
            if op["group_id"] in self.service_groups:
                for task in self.tasks:
                    if task.status in {"WAITING_RESOURCES", "WAITING_CALL_RESOURCES"} and op["group_id"] in self._services(task):
                        task.status = "FAILED"
                        task.ended = time.time()
                        task.error = dict(type="ServiceStartFailed", message=reason)
                        if task.thread is not None:
                            task.inbox.put(None)
                        self._release_task(task)
                        self._fail(task.error)
            # As in functional mode, a call whose instance cannot start fails; nothing else can host it.
            for r in self.runtime.call("root_view", self.wid):
                if r["state"] == "ENABLED" and r["target"].get("group_id") == op["group_id"]:
                    self.runtime.call("cancel_root", r["id"], "按需启动失败：" + reason)
        else:
            views, _ = self._state()
            key = (op["action"], op.get("mode"), op["group_id"], op.get("instance_id") if op["action"] != "scale_out"
                   and op.get("mode") != "start" else None)
            self.withheld[key] = self._signature(views[op["group_id"]])

    def _cleanup(self, op):
        def work():
            try:
                result = self.deployments.drain_stop(op["group_id"], dict(instance_id=op["instance_id"]),
                                                     run_id=self.rid, reason="start_failed")
                info = dict(status="REQUESTED", instances=[item.get("id") for item in result.get("instances", [])])
            except Exception as error:
                info = dict(status="REFUSED", error=type(error).__name__ + ": " + str(error))
            self.events.put(("cleanup", op, info))
        threading.Thread(target=work, daemon=True, name="seampact-joint-cleanup").start()

    def _cancel_waiting(self, reason):
        for r in self.runtime.call("root_view", self.wid):
            if r["state"] == "ENABLED":
                self.runtime.call("cancel_root", r["id"], reason)

    # Window result.

    def _item(self):
        # Reconcile once more after drain/release; unresolved tasks are censored at
        # this reporting endpoint, and their window remains ineligible as before.
        if self.start is not None:
            self._charge_task_time(self.end_at or self.window_end)
        images = getattr(self.platform, "images", None)
        run = self.platform.store.get("scheduler_run", self.rid) or {}
        scope_id = run.get("retention_scope_id")
        preparation_failure = images.get(scope_id).get("failure") if images and scope_id else None
        if preparation_failure:
            self.execution_error = dict(type="PreparationFailed", message=preparation_failure["reason"])
        roots = self.runtime.call("root_view", self.wid)
        unfinished = [task for task in self.tasks if task.deadline_status == "UNFINISHED"]
        if unfinished and not getattr(self, "unfinished_credited", False):
            self._credit("unfinished", self.config["workload"]["unfinished_reward"] * len(unfinished))
            self.unfinished_credited = True
        if self.trajectory:
            self.trajectory[-1]["transition"].update(terminated=True, truncated=False, next_state=None,
                next_candidates=[], next_legal=[], elapsed_ms=max(0.0,
                    ((self.end_at or self.window_end) - self.start) * 1000 - self.trajectory[-1]["at_ms"]))
        outcomes = {op["id"]: dict(status=op["status"], reason=op.get("outcome_reason")) for op in self.ops}
        for stored in self.trajectory:
            if stored.get("op"):
                stored["outcome"] = outcomes[stored["op"]]
        reported_envelope = self.envelope
        if self.budget_policy:
            local = [dict(unit_id=unit,
                task_ids=[task.task["id"] for task in self.tasks
                          if unit in self.config["working_sets"]["allowed"][task.task["id"]]],
                root_ids=[call["root"]["root_id"] for task in self.tasks if task.session is not None
                          for call in task.session.context.calls
                          if call["unit_id"] == unit and call.get("root")],
                reason="frozen_local_owner_has_no_attributable_full_lifetime_resource_integral")
                for unit, placement in self.catalog.placement.items() if placement["kind"] == "LOCAL"]
            if local:
                # Deployment residency covers managed allocations, not the
                # in-process Unit owner. This gap is frozen-pool dependent,
                # never selected-call dependent: a skipped local owner may
                # already be resident. Known managed dependencies do not turn
                # this missing local lifetime integral into zero resource use.
                reported_envelope = copy.deepcopy(self.envelope)
                known = reported_envelope.get("resource_seconds")
                if known is None:
                    known = {name: sum(row["resource_seconds"].get(name, 0.0)
                        for row in reported_envelope.get("instances", []))
                        for name in self.config["capacity"]["resources"]}
                reported_envelope.update(status="UNKNOWN", resource_seconds=None,
                    known_resource_seconds=known, local_allocation_gaps=local,
                    coverage="managed allocation integral is partial; local Unit owner lifetime is not attributed")
                reported_envelope["unknown_allocations"] = sorted(set(
                    reported_envelope.get("unknown_allocations", [])) |
                    {"local:" + row["unit_id"] for row in local})
        failed = [task.task["id"] for task in self.tasks if task.status == "FAILED" and task.deadline_status != "UNFINISHED"]
        eligible = (not self.execution_error and not self.environment_changes and not self.missing_feedback
                    and not self.truncated and not failed and not self.unknown_outcomes
                    and not getattr(self, "release_left", {})
                    and self.scoring["status"] == "COMPLETED" and not self.scoring["left"]
                    and self.closing in (None, "horizon"))
        exclusion = None if eligible else (
            "execution_error" if self.execution_error or failed else
            "runtime_environment_changed" if self.environment_changes else
            "resource_outcome_unknown:" + ",".join(self.unknown_outcomes) if self.unknown_outcomes else
            "release_unconfirmed" if getattr(self, "release_left", {}) else
            "scoring_" + self.scoring["status"].lower() if self.scoring["status"] != "COMPLETED" else
            "feedback_unknown:" + ",".join(self.missing_feedback) if self.missing_feedback else
            "truncated:" + ",".join(self.truncated) if self.truncated else
            "window_closed:" + str(self.closing))
        span = max((self.window_end or time.time()) - self.start, 1e-9) if self.start else None
        task_items, online, evaluation = [], [], []
        for task in self.tasks:
            response = (task.done - task.arrived) * 1000 if task.done is not None and task.arrived is not None else None
            ledger = None
            if task.session is not None and task.arrived is not None:
                transitions = [dict(event=dict(representation_duration_ms=d["representation_ms"]),
                                    decision=dict(duration_ms=d["policy_ms"])) for d in task.decisions]
                cost_manifest = dict(self.manifest,
                    criteria=scene_task_criteria(self.manifest["scene"], task.task, self.manifest["criteria"]),
                    evaluation_criteria=scene_task_criteria(self.manifest["scene"], task.task,
                        self.manifest["evaluation_criteria"], "evaluation_criteria"))
                ledger = episode_costs(task.session.context, cost_manifest, response or 0.0, transitions,
                                       task.feedback, task.independent)
                online.extend(ledger["online"]["records"])
                evaluation.extend(ledger["evaluation"]["records"])
            task_items.append(dict(index=task.index, task_id=task.task["id"], context=task.context,
                route=task.task.get("route"), task_class=task.task.get("task_class"),
                capacity_wait_ms=((getattr(task, "admitted_at", None) or self.window_end or time.time()) - task.arrived) * 1000
                    if task.arrived is not None else None,
                arrival_offset_ms=task.offset_ms, status=task.status, deadline_status=task.deadline_status,
                credited=task.deadline_status != "UNFINISHED",
                arrived_ms=(task.arrived - self.start) * 1000 if task.arrived is not None else None,
                done_ms=(task.done - self.start) * 1000 if task.done is not None else None, response_ms=response,
                ended_ms=(task.ended - self.start) * 1000 if task.ended is not None else None,
                terminal_reason=(task.terminal or {}).get("reason"), final_response=(task.terminal or {}).get("response"),
                evidence=(task.terminal or {}).get("evidence", {}), program_metrics=task.program,
                evaluation_metrics=(task.terminal or {}).get("evaluation_metrics", {}),
                feedback=task.feedback, independent_feedback=task.independent, error=task.error,
                quality_credit_index=task.quality_credit_index,
                evaluation_capture=dict(status="BLOCKED" if task.evidence_blocked else
                    "FROZEN" if task.evaluation_snapshot is not None else "NOT_AVAILABLE",
                    artifacts=(task.evaluation_snapshot or {}).get("artifacts", []),
                    duration_ms=(task.evaluation_snapshot or {}).get("capture_duration_ms")),
                evaluation_error=task.evaluation_error, decisions=len(task.decisions),
                calls=copy.deepcopy(task.session.context.calls) if task.session else [],
                model_calls=copy.deepcopy(task.session.context.model_calls) if task.session else [],
                roots=[r for r in roots if r["task_id"] == task.context], costs=ledger))
        components = {key: sum(t["components"][key] for t in self.trajectory) for key in self.unattributed}
        window_return = sum(components.values()) + sum(self.unattributed.values())
        ledger = dict(self.ledger, span_ms=span * 1000 if span else None,
            end_ms=(self.end_at - self.start) * 1000 if self.start and self.end_at else None,
            mean_active_roots=self.ledger["root_seconds"] / span if span else None,
            parallel_fraction=self.ledger["parallel_seconds"] / span if span else None,
            weights=self.config["costs"],
            semantics="task_seconds sums arrival-to-terminal timestamp intervals, unresolved tasks censored at end_ms; "
                      "charged_task_seconds additionally clips each interval at horizon_ms when set; instance_seconds "
                      "integrates held instances (UNDEPLOYED/STARTING/READY/DRAINING/unreleased) as a reservation "
                      "proxy, not measured memory, until the common end point after release (end_ms), of which "
                      "release_tail_seconds falls after the last event; parallelism counts ACTIVE root requests "
                      "executing at once over span_ms, UNKNOWN roots are reported apart; not the instance count")
        return dict(kind="joint_window", episode=self.number, window_id=self.wid,
            preparation_failure=preparation_failure,
            resource_observation=self.observation,
            route_metrics=route_metric_report(self.manifest["scene"], task_items, self.manifest["criteria"],
                                             self.manifest["evaluation_criteria"]),
            service=service_summary(task_items, span),
            task_ids=[task.task["id"] for task in self.tasks], tasks=task_items, decisions=self.records,
            counters=dict(sorted(self.counters.items())), transitions=self.trajectory,
            resource_ops=self.ops, unknown_outcomes=self.unknown_outcomes, on_demand=self.on_demand, roots=roots,
            reward=window_return if eligible else None, window_return=window_return,
            reward_components=dict(credited=components, unattributed=self.unattributed, timing="joint_window/1",
                                   semantics="window return = credited + unattributed; only credited parts reach "
                                             "learned transitions"),
            eligible=eligible, exclusion_reason=exclusion, closing=self.closing,
            execution_error=self.execution_error, environment_changes=self.environment_changes,
            scoring=copy.deepcopy(self.scoring),
            setup=self.setup, release=dict(groups=self.release, release_ms=getattr(self, "release_ms", None),
                                           left=getattr(self, "release_left", {})),
            parallelism=ledger, residency=self.residency,
            costs=dict(schema="joint-cost-ledger/2" if self.config["costs"].get("schema") else "joint-cost-ledger/1",
                resource_envelope=reported_envelope,
                online=dict(wall_ms=span * 1000 if span else 0.0,
                    representation_ms=sum(d.get("representation_ms", 0) for d in self.records),
                    # Sums of overlapping task durations; not additive to window wall time.
                    joint_observation_build_ms=sum(d.get("joint_observation_build_ms", 0) for d in self.records),
                    joint_state_snapshot_ms=self.state_snapshot_ms, joint_state_snapshot_calls=self.state_snapshot_calls,
                    scene_represent_ms=sum(d.get("scene_represent_ms", 0) for d in self.records),
                    scene_dispatch_conditions_ms=sum(d.get("scene_dispatch_conditions_ms", 0) for d in self.records),
                    timing_scope="cumulative durations may overlap across tasks and are not window wall time; "
                                 "joint observation build covers learned callbacks only; state snapshot time covers "
                                 "all _state calls and may overlap the build time",
                    policy_ms=sum(d.get("policy_ms", 0) for d in self.records),
                    records=online, usage=summarize_usage(online)),
                evaluation=dict(wall_ms=sum(1000 * ((t.feedback or {}).get("cost_duration", 0) +
                                                    (t.independent or {}).get("cost_duration", 0)) for t in self.tasks),
                                records=evaluation, usage=summarize_usage(evaluation)),
                interval=ledger, residency=self.residency),
            online_duration_ms=span * 1000 if span else 0.0, choice_interactions=len(self.trajectory),
            estimates={unit: dict(mean_ms=sum(v) / len(v), samples=len(v)) for unit, v in self.estimates.items()})


def run_windows(jobs, rid, run, folder, manifest, catalog, bound, learner):
    """Execute independent windows; budget PPO updates only after a complete block."""
    tasks = {task["id"]: task for task in manifest["scene"]["tasks"]}
    estimates, rewards = {}, []
    counts, totals, definitions, populations = {}, {}, {}, {}
    records = {"online": [], "evaluation": []}
    budget = manifest["joint"].get("budget_policy")
    if budget:
        initial = [budget["initial_multipliers"][name] for name in budget_policy.dimensions(budget)]
        duals = run["budget_multipliers"] = copy.deepcopy(manifest.get("trained_budget_multipliers",
                         [list(initial) for _ in budget["points"]]))
        run["budget_choice_windows"] = [0] * len(duals)
    summary = run.setdefault("joint", dict(windows=0, tasks=0, unfinished_tasks=0, failed_tasks=0,
                                           max_active_roots=0, mean_active_roots=[], counters={}))
    def collected():
        blocks = budget["blocks"] if budget else [{"windows": [i]} for i in range(manifest["episodes"])]
        for block in blocks:
            begin = getattr(jobs, "begin_joint_block", None)
            if begin and not begin(rid, block["windows"][0], block["windows"][-1] + 1):
                break
            windows, items = [], []
            index = budget_policy.budget_index(budget, manifest["seed"], block["windows"][0]) if budget else None
            for number in block["windows"]:
                if jobs.stops[rid].is_set() or jobs.closed:
                    break
                window = Window(jobs, rid, manifest, catalog, bound, learner,
                    number, [tasks[t] for t in window_tasks(manifest, number)], {} if budget else estimates,
                    **({"block_budget_index": index, "block_multipliers": list(duals[index])} if budget else {}))
                item = window.run(defer_evaluation=bool(budget))
                windows.append(window)
                items.append(item)
                if (item.get("execution_error") or item.get("environment_changes") or item["release"]["left"]
                        or any(task.get("evaluation_capture", {}).get("status") == "BLOCKED"
                               for task in item["tasks"])):
                    break
            if not items:
                break
            if budget:
                items = [window.evaluate_pending() for window in windows]
                complete = len(items) == len(block["windows"])
                if complete:
                    eligible = budget_policy.settle_block(items, budget, block, index, duals[index], manifest["mode"] != "freeze")
                else:
                    eligible = False
                    for item in items:
                        item.update(eligible=False, reward=None, exclusion_reason="incomplete_collection_block",
                                    budget_policy=dict(schema="budget-settlement/2", status="INELIGIBLE",
                                                       errors=["incomplete_collection_block"],
                                                       frozen_windows=copy.deepcopy(block["windows"]),
                                                       frozen_quality=copy.deepcopy(block["quality"]),
                                                       api=copy.deepcopy(budget["api"])))
                if eligible:
                    run["budget_choice_windows"][index] += sum(bool(item["transitions"]) for item in items)
            else:
                eligible = items[0]["eligible"]
            stats = {}
            if learner and manifest["mode"] != "freeze" and eligible:
                for item in items:
                    for stored in item["transitions"]:
                        learner.observe(stored["transition"])
                started = time.perf_counter()
                stats = learner.update(force=True) if budget else learner.update()
                run["gradient_duration_ms"] = run.get("gradient_duration_ms", 0) + (time.perf_counter() - started) * 1000
                if budget:
                    budget_policy.update_dual(items, budget, index, duals[index])
                    jobs._save_checkpoint(rid, run, folder, manifest, learner,
                                          committed_window_end=block["windows"][-1] + 1)
            for item in items:
                yield item, stats
            if len(items) == len(block["windows"]):
                finish = getattr(jobs, "finish_joint_block", None)
                if finish:
                    finish(rid, block["windows"][-1] + 1)
    for item, stats in collected():
        run["episodes"] += 1
        run["interactions"] += len(item["transitions"])
        run["choice_interactions"] += item["choice_interactions"]
        run["no_choice_episodes"] += not item["transitions"]
        run["eligible_episodes" if item["eligible"] else "excluded_episodes"] += 1
        if item["reward"] is not None:
            rewards.append(item["reward"])
        run["reward_mean"] = sum(rewards) / len(rewards) if rewards else None
        run["reward_denominator"] = len(rewards)
        for bucket in item["route_metrics"]:
            route = bucket["route"]
            populations[route] = populations.get(route, 0) + bucket["tasks"]
            for row in bucket["metrics"].values():
                metric = row["definition"]
                scope = "training" if row["scope"] == "criteria" else "independent"
                key = (scope, row["id"], metric.get("version"), metric.get("unit"), metric.get("direction"),
                       metric.get("level", "system"), metric.get("unit_id"), route)
                statuses = counts.setdefault(key, {})
                for status in ("KNOWN", "UNKNOWN", "NOT_APPLICABLE"):
                    statuses[status] = statuses.get(status, 0) + row[status.lower()]
                definitions[key] = metric.get("definition")
                totals.setdefault(key, []).extend(row["values"])
        population = summary["tasks"] + sum(t["arrived_ms"] is not None for t in item["tasks"])
        run["metrics"] = [dict(scope=k[0], id=k[1], version=k[2], unit=k[3], direction=k[4], level=k[5], unit_id=k[6], route=k[7],
            definition=definitions[k], value=sum(totals[k]) / len(totals[k]) if totals.get(k) else None,
            denominator=len(totals.get(k, [])), population=populations.get(k[7], 0), status_counts=v,
            timing_scope="observed quality including late completion; see per-route outcomes and service deadline counts")
            for k, v in counts.items()]
        summary.update(windows=summary["windows"] + 1, tasks=population,
            unfinished_tasks=summary["unfinished_tasks"] + sum(
                t["deadline_status"] == "UNFINISHED" or t["status"] == "UNFINISHED" for t in item["tasks"]),
            failed_tasks=summary["failed_tasks"] + sum(t["status"] == "FAILED" for t in item["tasks"]),
            max_active_roots=max(summary["max_active_roots"], item["parallelism"]["max_active_roots"]))
        summary["mean_active_roots"].append(item["parallelism"]["mean_active_roots"])
        summary.setdefault("service_windows", []).append(dict(window_id=item["window_id"], **item["service"]))
        summary.setdefault("route_windows", []).append(dict(window_id=item["window_id"], routes=item["route_metrics"]))
        summary.setdefault("resource_windows", []).append(item["resource_observation"])
        for key, value in item["counters"].items():
            summary["counters"][key] = summary["counters"].get(key, 0) + value
        summary["explored"] = summary.get("explored", 0) + sum(t["explored"] for t in item["transitions"])
        costs = run.setdefault("costs", dict(online_wall_ms=0, evaluation_wall_ms=0, representation_ms=0, policy_ms=0,
            task_seconds=0.0, instance_seconds=0.0, release_tail_seconds=0.0,
            coverage="see per-window joint-cost-ledger/1 and per-task ledgers; instance_seconds includes the release tail"))
        costs["online_wall_ms"] += item["costs"]["online"]["wall_ms"]
        costs["evaluation_wall_ms"] += item["costs"]["evaluation"]["wall_ms"]
        costs["representation_ms"] += item["costs"]["online"]["representation_ms"]
        for key in ("joint_observation_build_ms", "joint_state_snapshot_ms", "joint_state_snapshot_calls",
                    "scene_represent_ms", "scene_dispatch_conditions_ms"):
            costs[key] = costs.get(key, 0) + item["costs"]["online"][key]
        costs["policy_ms"] += item["costs"]["online"]["policy_ms"]
        costs["task_seconds"] += item["parallelism"]["task_seconds"]
        costs["instance_seconds"] += item["parallelism"]["instance_seconds"]
        costs["release_tail_seconds"] = costs.get("release_tail_seconds", 0.0) + item["parallelism"]["release_tail_seconds"]
        envelope = item["costs"].get("resource_envelope") or {}
        if envelope.get("status") == "KNOWN":
            resource_totals = costs.setdefault("resource_envelope_seconds", {})
            for name, value in envelope["resource_seconds"].items():
                resource_totals[name] = resource_totals.get(name, 0.0) + value
        else:
            costs["resource_envelope_status"] = "UNKNOWN"
        for scope in records:
            records[scope].extend(item["costs"][scope]["records"])
            costs[scope + "_usage"] = summarize_usage(records[scope])
        run["online_duration_ms"] = run.get("online_duration_ms", 0) + item["online_duration_ms"]
        run["evaluation_duration_ms"] = run.get("evaluation_duration_ms", 0) + item["costs"]["evaluation"]["wall_ms"]
        run["learning"] = learner.snapshot() if learner else None
        run["updates"] = run["learning"]["updates"] if learner else 0
        jobs._append(folder, "history", item)
        jobs._append(folder, "curves", dict(episode=run["episodes"], interactions=run["interactions"],
            reward=item["reward"], window_return=item["window_return"], eligible=item["eligible"], stats=stats,
            **({"budget_policy": item["budget_policy"]} if budget else {}),
            mean_active_roots=item["parallelism"]["mean_active_roots"], metrics=run["metrics"]))
        jobs.store.save("scheduler_run", rid, run)
        if item.get("preparation_failure"):
            from seampact.hosting.images import PreparationFailed
            raise PreparationFailed(item["preparation_failure"]["reason"], run.get("retention_scope_id"))
        if item["execution_error"]:
            raise ValueError("窗口执行中断，已保留实际调用和费用材料：" + item["execution_error"]["message"])
        if any(task.get("evaluation_capture", {}).get("status") == "BLOCKED" for task in item["tasks"]):
            raise ValueError("任务证据仍依赖原owner，已保留当前窗口及分母并中断后续采集")
        if item["environment_changes"]:
            raise ValueError("运行组执行条件在窗口中改变（实例失联、进程替换或响应未取得），已保留本窗口材料并中断运行")
        if item["release"]["left"]:
            raise ValueError("窗口末仍有未释放实例，下一窗口无法从冻结初态开始：" + str(item["release"]["left"]))
        if item.get("scoring", {}).get("left"):
            raise ValueError("评分资源未确认释放，下一采集块无法从冻结初态开始：" + str(item["scoring"]["left"]))
