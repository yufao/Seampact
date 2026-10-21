"""joint/1 workload windows: rules build legal sets, a policy selects, the executor re-checks.

Original dependencies, owner/session order, protection preconditions and resource
limits are kept; the cross-task global arrival order is not. A business decision
submits an approved request without blocking; dispatch chooses among enabled root
requests; resource decisions prepare, add or release instances. A running call is
never preempted. Task quality is settled when the task completes; time-integral
costs accrue between adjacent events. Vertical, node scaling and live migration
are outside joint/1.
"""

import copy
import contextlib
import math
import queue
import threading
import time
import uuid

from .capabilities import VERSION_2, freeze_joint, joint_candidate, joint_observation
from .costs import episode_costs, summarize_usage
from .evaluation import evaluate
from .runtime import ProtocolError
from .scenarios import SceneSession

MODE = "joint/1"
DISPATCH = ("fifo/1", "shortest_estimated/1", "edf/1", "learned_ready/1")
RESOURCE = ("fixed/1", "queue_threshold/1", "learned/1")
FIELDS = {"functional_policy", "dispatch_policy", "resource_policy", "retention", "queue_threshold", "age_guard_ms",
          "max_consecutive_resource_ops", "review_interval_ms", "capacity", "workload", "costs", "scales",
          "initial_state", "start_timeout_s"}
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


def configure(config, *, scene, tasks, task_ids, algorithm, criteria, contracts, groups, deployments,
              learning, references, mode):
    """Validate and freeze one joint/1 run; every compared policy shares these values."""
    value = copy.deepcopy(config.get("joint") or {})
    if not isinstance(value, dict) or set(value) - FIELDS:
        raise ValueError("joint 仅接收：" + "、".join(sorted(FIELDS)))
    if (scene.get("representation") or {}).get("version") != VERSION_2:
        raise ValueError("联合模式的业务候选需要 capability-features/2 共同表示")
    if config.get("resource_control"):
        raise ValueError("联合模式的初态、副本与回收由 joint 配置冻结，不再接收 resource_control")
    if (config.get("feedback_protocol") or {}).get("reward_timing", "sparse_terminal") != "sparse_terminal":
        raise ValueError("联合模式在任务完成时结算质量、按事件区间累计时间成本；event_module/1 局部奖励未定义")
    if mode == "rejudge":
        raise ValueError("联合窗口的旧经历重评未定义；请采集新经历")
    functional = value.get("functional_policy", "learned" if algorithm in learning else algorithm)
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
            native = [g for g in groups if not deployments._idle_rebuild_allowed(deployments.platform.store.get("runtime_group", g))]
            if native:
                raise ValueError("原生有状态运行组须使用 fixed_resident；尚无任务/owner 结束及可重建契约：" + ", ".join(native))
        elif set(retention) != {"mode"}:
            raise ValueError("fixed_resident 不接收其他字段")
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
    if not isinstance(resources, dict) or not isinstance(demands, dict):
        raise ValueError("capacity.resources 与 capacity.demands 须为对象")
    for name, quota in resources.items():
        _positive(quota, "资源配额 " + str(name))
    if set(demands) - set(groups):
        raise ValueError("驻留需求只能声明本次远程运行组")
    for group_id in groups if resources else []:
        row = demands.get(group_id)
        # Unknown residency demands are not filled with zero.
        if not isinstance(row, dict) or set(row) != set(resources) or any(not _number(v) or v < 0 for v in row.values()):
            raise ValueError("声明资源配额后，每个远程运行组须给出每项资源的实例驻留需求：" + group_id)
    for name, quota in resources.items():
        if sum(demands[g][name] for g in groups) > quota:
            raise ValueError("资源配额不足以让每个运行组各保留一个实例，按需启动会无法满足：" + name)
    scalable = [g for g in groups if contracts[g]["replicas"].get("replicable") is True
                and contracts[g]["operations"].get("add_replica") == "SUPPORTED"]
    if resource != "fixed/1" and capacity["replica_cap"] >= 2 and scalable and capacity["call_slots"] < 2:
        raise ValueError("扩缩容实验须按实际资源配置至少两个根调用槽；call_slots=1 仅作串行对照")

    workload = copy.deepcopy(value.get("workload") or {})
    if not isinstance(workload, dict) or set(workload) - {"tasks_per_window", "arrival", "horizon_ms", "unfinished_reward"}:
        raise ValueError("workload 仅接收 tasks_per_window、arrival、horizon_ms、unfinished_reward")
    size = _count(workload.get("tasks_per_window"), "workload.tasks_per_window")
    if size > len(task_ids):
        raise ValueError("一个窗口的任务数不能超过任务清单")
    arrival = workload.get("arrival")
    if isinstance(arrival, dict) and set(arrival) == {"interval_ms"}:
        if not _number(arrival["interval_ms"]) or arrival["interval_ms"] < 0:
            raise ValueError("arrival.interval_ms 须为非负有限数")
        offsets = [arrival["interval_ms"] * index for index in range(size)]
    elif isinstance(arrival, dict) and set(arrival) == {"offsets_ms"}:
        offsets = arrival["offsets_ms"]
        if (not isinstance(offsets, list) or len(offsets) != size or any(not _number(v) or v < 0 for v in offsets)
                or offsets != sorted(offsets)):
            raise ValueError("arrival.offsets_ms 须为与窗口任务数等长、非负且不减的到达偏移")
    else:
        raise ValueError("workload.arrival 须为 {interval_ms} 或 {offsets_ms}")
    workload["offsets_ms"] = list(offsets)
    horizon = workload.get("horizon_ms")
    if horizon is not None:
        _positive(horizon, "workload.horizon_ms")
        if horizon < offsets[-1]:
            raise ValueError("horizon_ms 不能早于窗口内最后到达")
        if not _number(workload.get("unfinished_reward")):
            raise ValueError("设置收尾时限时须预先声明未完成任务的 unfinished_reward")
    elif "unfinished_reward" in workload:
        raise ValueError("unfinished_reward 只用于设置了 horizon_ms 的窗口")
    if dispatch == "edf/1":
        by_id = {task["id"]: task for task in tasks}
        if any(not _number(by_id[t].get("deadline_ms")) or by_id[t]["deadline_ms"] <= 0 for t in task_ids):
            raise ValueError("edf/1 仅在每个任务确有 deadline_ms 时使用")

    costs = value.get("costs")
    if not isinstance(costs, dict) or set(costs) != {"task_seconds", "instance_seconds"} or any(
            not _number(v) or v < 0 for v in costs.values()):
        raise ValueError("joint.costs 须显式给出 task_seconds 与 instance_seconds 的非负权重")
    if costs["task_seconds"] > 0:
        weights = criteria["objective"]["weights"]
        doubled = [d["id"] for d in criteria["dimensions"] if d["provider"] == "program"
                   and d.get("binding") == "latency_ms" and weights.get(d["id"], 0) > 0]
        if doubled:
            raise ValueError("已用任务数×时间积分计响应代价，判据不再重复加每任务完整时延：" + ", ".join(doubled))
    initial = value.get("initial_state", "cold")
    if initial not in {"cold", "warm"}:
        raise ValueError("initial_state 须为 cold 或 warm")
    timeout = _count(value.get("start_timeout_s"), "start_timeout_s", 900)
    frozen = dict(version=MODE, functional_policy=functional, dispatch_policy=dispatch, resource_policy=resource,
        learned=learned, retention=retention, queue_threshold=threshold,
        age_guard_ms=_positive(value.get("age_guard_ms"), "age_guard_ms"),
        max_consecutive_resource_ops=_count(value.get("max_consecutive_resource_ops"), "max_consecutive_resource_ops"),
        review_interval_ms=review, capacity=capacity, workload=workload, costs=dict(costs),
        representation=freeze_joint(value.get("scales")), initial_state=initial, start_timeout_s=timeout,
        rules=dict(
            decision_order="at each event: business decisions in arrival order, deterministic on-demand starts, "
                           "resource decisions (at most max_consecutive_resource_ops non-yield operations), then "
                           "dispatch while a legal root request and a free root slot exist",
            single_candidate="executed and recorded; not a learning step",
            dispatch="legal roots: enabled, host (local process or affinity/accepting instance) free, root slot free",
            age_guard="age = time since the root request was enabled; when a legal root reaches age_guard_ms the oldest "
                      "such root is dispatched first; it never lifts host, owner, slot or placement conditions",
            shortest_estimated="historical mean root duration of the same Unit from completed calls earlier in this "
                               "run; if any candidate lacks an estimate the decision falls back to FIFO",
            edf="earliest task arrival + declared deadline_ms; ties FIFO",
            on_demand="a waiting root of a group with no held instance starts one deterministically; prepare/scale "
                      "candidates for that group are withheld while the start is pending",
            scale_in="READY, accepting, no in-flight or prewarm, no active or waiting root that could use it, no "
                     "unfinished task lease, reclaim allowed by the group contract",
            capacity="root slots = call_slots; held instances (UNDEPLOYED/STARTING/READY/DRAINING/unreleased) and "
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
            controller="this window is the only replica controller; HPA is not active in the same experiment"))
    return dict(scheduling_mode=MODE, joint=frozen, lower_policy=dispatch,
        resource_control=dict(mode=MODE, initial_state=initial, start_timeout_s=timeout,
            replicas="cap %d per group, frozen in this run" % capacity["replica_cap"],
            run_window="each window starts from the frozen initial state; instances it started are drained and stopped at its end"),
        feedback_protocol=dict(reward_timing="joint_window/1", local_weights={},
            terminal="task objective at task completion, credited to the open learned transition",
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
    ids, size = manifest["task_ids"], manifest["joint"]["workload"]["tasks_per_window"]
    return [ids[(number * size + index) % len(ids)] for index in range(size)]


class TaskRun:
    def __init__(self, index, task, context, session, offset_ms):
        self.index, self.task, self.context, self.session, self.offset_ms = index, task, context, session, offset_ms
        self.inbox = queue.Queue()
        self.status, self.event, self.thread = "PENDING", None, None
        self.arrived = self.done = self.deadline_status = None
        self.terminal = self.error = self.feedback = self.independent = self.program = None
        self.evaluating, self.evaluation_error, self.decisions, self.roots_done = False, None, [], 0


class Window:
    def __init__(self, jobs, rid, manifest, catalog, bound, learner, number, tasks, estimates):
        self.jobs, self.platform = jobs, jobs.platform
        self.runtime, self.deployments = jobs.platform.runtime, jobs.platform.deployments
        self.rid, self.manifest, self.config = rid, manifest, manifest["joint"]
        self.catalog, self.bound, self.learner = catalog, bound, learner
        self.number, self.wid = number, rid + ":w" + str(number)
        self.training = manifest["mode"] != "freeze"
        self.learned = set(self.config["learned"])
        self.scales = self.config["representation"]["scales"]
        self.cards = manifest["scene"]["representation"]["cards"]
        self.groups = catalog.remote_groups()
        self.members = {g: [u for u, p in catalog.placement.items() if p.get("group_id") == g] for g in self.groups}
        self.events = queue.Queue()
        self.estimates = estimates
        offsets = self.config["workload"]["offsets_ms"]
        self.tasks = [TaskRun(index, task, self.wid + ":" + str(index) + ":" + task["id"], None, offsets[index])
                      for index, task in enumerate(tasks)]
        self.by_context = {task.context: task for task in self.tasks}
        self.trajectory, self.records, self.ops, self.on_demand = [], [], [], []
        self.counters = {}
        self.pending_groups, self.stopping, self.seen_roots = set(), set(), set()
        # op id -> (group, held instance ids at submission): a start not yet registered still holds budget.
        self.reserved, self.op_walls, self.withheld, self.idle_seen = {}, {}, {}, set()
        self.unknown_outcomes = []
        self.unattributed = dict(quality=0.0, task_time=0.0, instance_time=0.0, unfinished=0.0)
        self.ledger = dict(task_seconds=0.0, instance_seconds=0.0, release_tail_seconds=0.0, root_seconds=0.0,
                           unknown_root_seconds=0.0, parallel_seconds=0.0, max_active_roots=0,
                           max_unknown_roots=0, max_held_instances=0, hosts_used=[])
        self.levels = (0, 0, 0, 0)
        self.closing = self.execution_error = None
        self.environment_changes, self.missing_feedback, self.truncated = [], [], []
        self.setup, self.release, self.residency = {}, {}, None
        self.start = self.clock = self.done_at = self.horizon_at = self.end_at = None
        self.resource_due, self.consecutive = True, 0

    # Window lifecycle.

    def run(self):
        try:
            self._setup()
            self._loop()
        except Exception as error:
            if self.execution_error is None:
                self.execution_error = dict(type=type(error).__name__, message=str(error))
            self._close("execution_error")
            self._drain_after_error()
        finally:
            self._teardown()
        return self._item()

    def _setup(self):
        deployments = self.deployments
        for group_id in self.groups:
            if deployments.live_instances(group_id):
                raise ValueError("窗口初态要求运行组没有存活实例：" + group_id)
        with deployments.lock:
            self.changes_seen = len(deployments.environment_changes.get(self.rid, []))
        if self.config["initial_state"] == "warm":
            for group_id, placement in self.groups.items():
                started = time.perf_counter()
                record = deployments.ensure_ready(placement, self.members[group_id][0])
                required = self.members[group_id]
                setup = dict(instance_id=record["id"], start_to_ready_ms=(time.perf_counter() - started) * 1000,
                             process_ready=record["state"] == "READY", required_units=required)
                self.setup[group_id] = setup
                if set(required) - set(record["units"]) or set(required) - set(self._preparable(group_id)):
                    setup["status"] = "FAILED"
                    raise ValueError("暖态准备要求全部所需成员可用且契约允许提前初始化：" + group_id)
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
            placement = self.catalog.placement[unit_id]
            target = dict(kind="LOCAL") if placement["kind"] == "LOCAL" else dict(
                kind="REMOTE", group_id=placement["group_id"],
                instance_id=self.deployments.bound(task.context, placement["group_id"]))
            identity = self.runtime.call("enable_root", task.context, unit_id, target)
            grant = self.runtime.call("wait_root", identity)
            receipt = "RETURN"
            try:
                yield dict(grant)
            except BaseException as error:
                receipt = type(error).__name__
                raise
            finally:
                self.runtime.call("finish_root", identity, receipt)
        return admit

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
            if not self.closing and (self.jobs.stops[self.rid].is_set() or self.jobs.closed):
                self._close("stopped")
            if horizon is not None and now >= horizon and not self.closing:
                self._close("horizon")
            if self.closing:
                arrivals.clear()
            while arrivals and arrivals[0][0] <= now:
                _, task = arrivals.pop(0)
                if self.closing:
                    task.status = "NOT_ARRIVED"
                    continue
                task.arrived, task.status = now, "STARTING"
                task.thread = threading.Thread(target=self._task_body, args=(task,), daemon=True,
                                               name="seampact-joint-task")
                task.thread.start()
                self.resource_due = True
            for event in batch:
                self._handle(event)
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
            elif self.done_at is None:
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
        elif kind == "task_error":
            _, task, error, at = event
            task.error = dict(type=type(error).__name__, message=str(error))
            if self.closing:
                task.status = "UNFINISHED"
            else:
                task.status = "FAILED"
                self._fail(dict(type=type(error).__name__, message=str(error), task_id=task.task["id"]))
            self.deployments.release_task(task.context)
        elif kind == "root":
            self.resource_due = True
        elif kind == "instance":
            if event[1] in {"registered", "stopped", "stop_blocked", "start_failed", "unknown", "process_replaced",
                            "apply", "prewarm"}:
                self.resource_due = True
        elif kind == "op_submitted":
            _, op, outcome = event
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

    def _complete(self, task, event, at):
        # Use the completion timestamp, not the order in which the queue is drained.
        # A terminal event can cross the horizon while the decision thread is busy.
        if self.horizon_at is not None:
            if at > self.horizon_at:
                task.deadline_status = "UNFINISHED"
            elif self.closing == "horizon":
                task.deadline_status = "DONE"
        task.status, task.done, task.terminal = "DONE", at, event
        if event.get("truncated") and task.deadline_status != "UNFINISHED":
            self.truncated.append(task.task["id"])
        self.deployments.release_task(task.context)
        self.resource_due = True
        task.evaluating = True
        index = len(self.trajectory) - 1 if self.trajectory else None
        threading.Thread(target=self._evaluate, args=(task, index), daemon=True, name="seampact-joint-eval").start()

    def _evaluate(self, task, index):
        from .training import task_criteria
        try:
            event, calls = task.terminal, task.session.context.calls
            program = dict(event.get("program_metrics", {}), latency_ms=(task.done - task.arrived) * 1000,
                           module_calls=len(calls), module_duration_ms=sum(c["duration_ms"] for c in calls),
                           root_wait_ms=sum((c.get("root") or {}).get("wait_ms") or 0 for c in calls))
            task.program = program
            applicability = copy.deepcopy(task.task.get("metric_applicability", {}))
            called = {c["unit_id"] for c in calls}
            criterion = task_criteria(self.manifest["criteria"], applicability, called)
            final = task_criteria(self.manifest["evaluation_criteria"], applicability, called)
            evidence = event.get("evidence", {})
            feedback = evaluate(task.session.context.profiles, criterion, evidence, program)
            final_program = dict(program)
            final_program.update(event.get("evaluation_metrics", {}))
            final_program.update({key: program[key] for key in ("latency_ms", "module_calls", "module_duration_ms")})
            independent = evaluate(task.session.context.profiles, final, evidence, final_program) if final else None
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
        if self.done_at is None:
            self.done_at = time.time()

    def _teardown(self):
        deployments = self.deployments
        with deployments.lock:
            if self._on_instance_event in deployments.listeners:
                deployments.listeners.remove(self._on_instance_event)
        if getattr(self, "roots_configured", False):
            self.runtime.call("configure_roots", None)
        for task in self.tasks:
            if task.thread is not None:
                task.thread.join(30)
                if task.thread.is_alive():
                    task.error = task.error or dict(type="ThreadStillRunning", message="任务线程未在收尾时限内结束")
            deployments.release_task(task.context)
        end = self.done_at or time.time()
        if self.start is not None:
            self._track_ops(time.time())
        for op in self.ops:
            if op["status"] == "RUNNING" and op["phase"] == "awaiting" and op["action"] != "scale_in":
                # The window ends before this start resolved; release stops it, which is not a start failure.
                self._settle(op, "CANCELLED", "窗口收尾时撤回未就绪的启动")
        released_at = time.time()
        for group_id in self.groups:
            try:
                self.release[group_id] = deployments.release_run(group_id, self.rid)
            except Exception as error:
                self.release[group_id] = dict(error=str(error))
        deadline = time.time() + self.config["start_timeout_s"]
        while time.time() < deadline:
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
        if self.start is not None and self.groups:
            # Occupancy after the last event, drain and release included, up to the common end point.
            tail = deployments.residency(set(self.groups), self.clock, self.end_at)["resident_ms"] / 1000
            self.ledger["instance_seconds"] += tail
            self.ledger["release_tail_seconds"] = tail
            self._credit("instance_time", -self.config["costs"]["instance_seconds"] * tail)
            self.clock = self.end_at
            self.residency = deployments.residency(set(self.groups), self.start, self.end_at)
        self.window_end = end
        while any(task.evaluating for task in self.tasks):
            try:
                self._handle(self.events.get(timeout=1.0))
            except queue.Empty:
                pass
        for task in self.tasks:
            if task.session is not None:
                try:
                    task.session.close()
                except Exception as error:
                    task.error = task.error or dict(type=type(error).__name__, message="场景关闭失败：" + str(error))

    # Observable state.

    def _state(self):
        views = {g: self.deployments.group_view(g, p.get("image")) for g, p in self.groups.items()}
        return views, self.runtime.call("root_view", self.wid)

    def _refresh(self):
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
        self.views = views

    def _advance(self, now):
        span = now - self.clock
        if span <= 0:
            return
        tasks, held, roots, unknown = self.levels
        # Task time ends when every task has ended; instance time runs on to the common end point.
        task_end = now if self.done_at is None else min(now, self.done_at)
        task_span = max(0.0, task_end - self.clock)
        self.ledger["task_seconds"] += tasks * task_span
        self.ledger["instance_seconds"] += held * span
        self.ledger["root_seconds"] += roots * span
        self.ledger["unknown_root_seconds"] += unknown * span
        if roots >= 2:
            self.ledger["parallel_seconds"] += span
        charged = task_span if self.horizon_at is None else max(0.0, min(task_end, self.horizon_at) - self.clock)
        weights = self.config["costs"]
        self._credit("task_time", -weights["task_seconds"] * tasks * charged)
        self._credit("instance_time", -weights["instance_seconds"] * held * span)
        self.clock = now

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
        for view in getattr(self, "views", {}).values():
            if not view["reclaim"]:
                continue
            for item in view["instances"]:
                if (item["state"] == "READY" and not item["in_flight"] and item.get("last_activity_wall")
                        and item["id"] not in self.stopping):
                    result[(item["id"], item["last_activity_wall"])] = item["last_activity_wall"] + retention["idle_ms"] / 1000
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
        demands, occupancy = self.config["capacity"]["demands"], self._occupancy(views)
        return {name: sum(occupancy[g] * demands[g][name] for g in views)
                for name in self.config["capacity"]["resources"]}

    def _budget_after(self, group_id, views):
        resources = self.config["capacity"]["resources"]
        if not resources:
            return None
        used, demand = self._resource_use(views), self.config["capacity"]["demands"][group_id]
        return min((quota - used[name] - demand[name]) / quota for name, quota in resources.items())

    def _preparable(self, group_id):
        cache = self.__dict__.setdefault("preparable", {})
        if group_id not in cache:
            # The run lock and contract digest keep these rules fixed for the run.
            rules = self.platform.store.get("runtime_group", group_id)["contract"]["prepare"]
            cache[group_id] = [u for u in self.members[group_id]
                               if rules.get((self.catalog.unit(u) or {}).get("entry_id"), {}).get("status") == "SUPPORTED"]
        return cache[group_id]

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
        represented = (time.perf_counter() - started) * 1000
        started = time.perf_counter()
        decision = self.learner.act(observation, candidates, legal, training=self.training)
        policy = (time.perf_counter() - started) * 1000
        at_ms = (time.time() - self.start) * 1000
        row = dict(state=observation, candidates=candidates, legal=legal, index=decision["index"], reward=0.0,
                   terminated=False, truncated=False, next_state=None, next_candidates=[], next_legal=[],
                   policy_version=decision.get("policy_version"), old_log_prob=decision.get("log_prob"),
                   value=decision.get("value"), episode_id=self.wid, elapsed_ms=None)
        if self.trajectory:
            self.trajectory[-1]["transition"].update(next_state=observation, next_candidates=candidates,
                next_legal=legal, elapsed_ms=max(0.0, at_ms - self.trajectory[-1]["at_ms"]))
        self.trajectory.append(dict(transition=row, kind=kind, at_ms=at_ms,
            components=dict(quality=0.0, task_time=0.0, instance_time=0.0, unfinished=0.0),
            explored=bool((decision.get("diagnostics") or {}).get("explored")),
            diagnostics=copy.deepcopy(decision.get("diagnostics"))))
        return decision["index"], "learned", represented, policy

    def _count(self, *key):
        name = "/".join(key)
        self.counters[name] = self.counters.get(name, 0) + 1

    def _record(self, **value):
        value.setdefault("at_ms", (time.time() - self.start) * 1000)
        self.records.append(value)
        self._count(value["kind"], value["choice"])
        return value

    def _decide(self):
        for task in sorted(self.tasks, key=lambda item: (item.event_at if item.status == "DECISION" else 0, item.index)):
            if task.status == "DECISION":
                self._business(task)
        self._on_demand()
        if self.resource_due:
            self.resource_due = False
            # fixed/1 with resident retention has nothing but yield to choose.
            if (self.config["resource_policy"] != "fixed/1"
                    or (self.config.get("retention") or {}).get("mode") == "idle_retire/1"):
                self._resource_stage()
        self._dispatch_stage()

    def _business(self, task):
        event = task.event
        legal = [c["legal"] for c in event["candidates"]]
        policy = self.config["functional_policy"]
        def build():
            views, roots = self._state()
            observation = joint_observation("business", self.scales, self._workload(views, roots, time.time()),
                                            event["observation"])
            return observation, [joint_candidate(c["action_type"], self.scales, business=c["features"])
                                 for c in event["candidates"]]
        def reference():
            target = (event.get("references") or {}).get(policy)
            for index, candidate in enumerate(event["candidates"]):
                if candidate["id"] == target and candidate["legal"]:
                    return index
            raise ValueError("场景未给出满足相同义务的合法参照动作：" + policy)
        index, choice, represented, policy_ms = self._choose("business", legal, build, reference)
        candidate = event["candidates"][index]
        record = self._record(kind="business", choice=choice, task=task.index, task_id=task.task["id"],
            candidate=candidate["id"], action=candidate["action_type"], units=list(candidate.get("units", [])),
            legal=[c["id"] for c in event["candidates"] if c["legal"]],
            representation_ms=represented + event.get("representation_duration_ms", 0), policy_ms=policy_ms)
        task.decisions.append(record)
        task.status = "RUNNING"
        task.inbox.put(index)
        self._count("action", candidate["action_type"])

    def _on_demand(self):
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
                    return observation, [joint_candidate("dispatch", self.scales, cards=[self.cards[r["unit_id"]]],
                                                         dispatch=self._dispatch_facts(r, host, views, now))
                                         for r, host in pool]
                index, choice, represented, policy_ms = self._choose("dispatch", [True] * len(pool), build,
                                                                     lambda: self._dispatch_rule(pool))
            root, host = pool[index]
            target = root["target"]
            assigned = False
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
        return dict(wait_ms=(now - root["enabled_at"]) * 1000, estimate_ms=self._estimate(root["unit_id"]),
                    task_age_ms=(now - task.arrived) * 1000, task_roots=task.roots_done, remote=remote,
                    warm_target=warm, aged=False)

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
                            and not view["leases"].get(item["id"]) and not usable):
                        idle = now - (item.get("last_activity_wall") or item.get("ready_wall") or now)
                        options.append(dict(action="scale_in", group_id=group_id, instance_id=item["id"],
                            facts=dict(facts, idle_ms=idle * 1000,
                                       warm=all((item.get("initialization") or {}).get(u) == "BOUND" for u in preparable)
                                       if preparable else False),
                            cards=cards))
        # A known failure is not repeated on the same group state; the policy learns from the attempt itself.
        signatures = {g: self._signature(view) for g, view in views.items()}
        return [o for o in options if self.withheld.get((o["action"], o.get("mode"), o["group_id"], o.get("instance_id")))
                != signatures[o["group_id"]]]

    def _resource_rule(self, options):
        policy, retention = self.config["resource_policy"], self.config.get("retention") or {}
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
                return observation, [joint_candidate(o["action"], self.scales, cards=o.get("cards"),
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
        op = dict(id=uuid.uuid4().hex, action=option["action"], mode=option.get("mode"), group_id=option["group_id"],
                  instance_id=option.get("instance_id"), units=option.get("units"), origin=origin, status="RUNNING",
                  phase="submitting", requested_ms=(time.time() - self.start) * 1000)
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
                else:
                    result = self.deployments.start(option["group_id"], dict(expected_image=image), run_id=self.rid,
                                                    replica=option["action"] == "scale_out")
                    outcome.update(instance_id=result.get("id"), result=dict(state=result.get("state"),
                                   status=result.get("status"), reason=result.get("reason")))
                    if result.get("state") == "FAILED":
                        outcome.update(status="FAILED", reason="实例启动失败：" + str(result.get("reason")))
            except Exception as error:
                reason = type(error).__name__ + ": " + str(error)
                created = [item["id"] for item in self.deployments.live_instances(option["group_id"])
                           if item["id"] not in before] if starting else []
                if created:
                    # The record exists although submission raised; its own state decides the outcome.
                    outcome.update(instance_id=created[0], error=reason)
                else:
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
        if op["action"] != "scale_in" and op.get("mode") != "prewarm" and op.get("instance_id"):
            fact = self.deployments.instance_fact(op["instance_id"]) or {}
            if fact.get("held"):
                self._cleanup(op)
        if op["origin"] == "on_demand":
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
        roots = self.runtime.call("root_view", self.wid)
        unfinished = [task for task in self.tasks if task.deadline_status == "UNFINISHED"]
        if unfinished:
            self._credit("unfinished", self.config["workload"]["unfinished_reward"] * len(unfinished))
        if self.trajectory:
            self.trajectory[-1]["transition"].update(terminated=True, truncated=False, next_state=None,
                next_candidates=[], next_legal=[], elapsed_ms=max(0.0,
                    ((self.end_at or self.window_end) - self.start) * 1000 - self.trajectory[-1]["at_ms"]))
        outcomes = {op["id"]: dict(status=op["status"], reason=op.get("outcome_reason")) for op in self.ops}
        for stored in self.trajectory:
            if stored.get("op"):
                stored["outcome"] = outcomes[stored["op"]]
        failed = [task.task["id"] for task in self.tasks if task.status == "FAILED" and task.deadline_status != "UNFINISHED"]
        eligible = (not self.execution_error and not self.environment_changes and not self.missing_feedback
                    and not self.truncated and not failed and not self.unknown_outcomes
                    and not getattr(self, "release_left", {})
                    and self.closing in (None, "horizon"))
        exclusion = None if eligible else (
            "execution_error" if self.execution_error or failed else
            "runtime_environment_changed" if self.environment_changes else
            "resource_outcome_unknown:" + ",".join(self.unknown_outcomes) if self.unknown_outcomes else
            "release_unconfirmed" if getattr(self, "release_left", {}) else
            "feedback_unknown:" + ",".join(self.missing_feedback) if self.missing_feedback else
            "truncated:" + ",".join(self.truncated) if self.truncated else
            "window_closed:" + str(self.closing))
        span = max((self.window_end or time.time()) - self.start, 1e-9) if self.start else None
        task_items, online, evaluation = [], [], []
        for task in self.tasks:
            response = (task.done - task.arrived) * 1000 if task.done and task.arrived else None
            ledger = None
            if task.session is not None and task.arrived is not None:
                transitions = [dict(event=dict(representation_duration_ms=d["representation_ms"]),
                                    decision=dict(duration_ms=d["policy_ms"])) for d in task.decisions]
                ledger = episode_costs(task.session.context, self.manifest, response or 0.0, transitions,
                                       task.feedback, task.independent)
                online.extend(ledger["online"]["records"])
                evaluation.extend(ledger["evaluation"]["records"])
            task_items.append(dict(index=task.index, task_id=task.task["id"], context=task.context,
                arrival_offset_ms=task.offset_ms, status=task.status, deadline_status=task.deadline_status,
                credited=task.deadline_status != "UNFINISHED",
                arrived_ms=(task.arrived - self.start) * 1000 if task.arrived else None,
                done_ms=(task.done - self.start) * 1000 if task.done else None, response_ms=response,
                terminal_reason=(task.terminal or {}).get("reason"), final_response=(task.terminal or {}).get("response"),
                evidence=(task.terminal or {}).get("evidence", {}), program_metrics=task.program,
                evaluation_metrics=(task.terminal or {}).get("evaluation_metrics", {}),
                feedback=task.feedback, independent_feedback=task.independent, error=task.error,
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
            semantics="task_seconds integrates arrived unfinished tasks until every task ends; instance_seconds "
                      "integrates held instances (UNDEPLOYED/STARTING/READY/DRAINING/unreleased) as a reservation "
                      "proxy, not measured memory, until the common end point after release (end_ms), of which "
                      "release_tail_seconds falls after the last event; parallelism counts ACTIVE root requests "
                      "executing at once over span_ms, UNKNOWN roots are reported apart; not the instance count")
        return dict(kind="joint_window", episode=self.number, window_id=self.wid,
            task_ids=[task.task["id"] for task in self.tasks], tasks=task_items, decisions=self.records,
            counters=dict(sorted(self.counters.items())), transitions=self.trajectory,
            resource_ops=self.ops, unknown_outcomes=self.unknown_outcomes, on_demand=self.on_demand, roots=roots,
            reward=window_return if eligible else None, window_return=window_return,
            reward_components=dict(credited=components, unattributed=self.unattributed, timing="joint_window/1",
                                   semantics="window return = credited + unattributed; only credited parts reach "
                                             "learned transitions"),
            eligible=eligible, exclusion_reason=exclusion, closing=self.closing,
            execution_error=self.execution_error, environment_changes=self.environment_changes,
            setup=self.setup, release=dict(groups=self.release, release_ms=getattr(self, "release_ms", None),
                                           left=getattr(self, "release_left", {})),
            parallelism=ledger, residency=self.residency,
            costs=dict(schema="joint-cost-ledger/1",
                online=dict(wall_ms=span * 1000 if span else 0.0,
                    representation_ms=sum(d.get("representation_ms", 0) for d in self.records),
                    policy_ms=sum(d.get("policy_ms", 0) for d in self.records),
                    records=online, usage=summarize_usage(online)),
                evaluation=dict(wall_ms=sum(1000 * ((t.feedback or {}).get("cost_duration", 0) +
                                                    (t.independent or {}).get("cost_duration", 0)) for t in self.tasks),
                                records=evaluation, usage=summarize_usage(evaluation)),
                interval=ledger, residency=self.residency),
            online_duration_ms=span * 1000 if span else 0.0, choice_interactions=len(self.trajectory),
            estimates={unit: dict(mean_ms=sum(v) / len(v), samples=len(v)) for unit, v in self.estimates.items()})


def run_windows(jobs, rid, run, folder, manifest, catalog, bound, learner):
    """Execute the run's windows; learning happens between windows, never inside one."""
    tasks = {task["id"]: task for task in manifest["scene"]["tasks"]}
    estimates, rewards = {}, []
    counts, totals, definitions = {}, {}, {}
    records = {"online": [], "evaluation": []}
    summary = run.setdefault("joint", dict(windows=0, tasks=0, unfinished_tasks=0, failed_tasks=0,
                                           max_active_roots=0, mean_active_roots=[], counters={}))
    for number in range(manifest["episodes"]):
        if jobs.stops[rid].is_set() or jobs.closed:
            break
        window = Window(jobs, rid, manifest, catalog, bound, learner,
                        number, [tasks[t] for t in window_tasks(manifest, number)], estimates)
        item = window.run()
        stats = {}
        if learner and manifest["mode"] != "freeze" and item["eligible"]:
            for stored in item["transitions"]:
                learner.observe(stored["transition"])
            started = time.perf_counter()
            stats = learner.update()
            run["gradient_duration_ms"] = run.get("gradient_duration_ms", 0) + (time.perf_counter() - started) * 1000
        run["episodes"] += 1
        run["interactions"] += len(item["transitions"])
        run["choice_interactions"] += item["choice_interactions"]
        run["no_choice_episodes"] += not item["transitions"]
        run["eligible_episodes" if item["eligible"] else "excluded_episodes"] += 1
        if item["reward"] is not None:
            rewards.append(item["reward"])
        run["reward_mean"] = sum(rewards) / len(rewards) if rewards else None
        run["reward_denominator"] = len(rewards)
        for task in item["tasks"]:
            if not task["credited"]:
                continue
            for scope, feedback in (("training", task["feedback"]), ("independent", task["independent_feedback"])):
                for metric in (feedback or {}).get("metrics", []):
                    key = (scope, metric["id"], metric.get("version"), metric.get("unit"), metric.get("direction"),
                           metric.get("level", "system"), metric.get("unit_id"))
                    counts.setdefault(key, {})
                    counts[key][metric["status"]] = counts[key].get(metric["status"], 0) + 1
                    definitions[key] = metric.get("definition")
                    if metric["status"] == "KNOWN":
                        totals.setdefault(key, []).append(metric["value"])
        population = summary["tasks"] + len(item["tasks"])
        run["metrics"] = [dict(scope=k[0], id=k[1], version=k[2], unit=k[3], direction=k[4], level=k[5], unit_id=k[6],
            definition=definitions[k], value=sum(totals[k]) / len(totals[k]) if totals.get(k) else None,
            denominator=len(totals.get(k, [])), population=population, status_counts=v) for k, v in counts.items()]
        summary.update(windows=summary["windows"] + 1, tasks=population,
            unfinished_tasks=summary["unfinished_tasks"] + sum(
                t["deadline_status"] == "UNFINISHED" or t["status"] == "UNFINISHED" for t in item["tasks"]),
            failed_tasks=summary["failed_tasks"] + sum(t["status"] == "FAILED" for t in item["tasks"]),
            max_active_roots=max(summary["max_active_roots"], item["parallelism"]["max_active_roots"]))
        summary["mean_active_roots"].append(item["parallelism"]["mean_active_roots"])
        for key, value in item["counters"].items():
            summary["counters"][key] = summary["counters"].get(key, 0) + value
        summary["explored"] = summary.get("explored", 0) + sum(t["explored"] for t in item["transitions"])
        costs = run.setdefault("costs", dict(online_wall_ms=0, evaluation_wall_ms=0, representation_ms=0, policy_ms=0,
            task_seconds=0.0, instance_seconds=0.0, release_tail_seconds=0.0,
            coverage="see per-window joint-cost-ledger/1 and per-task ledgers; instance_seconds includes the release tail"))
        costs["online_wall_ms"] += item["costs"]["online"]["wall_ms"]
        costs["evaluation_wall_ms"] += item["costs"]["evaluation"]["wall_ms"]
        costs["representation_ms"] += item["costs"]["online"]["representation_ms"]
        costs["policy_ms"] += item["costs"]["online"]["policy_ms"]
        costs["task_seconds"] += item["parallelism"]["task_seconds"]
        costs["instance_seconds"] += item["parallelism"]["instance_seconds"]
        costs["release_tail_seconds"] = costs.get("release_tail_seconds", 0.0) + item["parallelism"]["release_tail_seconds"]
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
            mean_active_roots=item["parallelism"]["mean_active_roots"], metrics=run["metrics"]))
        jobs.store.save("scheduler_run", rid, run)
        if item["execution_error"]:
            raise ValueError("窗口执行中断，已保留实际调用和费用材料：" + item["execution_error"]["message"])
        if item["environment_changes"]:
            raise ValueError("运行组执行条件在窗口中改变（实例失联、进程替换或响应未取得），已保留本窗口材料并中断运行")
        if item["release"]["left"]:
            raise ValueError("窗口末仍有未释放实例，下一窗口无法从冻结初态开始：" + str(item["release"]["left"]))
