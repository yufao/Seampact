"""Event-driven dispatch of installed capability packages.

The catalogue and instance controller perform real execution and enforce their
contracts. This scheduler chooses only ready calls and idle-instance releases.
It does not implement scenes, simulate calls, or make up quality labels.
"""

import copy
from bisect import bisect_right
import hashlib
import math
import threading
import time
import uuid
from collections import Counter, deque
from pathlib import Path
from queue import Empty, SimpleQueue

from seampact.capability_contract import WORKER_PROFILE, digest
from seampact.scheduling.capability_perception import GroupTiming, FEATURE_DIM as TIMING_DIM, features as timing_features
from seampact.scheduling.unified_objective import ResourceLedger, TaskLedger, freeze_objective, number

SCHEMA = "capability-scheduler/1"
FEATURE_SCHEMA = "capability-event-features/7"
HOOK_FAMILIES = ("fact_support", "injection", "privacy", "secrets", "code_safety",
                 "input_safety", "output_safety", "sanitize")
OPERATION_EFFECTS = ("read", "write", "send", "execute", "other")
OPERATION_DESTINATIONS = ("local", "external", "user", "unknown")
OPERATION_PHASES = ("input", "pending", "executed", "delivery")
OBSERVATION_DIM = 14 + 2 * 5 + 2 + len(OPERATION_EFFECTS) + 1 + len(OPERATION_DESTINATIONS) + 1 + len(OPERATION_PHASES) + 1
CANDIDATE_DIM = 19 + len(HOOK_FAMILIES) + 1 + 2 + 4 + 2 + 6 + TIMING_DIM
DEFAULTS = dict(schema=SCHEMA, max_in_flight=4, idle_timeout_s=60.0,
                measurement_interval_s=1.0, retention_recheck_s=None,
                elastic_actions=False, max_replicas_per_group=2,
                vram_source="nvml_process",
                objective=None, resource_envelopes={}, policy=dict(kind="rule"))


def freeze_config(body, *, verify_policy=True):
    if not isinstance(body, dict) or set(body) - set(DEFAULTS):
        raise ValueError("不支持的能力调度配置；旧资源偏好和预算格式不能用于新路径")
    value = dict(copy.deepcopy(DEFAULTS), **copy.deepcopy(body))
    if value["schema"] != SCHEMA:
        raise ValueError("旧调度格式不能用于能力执行")
    if type(value["max_in_flight"]) is not int or not 1 <= value["max_in_flight"] <= 1024:
        raise ValueError("max_in_flight 须为 1 至 1024 的整数")
    if type(value["max_replicas_per_group"]) is not int or not 1 <= value["max_replicas_per_group"] <= 16:
        raise ValueError("max_replicas_per_group 须为 1 至 16 的整数")
    if type(value["elastic_actions"]) is not bool:
        raise ValueError("elastic_actions 须明确为布尔值")
    value["idle_timeout_s"] = number(value["idle_timeout_s"], "idle_timeout_s", positive=True)
    value["retention_recheck_s"] = number(value["retention_recheck_s"] if value["retention_recheck_s"] is not None
                                           else value["idle_timeout_s"], "retention_recheck_s", positive=True)
    value["measurement_interval_s"] = number(value["measurement_interval_s"], "measurement_interval_s", positive=True)
    if not 0.1 <= value["measurement_interval_s"] <= 60:
        raise ValueError("measurement_interval_s 须在 0.1 至 60 秒间")
    value["objective"] = freeze_objective(value["objective"])
    if value["vram_source"] not in {"nvml_process", "torch_allocator_reserved"}:
        raise ValueError("vram_source 须显式选择 nvml_process 或 torch_allocator_reserved")
    policy = value["policy"]
    if not isinstance(policy, dict) or policy.get("kind") not in {"rule", "ppo"}:
        raise ValueError("policy.kind 须为 rule 或 ppo")
    if policy["kind"] == "rule":
        if set(policy) - {"kind", "optional_hooks", "retention", "prefer_warm", "scale_when_saturated"}:
            raise ValueError("未知规则策略设置")
        optional = policy.get("optional_hooks", "all")
        if optional not in ("all", "none"):
            if (not isinstance(optional, dict) or any(not isinstance(key, str) or not isinstance(names, list)
                    or any(not isinstance(name, str) or not name for name in names) or len(names) != len(set(names))
                    for key, names in optional.items())):
                raise ValueError("optional_hooks 须为 all、none 或按事件列出的有序工具名")
        if policy.get("retention", "ttl") not in {"ttl", "release", "keep"}:
            raise ValueError("retention 须为 ttl、release 或 keep")
        if type(policy.get("prefer_warm", False)) is not bool:
            raise ValueError("prefer_warm 须为布尔值")
        if type(policy.get("scale_when_saturated", False)) is not bool:
            raise ValueError("scale_when_saturated 须为布尔值")
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
        return RulePolicy(config["policy"])
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
    """Explicit optional-hook choices and retention over the same legal actions."""

    def __init__(self, config=None):
        self.config = copy.deepcopy(config or dict(kind="rule"))

    def choose(self, observation, candidates):
        allowed = self.config.get("optional_hooks", "all")
        hooks = [(index, item) for index, item in enumerate(candidates) if item["kind"] == "run_hook"]
        if hooks:
            selected = [(index, item) for index, item in hooks if allowed == "all" or
                        isinstance(allowed, dict) and item["hook"] in allowed.get(item.get("event"), [])]
            if selected:
                return dict(index=min(selected, key=lambda pair: (
                    -int(bool((pair[1].get("fact") or {}).get("warm"))) if self.config.get("prefer_warm") else 0,
                    allowed[pair[1]["event"]].index(pair[1]["hook"]) if isinstance(allowed, dict) else 0,
                    pair[1]["id"]))[0])
            return dict(index=next(index for index, item in enumerate(candidates) if item["kind"] == "continue"))
        dispatch = [(index, item) for index, item in enumerate(candidates) if item["kind"] == "dispatch"]
        if dispatch:
            return dict(index=min(dispatch, key=lambda pair: (pair[1].get("deadline_wall", float("inf")),
                pair[1].get("queued_wall", float("inf")), pair[1]["id"]))[0])
        replicas = [index for index, item in enumerate(candidates) if item["kind"] == "add_replica"]
        if self.config.get("scale_when_saturated", False) and replicas:
            return dict(index=replicas[0])
        keep = self.config.get("retention", "ttl")
        preferred = [index for index, item in enumerate(candidates) if
                     item["kind"] == ("retain" if keep == "keep" or
                         keep == "ttl" and item.get("idle_s", 0) < item.get("idle_timeout_s", 0) else "drain_stop")]
        if not preferred:
            preferred = [index for index, item in enumerate(candidates) if item["kind"] == "wait"]
        return dict(index=(preferred or list(range(len(candidates))))[0])


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
        self.groups.clear()
        self.pools.clear()
        self.cancelled.clear()
        self.owned.clear()
        self.tracked.clear()
        self.decisions.clear()
        self.counts.clear()
        self.session = uuid.uuid4().hex
        self.started_wall = time.time()
        self.tasks = TaskLedger(self._config["objective"])
        self.resources = ResourceLedger()
        self.measure_samples, self.last_samples, self.measurement_error = {}, [], None
        self.metric_samples = {}
        self.last_capture_s = 0.0
        self.config_id = digest(self._config)
        self.policy_config_id = policy_config_id(self._config)
        self.workload = None
        self.decision_sequence = 0
        self.workload_decision_ids, self.workload_memory_decisions = [], []
        self.workload_results = {}
        self.interval_decision_offset, self.resource_cutoffs = 0, {}
        self.retention_due = {}
        self.elastic_active, self.elastic_runs = {}, Counter()
        self.elastic_attempted, self.elastic_waited = set(), {}
        self.force_drain = False
        # Only this session's completed, attributed calls; no host-level totals.
        self.recent_call_costs = {}
        self.group_timing = GroupTiming()
        self.live_instance_ids = {}

    def configure(self, body):
        # A complete replacement avoids silently inheriting old objective terms.
        value = freeze_config(body)
        selected_policy = load_policy(value)
        with self.condition:
            self._resource_summary()
            held = self._has_held()
            if (self.tasks.active or self.queue or self.running or self.elastic_active or held or
                    self.workload and self.workload["status"] in {"RUNNING", "DRAINING"}):
                raise ValueError("当前任务或实例尚未收尾，请排空释放后再修改冻结配置")
            if self.store is not None:
                self.store.save("capability_scheduler_session", self.session, self._status_locked())
                self.store.save("capability_scheduler_config", "current", value)
            self._config = value
            self.policy = selected_policy
            self.policy_error = None
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

    def _observation(self, event, context=None):
        context = context or {}
        risk = number(context.get("risk_signal", 0), "risk_signal")
        if risk > 1:
            raise ValueError("risk_signal 须在 [0,1]，且来自已执行检查而不是金标")
        def boolean(key):
            value = context.get(key)
            return [float(value is True), float(type(value) is bool)]
        def category(key, choices):
            value = context.get(key)
            return [*[float(value == choice) for choice in choices], float(value in choices)]
        remaining = context.get("revision_remaining")
        return [float(len(self.queue)), float(len(self.running)), float(len(self.owned)),
                float(self._config["max_in_flight"]),
                *[float(event == stage) for stage in ("input_received", "before_tool", "after_tool", "before_delivery")],
                math.log1p(number(context.get("text_chars", 0), "text_chars")),
                *[float(bool(context.get(key))) for key in ("has_code", "has_evidence", "external_material")],
                number(context.get("checks_run", 0), "checks_run"), risk,
                *[value for key in ("linked_receipt", "linked_source", "missing_evidence", "finding_valid", "evidence_partial")
                  for value in boolean(key)],
                number(remaining, "revision_remaining") if remaining is not None else 0.0,
                float(remaining is not None),
                *category("operation_effect", OPERATION_EFFECTS),
                *category("operation_destination", OPERATION_DESTINATIONS),
                *category("operation_phase", OPERATION_PHASES)]

    def _features(self, item, *, now=None, monotonic_s=None, group_context=None):
        now = time.time() if now is None else now
        monotonic_s = time.monotonic() if monotonic_s is None else monotonic_s
        fact = item.get("fact", {})
        envelope = self._config["resource_envelopes"].get(item.get("group_id"), {})
        family = item.get("hook_family")
        if group_context is None:
            group_context = self._group_contexts([item], now, monotonic_s)[item.get("group_id")]
        observed_cost = group_context["costs"].get(item.get("unit_id"))
        ram, vram = self._candidate_memory(item, now, instances=group_context["instances"])
        deadline = item.get("deadline_wall")
        remaining = deadline - now if deadline is not None else 0.0
        perception = group_context["perception"]
        item["perception"] = perception
        return [float(item["kind"] == "dispatch"), float(item["kind"] == "drain_stop"),
            math.log1p(max(0.0, now - item.get("queued_wall", now))),
            math.copysign(math.log1p(abs(remaining)), remaining),
            float(bool(fact.get("ready_instance"))), float(bool(fact.get("warm"))),
            float(group_context["running"]),
            float(envelope.get("ram_bytes", 0)) / 2**30, float(envelope.get("ram_bytes") is not None),
            float(envelope.get("vram_bytes", 0)) / 2**30, float(envelope.get("vram_bytes") is not None),
            float(bool(fact.get("cold_startable"))),
            float(item["kind"] == "retain"), float(item["kind"] == "run_hook"),
            float(item["kind"] == "continue"), float(deadline is not None),
            math.log1p(item.get("idle_s", 0)), math.log1p(item.get("estimated_cost_s", 0)),
            float(item.get("estimated_cost_s") is not None),
            *[float(family == kind) for kind in HOOK_FAMILIES], float(family in HOOK_FAMILIES),
            math.log1p(observed_cost) if observed_cost is not None else 0.0, float(observed_cost is not None),
            (ram / 2**30 if ram is not None else 0.0), float(ram is not None),
            (vram / 2**30 if vram is not None else 0.0), float(vram is not None),
            float(item["kind"] == "prewarm"), float(item["kind"] == "add_replica"), float(item["kind"] == "wait"),
            float(item.get("ready_replicas", 0)), math.log1p(item.get("queue_depth", 0)),
            float(self._config["max_replicas_per_group"]),
            float("family_finding_valid" in item), float(item.get("family_finding_valid") is True),
            *group_context["timing_features"]]

    def _live_instances(self, group_ids):
        """Discover a group once, then read its live IDs without terminal history.

        Lifecycle callbacks update the index. Only scalars used by decisions
        leave the deployment lock; handles, logs and resource histories stay put.
        """
        group_ids = {identity for identity in group_ids if identity}
        index = getattr(self, "live_instance_ids", None)
        # Small __new__ test fixtures do not install controller callbacks.
        index = {} if index is None else index
        with self.deployments.lock:
            unseen = group_ids - index.keys()
            if unseen:
                index.update({identity: set() for identity in unseen})
                for record in self.deployments.instances.values():
                    if (record.get("group_id") in unseen and record.get("released") is not True
                            and record.get("state") not in {"STOPPED", "FAILED"}):
                        index[record["group_id"]].add(record["id"])
            snapshots = []
            fields = ("id", "group_id", "generation", "state", "released", "accepting", "in_flight",
                      "start_wall", "ready_wall", "last_activity_wall", "contract_digest")
            for group_id in group_ids:
                for identity in list(index[group_id]):
                    record = self.deployments.instances.get(identity)
                    if (not record or record.get("group_id") != group_id or record.get("released") is True
                            or record.get("state") in {"STOPPED", "FAILED"}):
                        index[group_id].discard(identity)
                        continue
                    snapshots.append(dict({key: record[key] for key in fields if key in record},
                        units=tuple(record.get("units") or ()), pending_stop=bool(record.get("pending_stop")),
                        container_id=(record.get("handle") or {}).get("container_id")))
            return snapshots

    def _group_contexts(self, candidates, wall, monotonic_s, *, instances=None):
        group_ids = {item.get("group_id") for item in candidates}
        groups = {group_id: self.catalog._group(group_id) for group_id in group_ids if group_id}
        if instances is None:
            instances = self._live_instances(group_ids)
        queued, running, live = {}, {}, {}
        for records, target in ((self.queue.values(), queued), (self.running.values(), running), (instances, live)):
            for record in records:
                if record.get("group_id") in group_ids:
                    target.setdefault(record.get("group_id"), []).append(record)
        contexts = {}
        for group_id in group_ids:
            group = groups.get(group_id)
            identity = (dict(package_digest=group["package_digest"], contract_digest=group["contract"]["digest"],
                             installation_id=group["installation_id"]) if group else None)
            perception = self.group_timing.snapshot(group_id, identity, now=monotonic_s, wall=wall,
                queue=queued.get(group_id, ()), running=running.get(group_id, ()), instances=live.get(group_id, ()))
            contexts[group_id] = dict(perception=perception, timing_features=timing_features(perception),
                instances=live.get(group_id, ()), running=len(running.get(group_id, ())), identity=identity, costs={})
        for item in candidates:
            context = contexts[item.get("group_id")]
            unit_id = item.get("unit_id")
            if unit_id not in context["costs"]:
                context["costs"][unit_id] = self._recent_cost(unit_id, item.get("group_id"), identity=context["identity"])
        return contexts

    def _perception(self, item, wall, monotonic_s, instances=None):
        group_id = item.get("group_id")
        group = self.catalog._group(group_id) if group_id else None
        identity = (dict(package_digest=group["package_digest"], contract_digest=group["contract"]["digest"],
                         installation_id=group["installation_id"]) if group else None)
        if instances is None:
            instances = self._live_instances({group_id})
        return self.group_timing.snapshot(group_id, identity, now=monotonic_s, wall=wall,
            queue=self.queue.values(), running=self.running.values(), instances=instances)

    def _candidate_memory(self, item, now, *, instances=None):
        """Latest fresh instance counters; a group is used only if unambiguous."""
        group_id = item.get("group_id")
        if not group_id:
            return None, None
        with self.deployments.lock:
            if instances is None:
                instances = self._live_instances({group_id})
            if item.get("instance_id"):
                instance = next((row for row in instances if row.get("id") == item["instance_id"]), None)
            else:
                live = [row for row in instances
                        if row.get("group_id") == group_id and row.get("released") is not True
                        and row.get("state") not in {"STOPPED", "FAILED"}]
                instance = live[0] if len(live) == 1 else None
            if (not instance or instance.get("group_id") != group_id or instance.get("released") is True
                    or instance.get("state") != "READY"):
                return None, None
            identity, generation = instance.get("id"), instance.get("generation")
            if item.get("instance_id") and item.get("generation") is not None and item["generation"] != generation:
                return None, None
            container_id = instance.get("container_id")
            if identity not in self.tracked or not isinstance(container_id, str) or not container_id or generation is None:
                return None, None
            sample = self.measure_samples.get((identity, generation))
            if not sample or sample.get("identity") != container_id + ":" + str(generation):
                return None, None
            at = sample.get("at_s")
            if (type(at) not in (int, float) or not math.isfinite(at) or at > now
                    or now - at > 2 * self._config["measurement_interval_s"]
                    or sample.get("boundary") not in (None, "started")):
                return None, None
            def value(field):
                amount = sample.get(field)
                if (type(amount) not in (int, float) or not math.isfinite(amount) or amount < 0
                        or field == "vram_bytes" and sample.get("vram_source") != self._config["vram_source"]):
                    return None
                return float(amount)
            return value("ram_bytes"), value("vram_bytes")

    def _recent_cost(self, unit_id, group_id, *, identity=None):
        record = self.recent_call_costs.get(unit_id)
        if not record or not group_id:
            return None
        if identity is None:
            group = self.catalog._group(group_id)
            identity = dict(package_digest=group["package_digest"], contract_digest=group["contract"]["digest"],
                            installation_id=group["installation_id"])
        if tuple(identity[key] for key in ("package_digest", "contract_digest", "installation_id")) != record["identity"]:
            return None
        return sum(record["samples"]) / len(record["samples"])

    def decide(self, event, candidates, *, task_id=None, context=None, observation=None):
        """Select and record once. Execution always happens after this lock is left."""
        with self.condition:
            if self.closed or self.policy is None:
                raise RuntimeError("冻结调度策略不可用：" + str(self.policy_error))
            candidates = copy.deepcopy(candidates)
            if not candidates or len({row["id"] for row in candidates}) != len(candidates):
                raise ValueError("决策需要非空且身份唯一的合法候选")
            self._consume_instance_events()
            # Every candidate in a real choice shares one causal cutoff.
            with self.deployments.lock:
                instances = self._live_instances({item.get("group_id") for item in candidates})
                wall, monotonic_s = time.time(), time.monotonic()
            contexts = self._group_contexts(candidates, wall, monotonic_s, instances=instances)
            for row in candidates:
                row["features"] = self._features(row, now=wall, monotonic_s=monotonic_s,
                    group_context=contexts[row.get("group_id")])
            observation = self._observation(event, context) if observation is None else copy.deepcopy(observation)
            if len(candidates) > 1 and self.workload and self.workload.get("pending_update"):
                # This is the actual next legal choice, before its action is sampled.
                self._update_interval(bootstrap=dict(observation=observation, candidates=candidates,
                                                     at=time.time(), monotonic_s=time.monotonic()))
            selection = self.policy.choose(observation, candidates) if len(candidates) > 1 else dict(index=0)
            index = selection.get("index")
            if type(index) is not int or not 0 <= index < len(candidates):
                raise ValueError("策略返回了候选集合之外的动作")
            self.counts["decisions" if len(candidates) > 1 else "forced_actions"] += 1
            if len(candidates) > 1:
                self.decision_sequence += 1
                decision = dict(id=uuid.uuid4().hex, sequence=self.decision_sequence, at=wall,
                    monotonic_s=monotonic_s, event=event, task_id=task_id,
                    config_id=self.config_id, policy_config_id=self.policy_config_id,
                    feature_schema=FEATURE_SCHEMA, observation=observation, candidates=candidates,
                    selection=selection, session=self.session,
                    workload_id=self.workload["id"] if self.workload else None)
                self.decisions.append(decision)
                if self.store:
                    self.store.save("capability_decision", decision["id"], decision)
                if self.workload and self.workload["status"] in {"RUNNING", "DRAINING"}:
                    self.workload_decision_ids.append(decision["id"])
                    if self.store is None:
                        self.workload_memory_decisions.append(decision)
            return copy.deepcopy(candidates[index])

    def choose_optional(self, task_id, event, options, context=None):
        """Choose one optional check or continue; required checks never reach the actor."""
        with self.condition:
            if self.workload and self.workload["status"] != "RUNNING":
                raise RuntimeError("工作负载已结束接单")
            if task_id is not None and (task_id not in self.tasks.active or task_id in self.cancelled
                    or self.tasks.active[task_id].get("business_finished_s") is not None):
                raise ValueError("可选防护需要当前尚未结束或取消的任务")
            task = self.tasks.active.get(task_id, {})
            deadline = ({"deadline_wall": time.time() + task["deadline_s"] - (self.tasks.clock() - task["arrived_s"])}
                        if task.get("deadline_s") is not None else {})
            family_results = (context or {}).get("family_results", {})
            candidates = [dict(kind="continue", id="continue:" + event, event=event, **deadline)]
            for supplied in options:
                option = copy.deepcopy(supplied)
                if option.get("required"):
                    continue
                name = option["name"]
                group_id = option.get("group_id")
                if group_id is None and option.get("unit_id"):
                    group_id = self.catalog.unit(option["unit_id"])["runtime_group"]
                fact = self.deployments.runtime_fact(group_id, option.get("unit_id")) if group_id else {}
                family = option.get("hook_family")
                candidates.append(dict(kind="run_hook", id="hook:" + name, hook=name, event=event,
                    group_id=group_id, unit_id=option.get("unit_id"), fact=fact, **deadline,
                    hook_family=family, **({"family_finding_valid": family_results[family]} if family in family_results else {}),
                    **({"estimated_cost_s": number(option["estimated_cost_s"], "estimated_cost_s")}
                       if option.get("estimated_cost_s") is not None else {})))
            return self.decide(event, candidates, task_id=task_id, context=context)

    def optional_selector(self, task_id, bindings=None, observe=None):
        """Adapter for AgentRuntime.optional_hooks; it selects, never invokes tools."""
        bindings = copy.deepcopy(bindings or {})
        def choose_next(event, payload, remaining_hooks):
            # Recompute visible feedback after each actual check; no hook is executed here.
            context = observe(event, payload) if observe is not None else {}
            options = []
            for hook in remaining_hooks:
                name = hook["name"] if isinstance(hook, dict) else hook.name
                required = hook.get("required", False) if isinstance(hook, dict) else hook.required
                options.append(dict(bindings.get(name, {}), name=name, required=required))
            return self.choose_optional(task_id, event, options, context)
        def select(event, payload, hooks):
            remaining = list(hooks)
            selected = []
            while remaining:
                choice = choose_next(event, payload, remaining)
                if choice["kind"] == "continue":
                    break
                selected.append(choice["hook"])
                remaining = [hook for hook in remaining if
                             (hook["name"] if isinstance(hook, dict) else hook.name) != choice["hook"]]
            return tuple(selected)
        select.choose_next = choose_next
        return select

    def start_workload(self, workload_id, task_ids, *, training=False, fresh=True, learner_config=None, seed=0,
                       continuous=False):
        """Explicit fresh training or frozen inference; no reward or task labels are invented."""
        with self.condition:
            if (not isinstance(workload_id, str) or not workload_id or not isinstance(task_ids, (list, tuple))
                    or not task_ids or any(not isinstance(item, str) or not item for item in task_ids)
                    or len(set(task_ids)) != len(task_ids)):
                raise ValueError("工作负载需要唯一身份及非空、无重复的任务清单")
            if type(continuous) is not bool:
                raise ValueError("continuous 须为布尔值")
            if self.store and self.store.get("capability_workload", workload_id):
                raise ValueError("工作负载身份已使用")
            self._resource_summary()
            if self.tasks.active or self.queue or self.running or self._has_held():
                raise ValueError("当前任务或实例尚未收尾，不能开始冻结工作负载")
            if self.workload and self.workload["status"] in {"RUNNING", "DRAINING"}:
                raise ValueError("上一个工作负载尚未结束")
            if training and fresh:
                from seampact.scheduling.capability_ppo import CandidatePPO
                selected = CandidatePPO(policy_config_id(self._config), self._config["objective"],
                                        config=learner_config, seed=seed, training=True)
            elif training:
                selected = self.policy
                if (not getattr(selected, "training", False) or
                        selected.identity["config_id"] != self.policy_config_id or learner_config is not None):
                    raise ValueError("继续训练需要当前同配置 PPO；不重新指定 learner_config")
                seed = selected.learner.seed
            else:
                selected = load_policy(self._config)
            self._reset_ledger()
            self.policy = selected
            self.policy_error = None
            self.workload = dict(id=workload_id, session=self.session, status="RUNNING", task_ids=list(task_ids),
                started=[], finished=[], training=training, updated=False, pool=self._pool(),
                policy=type(selected).__name__, policy_config_id=self.policy_config_id,
                learner_config=copy.deepcopy(learner_config), seed=seed, fresh=fresh,
                continuous=continuous, updates=[], updated_tasks=[], pending_update=None)
            if continuous and training:
                self.resources.enable_intervals()
            if self.store:
                self.store.save("capability_workload", workload_id, self.workload)
            self.condition.notify_all()
            return copy.deepcopy(self.workload)

    def _workload_decisions(self, start=0):
        return ([self.store.get("capability_decision", key) for key in self.workload_decision_ids[start:]]
                if self.store else copy.deepcopy(self.workload_memory_decisions[start:]))

    def _task_result(self, task_id):
        return self.store.get("capability_task_result", task_id) if self.store else self.workload_results.get(task_id)

    def request_update(self, *, minimum_tasks=16):
        """Queue a scored task-boundary update; keep every held instance running."""
        if type(minimum_tasks) is not int or minimum_tasks < 1:
            raise ValueError("minimum_tasks 须为正整数")
        with self.condition:
            if (not self.workload or not self.workload.get("continuous") or not self.workload.get("training")
                    or self.workload["status"] != "RUNNING"):
                raise ValueError("区间更新需要运行中的连续训练工作负载")
            if self.workload.get("pending_update"):
                return dict(pending=True, **copy.deepcopy(self.workload["pending_update"]))
            tasks = [key for key in self.workload["finished"] if key not in self.workload["updated_tasks"]]
            if self.tasks.active or self.queue or self.running:
                return dict(pending=False, reason="等待业务任务边界", completed_tasks=len(tasks))
            if len(tasks) < minimum_tasks:
                return dict(pending=False, reason="尚未达到更新任务数", completed_tasks=len(tasks), minimum_tasks=minimum_tasks)
            if any((self._task_result(key) or {}).get("quality") is None for key in tasks):
                return dict(pending=False, reason="真实任务质量尚未全部结算", completed_tasks=len(tasks))
            self.workload["pending_update"] = dict(task_ids=tasks, requested_at=time.time())
            if self.store:
                self.store.save("capability_workload", self.workload["id"], self.workload)
            return dict(pending=True, **copy.deepcopy(self.workload["pending_update"]))

    def _update_interval(self, *, bootstrap=None, final=False):
        """Settle task utility and newly observed resource costs exactly once."""
        workload = self.workload
        task_ids = [key for key in workload["finished"] if key not in workload["updated_tasks"]]
        if not final and any(self.decision_sequence > row.get("decision_sequence_at_arrival", self.decision_sequence)
                             for row in self.tasks.active.values()):
            # A deferred update must not consume choices of an unfinished task:
            # its eventual native score belongs to that same policy rollout.
            workload["pending_update"]["blocked"] = "等待已产生策略选择的业务任务完成及评分"
            return dict(updated=False, reason=workload["pending_update"]["blocked"])
        rows = [self._task_result(key) for key in task_ids]
        if any(not row or row.get("quality") is None for row in rows):
            reason = "真实任务质量尚未全部结算"
            if final:
                raise ValueError(reason)
            workload["pending_update"]["blocked"] = reason
            return dict(updated=False, reason=reason)
        if final:
            frozen = workload["summary"]
            if not frozen["training_ready"]:
                raise ValueError("最终任务评分或完整资源计量缺失，不能更新")
            end_s = frozen["resources"]["end_s"]
        else:
            resource_snapshot = self._resource_summary()
            end_s = resource_snapshot["end_s"]
        objective = self._config["objective"]
        measured = self.resources.settlement(objective["resource_source"], self._config["resource_envelopes"],
                                            self.resource_cutoffs, end_s=end_s, final=final)
        # A genuinely unavailable counter is not an ordinary unsampled live tail.
        if objective["resource_source"] == "measured" and not final:
            for identity in self.tracked:
                instance = self.deployments.instances.get(identity) or {}
                sample = self.measure_samples.get((identity, instance.get("generation"))) or {}
                for field in ("ram_bytes", "vram_bytes"):
                    if sample.get(field) is None and sample.get(field + "_blocked", True):
                        measured["unknown"].append(dict(instance_id=identity, metric=field, reason="counter_unavailable"))
            measured["ready"] = not measured["unknown"]
        if not measured["ready"]:
            reason = "连续区间存在未知资源计量，等待真实证据"
            if final:
                raise ValueError(reason)
            workload["pending_update"]["blocked"] = dict(reason=reason, unknown=measured["unknown"])
            return dict(updated=False, reason=reason)
        decisions = self._workload_decisions(self.interval_decision_offset)
        rewards, prefix = [0.0] * len(decisions), 0.0
        normalizer = len(workload["task_ids"])
        weights, scales = objective["weights"], objective["scales"]
        def credit(value, timestamp, clock):
            nonlocal prefix
            index = bisect_right([decision[clock] for decision in decisions], timestamp) - 1
            if index < 0:
                prefix += value
            else:
                rewards[index] += value
        for row in rows:
            tokens = row["metrics"].get("api_tokens")
            if tokens is None and weights["api"]:
                if final:
                    raise ValueError("启用的 API token 计量缺失")
                workload["pending_update"]["blocked"] = "启用的 API token 计量缺失"
                return dict(updated=False, reason="启用的 API token 计量缺失")
            value = (weights["quality"] * row["quality"] - weights["delivery"] * int(not row["delivered"])
                     - weights["latency"] * row["latency_s"] / scales["latency_s"]
                     - weights["api"] * (tokens or 0) / scales["api_tokens"]) / normalizer
            credit(value, row["finished_s"], "monotonic_s")
        for piece in measured["pieces"]:
            boundaries = [piece["start_s"], *[decision["at"] for decision in decisions
                if piece["start_s"] < decision["at"] < piece["end_s"]], piece["end_s"]]
            metric = piece["metric"]
            for left, right in zip(boundaries, boundaries[1:]):
                if "rate" in piece:
                    amount = piece["rate"] * (right - left)
                else:
                    amount = self.resources.interval_amount(piece["evidence"], metric, left, right)
                if amount is None:
                    reason = "跨决策资源区间缺少真实端点，不能分配回报"
                    if final:
                        raise ValueError(reason)
                    workload["pending_update"]["blocked"] = reason
                    return dict(updated=False, reason=reason)
                value = -weights[metric.removesuffix("_byte_s")] * amount / scales[metric] / normalizer
                credit(value, left, "at")
        interval_id = str(len(workload["updates"]) + 1)
        summary = dict(training_ready=True, objective_id=digest(objective), policy_config_id=self.policy_config_id,
                       session=self.session, interval_id=interval_id, utility=prefix + sum(rewards),
                       normalizer=normalizer, complete=final)
        if final and not math.isclose(sum(item["utility"] for item in workload["updates"]) + summary["utility"],
                                      workload["summary"]["utility"], rel_tol=1e-7, abs_tol=1e-9):
            raise ValueError("连续区间效用之和与整个工作负载不一致，拒绝更新")
        settlements = dict(before_first=prefix, after_decisions=rewards)
        previous_version = self.policy.learner.policy_version
        result = self.policy.update_interval(decisions, settlements, summary, bootstrap=bootstrap, final=final)
        receipt = dict(result, interval_id=interval_id, task_ids=list(task_ids), normalizer=normalizer,
                       resource_totals=measured["totals"], resource_coverage=measured["coverage"],
                       settlements=settlements, final=final,
                       decision_ids=[decision["id"] for decision in decisions],
                       bootstrap=copy.deepcopy(bootstrap), previous_policy_version=previous_version,
                       policy_version=self.policy.learner.policy_version)
        self.resource_cutoffs = measured["cutoffs"]
        self.resources.commit_settlement(self.resource_cutoffs)
        self.interval_decision_offset += len(decisions)
        workload["updated_tasks"].extend(task_ids)
        workload["updates"].append(receipt)
        workload.update(pending_update=None, update=receipt)
        if final:
            workload["updated"] = True
        if self.store:
            if result.get("updated"):
                checkpoint = self.store.path.parent / "checkpoints" / self.session / ("interval-" + interval_id + ".pt")
                checkpoint.parent.mkdir(parents=True, exist_ok=True)
                self.policy.save(checkpoint)
                receipt["checkpoint"] = dict(path=str(checkpoint),
                    sha256=hashlib.sha256(checkpoint.read_bytes()).hexdigest(),
                    policy_version=self.policy.learner.policy_version)
            self.store.save("capability_workload", workload["id"], workload)
        return copy.deepcopy(receipt)

    def finish_workload(self, *, settlements=None, update=False):
        """Request final drain; poll until actual release, then optionally settle once."""
        with self.condition:
            if self.workload is None:
                raise ValueError("没有明确开始的工作负载")
            if self.tasks.active or self.queue or self.running or set(self.workload["finished"]) != set(self.workload["task_ids"]):
                raise ValueError("冻结任务清单尚未完整收尾")
            if self.workload["status"] == "RUNNING":
                self.workload["status"] = "DRAINING"
                self.force_drain = True
                self.condition.notify_all()
            summary = (copy.deepcopy(self.workload["summary"]) if self.workload["status"] == "COMPLETE"
                       else self._status_locked()["objective"])
            if not self.elastic_active and not self._has_held():
                self.workload["status"] = "COMPLETE"
                self.workload["summary"] = summary
                self.workload["decision_ids"] = list(self.workload_decision_ids)
                if update:
                    if self.workload["updated"]:
                        raise ValueError("本工作负载已经更新，不能重复结算")
                    if not self.workload["training"]:
                        raise ValueError("更新须来自显式训练工作负载")
                    if self.workload.get("continuous"):
                        self._update_interval(final=True)
                    elif settlements is None:
                        raise ValueError("更新须来自显式训练工作负载和调用者提供的真实结算")
                    else:
                        result = self.policy.update_workload(self._workload_decisions(), settlements, summary)
                        self.workload.update(updated=True, update=result)
            if self.store:
                self.store.save("capability_workload", self.workload["id"], self.workload)
            return copy.deepcopy(self.workload)

    def begin(self, task_id, *, deadline_s=None, metadata=None):
        with self.condition:
            if self.closed:
                raise RuntimeError("能力调度已关闭")
            if self.store and self.store.get("capability_task_result", task_id):
                raise ValueError("任务身份已完成，不能重复计入到达分母")
            # A completed evaluation has already persisted its frozen ledger.
            # Start an independent online ledger on the next ordinary arrival.
            if (self.workload and self.workload["status"] == "COMPLETE"
                    and not self.workload["training"] and task_id not in self.workload["task_ids"]):
                self._reset_ledger()
            pool = self._pool()
            if self.workload:
                if (self.workload["status"] != "RUNNING" or task_id not in self.workload["task_ids"]
                        or task_id in self.workload["started"] or pool != self.workload["pool"]):
                    raise ValueError("任务不符合当前冻结工作负载清单或能力池")
            row = self.tasks.begin(task_id, deadline_s=deadline_s, metadata=metadata)
            self.tasks.active[task_id]["decision_sequence_at_arrival"] = self.decision_sequence
            if self.workload:
                self.workload["started"].append(task_id)
            self.pools[task_id] = pool
            row.update(config_id=self.config_id, pool_id=digest(pool), session=self.session)
            self.condition.notify_all()
            return row

    def business_terminal(self, task_id, *, finished_s=None):
        with self.condition:
            result = self.tasks.terminal(task_id, finished_s=finished_s)
            self.deployments.release_task(task_id)
            self.condition.notify_all()
            return result

    def finish(self, task_id, *, delivered, quality=None, status="COMPLETED", metrics=None, business_finished_s=None):
        with self.condition:
            row = self.tasks.finish(task_id, delivered=delivered, quality=quality, status=status, metrics=metrics,
                                    business_finished_s=business_finished_s)
            pool = self.pools.pop(task_id, {})
            row.update(config_id=self.config_id, pool_id=digest(pool), session=self.session)
            if self.store:
                self.store.save("capability_task_result", task_id, row)
            elif self.workload:
                self.workload_results[task_id] = row
            self.deployments.release_task(task_id)
            self.cancelled.discard(task_id)
            if self.workload:
                self.workload["finished"].append(task_id)
            self.condition.notify_all()
            return row

    def settle_quality(self, task_id, *, quality, metrics=None, evidence_id):
        """Attach delayed native scoring after task finish and before its policy update.

        Only quality changes. Business timestamps, delivery and usage already
        belong to the finished task and cannot be overwritten by its grader.
        """
        if type(quality) is bool:
            quality = float(quality)
        if quality is not None:
            quality = number(quality, "quality")
            if quality > 1:
                raise ValueError("quality 须在 [0,1] 内")
        if not isinstance(evidence_id, str) or not evidence_id or not isinstance(metrics or {}, dict):
            raise ValueError("延迟评分需要证据身份及指标对象")
        with self.condition:
            row = self._task_result(task_id)
            if not row:
                raise ValueError("延迟评分需要已持久化的任务终态")
            prior = row.get("quality_evidence")
            feedback = dict(id=evidence_id, quality=quality, metrics=copy.deepcopy(metrics or {}))
            if prior is not None:
                if prior != feedback:
                    raise ValueError("已登记评分证据不能覆盖")
                return copy.deepcopy(row)
            if (not self.workload or self.workload["status"] not in {"RUNNING", "DRAINING", "COMPLETE"}
                    or self.workload.get("updated") or task_id in self.workload.get("updated_tasks", [])
                    or row.get("session") != self.session or task_id not in self.workload["finished"]
                    or row.get("quality") is not None):
                raise ValueError("仅可回填当前已完成任务、尚未更新的未知质量")
            row.update(quality=quality, quality_evidence=feedback)
            row["metrics"]["native_score"] = copy.deepcopy(metrics or {})
            if self.store:
                self.store.save("capability_task_result", task_id, row)
            if quality is not None:
                self.tasks.totals["quality_known"] += 1
                self.tasks.totals["quality_sum"] += quality
            if self.workload["status"] == "COMPLETE":
                previous = self.workload["summary"]
                summary = self.tasks.summary(previous["resources"])
                summary.update({key: previous[key] for key in ("config_id", "policy_config_id", "session")})
                self.workload["summary"] = summary
            if self.store:
                self.store.save("capability_workload", self.workload["id"], self.workload)
            return copy.deepcopy(row)

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
            if self.workload and (self.workload["status"] != "RUNNING" or run_id not in self.tasks.active
                    or self.tasks.active[run_id].get("business_finished_s") is not None):
                raise RuntimeError("冻结工作负载不接受清单之外或业务结束后的能力调用")
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
                           identity=identity, queued_wall=time.time(), queued_s=time.monotonic(),
                           granted=threading.Event(), error=None,
                           concurrency=group["contract"]["concurrency"])
            if deadline is not None:
                request["deadline_wall"] = time.time() + deadline - elapsed
            self.groups.add(group_id)
            self.queue[call_id] = request
            self.group_timing.arrive(request)
            self.counts["calls_arrived"] += 1
            self.condition.notify_all()
            self.measure_wake.set()
        request["granted"].wait()
        if request["error"] is not None:
            raise request["error"]
        def observed(event):
            if event.get("instance_id"):
                with self.condition:
                    self._consume_instance_events()
                    identity = event["instance_id"]
                    if event.get("kind") == "call_started":
                        request["instance_id"] = identity
                        request.setdefault("execution_started_s", time.monotonic())
                        self.live_instance_ids.setdefault(group_id, set()).add(identity)
                        self.condition.notify_all()
                    self.tracked.add(identity)
                    with self.deployments.lock:
                        instance = self.deployments.instances.get(identity) or {}
                        if (instance.get("run_id") == request["execution_run_id"] and
                                (instance.get("start_wall") or 0) >= request["queued_wall"]):
                            self.owned[identity] = dict(group_id=group_id, run_id=request["execution_run_id"])
            if on_event is not None:
                on_event(event)

        facts = None
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
            result = self.catalog.invoke(unit_id, arguments, on_event=observed, run_id=request["execution_run_id"])
            facts = result.get("call") or {}
            elapsed_ms = facts.get("round_trip_ms")
            if (facts.get("execution") == "REMOTE" and type(elapsed_ms) in (int, float)
                    and math.isfinite(elapsed_ms) and elapsed_ms >= 0):
                identity_key = (identity["package_digest"], identity["contract_digest"], identity["installation_id"])
                with self.condition:
                    record = self.recent_call_costs.get(unit_id)
                    if not record or record["identity"] != identity_key:
                        record = self.recent_call_costs[unit_id] = dict(identity=identity_key, samples=deque(maxlen=8))
                    record["samples"].append(elapsed_ms / 1000)
            return result
        except Exception as error:
            facts = getattr(error, "facts", None)
            raise
        finally:
            with self.condition:
                self._consume_instance_events()
                # A failed remote execution is still a measured execution;
                # missing worker timing and transport failures remain unknown.
                execute_ms = (facts or {}).get("execute_ms")
                if (facts or {}).get("execution") == "REMOTE" and type(execute_ms) in (int, float):
                    self.group_timing.sample(group_id, identity, "execution", execute_ms / 1000, time.monotonic())
                self.running.pop(call_id, None)
                self.elastic_attempted.discard(("prewarm", call_id))
                self.elastic_attempted.discard(("add_replica", call_id))
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
                    end = (instance.get("resource_release_observed_s") or instance.get("stopped_wall")) if instance.get("released") is True else time.time()
                if row.get("start_s", -1) < start or row.get("end_s", float("inf")) > end:
                    raise ValueError("资源区间超出本次实例持有范围")
                record_id = digest([self.session, row])
                if self.store and self.store.get("capability_resource_interval", record_id):
                    continue
                self.resources.record([row])
                if self.store:
                    self.store.save("capability_resource_interval", digest([self.session, row]),
                                    dict(row, session=self.session, config_id=self.config_id))

    def _ingest_resource_sample(self, sample):
        identity = sample["instance_id"]
        if identity not in self.tracked:
            return
        key = (identity, sample["generation"])
        previous_raw = self.measure_samples.get(key)
        if previous_raw and sample["at_s"] <= previous_raw["at_s"]:
            return
        with self.deployments.lock:
            instance = self.deployments.instances.get(identity) or {}
            if instance.get("generation") != sample["generation"]:
                return
            start = max(self.started_wall, instance.get("start_wall") or self.started_wall)
            end = (instance.get("resource_release_observed_s") or instance.get("stopped_wall") or sample["at_s"])
        rows = []
        for field, metric in (("ram_bytes", "ram_byte_s"), ("vram_bytes", "vram_byte_s")):
            metric_key = (*key, field)
            if sample.get(field) is None or sample.get("identity") is None:
                # Unavailable counters must not turn two lifecycle zeroes into a fabricated zero cost.
                if sample.get(field + "_blocked", True):
                    self.metric_samples.pop(metric_key, None)
                continue
            previous = self.metric_samples.get(metric_key)
            if previous:
                seconds = sample["at_s"] - previous["at_s"]
                birth_link = previous.get("boundary") == "before_create" and sample.get("boundary") == "created"
                same_backend = sample["identity"] == previous["identity"] or birth_link
                left, right = max(start, previous["at_s"]), min(end, sample["at_s"])
                # Probe duration can exceed the requested polling interval.
                # Real adjacent endpoints still define a trapezoidal estimate;
                # an unavailable counter above breaks the chain explicitly.
                if same_backend and seconds > 0 and left < right:
                    a, b = previous[field], sample[field]
                    # Interpolate only between real endpoints; keep their original timestamps as evidence.
                    at_left = a + (b - a) * (left - previous["at_s"]) / seconds
                    at_right = a + (b - a) * (right - previous["at_s"]) / seconds
                    row = dict(source="measured", attribution="instance", instance_id=identity,
                        generation=key[1], start_s=left, end_s=right,
                        ram_byte_s=None, vram_byte_s=None, vram_source=self._config["vram_source"],
                        sample_span_s=seconds,
                        provenance="trapezoidal sampling estimate clipped between verified real endpoints",
                        endpoints=[{name: point.get(name) for name in
                            ("at_s", "identity", "boundary", "proof", field, field.removesuffix("_bytes") + "_provenance")}
                            for point in (previous, sample)])
                    row[metric] = (at_left + at_right) * (right - left) / 2
                    rows.append(row)
            self.metric_samples[metric_key] = copy.deepcopy(sample)
        self.measure_samples[key] = copy.deepcopy(sample)
        if rows:
            self.record_resources(rows)

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
                samples = sample_instances(self.deployments, identities, vram_source=self._config["vram_source"]) if identities and due else []
                with self.condition:
                    if self.closed:
                        return
                    if session != self.session:
                        continue
                    self._consume_instance_events()
                    for sample in samples:
                        self._ingest_resource_sample(sample)
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
            index = getattr(self, "live_instance_ids", {})
            if event["group_id"] in index:
                with self.deployments.lock:
                    instance = self.deployments.instances.get(event["instance_id"]) or {}
                    if instance.get("released") is True or instance.get("state") in {"STOPPED", "FAILED"}:
                        index[event["group_id"]].discard(event["instance_id"])
                    elif instance:
                        index[event["group_id"]].add(event["instance_id"])
            if event["group_id"] not in self.groups:
                continue
            identity = event["instance_id"]
            if self.store and self.store.get("capability_resource_final", digest([self.session, identity, event["generation"]])):
                continue
            self.tracked.add(identity)
            if event["kind"] == "resource_sample" and event.get("sample"):
                self._ingest_resource_sample(event["sample"])
            if event["kind"] == "start_requested" and (event.get("run_id") in self.elastic_runs or any(
                    request["execution_run_id"] == event.get("run_id") for request in self.running.values())):
                self.owned[identity] = dict(group_id=event["group_id"], run_id=event.get("run_id"))
                request = next((row for row in (*self.queue.values(), *self.running.values())
                                if row["group_id"] == event["group_id"] and
                                (row["execution_run_id"] == event.get("run_id") or
                                 self.elastic_active.get(event["group_id"], {}).get("request_id") == row["id"])), None)
                with self.deployments.lock:
                    instance = self.deployments.instances.get(identity) or {}
                    start_wall = instance.get("start_wall")
                if request and type(start_wall) in (int, float) and start_wall >= self.started_wall:
                    self.group_timing.start(event, copy.deepcopy(request["identity"]), start_wall)
            if event["kind"] == "registered":
                self.group_timing.ready(event, time.monotonic(), time.time())
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
                self.elastic_attempted.discard(("prewarm", key))
                self.elastic_attempted.discard(("add_replica", key))
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
        with self.deployments.lock:
            instances = self._live_instances({row["group_id"] for row in self.queue.values()} |
                                             {row["group_id"] for row in self.owned.values()})
            affinity = dict(self.deployments.affinity)
        grouped = {}
        for request in self.queue.values():
            grouped.setdefault(request["group_id"], []).append(request)
        live_by_group, group_cache, fact_cache = {}, {}, {}
        for instance in instances:
            live_by_group.setdefault(instance["group_id"], []).append(instance)
        def group_for(group_id):
            if group_id not in group_cache:
                group_cache[group_id] = self.catalog._group(group_id)
            return group_cache[group_id]
        for group_id in set(self.elastic_waited) - set(grouped):
            del self.elastic_waited[group_id]
        if len(self.running) < self._config["max_in_flight"]:
            for request in self.queue.values():
                group_id = request["group_id"]
                live = live_by_group.get(group_id, ())
                ready = [row for row in live if row.get("state") == "READY" and row.get("accepting", True)
                         and (not row.get("units") or request["unit_id"] in row["units"])]
                group = group_for(group_id)
                replicable = group["contract"]["replicas"]["replicable"] is True
                capacity = request["concurrency"] * (len(ready) if replicable and ready else 1)
                load = max(busy[group_id], sum(row.get("in_flight", 0) for row in ready))
                pinned = affinity.get((request.get("run_id"), group_id))
                pinned_record = next((row for row in ready if row["id"] == pinned), None)
                if (load >= capacity or pinned_record and pinned_record.get("in_flight", 0) >= request["concurrency"]
                        or self.elastic_active.get(group_id, {}).get("kind") == "prewarm"):
                    continue
                fact_key = (group_id, request["unit_id"])
                if fact_key not in fact_cache:
                    fact_cache[fact_key] = self.deployments.runtime_fact(*fact_key)
                fact = fact_cache[fact_key]
                # A single cold start owns this group's start handshake.
                if busy[group_id] and not fact["ready_instance"]:
                    continue
                candidates.append(dict(kind="dispatch", **{key: request[key] for key in
                    ("id", "group_id", "unit_id", "queued_wall")},
                    **({"deadline_wall": request["deadline_wall"]} if "deadline_wall" in request else {}), fact=fact))
                # A real waiting request can choose initialization before dispatch.
                attempt = ("prewarm", request["id"])
                if (self._config["elastic_actions"] and len(self.running) + len(self.elastic_active) < self._config["max_in_flight"]
                        and attempt not in self.elastic_attempted and group_id not in self.elastic_active
                        and not fact.get("warm") and not busy[group_id]
                        and group["contract"]["operations"]["prewarm"]["status"] == "SUPPORTED"):
                    available = [row for row in ready if row["id"] in self.owned
                                 and not row.get("in_flight") and not row.get("pending_stop")
                                 and row["id"] not in affinity.values()]
                    if available or not live and fact.get("cold_startable"):
                        instance_id = min(available, key=lambda row: row.get("generation", 0))["id"] if available else None
                        candidates.append(dict(kind="prewarm", id="prewarm:" + request["id"],
                            request_id=request["id"], group_id=group_id, unit_id=request["unit_id"],
                            instance_id=instance_id, fact=fact, ready_replicas=len(ready),
                            queue_depth=len(grouped[group_id])))
        if self._config["elastic_actions"] and not self.force_drain:
            for group_id, requests in grouped.items():
                if group_id in self.elastic_active or len(self.running) + len(self.elastic_active) >= self._config["max_in_flight"]:
                    continue
                live = live_by_group.get(group_id, ())
                ready = [row for row in live if row.get("state") == "READY" and row.get("accepting", True)]
                group = group_for(group_id)
                contract = group["contract"]
                unpinned = [r for r in requests if (r.get("run_id"), group_id) not in affinity]
                if (not ready or not unpinned or len(live) >= self._config["max_replicas_per_group"]
                        or any(row.get("state") != "READY" or row.get("pending_stop") for row in live)
                        or contract["replicas"]["replicable"] is not True
                        or contract["operations"]["add_replica"]["status"] != "SUPPORTED"
                        or max(busy[group_id], sum(row.get("in_flight", 0) for row in ready))
                           < len(ready) * contract["concurrency"]):
                    continue
                request = unpinned[0]
                token = (tuple(r["id"] for r in requests),
                         tuple(sorted(r["id"] for r in self.running.values() if r["group_id"] == group_id)),
                         tuple(sorted((row["id"], row.get("generation")) for row in ready)))
                if self.elastic_waited.get(group_id) == token or ("add_replica", request["id"]) in self.elastic_attempted:
                    continue
                shared = dict(group_id=group_id, unit_id=request["unit_id"], request_id=request["id"],
                              queue_depth=len(requests), ready_replicas=len(ready), wait_token=token)
                candidates.append(dict(kind="wait", id="wait:" + group_id, **shared))
                candidates.append(dict(kind="add_replica", id="replica:" + group_id, **shared))
        queued_groups = {item["group_id"] for item in self.queue.values()}
        instance_by_id = {row["id"]: row for row in instances}
        for instance_id, ownership in list(self.owned.items()):
            group_id = ownership["group_id"]
            instance = instance_by_id.get(instance_id)
            if instance is None or instance.get("released") is True:
                del self.owned[instance_id]
                continue
            if (group_id in queued_groups or group_id in self.elastic_active or
                    instance.get("state") != "READY" or instance.get("in_flight")):
                continue
            # Respect owner leases established by other runtime consumers.
            with self.deployments.lock:
                leased = instance_id in self.deployments.affinity.values()
            if leased or instance.get("pending_stop"):
                continue
            idle_replica = bool(busy[group_id])
            if idle_replica:
                if (not self._config["elastic_actions"] or self.force_drain or
                        not instance.get("accepting", True) or
                        group_for(group_id)["contract"]["replicas"]["replicable"] is not True or
                        any(row.get("state") in {"UNDEPLOYED", "STARTING"} for row in live_by_group.get(group_id, ()))):
                    continue
                ready = {row["id"]: row for row in live_by_group.get(group_id, ())
                         if row.get("state") == "READY" and row.get("accepting", True)
                         and not row.get("pending_stop") and row["id"] != instance_id}
                placements = [request.get("instance_id") for request in self.running.values()
                              if request["group_id"] == group_id]
                placed = Counter(placements)
                if not ready or any(identity not in ready or ready[identity].get("in_flight", 0) < count
                                    for identity, count in placed.items()):
                    continue
            due = self.retention_due.get(instance_id, 0)
            if self.force_drain or due <= now:
                shared = dict(group_id=group_id, instance_id=instance_id, generation=instance.get("generation"), run_id=ownership["run_id"],
                    idle_s=max(0, now - instance.get("last_activity_wall", instance.get("ready_wall", now))),
                    idle_timeout_s=self._config["idle_timeout_s"])
                if idle_replica:
                    shared["idle_replica"] = True
                candidates.append(dict(kind="drain_stop", id="release:" + instance_id, **shared))
                if not self.force_drain:
                    candidates.append(dict(kind="retain", id="retain:" + instance_id, **shared))
            else:
                next_timer = due if next_timer is None else min(next_timer, due)
        if self.force_drain and candidates:
            # All remaining closes are mandatory; their order is not a learned choice.
            candidates = [min(candidates, key=lambda item: item["id"])]
        observation = self._observation("resource")
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
                    action = self.decide("resource", candidates, observation=observation)
                    if action["kind"] == "dispatch":
                        request = self.queue.pop(action["id"])
                        dispatched_s = time.monotonic()
                        if request.get("queued_s") is not None:
                            self.group_timing.sample(request["group_id"], request["identity"], "queue_wait",
                                dispatched_s - request["queued_s"], dispatched_s)
                        self.running[action["id"]] = request
                        request["granted"].set()
                        action = None
                    elif action["kind"] == "retain":
                        self.retention_due[action["instance_id"]] = time.time() + self._config["retention_recheck_s"]
                        self.counts["retains"] += 1
                        action = None
                    elif action["kind"] == "wait":
                        self.elastic_waited[action["group_id"]] = action["wait_token"]
                        self.counts["elastic_waits"] += 1
                        action = None
                    elif action["kind"] in {"prewarm", "add_replica"}:
                        request = self.queue.get(action["request_id"])
                        if request:
                            action["decision_id"] = self.decisions[-1]["id"] if len(candidates) > 1 else None
                            action["started_at"] = time.time()
                            self.elastic_attempted.add((action["kind"], request["id"]))
                            self.elastic_active[action["group_id"]] = dict(kind=action["kind"], request_id=request["id"])
                            self.elastic_runs[request["execution_run_id"]] += 1
                            thread = threading.Thread(target=self._elastic_action, args=(copy.deepcopy(action),
                                request["execution_run_id"]), daemon=True, name="seampact-elastic-action")
                            thread.start()
                        action = None
                    self.controller_error = None
                if action is not None:
                    # The controller rechecks intake/in-flight state before actual close.
                    body = dict(instance_id=action["instance_id"])
                    if action.get("idle_replica"):
                        body["idle_replica"] = True
                    result = self.deployments.drain_stop(action["group_id"], body,
                                                         run_id=action["run_id"], reason=SCHEMA + ":idle")
                    with self.condition:
                        if result.get("status") != "SKIPPED":
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

    def _elastic_action(self, action, run_id):
        """A bounded launch/prewarm; account the real instance even if demand disappears."""
        group_id, instance_id, error, status = action["group_id"], action.get("instance_id"), None, "CANCELLED"
        try:
            with self.condition:
                request = self.queue.get(action["request_id"])
                if self.closed or self.force_drain or not request:
                    return
                frozen = copy.deepcopy(request["identity"])
            group = self.catalog._group(group_id)
            actual = dict(package_digest=group["package_digest"], contract_digest=group["contract"]["digest"],
                          installation_id=group["installation_id"])
            if actual != frozen:
                raise ValueError("排队期间能力版本或绑定变化，弹性动作未执行")
            if action["kind"] == "add_replica" or instance_id is None:
                started = self.deployments.start(group_id, {}, run_id=run_id, replica=action["kind"] == "add_replica")
                instance_id = started["id"]
                with self.condition:
                    self._consume_instance_events()
                    if hasattr(self, "live_instance_ids"):
                        self.live_instance_ids.setdefault(group_id, set()).add(instance_id)
                    self.tracked.add(instance_id)
                    self.owned[instance_id] = dict(group_id=group_id, run_id=run_id)
                    self.measure_wake.set()
            timeout = self.deployments.backend().get("start_timeout_s", 120)
            deadline = time.monotonic() + timeout
            while True:
                with self.condition:
                    with self.deployments.lock:
                        record = copy.deepcopy(self.deployments.instances.get(instance_id) or {})
                    state = record.get("state")
                    if state == "READY" and record.get("accepting", True):
                        break
                    if action["request_id"] not in self.queue:
                        return
                    if self.closed or self.force_drain or state in {"FAILED", "STOPPED", "UNKNOWN"}:
                        raise RuntimeError("弹性实例未能就绪或工作负载正在收尾")
                    if state == "UNDEPLOYED" and record.get("apply", {}).get("status") == "BACKEND_UNAVAILABLE":
                        raise RuntimeError("弹性实例的后端不可用")
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("弹性实例就绪超时")
                    self.condition.wait(min(0.5, remaining))
            if action["kind"] == "prewarm":
                with self.condition:
                    needed = not self.closed and not self.force_drain and action["request_id"] in self.queue
                if needed:
                    result = self.deployments.prewarm(group_id,
                        dict(instance_id=instance_id, units=[action["unit_id"]]), run_id=run_id)
                    if result.get("status") != "PREPARED" or result.get("initialization_ready") is not True:
                        raise RuntimeError("预热未成功")
            status = "COMPLETE"
        except Exception as exc:
            status, error = "FAILED", type(exc).__name__
        finally:
            with self.condition:
                self._consume_instance_events()
                self.elastic_runs[run_id] -= 1
                if self.elastic_runs[run_id] <= 0:
                    self.elastic_runs.pop(run_id, None)
                self.elastic_active.pop(group_id, None)
                self.counts["elastic_" + status.lower()] += 1
                if self.store:
                    identity = uuid.uuid4().hex
                    self.store.save("capability_elastic_action", identity, dict(id=identity, session=self.session,
                        workload_id=self.workload.get("id") if self.workload else None, kind=action["kind"],
                        group_id=group_id, unit_id=action["unit_id"], instance_id=instance_id, status=status,
                        task_id=run_id, decision_id=action.get("decision_id"), started_at=action.get("started_at"),
                        error_type=error, at=time.time()))
                self.condition.notify_all()
                self.measure_wake.set()

    def _resource_summary(self):
        self._consume_instance_events()
        end = time.time()
        lifetimes = []
        # Read exact current-session identities, not the controller's entire
        # terminal history or call-event index on every state refresh.
        with self.deployments.lock:
            # A stop can enqueue its last boundary after the first drain above
            # but before this lock is acquired. Consume it before freezing the
            # now-released identity; controller callbacks only enqueue events.
            self._consume_instance_events()
            for identity in list(self.tracked):
                instance = self.deployments.instances.get(identity)
                if not instance:
                    continue
                start = max(self.started_wall, instance.get("start_wall") or instance.get("ready_wall") or self.started_wall)
                released = instance.get("released") is True or not self.deployments._held(instance)
                finish = min(end, instance.get("resource_release_observed_s") or instance.get("stopped_wall") or end) if released else end
                lifetime = dict(instance_id=identity, generation=instance["generation"],
                    group_id=instance["group_id"], released=released,
                    resident_ms=max(0.0, finish - start) * 1000, start_s=start, end_s=finish,
                    start_observed=instance.get("start_wall") is not None,
                    end_observed=instance.get("state") != "UNKNOWN",
                    controller_start_s=instance.get("start_wall"), controller_stop_s=instance.get("stopped_wall"),
                    release_observation=copy.deepcopy((instance.get("resource_boundaries") or {}).get("released")))
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
                    for field in ("ram_bytes", "vram_bytes"):
                        self.metric_samples.pop((identity, lifetime["generation"], field), None)
                else:
                    lifetimes.append(lifetime)
        # Only actual controller-held instances count. Failed known releases have zero tail.
        summary = self.resources.summary(lifetimes, self._config["resource_envelopes"],
                                         start_s=self.started_wall, end_s=end)
        summary.update(vram_source=self._config["vram_source"],
            estimator="trapezoidal sampling between real attributed endpoints; uncovered gaps remain unknown",
            vram_semantics=("main-worker-process PyTorch reserved allocator; excludes CUDA context, non-PyTorch and children"
                            if self._config["vram_source"] == "torch_allocator_reserved" else "attributed NVML process memory"))
        return summary

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
        if release_pending or self.running or self.queue or self.elastic_active:
            objective.update(training_ready=False, reason="资源仍驻留，释放尾段尚未完成；当前效用仅为观测终点快照")
        return dict(schema=SCHEMA, session=self.session, config=self.config, config_id=self.config_id,
                    policy_config_id=self.policy_config_id,
                    feature_schema=FEATURE_SCHEMA, policy=type(self.policy).__name__,
                    policy_error=self.policy_error,
                    active_tasks=len(self.tasks.active), queued_calls=len(self.queue), running_calls=len(self.running),
                    elastic_actions_in_flight=len(self.elastic_active),
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
