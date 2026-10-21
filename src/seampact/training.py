"""Sequential real-environment learning, frozen evaluation and criterion adaptation."""

import copy
import hashlib
import importlib.metadata
import json
import queue
import secrets
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .evaluation import evaluate, validate_criteria, model_binding
from .learners import Learner, default_config, learner_options
from .scenarios import SceneSession, FrozenCatalog, arrays, freeze_scene, digest_file
from .store import now
from .costs import validate_costs, episode_costs, usage_record, summarize_usage

CANONICAL_VERSION = "seampact-json/1"
LEARNING = {"double_dqn", "ppo", "discrete_sac"}
REFERENCES = {"required_only", "all_applicable", "simple"}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def identity():
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return stamp + "_" + hashlib.sha256((stamp + secrets.token_hex(32)).encode()).hexdigest()


class BoundProfiles:
    """A run's immutable in-memory model binding; secrets never enter manifests."""
    def __init__(self, profiles):
        self.keys = {}
        with profiles.lock:
            self.profiles = {role: copy.deepcopy(profiles.role_profile(role))
                             for role in ("chat", "analysis", "teacher")}
            for role, profile in self.profiles.items():
                if profile.get("base_url") and profile.get("model"):
                    try:
                        self.keys[role] = profiles.role_key(role)
                    except Exception:
                        self.keys[role] = None

    def role_profile(self, role):
        return copy.deepcopy(self.profiles[role])

    def role_key(self, role):
        key = self.keys.get(role)
        if key is None:
            raise ValueError("运行创建时未取得该模型凭据：" + role)
        return key

    def public(self):
        return {role: dict(model_binding(profile), **{field: copy.deepcopy(profile.get(field)) for field in
                ("connection_id", "config_digest", "revision", "protocol", "parameters")})
                for role, profile in self.profiles.items()}

    def resources(self):
        return {role: {field: copy.deepcopy(profile.get(field)) for field in
                ("connection_id", "config_digest", "revision", "credential_ref", "credential_version", "auth")}
                for role, profile in self.profiles.items()}


def compatibility(manifest):
    """Conservative replay boundary: only criterion/aggregation may change."""
    result = {key: manifest[key] for key in
              ("scene", "pool", "observation_dim", "candidate_dim", "algorithm", "parameters", "implementation_files")}
    roles = manifest["scene"].get("model_roles", ["chat"])
    result["behavior_models"] = {role: manifest["models"][role] for role in roles}
    result["harness_resources"] = manifest.get("harness_resources")
    return result


def task_criteria(criteria, applicability, called_units=None):
    """Apply only task-owner declarations frozen inside the task manifest."""
    if criteria is None:
        return None
    effective = copy.deepcopy(criteria)
    for dimension in effective["dimensions"]:
        if dimension["id"] in applicability:
            dimension["applicable"] = applicability[dimension["id"]]
        if dimension.get("level") == "module" and called_units is not None:
            dimension["applicable"] = dimension.get("applicable", True) and dimension["unit_id"] in called_units
    return effective


class TrainingJobs:
    def __init__(self, platform):
        self.platform, self.store = platform, platform.store
        self.directory = platform.home / "policy" / "runs"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.pending = queue.Queue()
        self.stops, self.bindings = {}, {}
        self.closed = False
        self.active = self.store.get("scheduler_active", "current")
        for run in self.store.list("scheduler_run"):
            if run["status"] in {"RUNNING", "QUEUED"}:
                run.update(status="INTERRUPTED", ended_at=now(), error="平台进程已结束；历史材料保留")
                self.store.save("scheduler_run", run["id"], run)
        self.worker = threading.Thread(target=self._worker, daemon=True)
        self.worker.start()

    def snapshot(self):
        config = self.store.get("scheduler_config", "current")
        return dict(config=(config or {}).get("config"), config_id=(config or {}).get("digest"),
                    options=learner_options(), runs=self.store.list("scheduler_run"),
                    active_checkpoint=self.active, scene_contract="native-event/1",
                    readiness=dict(ready_units=sum(u["status"] == "READY" for u in self.platform.catalog.public()),
                        note="READY 表示源码入口已绑定；任务数据、模型及场景前提需单独配置"))

    def configure(self, body):
        config = copy.deepcopy(body.get("config", body))
        # Save an editable draft; starting a run freezes files and actual bindings.
        validate_criteria(config["criteria"])
        if config.get("mode", "train") not in {"train", "freeze", "warmstart", "rejudge"}:
            raise ValueError("未知运行方式")
        fingerprint = digest(config)
        value = dict(config=config, digest=fingerprint, updated_at=now())
        self.store.save("scheduler_config", fingerprint, value)
        self.store.save("scheduler_config", "current", value)
        return dict(config_id=fingerprint, config_digest=fingerprint)

    def _manifest(self, config, bound, catalog):
        current = {u["id"]: u for u in catalog.public()}
        scene = freeze_scene(config["scene"], {uid: unit.get("capability") for uid, unit in current.items()})
        pool = []
        for uid in scene.get("unit_ids", []):
            unit = catalog.unit(uid)
            if not unit or unit["status"] != "READY":
                raise ValueError("所选 Unit 尚不可执行：" + uid)
            if current[uid]["scheduling"]["activation"] != "platform":
                raise ValueError("透明干预场景请先将所选 Unit 配置为平台触发：" + uid)
            source = self.store.get("harness", unit["source_id"])
            request = catalog.frozen[uid][0].inspector.request
            pool.append(dict(id=uid, source_id=source["id"], revision=request.get("source_revision") or source.get("revision", "UNKNOWN"),
                source_version=unit.get("source_version", "UNKNOWN"), path=unit["path"], symbol=unit["symbol"],
                entry_id=unit.get("entry_id"), configuration=copy.deepcopy(request.get("configuration", {})),
                resources=copy.deepcopy(request.get("user_resources", [])), analysis_id=unit.get("analysis_id")))
            pool[-1]["initialization_at_creation"] = unit.get("initialization", "UNKNOWN")
            pool[-1]["execution_at_creation"] = current[uid].get("execution", "UNKNOWN")
            pool[-1]["capability"] = current[uid].get("capability")
        criteria = validate_criteria(config["criteria"])
        final = validate_criteria(config["evaluation_criteria"]) if config.get("evaluation_criteria") else None
        for spec in [criteria, final]:
            if spec and any(d["provider"] == "teacher" and d.get("applicable", True) for d in spec["dimensions"]):
                role = spec.get("teacher", {}).get("role", "teacher")
                spec.setdefault("teacher", {})["model_binding"] = model_binding(bound.role_profile(role))
                if not bound.role_profile(role).get("base_url"):
                    raise ValueError("请先配置教师模型，或明确选择复用分析模型")
                bound.role_key(role)
        algorithm = config.get("algorithm", "double_dqn")
        if algorithm not in LEARNING | REFERENCES:
            raise ValueError("未知学习器或参照策略")
        task_ids = config.get("task_ids") or [t["id"] for t in scene["tasks"]]
        if not task_ids or set(task_ids) - {t["id"] for t in scene["tasks"]}:
            raise ValueError("任务清单不在冻结场景内")
        for task in scene["tasks"]:
            applicability = task.get("metric_applicability", {})
            if (not isinstance(applicability, dict) or any(not isinstance(k, str) or not k or type(v) is not bool
                                                         for k, v in applicability.items())):
                raise ValueError("任务 metric_applicability 必须为预先声明的指标 ID 与布尔值映射")
        episodes = config.get("episodes", len(task_ids))
        if type(episodes) is not int or episodes < 1:
            raise ValueError("任务交互预算 episodes 必须为正整数")
        parameters = copy.deepcopy(config.get("parameters", {}))
        if not isinstance(parameters, dict):
            raise ValueError("parameters 必须为学习器配置对象")
        seed = config.get("seed", 0)
        if type(seed) is not int:
            raise ValueError("seed 必须为整数")
        if algorithm in LEARNING:
            resolved = default_config(algorithm)
            unknown = set(parameters) - set(resolved)
            if unknown:
                raise ValueError("未知学习器参数：" + ", ".join(sorted(unknown)))
            resolved.update(parameters)
            # The learner performs its own numeric/device validation before any
            # member of a comparison enters the execution queue.
            parameters = Learner(algorithm, len(scene["observation_schema"]),
                                 len(scene["candidate_schema"]), resolved, seed).config
        manifest = dict(canonical_version=CANONICAL_VERSION, method="SeamPact", implementation="native-event/1",
            algorithm=algorithm, parameters=parameters, seed=seed,
            mode=config.get("mode", "train"), parent_run_id=config.get("parent_run_id"), episodes=episodes,
            task_ids=task_ids, split=config.get("split", "development"), scene=scene, pool=pool,
            observation_dim=len(scene["observation_schema"]), candidate_dim=len(scene["candidate_schema"]),
            criteria=criteria, evaluation_criteria=final, models=bound.public(), role_resources=bound.resources(),
            harness_resources=catalog.snapshot_resources(), lower_policy="FIFO",
            reward_timing="sparse_terminal", missing_rule="exclude_incomplete_episode_keep_budget",
            costs=validate_costs(config.get("costs")),
            upstream_costs=config.get("upstream_costs", {}))
        implementation = Path(__file__).parent
        manifest["implementation_files"] = [dict(path=name, sha256=digest_file(implementation / name))
            for name in ("training.py", "learners.py", "scenarios.py", "evaluation.py", "providers.py",
                         "scheduler.py", "units.py", "runtime.py", "composition.py", "capabilities.py", "costs.py",
                         "dependencies.py", "resources.py")]
        # Keep the actual code bytes for this uncommitted research workspace.
        # Histories remain interpretable after a later authorized source edit.
        archive = self.directory.parent / "source-archive"
        archive.mkdir(exist_ok=True)
        for record in manifest["implementation_files"] + scene["adapter"]["files"]:
            source = Path(record["path"])
            if not source.is_absolute():
                source = implementation / source
            data = source.read_bytes()
            if hashlib.sha256(data).hexdigest() != record["sha256"]:
                raise ValueError("创建运行时源码已改变，请重新提交配置")
            target = archive / record["sha256"]
            if not target.exists():
                target.write_bytes(data)
            record["source_blob"] = str(target)
        versions = {}
        for package in ("torch", "pyserini", "pyjnius", "datasets", "faiss-cpu", "numpy", "transformers"):
            try:
                versions[package] = importlib.metadata.version(package)
            except importlib.metadata.PackageNotFoundError:
                versions[package] = None
        manifest["runtime_versions"] = dict(python=sys.version, packages=versions)
        if manifest["mode"] in {"warmstart", "freeze", "rejudge"} and algorithm in LEARNING:
            parent_id = manifest.get("parent_run_id")
            if not parent_id:
                raise ValueError("该运行方式需要原训练 run_id")
            parent = self.detail(parent_id)
            artifact = parent.get("checkpoint")
            if parent["status"] not in {"COMPLETED", "STOPPED"} or not artifact:
                raise ValueError("父运行需要已保存的完整策略工件")
            if digest_file(artifact["path"]) != artifact["artifact_digest"]:
                raise ValueError("父工件内容身份不符")
            manifest["parent_checkpoint"] = copy.deepcopy(artifact)
            manifest["parent_config_digest"] = parent["config_digest"]
        return manifest

    def start(self, body):
        config = body.get("config")
        if config is None:
            item = self.store.get("scheduler_config", body.get("config_id", "current"))
            if not item:
                raise ValueError("请先保存训练配置")
            config = item["config"]
        config = copy.deepcopy(config)
        algorithms = config.pop("algorithms", None) or [config.get("algorithm", "double_dqn")]
        if not isinstance(algorithms, list) or not algorithms:
            raise ValueError("请选择学习器")
        created = []
        with self.lock:
            # Prepare every run before queueing comparisons, so a bad config
            # does not silently execute just the first member of a comparison.
            prepared = []
            for algorithm in algorithms:
                cfg = dict(config, algorithm=algorithm)
                overrides = config.get("algorithm_parameters", {}).get(algorithm, {})
                cfg["parameters"] = dict(config.get("parameters", {}), **overrides)
                bound = BoundProfiles(self.platform.profiles)
                catalog = FrozenCatalog(self.platform.catalog, cfg["scene"].get("unit_ids", []))
                manifest = self._manifest(cfg, bound, catalog)
                prepared.append((manifest, bound, catalog))
            for manifest, bound, catalog in prepared:
                while True:
                    rid = identity()
                    folder = self.directory / rid
                    if self.store.get("scheduler_run", rid):
                        continue
                    try:
                        folder.mkdir(exist_ok=False)
                        break
                    except FileExistsError:
                        continue
                (folder / "manifest.json").write_text(canonical(manifest), encoding="utf-8")
                run = dict(id=rid, run_id=rid, config_digest=digest(manifest), algorithm=manifest["algorithm"],
                    mode=manifest["mode"], split=manifest["split"], seed=manifest["seed"],
                    scene_id=manifest["scene"]["id"], source_ids=[p["source_id"] for p in manifest["pool"]],
                    tags=config.get("tags", []), status="QUEUED", started_at=None, created_at=now(), ended_at=None,
                    episodes=0, interactions=0, choice_interactions=0, rejudged_episodes=0,
                    no_choice_episodes=0, eligible_episodes=0, excluded_episodes=0,
                    budget=manifest["episodes"], metrics=[], reward_mean=None, updates=0, error=None)
                self.store.create("scheduler_run", rid, run)
                self.stops[rid] = threading.Event()
                self.bindings[rid] = (bound, catalog)
                created.append(rid)
                self.pending.put(rid)
        return dict(run_ids=created)

    def stop(self, rid):
        run = self.store.get("scheduler_run", rid)
        if rid not in self.stops or not run or run["status"] not in {"QUEUED", "RUNNING"}:
            raise ValueError("运行不在当前队列")
        self.stops[rid].set()
        return dict(run_id=rid, stop="任务边界停止")

    def detail(self, rid):
        run = self.store.get("scheduler_run", rid)
        if not run:
            raise ValueError("没有这个运行；需要完整 run_id")
        folder = self.directory / rid
        result = dict(run)
        with self.lock:
            result["manifest"] = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
            if digest(result["manifest"]) != run["config_digest"]:
                raise ValueError("运行清单内容身份不符")
            for key in ("history", "curves"):
                path = folder / (key + ".jsonl")
                result[key] = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []
        return result

    def _append(self, folder, name, value):
        with self.lock, (folder / (name + ".jsonl")).open("a", encoding="utf-8") as stream:
            stream.write(canonical(value) + "\n")

    def _learner(self, manifest):
        if manifest["algorithm"] not in LEARNING:
            return None
        learner = Learner(manifest["algorithm"], manifest["observation_dim"], manifest["candidate_dim"],
                          manifest["parameters"], manifest["seed"])
        parent_id = manifest.get("parent_run_id")
        if manifest["mode"] in {"warmstart", "freeze", "rejudge"}:
            if not parent_id:
                raise ValueError("该运行方式需要原训练 run_id")
            parent = self.detail(parent_id)
            previous = parent["manifest"]
            if any(manifest[k] != previous[k] for k in ("algorithm", "observation_dim", "candidate_dim")):
                raise ValueError("父工件的学习器或特征维度不兼容")
            if manifest["scene"]["observation_schema"] != previous["scene"]["observation_schema"] or \
                    manifest["scene"]["candidate_schema"] != previous["scene"]["candidate_schema"]:
                raise ValueError("特征语义已变化，不能直接载入原参数")
            for key in ("version", "scales"):
                if manifest["scene"].get("representation", {}).get(key) != previous["scene"].get("representation", {}).get(key):
                    raise ValueError("共同特征版本或尺度已变化，不能直接载入原参数")
            if parent["config_digest"] != manifest.get("parent_config_digest"):
                raise ValueError("父运行清单与本次冻结身份不符")
            artifact = manifest.get("parent_checkpoint")
            if not artifact or digest_file(artifact["path"]) != artifact["artifact_digest"]:
                raise ValueError("父工件缺失或内容身份不符")
            learner.load(artifact["path"], optimizer=False)
        return learner

    def _choose(self, manifest, learner, event):
        if learner:
            return learner.act(event["observation"], [c["features"] for c in event["candidates"]],
                               [c["legal"] for c in event["candidates"]], training=manifest["mode"] != "freeze")
        target = event.get("references", {}).get(manifest["algorithm"])
        if target is None:
            raise ValueError("场景未给出满足相同义务的参照动作：" + manifest["algorithm"])
        for index, candidate in enumerate(event["candidates"]):
            if candidate["id"] == target and candidate["legal"]:
                return dict(index=index)
        raise ValueError("参照动作不在本次合法候选内")

    def _episode(self, manifest, session, task, learner, number):
        started = time.perf_counter()
        transitions = []
        event, execution_error = None, None
        try:
            event = session.reset(task, manifest["seed"] + number)
            while event["kind"] == "decision":
                choice_started = time.perf_counter()
                decision = self._choose(manifest, learner, event)
                decision["duration_ms"] = (time.perf_counter() - choice_started) * 1000
                old = copy.deepcopy(event)
                transition = dict(**arrays(old), index=decision["index"],
                    old_log_prob=decision.get("log_prob"), value=decision.get("value"),
                    policy_version=decision.get("policy_version"), episode_id=session.context.run_id + ":" + str(number),
                    reward=0.0, terminated=False, truncated=True,
                    next_state=None, next_candidates=[], next_legal=[])
                # Retain the actual selected action even if native continuation
                # raises after a paid model or Unit invocation.
                transitions.append(dict(event=old, decision=decision, transition=transition))
                event = session.step(decision["index"])
                transition.update(terminated=event["kind"] == "terminal" and event["terminated"],
                                  truncated=event["kind"] == "terminal" and event["truncated"])
                following = event if event["kind"] == "decision" else event.get("bootstrap_decision")
                if following:
                    data = arrays(following)
                    transition.update(next_state=data["state"], next_candidates=data["candidates"], next_legal=data["legal"])
        except Exception as error:
            execution_error = dict(type=type(error).__name__, message=str(error))
            previous_event = copy.deepcopy(event)
            event = dict(kind="terminal", terminated=False, truncated=True,
                         reason="execution_error", evidence={}, interrupted_event=previous_event)
        milliseconds = (time.perf_counter() - started) * 1000
        evidence = event.get("evidence", {})
        program = dict(event.get("program_metrics", {}), latency_ms=milliseconds,
                       module_calls=len(session.context.calls),
                       module_duration_ms=sum(c["duration_ms"] for c in session.context.calls))
        applicability = copy.deepcopy(task.get("metric_applicability", {}))
        called_units = {c["unit_id"] for c in session.context.calls}
        criterion = task_criteria(manifest["criteria"], applicability, called_units)
        final_criterion = task_criteria(manifest["evaluation_criteria"], applicability, called_units)
        feedback = evaluate(session.context.profiles, criterion, evidence, program)
        final_program = dict(program)
        final_program.update(event.get("evaluation_metrics", {}))
        final_program.update({key: program[key] for key in ("latency_ms", "module_calls", "module_duration_ms")})
        independent = evaluate(session.context.profiles, final_criterion, evidence,
                               final_program) if final_criterion else None
        eligible = feedback["eligible"] and not event["truncated"] and bool(transitions) and not execution_error
        if transitions:
            transitions[-1]["transition"]["reward"] = feedback["reward"]
        costs = episode_costs(session.context, manifest, milliseconds, transitions, feedback, independent)
        return dict(task_id=task["id"], episode=number, terminated=event["terminated"], truncated=event["truncated"],
            costs=costs,
            transitions=transitions, evidence=evidence, program_metrics=program,
            evaluation_metrics=event.get("evaluation_metrics", {}), feedback=feedback,
            independent_feedback=independent, eligible=eligible, calls=copy.deepcopy(session.context.calls),
            model_calls=copy.deepcopy(session.context.model_calls), online_duration_ms=milliseconds,
            choice_interactions=sum(sum(t["transition"]["legal"]) > 1 for t in transitions),
            final_response=event.get("response"), terminal_reason=event.get("reason"),
            interrupted_event=event.get("interrupted_event"), execution_error=execution_error,
            metric_applicability=applicability,
            effective_applicability={"training": {d["id"]: d["applicable"] for d in criterion["dimensions"]},
                "independent": {d["id"]: d["applicable"] for d in final_criterion["dimensions"]} if final_criterion else None})

    def _rejudge(self, manifest, bound):
        parent = self.detail(manifest["parent_run_id"])
        if manifest["algorithm"] == "ppo":
            raise ValueError("PPO 必须采集当前策略 rollout，不能重放重评后的旧经历")
        if compatibility(manifest) != compatibility(parent["manifest"]):
            raise ValueError("重评仅适用于环境、池、模型、候选和参数均不变的判据变更")
        roles = manifest["scene"].get("model_roles", ["chat"])
        if any(manifest["models"][role].get("service_version") in {None, "", "UNKNOWN"} for role in roles):
            raise ValueError("模型服务版本未知，不能自动认定旧转移兼容；请参数热启动并采集新经历")
        if manifest["scene"].get("environment_version", "UNKNOWN") in {None, "", "UNKNOWN"}:
            raise ValueError("环境版本未知，不能自动认定旧转移兼容；请参数热启动并采集新经历")
        history_path = self.directory / parent["id"] / "history.jsonl"
        historical_artifact = next((artifact for artifact in parent.get("artifacts", [])
                                    if Path(artifact["path"]) == history_path), None)
        if parent["history"] and (not historical_artifact or
                digest_file(history_path) != historical_artifact["artifact_digest"]):
            raise ValueError("旧经历文件缺少固定内容身份，或内容已改变，不能自动重放")
        used = 0
        selected_tasks = set(manifest["task_ids"])
        previous_tasks = {task["id"]: task for task in parent["manifest"]["scene"]["tasks"]}
        for historical in parent["history"]:
            if used >= manifest["episodes"]:
                break
            if (historical.get("task_id") not in selected_tasks or not historical.get("transitions")
                    or historical.get("truncated") or historical.get("execution_error")):
                continue
            item = copy.deepcopy(historical)
            for previous_feedback in (item.get("feedback"), item.get("independent_feedback")):
                for image in (previous_feedback or {}).get("raw", {}).get("images", []):
                    if digest_file(image["path"]) != image["sha256"]:
                        raise ValueError("旧图像证据内容已变化，不能重评原经历")
            item["previous_feedback"] = item["feedback"]
            applicability = item.get("metric_applicability", previous_tasks[item["task_id"]].get("metric_applicability", {}))
            called_units = {c["unit_id"] for c in item.get("calls", [])}
            criterion = task_criteria(manifest["criteria"], applicability, called_units)
            final_criterion = task_criteria(manifest["evaluation_criteria"], applicability, called_units)
            item["feedback"] = evaluate(bound, criterion, item["evidence"], item["program_metrics"])
            item["eligible"] = item["feedback"]["eligible"]
            item["transitions"][-1]["transition"]["reward"] = item["feedback"]["reward"]
            item["previous_independent_feedback"] = item.get("independent_feedback")
            final_program = dict(item["program_metrics"])
            final_program.update(item.get("evaluation_metrics", {}))
            for key in ("latency_ms", "module_calls", "module_duration_ms"):
                if key in item["program_metrics"]:
                    final_program[key] = item["program_metrics"][key]
            item["independent_feedback"] = (evaluate(bound, final_criterion, item["evidence"], final_program)
                                            if final_criterion else None)
            item["effective_applicability"] = {
                "training": {d["id"]: d["applicable"] for d in criterion["dimensions"]},
                "independent": {d["id"]: d["applicable"] for d in final_criterion["dimensions"]} if final_criterion else None}
            item["rejudged_from"] = parent["id"]
            item["previous_costs"] = item.get("costs")
            records = []
            for scope, result, spec in (("training_feedback", item["feedback"], criterion),
                    ("independent_evaluation", item["independent_feedback"], final_criterion)):
                if result and result["usage"]["request_count"]:
                    record = usage_record(spec["teacher"]["role"], result["usage"], result["usage"]["request_count"],
                                          prices=manifest.get("costs", {}).get("prices", {}))
                    record["scope"] = scope
                    records.append(record)
            item["costs"] = dict(schema="cost-ledger/1", rejudged_from=parent["id"],
                online=dict(wall_ms=0, representation_ms=0, policy_ms=0, records=[], usage=summarize_usage([])),
                evaluation=dict(wall_ms=sum((r or {}).get("cost_duration", 0) * 1000
                                            for r in (item["feedback"], item["independent_feedback"])),
                                records=records, usage=summarize_usage(records)),
                historical_costs="retained in previous_costs; not charged again as new execution")
            used += 1
            yield item

    def _worker(self):
        while not self.closed:
            rid = self.pending.get()
            if rid is None:
                return
            self._run(rid)

    def _run(self, rid):
        run = self.store.get("scheduler_run", rid)
        folder = self.directory / rid
        session = learner = None
        rewards, totals = [], defaultdict(list)
        counts = defaultdict(lambda: defaultdict(int))
        definitions = {}
        cost_records = {"online": [], "evaluation": []}
        run.update(status="RUNNING", started_at=now())
        self.store.save("scheduler_run", rid, run)
        print("[SeamPact] learning_started " + rid, flush=True)
        try:
            if self.stops[rid].is_set() or self.closed:
                run["status"] = "STOPPED"
                return
            manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
            if digest(manifest) != run["config_digest"]:
                raise ValueError("运行清单内容身份不符")
            bound, catalog = self.bindings[rid]
            learner = self._learner(manifest)
            tasks = {t["id"]: t for t in manifest["scene"]["tasks"]}
            if manifest["mode"] == "rejudge":
                histories = self._rejudge(manifest, bound)
            else:
                started = time.perf_counter()
                session = SceneSession(manifest["scene"], self.platform, rid, profiles=bound, catalog=catalog)
                run["initialization_ms"] = (time.perf_counter() - started) * 1000
                histories = (self._episode(manifest, session,
                    tasks[manifest["task_ids"][i % len(manifest["task_ids"])]], learner, i)
                    for i in range(manifest["episodes"]))
            histories = iter(histories)
            while not self.stops[rid].is_set() and not self.closed:
                try:
                    item = next(histories)
                except StopIteration:
                    break
                # Stop only between complete tasks; never discard a paid result.
                self._append(folder, "history", item)
                run["episodes"] += 1
                if not item.get("rejudged_from"):
                    run["interactions"] += len(item["transitions"])
                    run["choice_interactions"] += item.get("choice_interactions", 0)
                else:
                    run["rejudged_episodes"] += 1
                if not item.get("choice_interactions"):
                    run["no_choice_episodes"] += 1
                run["eligible_episodes" if item["eligible"] else "excluded_episodes"] += 1
                stats = {}
                if learner and manifest["mode"] != "freeze" and item["eligible"]:
                    for stored in item["transitions"]:
                        learner.observe(stored["transition"])
                    started = time.perf_counter()
                    stats = learner.update()
                    run["gradient_duration_ms"] = run.get("gradient_duration_ms", 0) + (time.perf_counter() - started) * 1000
                if item["feedback"]["reward"] is not None:
                    rewards.append(item["feedback"]["reward"])
                for group, feedback in [("training", item["feedback"]), ("independent", item["independent_feedback"])]:
                    for metric in (feedback or {}).get("metrics", []):
                        key = (group, metric["id"], metric.get("version"), metric.get("unit"), metric.get("direction"),
                               metric.get("level", "system"), metric.get("unit_id"))
                        counts[key][metric["status"]] += 1
                        definitions[key] = metric.get("definition")
                        if metric["status"] == "KNOWN":
                            totals[key].append(metric["value"])
                run["metrics"] = [dict(scope=k[0], id=k[1], version=k[2], unit=k[3], direction=k[4],
                    level=k[5], unit_id=k[6], definition=definitions[k],
                    value=sum(totals[k]) / len(totals[k]) if totals[k] else None,
                    denominator=len(totals[k]), population=run["episodes"], status_counts=dict(v)) for k, v in counts.items()]
                run["reward_mean"] = sum(rewards) / len(rewards) if rewards else None
                run["reward_denominator"] = len(rewards)
                costs = run.setdefault("costs", dict(online_wall_ms=0, evaluation_wall_ms=0,
                    representation_ms=0, policy_ms=0, coverage="see per-episode cost-ledger/1 records and gaps"))
                costs["online_wall_ms"] += item["costs"]["online"]["wall_ms"]
                costs["evaluation_wall_ms"] += item["costs"]["evaluation"]["wall_ms"]
                for key in ("representation_ms", "policy_ms"):
                    costs[key] += item["costs"]["online"][key]
                for scope in cost_records:
                    cost_records[scope].extend(item["costs"][scope]["records"])
                    costs[scope + "_usage"] = summarize_usage(cost_records[scope])
                run["online_duration_ms"] = run.get("online_duration_ms", 0) + (0 if item.get("rejudged_from") else item["online_duration_ms"])
                run["evaluation_duration_ms"] = run.get("evaluation_duration_ms", 0) + 1000 * sum(
                    (feedback or {}).get("cost_duration", 0) for feedback in (item["feedback"], item["independent_feedback"]))
                usage = run.setdefault("usage", dict(reported_tokens=0, unknown_requests=0, requests=0))
                usage["coverage"] = "该旧字段仅汇总场景模型和评价教师；原生用量与缺口见 costs.online_usage 和逐任务账本"
                usage["native_internal_usage"] = "UNKNOWN" if item.get("calls") else "NOT_APPLICABLE"
                for call in item.get("model_calls", []) if not item.get("rejudged_from") else []:
                    tokens = (call.get("usage") or {}).get("total_tokens")
                    usage["requests"] += 1
                    if type(tokens) is int:
                        usage["reported_tokens"] += tokens
                    else:
                        usage["unknown_requests"] += 1
                for feedback in (item["feedback"], item["independent_feedback"]):
                    calls = (feedback or {}).get("usage", {})
                    usage["requests"] += calls.get("request_count", 0)
                    usage["unknown_requests"] += calls.get("unknown_requests", 0)
                    usage["reported_tokens"] += calls.get("total_tokens") or 0
                run["learning"] = learner.snapshot() if learner else None
                run["updates"] = run["learning"]["updates"] if learner else 0
                self._append(folder, "curves", dict(episode=run["episodes"], interactions=run["interactions"],
                    reward=item["feedback"]["reward"], eligible=item["eligible"], stats=stats, metrics=run["metrics"]))
                self.store.save("scheduler_run", rid, run)
                if item.get("execution_error"):
                    raise ValueError("任务执行中断，已保留实际调用和费用材料：" + item["execution_error"]["message"])
                if self.stops[rid].is_set() or self.closed:
                    break
            if learner and manifest["mode"] != "freeze" and learner.steps:
                if manifest["algorithm"] == "ppo":
                    stats = learner.update(force=True)
                    if stats["updated"]:
                        self._append(folder, "curves", dict(kind="final_rollout_update", episode=run["episodes"],
                            interactions=run["interactions"], stats=stats, reward=None, metrics=run["metrics"]))
                run["learning"] = learner.snapshot()
                run["updates"] = run["learning"]["updates"]
                path = folder / "policy.pt"
                learner.save(path)
                run["checkpoint"] = dict(checkpoint_id=identity(), parent_run_id=manifest.get("parent_run_id"),
                    parent_checkpoint_id=(manifest.get("parent_checkpoint") or {}).get("checkpoint_id"),
                    path=str(path), artifact_digest=digest_file(path))
            run["status"] = "STOPPED" if self.stops[rid].is_set() or self.closed else "COMPLETED"
        except Exception as error:
            run.update(status="FAILED", error=str(error))
        finally:
            if session:
                try:
                    session.close()
                except Exception as error:
                    run.update(status="FAILED", error=(run.get("error") or "") + " 场景关闭失败：" + str(error))
            run["ended_at"] = now()
            if run.get("costs") is not None:
                run["costs"].update(scene_initialization_ms=run.get("initialization_ms"),
                                    gradient_ms=run.get("gradient_duration_ms", 0))
            run["artifacts"] = [{"path": str(p), "artifact_digest": digest_file(p)}
                                for p in sorted(folder.iterdir()) if p.is_file()]
            self.store.save("scheduler_run", rid, run)
            self.bindings.pop(rid, None)
            print("[SeamPact] learning_finished " + rid + " " + run["status"], flush=True)

    def activate(self, rid):
        run = self.detail(rid)
        if (run["status"] != "COMPLETED" or not run.get("checkpoint")
                or not run["eligible_episodes"] or not run.get("updates")):
            raise ValueError("需要已完成且实际更新过参数的策略工件")
        if digest_file(run["checkpoint"]["path"]) != run["checkpoint"]["artifact_digest"]:
            raise ValueError("策略工件内容身份不符")
        self.active = dict(run_id=rid, checkpoint=run["checkpoint"], scene_id=run["manifest"]["scene"]["id"],
                           task_ids=[task["id"] for task in run["manifest"]["scene"]["tasks"]],
                           scope="仅作用于原场景真实事件；普通对话不会伪造安全事件")
        self.store.save("scheduler_active", "current", self.active)
        return self.active

    def start_chat(self, body):
        """Run an explicitly selected real scene task with the active checkpoint."""
        with self.lock:
            if self.closed or not self.active:
                raise ValueError("请先激活已训练的场景策略")
            active = copy.deepcopy(self.active)
            if body.get("scene_run_id") != active["run_id"]:
                raise ValueError("激活策略已改变，请刷新并重新选择场景任务")
            source = self.detail(active["run_id"])
            manifest = copy.deepcopy(source["manifest"])
            selected = next((task for task in manifest["scene"]["tasks"]
                             if task["id"] == body.get("scene_task_id")), None)
            if selected is None:
                raise ValueError("请选择激活策略对应的真实场景任务")
            if source.get("checkpoint") != active["checkpoint"]:
                raise ValueError("激活策略的工件身份已改变，请重新激活")
            bound = BoundProfiles(self.platform.profiles)
            current = bound.public()
            for role in manifest["scene"].get("model_roles", ["chat"]):
                if current[role] != manifest["models"][role]:
                    raise ValueError("业务模型绑定已改变；请按新条件运行冻结评价或适应")
                bound.role_key(role)
            catalog = FrozenCatalog(self.platform.catalog, manifest["scene"].get("unit_ids", []))
            resources = catalog.snapshot_resources()
            if manifest.get("harness_resources") is not None and resources != manifest["harness_resources"]:
                raise ValueError("Harness 资源配置已改变；请按新条件运行冻结评价或适应")
            manifest.update(mode="freeze", parent_run_id=source["id"],
                parent_checkpoint=copy.deepcopy(active["checkpoint"]), parent_config_digest=source["config_digest"],
                harness_resources=resources, role_resources=bound.resources())
            while True:
                tid = identity()
                if not self.store.get("task", tid):
                    break
            task = dict(id=tid, status="RUNNING", started_at=now(), response=None, error=None,
                calls=[], model_calls=0, usage=[], latency_ms=None, phase="scene_policy",
                scene_id=manifest["scene"]["id"], scene_task_id=selected["id"],
                checkpoint_id=active["checkpoint"]["checkpoint_id"], policy_run_id=source["id"],
                models=bound.public(), role_resources=bound.resources(), harness_resources=resources,
                input_scope="frozen_scene_task", transparent_decisions=[],
                request_note=copy.deepcopy(body.get("messages", [])))
            self.store.create("task", tid, task)
            threading.Thread(target=self._chat, args=(task, manifest, selected, bound, catalog), daemon=True).start()
            return dict(task_id=tid, scene_task_id=selected["id"])

    def _chat(self, task, manifest, selected, bound, catalog):
        started = time.perf_counter()
        session = None
        try:
            learner = self._learner(manifest)
            session = SceneSession(manifest["scene"], self.platform, task["id"], profiles=bound, catalog=catalog)
            event = session.reset(selected, manifest["seed"])
            while event["kind"] == "decision":
                if self.closed:
                    raise ValueError("平台已关闭，场景任务中断")
                choice_started = time.perf_counter()
                choice = learner.act(event["observation"], [c["features"] for c in event["candidates"]],
                                     [c["legal"] for c in event["candidates"]], training=False)
                choice["duration_ms"] = (time.perf_counter() - choice_started) * 1000
                task["transparent_decisions"].append(dict(event=copy.deepcopy(event),
                    decision=choice,
                    selected_index=choice["index"], selected_candidate=event["candidates"][choice["index"]]["id"],
                    checkpoint_id=task["checkpoint_id"]))
                self.store.save("task", task["id"], task)
                event = session.step(choice["index"])
            task.update(status="COMPLETED" if event["terminated"] and not event["truncated"] else "INTERRUPTED",
                response=event.get("response"), terminal_reason=event.get("reason"),
                evidence=event.get("evidence", {}), program_metrics=event.get("program_metrics", {}),
                evaluation_metrics=event.get("evaluation_metrics", {}),
                terminated=event["terminated"], truncated=event["truncated"])
            # Result criteria and teacher are training/evaluation work. This
            # deployed task uses only the frozen policy and real environment.
        except Exception as error:
            task.update(status="FAILED", error=str(error))
        finally:
            if session:
                task["scene_calls"] = copy.deepcopy(session.context.calls)
                task["model_calls"] = len(session.context.model_calls)
                task["usage"] = [call.get("usage") for call in session.context.model_calls]
                task["model_call_details"] = copy.deepcopy(session.context.model_calls)
                task["costs"] = episode_costs(session.context, manifest, (time.perf_counter() - started) * 1000,
                    task["transparent_decisions"], None, None)
                try:
                    session.close()
                except Exception as error:
                    task.update(status="FAILED", error=(task.get("error") or "") + " 场景关闭失败：" + str(error))
            task["calls"] = [call for call in self.platform.runtime.snapshot()["calls"].values()
                             if call.get("task_id") == task["id"]]
            task.update(latency_ms=(time.perf_counter() - started) * 1000, ended_at=now())
            self.store.save("task", task["id"], task)

    def close(self):
        self.closed = True
        for event in self.stops.values():
            event.set()
        self.pending.put(None)
