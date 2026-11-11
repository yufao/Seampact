"""Event-driven dispatch of installed capability packages.

The catalogue and instance controller perform real execution and enforce their
contracts. This scheduler chooses only ready calls and idle-instance releases.
It does not implement scenes, simulate calls, or make up quality labels.
"""

import copy
import hashlib
import threading
import time
import uuid
from collections import Counter, deque
from pathlib import Path
from queue import Empty, SimpleQueue

from seampact.capability_contract import WORKER_PROFILE, digest
from seampact.scheduling.unified_objective import ResourceLedger, TaskLedger, freeze_objective, number

SCHEMA = "capability-scheduler/1"
FEATURE_SCHEMA = "capability-event-features/1"
OBSERVATION_DIM = 4
CANDIDATE_DIM = 12
DEFAULTS = dict(schema=SCHEMA, max_in_flight=4, idle_timeout_s=60.0,
                measurement_interval_s=1.0, objective=None, resource_envelopes={}, policy=dict(kind="rule"))


def freeze_config(body, *, verify_policy=True):
    if not isinstance(body, dict) or set(body) - set(DEFAULTS):
        raise ValueError("不支持的能力调度配置；旧资源偏好和预算格式不能用于新路径")
    value = dict(copy.deepcopy(DEFAULTS), **copy.deepcopy(body))
    if value["schema"] != SCHEMA:
        raise ValueError("旧调度格式不能用于能力执行")
    if type(value["max_in_flight"]) is not int or not 1 <= value["max_in_flight"] <= 1024:
        raise ValueError("max_in_flight 须为 1 至 1024 的整数")
    value["idle_timeout_s"] = number(value["idle_timeout_s"], "idle_timeout_s", positive=True)
    value["measurement_interval_s"] = number(value["measurement_interval_s"], "measurement_interval_s", positive=True)
    if not 0.1 <= value["measurement_interval_s"] <= 60:
        raise ValueError("measurement_interval_s 须在 0.1 至 60 秒间")
    value["objective"] = freeze_objective(value["objective"])
    policy = value["policy"]
    if not isinstance(policy, dict) or policy.get("kind") not in {"rule", "ppo"}:
        raise ValueError("policy.kind 须为 rule 或 ppo")
    if policy["kind"] == "rule" and set(policy) != {"kind"}:
        raise ValueError("规则策略不接受额外策略设置")
    if policy["kind"] == "ppo":
        if set(policy) - {"kind", "checkpoint", "checkpoint_sha256", "learner_config", "seed"} or not policy.get("checkpoint"):
            raise ValueError("PPO 须指定匹配新目标的 checkpoint，可选 learner_config 与 seed")
        checkpoint = Path(policy["checkpoint"])
        if verify_policy and not checkpoint.is_file():
            raise ValueError("PPO 检查点不存在")
        actual = hashlib.sha256(checkpoint.read_bytes()).hexdigest() if verify_policy else policy.get("checkpoint_sha256")
        if not isinstance(actual, str) or len(actual) != 64:
            raise ValueError("PPO 配置缺少冻结检查点身份")
        if policy.get("checkpoint_sha256", actual) != actual:
            raise ValueError("PPO 检查点文件身份已变化")
        policy["checkpoint_sha256"] = actual
        policy["checkpoint"] = str(checkpoint.resolve())
        policy["learner_config"] = copy.deepcopy(policy.get("learner_config", {}))
        policy["seed"] = policy.get("seed", 0)
    if not isinstance(value["resource_envelopes"], dict):
        raise ValueError("resource_envelopes 须为按运行组标识的对象")
    for group, envelope in value["resource_envelopes"].items():
        if not isinstance(envelope, dict) or set(envelope) != {"ram_bytes", "vram_bytes", "provenance"}:
            raise ValueError("每个预留包络须明确 ram_bytes、vram_bytes、provenance")
        if not isinstance(group, str) or not envelope["provenance"]:
            raise ValueError("预留包络须有运行组和来源")
        for metric in ("ram_bytes", "vram_bytes"):
            envelope[metric] = number(envelope[metric], metric)
    return value


def policy_config_id(config):
    """Checkpoint target identity excludes the checkpoint itself (no hash cycle)."""
    return digest({key: value for key, value in config.items() if key != "policy"})


def load_policy(config):
    if config["policy"]["kind"] == "rule":
        return RulePolicy()
    from seampact.scheduling.capability_ppo import CandidatePPO
    settings = config["policy"]
    checkpoint = Path(settings["checkpoint"])
    if hashlib.sha256(checkpoint.read_bytes()).hexdigest() != settings["checkpoint_sha256"]:
        raise ValueError("PPO 检查点文件身份已变化")
    policy = CandidatePPO(policy_config_id(config), config["objective"], config=settings["learner_config"],
                          seed=settings["seed"], training=False)
    policy.load(settings["checkpoint"])
    if hashlib.sha256(checkpoint.read_bytes()).hexdigest() != settings["checkpoint_sha256"]:
        raise ValueError("加载期间PPO检查点文件发生变化")
    return policy


class RulePolicy:
    """Deadline/FIFO dispatch, then release an idle instance whose timer expired."""

    def choose(self, observation, candidates):
        return dict(index=min(range(len(candidates)), key=lambda index: (
            candidates[index]["kind"] != "dispatch",
            candidates[index].get("deadline_wall", float("inf")),
            candidates[index].get("queued_wall", float("inf")),
            candidates[index]["id"])))


class CapabilityScheduler:
    def __init__(self, catalog, deployments, *, config=None, policy=None, store=None):
        self.catalog, self.deployments = catalog, deployments
        self.store = store if store is not None else getattr(catalog, "store", None)
        saved = self.store.get("capability_scheduler_config", "current") if self.store is not None else None
        self._config = freeze_config(config if config is not None else (saved or DEFAULTS), verify_policy=config is not None)
        self.policy_error = None
        try:
            self.policy = policy or load_policy(self._config)
        except Exception as error:
            if config is not None:
                raise
            # Preserve the configured identity and expose a recoverable failure;
            # never silently substitute another policy after a file disappears.
            self.policy, self.policy_error = None, str(error)
        self.condition = threading.Condition(threading.RLock())
        self.closed = False
        self.queue, self.running = {}, {}
        self.cancelled = set()
        self.owned, self.groups, self.pools, self.tracked = {}, set(), {}, set()
        self.decisions = deque(maxlen=100)
        self.counts = Counter()
        self.controller_error = None
        self._reset_ledger()
        self.instance_events = SimpleQueue()
        self.measure_stop, self.measure_wake = threading.Event(), threading.Event()
        self.thread = threading.Thread(target=self._controller, daemon=True, name="seampact-capability-scheduler")
        self.thread.start()
        self.measure_thread = threading.Thread(target=self._measure, daemon=True, name="seampact-capability-memory")
        self.measure_thread.start()

    @property
    def config(self):
        return copy.deepcopy(self._config)

    def _reset_ledger(self):
        self.session = uuid.uuid4().hex
        self.started_wall = time.time()
        self.tasks = TaskLedger(self._config["objective"])
        self.resources = ResourceLedger()
        self.measure_samples, self.last_samples, self.measurement_error = {}, [], None
        self.last_capture_s = 0.0
        self.config_id = digest(self._config)
        self.policy_config_id = policy_config_id(self._config)

    def configure(self, body):
        # A complete replacement avoids silently inheriting old objective terms.
        value = freeze_config(body)
        selected_policy = load_policy(value)
        with self.condition:
            self._resource_summary()
            held = self._has_held()
            if self.tasks.active or self.queue or self.running or held:
                raise ValueError("当前任务或实例尚未收尾，请排空释放后再修改冻结配置")
            if self.store is not None:
                self.store.save("capability_scheduler_session", self.session, self._status_locked())
                self.store.save("capability_scheduler_config", "current", value)
            self._config = value
            self.policy = selected_policy
            self.policy_error = None
            self.groups.clear()
            self.pools.clear()
            self.cancelled.clear()
            self.owned.clear()
            self.tracked.clear()
            self.decisions.clear()
            self.counts.clear()
            self._reset_ledger()
            self.condition.notify_all()
            self.measure_wake.set()
            return self._status_locked()

    def _pool(self):
        groups = [group for group in self.store.list("runtime_group")
                  if group.get("kind") == "capability-package" and not group.get("detached")] if self.store else []
        return {group["id"]: dict(package_digest=group["package_digest"],
                                  contract_digest=group["contract"]["digest"],
                                  installation_id=group["installation_id"]) for group in groups}

    def begin(self, task_id, *, deadline_s=None, metadata=None):
        with self.condition:
            if self.closed:
                raise RuntimeError("能力调度已关闭")
            if self.store and self.store.get("capability_task_result", task_id):
                raise ValueError("任务身份已完成，不能重复计入到达分母")
            pool = self._pool()
            row = self.tasks.begin(task_id, deadline_s=deadline_s, metadata=metadata)
            self.pools[task_id] = pool
            row.update(config_id=self.config_id, pool_id=digest(pool))
            self.condition.notify_all()
            return row

    def finish(self, task_id, *, delivered, quality=None, status="COMPLETED", metrics=None):
        with self.condition:
            row = self.tasks.finish(task_id, delivered=delivered, quality=quality, status=status, metrics=metrics)
            pool = self.pools.pop(task_id, {})
            row.update(config_id=self.config_id, pool_id=digest(pool), session=self.session)
            if self.store:
                self.store.save("capability_task_result", task_id, row)
            self.deployments.release_task(task_id)
            self.cancelled.discard(task_id)
            self.condition.notify_all()
            return row

    def invoke(self, unit_id, arguments, on_event=None, run_id=None):
        self.catalog.validate_input(unit_id, arguments)
        unit = self.catalog.unit(unit_id)
        group_id = unit["runtime_group"]
        # Identity is checked both before queueing and immediately before execution.
        group = self.catalog._group(group_id)
        identity = dict(package_digest=group["package_digest"], contract_digest=group["contract"]["digest"],
                        installation_id=group["installation_id"])
        with self.condition:
            if self.closed:
                raise RuntimeError("能力调度已关闭")
            if run_id in self.cancelled:
                raise RuntimeError("任务已取消，能力未执行")
            if run_id and self.store and self.store.get("capability_task_result", run_id):
                raise RuntimeError("任务已结束，不能再发起能力调用")
            frozen_pool = self.pools.get(run_id)
            if frozen_pool is not None and frozen_pool.get(group_id) != identity:
                raise ValueError("能力池与任务开始时的冻结身份不同，请创建新任务")
            call_id = uuid.uuid4().hex
            task = self.tasks.active.get(run_id, {})
            deadline = task.get("deadline_s")
            elapsed = self.tasks.clock() - task["arrived_s"] if task else 0
            request = dict(id=call_id, unit_id=unit_id, group_id=group_id, run_id=run_id,
                           execution_run_id=run_id or "capability-call:" + call_id,
                           identity=identity, queued_wall=time.time(), granted=threading.Event(), error=None,
                           concurrency=group["contract"]["concurrency"])
            if deadline is not None:
                request["deadline_wall"] = time.time() + deadline - elapsed
            self.groups.add(group_id)
            self.queue[call_id] = request
            self.counts["calls_arrived"] += 1
            self.condition.notify_all()
            self.measure_wake.set()
        request["granted"].wait()
        if request["error"] is not None:
            raise request["error"]
        def observed(event):
            if event.get("instance_id"):
                with self.condition:
                    identity = event["instance_id"]
                    self.tracked.add(identity)
                    with self.deployments.lock:
                        instance = self.deployments.instances.get(identity) or {}
                        if (instance.get("run_id") == request["execution_run_id"] and
                                (instance.get("start_wall") or 0) >= request["queued_wall"]):
                            self.owned[identity] = dict(group_id=group_id, run_id=request["execution_run_id"])
            if on_event is not None:
                on_event(event)

        try:
            with self.condition:
                if run_id in self.cancelled:
                    raise RuntimeError("任务已取消，已获准但尚未开始的能力未执行")
            current = self.catalog._group(group_id)
            actual = dict(package_digest=current["package_digest"], contract_digest=current["contract"]["digest"],
                          installation_id=current["installation_id"])
            if actual != identity:
                raise ValueError("排队期间能力版本或绑定变化，未执行调用")
            # Execute on the caller's thread: preserve task context and callback ordering.
            return self.catalog.invoke(unit_id, arguments, on_event=observed, run_id=request["execution_run_id"])
        finally:
            with self.condition:
                self.running.pop(call_id, None)
                self.counts["calls_finished"] += 1
                self.condition.notify_all()

    def record_resources(self, records):
        """Accept attributed measured intervals; the caller supplies real evidence."""
        with self.condition:
            for row in records:
                identity = row.get("instance_id")
                with self.deployments.lock:
                    instance = self.deployments.instances.get(identity)
                    if (not instance or identity not in self.tracked
                            or instance.get("generation") != row.get("generation")):
                        raise ValueError("资源区间不属于本次活动实例代次")
                    start = max(self.started_wall, instance.get("start_wall") or self.started_wall)
                    end = instance.get("stopped_wall") if instance.get("released") is True else time.time()
                if row.get("start_s", -1) < start or row.get("end_s", float("inf")) > end:
                    raise ValueError("资源区间超出本次实例持有范围")
                record_id = digest([self.session, row])
                if self.store and self.store.get("capability_resource_interval", record_id):
                    continue
                self.resources.record([row])
                if self.store:
                    self.store.save("capability_resource_interval", digest([self.session, row]),
                                    dict(row, session=self.session, config_id=self.config_id))

    def _measure(self):
        from seampact.scheduling.capability_measurement import sample_instances
        while not self.measure_stop.is_set():
            identities, interval = set(), self._config["measurement_interval_s"]
            try:
                with self.condition:
                    self._consume_instance_events()
                    identities, session = set(self.tracked), self.session
                    interval = self._config["measurement_interval_s"]
                    due = time.monotonic() - self.last_capture_s >= interval
                samples = sample_instances(self.deployments, identities) if identities and due else []
                with self.condition:
                    if self.closed:
                        return
                    if session != self.session:
                        continue
                    intervals = []
                    for sample in samples:
                        if sample["instance_id"] not in self.tracked:
                            continue
                        key = (sample["instance_id"], sample["generation"])
                        previous = self.measure_samples.get(key)
                        if (previous and sample["identity"] is not None and sample["identity"] == previous["identity"]
                                and 0 < sample["at_s"] - previous["at_s"] <= 2.5 * interval):
                            seconds = sample["at_s"] - previous["at_s"]
                            row = dict(source="measured", attribution="instance", instance_id=key[0], generation=key[1],
                                start_s=previous["at_s"], end_s=sample["at_s"],
                                provenance="adjacent verified container cgroup RAM and container-host-PID GPU samples")
                            for field, metric in (("ram_bytes", "ram_byte_s"), ("vram_bytes", "vram_byte_s")):
                                a, b = previous[field], sample[field]
                                row[metric] = (a + b) * seconds / 2 if a is not None and b is not None else None
                            intervals.append(row)
                        self.measure_samples[key] = sample
                    if intervals:
                        self.record_resources(intervals)
                    if identities and due:
                        self.last_capture_s = time.monotonic()
                    if samples:
                        self.last_samples = samples[-100:]
                    self.counts["resource_samples"] += len(samples)
                    self.measurement_error = None
                    self._resource_summary()
            except Exception as error:
                with self.condition:
                    self.measurement_error = str(error)
            self.measure_wake.wait(max(0.001, interval - (time.monotonic() - self.last_capture_s))
                                   if identities and self.last_capture_s else interval)
            self.measure_wake.clear()

    def instance_event(self, event):
        """Controller callback: queue only; never take scheduler locks here."""
        if event.get("profile") == WORKER_PROFILE and not self.closed:
            self.instance_events.put(copy.deepcopy(event))
            self.measure_wake.set()

    def _consume_instance_events(self):
        changed = False
        while True:
            try:
                event = self.instance_events.get_nowait()
            except Empty:
                break
            if event["group_id"] not in self.groups:
                continue
            identity = event["instance_id"]
            if self.store and self.store.get("capability_resource_final", digest([self.session, identity, event["generation"]])):
                continue
            self.tracked.add(identity)
            if event["kind"] == "start_requested" and any(
                    request["execution_run_id"] == event.get("run_id") for request in self.running.values()):
                self.owned[identity] = dict(group_id=event["group_id"], run_id=event.get("run_id"))
            changed = True
        if changed:
            self.condition.notify_all()

    def cancel(self, task_id):
        """Cancel undispatched calls only; in-flight cancellation uses call IDs."""
        with self.condition:
            self.cancelled.add(task_id)
            pending = [key for key, request in self.queue.items() if request["run_id"] == task_id]
            for key in pending:
                request = self.queue.pop(key)
                request["error"] = RuntimeError("任务已取消，排队能力未执行")
                request["granted"].set()
            self.condition.notify_all()
            return dict(queued_cancelled=len(pending),
                        in_flight=sum(request["run_id"] == task_id for request in self.running.values()),
                        upstream_stop_confirmed=False)

    def _candidates(self):
        now = time.time()
        busy = Counter(item["group_id"] for item in self.running.values())
        candidates, next_timer = [], None
        if len(self.running) < self._config["max_in_flight"]:
            for request in self.queue.values():
                if busy[request["group_id"]] >= request["concurrency"]:
                    continue
                fact = self.deployments.runtime_fact(request["group_id"], request["unit_id"])
                # A single cold start owns this group's start handshake.
                if busy[request["group_id"]] and not fact["ready_instance"]:
                    continue
                candidates.append(dict(kind="dispatch", **{key: request[key] for key in
                    ("id", "group_id", "unit_id", "queued_wall")},
                    **({"deadline_wall": request["deadline_wall"]} if "deadline_wall" in request else {}), fact=fact))
        queued_groups = {item["group_id"] for item in self.queue.values()} | set(busy)
        for instance_id, ownership in list(self.owned.items()):
            group_id = ownership["group_id"]
            with self.deployments.lock:
                instance = copy.deepcopy(self.deployments.instances.get(instance_id))
            if instance is None or instance.get("released") is True:
                del self.owned[instance_id]
                continue
            if group_id in queued_groups or instance.get("state") != "READY" or instance.get("in_flight"):
                continue
            # Respect owner leases established by other runtime consumers.
            with self.deployments.lock:
                leased = instance_id in self.deployments.affinity.values()
            if leased or instance.get("pending_stop"):
                continue
            due = instance.get("last_activity_wall", instance.get("ready_wall", now)) + self._config["idle_timeout_s"]
            if due <= now:
                candidates.append(dict(kind="drain_stop", id="release:" + instance_id,
                                       group_id=group_id, instance_id=instance_id, run_id=ownership["run_id"]))
            else:
                next_timer = due if next_timer is None else min(next_timer, due)
        observation = [float(len(self.queue)), float(len(self.running)), float(len(self.owned)),
                       float(self._config["max_in_flight"])]
        for item in candidates:
            fact = item.get("fact", {})
            envelope = self._config["resource_envelopes"].get(item["group_id"], {})
            item["features"] = [float(item["kind"] == "dispatch"), float(item["kind"] == "drain_stop"),
                max(0.0, now - item.get("queued_wall", now)),
                max(-3600.0, min(3600.0, item.get("deadline_wall", now + 3600) - now)),
                float(bool(fact.get("ready_instance"))), float(bool(fact.get("warm"))),
                float(busy[item["group_id"]]), float(envelope.get("ram_bytes", 0)) / 2**30,
                float(envelope.get("ram_bytes") is not None), float(envelope.get("vram_bytes", 0)) / 2**30,
                float(envelope.get("vram_bytes") is not None), float(bool(fact.get("cold_startable")))]
        return observation, candidates, next_timer

    def _controller(self):
        while True:
            action = None
            try:
                with self.condition:
                    if self.closed:
                        return
                    observation, candidates, timer = self._candidates()
                    if not candidates:
                        self.condition.wait(None if timer is None else max(0.001, timer - time.time()))
                        continue
                    if self.policy is None:
                        raise RuntimeError("冻结调度策略不可用：" + str(self.policy_error))
                    # There is no policy invocation or learning record without a choice.
                    selection = self.policy.choose(observation, copy.deepcopy(candidates)) if len(candidates) > 1 else dict(index=0)
                    index = selection.get("index")
                    if type(index) is not int or not 0 <= index < len(candidates):
                        raise ValueError("策略返回了候选集合之外的动作")
                    action = candidates[index]
                    self.counts["decisions" if len(candidates) > 1 else "forced_actions"] += 1
                    if len(candidates) > 1:
                        decision = dict(id=uuid.uuid4().hex, at=time.time(), config_id=self.config_id,
                            policy_config_id=self.policy_config_id,
                            feature_schema=FEATURE_SCHEMA, observation=observation, candidates=candidates,
                            selection=selection, session=self.session)
                        self.decisions.append(decision)
                        if self.store:
                            self.store.save("capability_decision", decision["id"], decision)
                    if action["kind"] == "dispatch":
                        request = self.queue.pop(action["id"])
                        self.running[action["id"]] = request
                        request["granted"].set()
                        action = None
                    self.controller_error = None
                if action is not None:
                    # The controller rechecks intake/in-flight state before actual close.
                    self.deployments.drain_stop(action["group_id"], dict(instance_id=action["instance_id"]),
                                                run_id=action["run_id"], reason=SCHEMA + ":idle")
                    with self.condition:
                        self.counts["idle_drains"] += 1
            except Exception as error:
                with self.condition:
                    self.controller_error = str(error)
                    # A failed decision cannot leave callers waiting forever.
                    for request in self.queue.values():
                        request["error"] = RuntimeError("能力调度失败：" + str(error))
                        request["granted"].set()
                    self.queue.clear()
                    if action and action.get("kind") == "drain_stop":
                        self.owned.pop(action["instance_id"], None)
                    self.condition.wait(1.0)

    def _resource_summary(self):
        end = time.time()
        lifetimes = []
        # Read exact current-session identities, not the controller's entire
        # terminal history or call-event index on every state refresh.
        with self.deployments.lock:
            for identity in list(self.tracked):
                instance = self.deployments.instances.get(identity)
                if not instance:
                    continue
                start = max(self.started_wall, instance.get("start_wall") or instance.get("ready_wall") or self.started_wall)
                released = instance.get("released") is True or not self.deployments._held(instance)
                finish = min(end, instance.get("stopped_wall") or end) if released else end
                lifetime = dict(instance_id=identity, generation=instance["generation"],
                    group_id=instance["group_id"], released=released,
                    resident_ms=max(0.0, finish - start) * 1000, start_s=start, end_s=finish,
                    start_observed=instance.get("start_wall") is not None,
                    end_observed=instance.get("state") != "UNKNOWN")
                if lifetime["released"]:
                    key = digest([self.session, identity, lifetime["generation"]])
                    if self.store and self.store.get("capability_resource_final", key):
                        self.tracked.remove(identity)
                        self.owned.pop(identity, None)
                        self.measure_samples.pop((identity, lifetime["generation"]), None)
                        continue
                    frozen = self.resources.freeze(lifetime, self._config["resource_envelopes"],
                                                    start_s=self.started_wall, end_s=end)
                    if self.store:
                        self.store.save("capability_resource_final", key,
                            dict(frozen, lifetime=lifetime, session=self.session, config_id=self.config_id))
                    self.tracked.remove(identity)
                    self.owned.pop(identity, None)
                    self.measure_samples.pop((identity, lifetime["generation"]), None)
                else:
                    lifetimes.append(lifetime)
        # Only actual controller-held instances count. Failed known releases have zero tail.
        return self.resources.summary(lifetimes, self._config["resource_envelopes"],
                                      start_s=self.started_wall, end_s=end)

    def _has_held(self):
        with self.deployments.lock:
            return any((self.deployments.instances.get(identity) or {}).get("released") is not True
                       for identity in self.tracked)

    def _status_locked(self):
        self._consume_instance_events()
        objective = self.tasks.summary(self._resource_summary())
        objective.update(config_id=self.config_id, policy_config_id=self.policy_config_id, session=self.session)
        # A workload is only final after all owned resources actually release.
        release_pending = self._has_held()
        if release_pending or self.running or self.queue:
            objective.update(training_ready=False, reason="资源仍驻留，释放尾段尚未完成；当前效用仅为观测终点快照")
        return dict(schema=SCHEMA, session=self.session, config=self.config, config_id=self.config_id,
                    policy_config_id=self.policy_config_id,
                    feature_schema=FEATURE_SCHEMA, policy=type(self.policy).__name__,
                    policy_error=self.policy_error,
                    active_tasks=len(self.tasks.active), queued_calls=len(self.queue), running_calls=len(self.running),
                    owned_instances=len(self.owned), counts=dict(self.counts), controller_error=self.controller_error,
                    objective=objective, recent_decisions=copy.deepcopy(list(self.decisions)),
                    measurement=dict(interval_s=self._config["measurement_interval_s"],
                        error=self.measurement_error, last_samples=copy.deepcopy(self.last_samples),
                        semantics="sampled attributed memory; uncovered startup/release edges stay unknown"),
                    idle_timeout_basis="operational default; configurable, not an experiment setting",
                    limitations=["质量由显式任务评价器提供；聊天未评分时质量未知",
                                 "资源范围为受管能力实例；外部模型费用和设备总量不自动归属",
                                 "自动实测支持本会话Docker私有cgroup与已验证GPU进程；其他后端保留未知",
                                 "实测积分需覆盖整个持有区间；部分采样另报积分与覆盖率，声明容量仅作预留代理"])

    def status(self):
        with self.condition:
            return self._status_locked()

    def close(self):
        with self.condition:
            self.closed = True
            self.measure_stop.set()
            self.measure_wake.set()
            for request in self.queue.values():
                request["error"] = RuntimeError("能力调度已关闭，排队调用未执行")
                request["granted"].set()
            self.queue.clear()
            self.condition.notify_all()
        self.thread.join(timeout=2.0)
        self.measure_thread.join(timeout=2.0)
