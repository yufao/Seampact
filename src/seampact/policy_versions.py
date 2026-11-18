"""Immutable policy artifacts, lineage and explicit selection for the live host."""

import copy
from contextlib import contextmanager, nullcontext
import hashlib
import json
import os
import uuid
from pathlib import Path

from seampact.capability_contract import digest
from seampact.scheduling.capability_scheduler import FEATURE_SCHEMA, freeze_config
from seampact.store import now


VERSION = "capability-policy-version/1"


def _sha(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def _label(value, default, maximum=120):
    value = default if value is None else value
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise ValueError("名称需要1至%d个字符" % maximum)
    return value.strip()


class PolicyVersions:
    def __init__(self, platform):
        self.platform, self.store = platform, platform.store
        self.root = Path(platform.home) / "policy-versions"
        self.root.mkdir(parents=True, exist_ok=True)
        # Project admission already uses this reentrant store lock. Sharing it
        # avoids a registry-lock -> store-lock inversion during an import that
        # races with saving the currently selected policy.
        self.lock = self.store.lock
        with self.store.transaction():
            self.store.db.execute("""CREATE INDEX IF NOT EXISTS policy_task_decisions
                ON documents(json_extract(data,'$.task_id')) WHERE kind='capability_decision'""")

    def _local(self, value):
        if not isinstance(value, (str, Path)) or not str(value).strip():
            raise ValueError("需要服务器数据目录内的文件路径")
        path = Path(value).expanduser().resolve()
        if not path.is_relative_to(Path(self.platform.home).resolve()) or not path.is_file():
            raise ValueError("文件须存在于本平台数据目录内")
        return path

    def _copy_artifact(self, source, expected, folder, suffix):
        source = self._local(source)
        if not isinstance(expected, str) or _sha(source) != expected:
            raise ValueError("工件内容与登记摘要不一致")
        target = self.root / folder / (expected + suffix)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if _sha(target) != expected:
                raise ValueError("已保存的策略工件内容已变化，请先检查存储")
            return target
        temporary = target.with_name(target.name + "." + uuid.uuid4().hex + ".partial")
        try:
            with source.open("rb") as incoming, temporary.open("xb") as outgoing:
                for block in iter(lambda: incoming.read(1024 * 1024), b""):
                    outgoing.write(block)
                outgoing.flush()
                os.fsync(outgoing.fileno())
            if _sha(temporary) != expected or _sha(source) != expected:
                raise ValueError("保存过程中源工件发生变化")
            # Same-content addresses are never overwritten by another version.
            if target.exists():
                if _sha(target) != expected:
                    raise ValueError("策略工件内容冲突")
            else:
                temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
        return target

    @staticmethod
    def metrics(workload):
        summary = (workload or {}).get("summary") or {}
        raw = summary.get("raw") or {}
        count = raw.get("arrivals", summary.get("denominator", 0))
        measured = ((summary.get("resources") or {}).get("measured") or {})
        def mean(value):
            return value / count if count and value is not None else None
        return dict(arrivals=count, delivered=raw.get("delivered"),
            quality=mean(raw.get("quality_sum")), latency_s=mean(raw.get("latency_s")),
            ram_byte_s=mean(measured.get("ram_byte_s")), vram_byte_s=mean(measured.get("vram_byte_s")),
            api_tokens=mean(raw.get("api_tokens")), complete=summary.get("complete", False),
            resource_scope="scheduler-managed-capability-instances",
            ram_coverage=measured.get("ram_coverage"), vram_coverage=measured.get("vram_coverage"))

    def _register(self, config, checkpoint=None, *, name=None, notes="", parent_id=None,
                  source=None, metrics=None, training=None, recipe_id=None, learner_config=None, seed=0):
        from seampact.scheduling.policy_checkpoint import load_version_policy
        if parent_id is not None:
            self.get_internal(parent_id)
        if not isinstance(notes, str) or len(notes) > 4000:
            raise ValueError("备注须为不超过4000字符的文字")
        config = copy.deepcopy(config)
        if checkpoint:
            path = self._local(checkpoint["path"])
            config["policy"] = dict(kind="ppo", checkpoint=str(path), checkpoint_sha256=checkpoint["sha256"],
                                    learner_config=copy.deepcopy(learner_config or {}), seed=seed)
        config = freeze_config(config)
        # Validate both feature/target identity and the actual model before publishing a version.
        candidate = dict(id="pending", name=name, config=config, checkpoint=checkpoint,
                         learner_config=copy.deepcopy(learner_config or {}), seed=seed, feature_schema=FEATURE_SCHEMA)
        policy = load_version_policy(candidate)
        if checkpoint:
            path = self._copy_artifact(path, checkpoint["sha256"], "weights", ".pt")
            checkpoint = dict(path=str(path), sha256=checkpoint["sha256"])
            config["policy"]["checkpoint"] = str(path)
        source = copy.deepcopy(source or dict(kind="manual"))
        key_fields = dict(config=config, parent_id=parent_id, source=source)
        if source.get("kind") == "current":
            key_fields.update(name=name, notes=notes)
        key = digest(key_fields)
        with self.lock:
            previous = self.store.get("policy_version_source", key)
            if previous:
                return self.get_internal(previous["version_id"])
            identity = uuid.uuid4().hex
            record = dict(schema=VERSION, id=identity, name=_label(name, "PPO策略" if checkpoint else "规则策略"),
                notes=notes, kind="ppo" if checkpoint else "rule", created_at=now(), parent_id=parent_id,
                config=config, checkpoint=checkpoint, checkpoint_sha256=checkpoint["sha256"] if checkpoint else None,
                learner_config=copy.deepcopy(learner_config or {}), seed=seed, feature_schema=FEATURE_SCHEMA,
                metadata=copy.deepcopy(getattr(policy, "identity", None)), source=source,
                metrics=copy.deepcopy(metrics or {}), training=copy.deepcopy(training or {}), recipe_id=recipe_id)
            with self.store.transaction():
                self.store.create("policy_version", identity, record)
                self.store.create("policy_version_source", key, dict(version_id=identity))
            return copy.deepcopy(record)

    def get_internal(self, identity):
        record = self.store.get("policy_version", identity)
        if not record:
            raise ValueError("没有这个策略版本")
        return record

    def get_recipe(self, identity):
        record = self.store.get("policy_recipe", identity)
        if not record:
            raise ValueError("没有这个冻结训练方案，请先导入方案")
        if _sha(self._local(record["protocol"])) != record["protocol_sha256"]:
            raise ValueError("冻结训练方案内容已变化")
        return record

    def _ensure_recipe(self, path, plan, name, grader_command=None):
        path = self._local(path)
        checksum = _sha(path)
        with self.lock:
            existing = self.store.get("policy_recipe", checksum)
            if existing:
                return existing
            retained = self._copy_artifact(path, checksum, "recipes", ".json")
            recipe = dict(id=checksum, name=_label(name, "训练方案"), protocol=str(retained),
                protocol_sha256=checksum, source_protocol=str(path), project_id=plan.get("project_id"),
                training_tasks=len(plan["cases"]["training"]), grader_command=copy.deepcopy(grader_command), created_at=now())
            self.store.create("policy_recipe", checksum, recipe)
            return recipe

    @classmethod
    def combined_metrics(cls, workloads):
        if len(workloads) == 1:
            return cls.metrics(workloads[0])
        summaries = [(item or {}).get("summary") or {} for item in workloads]
        raws = [item.get("raw") or {} for item in summaries]
        measured = [((item.get("resources") or {}).get("measured") or {}) for item in summaries]
        def total(rows, key):
            values = [item.get(key) for item in rows]
            return sum(values) if all(value is not None for value in values) else None
        count = total(raws, "arrivals")
        def mean(value):
            return value / count if count and value is not None else None
        return dict(arrivals=count, delivered=total(raws, "delivered"), quality=mean(total(raws, "quality_sum")),
            latency_s=mean(total(raws, "latency_s")), ram_byte_s=mean(total(measured, "ram_byte_s")),
            vram_byte_s=mean(total(measured, "vram_byte_s")), api_tokens=mean(total(raws, "api_tokens")),
            complete=all(item.get("complete") for item in summaries), segments=len(workloads),
            resource_scope="scheduler-managed-capability-instances",
            ram_coverage=1.0 if all(item.get("ram_coverage") == 1 for item in measured) else None,
            vram_coverage=1.0 if all(item.get("vram_coverage") == 1 for item in measured) else None)

    def training_policy(self, version_id, target_config, learner_config=None, seed=None, mode="branch"):
        from seampact.scheduling.policy_checkpoint import load_version_policy
        return load_version_policy(self.get_internal(version_id), target_config=target_config,
                                   learner_config=learner_config, seed=seed, mode=mode)

    def _recorded_grader(self, protocol):
        """Reuse nearby experiment configuration without executing its contents."""
        home = Path(self.platform.home).resolve()
        for folder in (protocol.parent, protocol.parent.parent):
            if not folder.is_relative_to(home):
                continue
            direct = folder / "grader-argv.json"
            manifest, grader = folder / "manifest.json", folder / "grader.json"
            candidate = None
            if direct.is_file():
                candidate = self._local(direct)
            elif manifest.is_file() and grader.is_file():
                record = json.loads(self._local(manifest).read_bytes())
                expected = (record.get("files") or {}).get("grader.json")
                if expected and _sha(self._local(grader)) == expected:
                    candidate = grader
            if candidate:
                command = json.loads(candidate.read_bytes())
                if (isinstance(command, list) and command and all(isinstance(item, str) and item for item in command)
                        and command.count("{collection}") == 1):
                    return command
        return None

    def import_protocol(self, body):
        from seampact.office_pilot import _read_plan
        path = self._local(body.get("protocol"))
        checksum = _sha(path)
        identity, plan, _ = _read_plan(dict(protocol=str(path), protocol_sha256=checksum))
        if not plan.get("continuous_training") or not (plan.get("scheduler") or {}).get("objective"):
            raise ValueError("需要带明确目标及尺度的连续训练冻结方案")
        command = body.get("grader_command")
        previous = self.store.get("policy_recipe", identity)
        if command is None and previous:
            command = previous.get("grader_command")
        if command is None:
            command = self._recorded_grader(path)
        if command is not None and (not isinstance(command, list) or not command
                or any(not isinstance(item, str) or not item for item in command)
                or command.count("{collection}") != 1):
            raise ValueError("独立评分命令须为参数数组，并含一个{collection}占位符")
        retained = self._copy_artifact(path, checksum, "recipes", ".json")
        recipe = dict(id=identity, name=_label(body.get("name"), path.parent.name), protocol=str(retained),
            protocol_sha256=checksum, source_protocol=str(path), project_id=plan.get("project_id"),
            training_tasks=len((plan.get("cases") or {}).get("training", [])),
            grader_command=copy.deepcopy(command), created_at=previous.get("created_at") if previous else now())
        if not recipe["training_tasks"]:
            raise ValueError("训练方案没有训练题")
        self.store.save("policy_recipe", identity, recipe)
        imported, errors = [], []
        for run in self.store.list("office_pilot"):
            if (run.get("protocol_sha256") == identity and run.get("mode") == "training"
                    and run.get("status") == "COMPLETE" and run.get("checkpoint")):
                try:
                    imported.append(self.capture_run(run["id"], retained, recipe["name"])["id"])
                except (ValueError, RuntimeError, ImportError) as error:
                    errors.append(dict(run_id=run["id"], error=str(error)))
        return dict(recipe=recipe, imported=imported, errors=errors)

    def discover(self):
        paths = {str(Path(row["path"]).resolve()) for row in self.store.list("office_pilot_protocol") if row.get("path")}
        # Existing experiments predate the protocol registry. Only look for the exact
        # frozen filename two levels down, never traverse datasets or model trees.
        home = Path(self.platform.home)
        paths.update(str(path.resolve()) for pattern in ("*/frozen-pilot-protocol.json", "*/*/frozen-pilot-protocol.json")
                     for path in home.glob(pattern) if path.is_file())
        imported, errors = [], []
        for path in sorted(paths)[:200]:
            try:
                result = self.import_protocol(dict(protocol=path))
                imported.extend(result["imported"])
                errors.extend(result["errors"])
            except (ValueError, RuntimeError, ImportError, OSError, KeyError) as error:
                errors.append(dict(protocol=path, error=str(error)))
        if not self.store.list("policy_version"):
            try:
                imported.append(self.save_current(dict(name="当前规则策略"))["id"])
            except (ValueError, RuntimeError) as error:
                errors.append(dict(error=str(error)))
        return dict(imported=list(dict.fromkeys(imported)), errors=errors,
                    scanned=min(len(paths), 200), truncated=len(paths) > 200)

    def capture_run(self, run_id, protocol_path, name, parent_id=None):
        run = self.store.get("office_pilot", run_id)
        path = self._local(protocol_path)
        raw = path.read_bytes()
        checksum = hashlib.sha256(raw).hexdigest()
        if (not run or run.get("mode") != "training" or run.get("status") != "COMPLETE"
                or run.get("protocol_sha256") != checksum or not run.get("checkpoint")):
            raise ValueError("仅可登记与冻结方案匹配的已完成训练检查点")
        plan = json.loads(raw)
        workload = run.get("workload") or {}
        workloads = []
        updates = sum(bool(item.get("updated")) for item in workload.get("updates", []))
        source_tasks = len(run.get("case_ids", []))
        for segment_id in dict.fromkeys(run.get("previous_segments", [])):
            segment = self.store.get("office_pilot", segment_id) or {}
            if segment.get("mode") != "training" or segment.get("protocol_sha256") != checksum:
                raise ValueError("续训段与冻结训练方案不一致")
            workloads.append(segment.get("workload") or {})
            updates += sum(bool(item.get("updated")) for item in (segment.get("workload") or {}).get("updates", []))
            source_tasks += len(segment.get("admitted", []))
        if not updates and not (workload.get("update") or {}).get("updated"):
            raise ValueError("训练没有真实策略更新，不能登记为已训练版本")
        job_id = (plan.get("policy_training") or {}).get("job_id")
        job = self.store.get("policy_training", job_id) if job_id else None
        recipe_id = self._ensure_recipe(path, plan, name, (job or {}).get("grader_command"))["id"]
        source = dict(kind="office_training", run_id=run_id, protocol_sha256=checksum)
        metrics = self.combined_metrics([*workloads, workload])
        metrics["scope"] = "training"
        # Bind development results to the exact original checkpoint/config, not
        # merely to the same preference name or to a better-looking run.
        config = copy.deepcopy(plan["scheduler"])
        config["policy"] = dict(kind="ppo", checkpoint=run["checkpoint"]["path"],
            checkpoint_sha256=run["checkpoint"]["sha256"], learner_config=plan.get("learner_config", {}), seed=plan.get("seed", 0))
        validation_config_id = digest(freeze_config(config))
        validations = [item for item in self.store.list("office_pilot")
            if item.get("protocol_sha256") == checksum and item.get("mode") == "validation_ppo"
            and item.get("status") == "COMPLETE"
            and ((item.get("workload") or {}).get("summary") or {}).get("config_id") == validation_config_id]
        if validations:
            validation = max(validations, key=lambda item: item.get("finished_at", ""))
            metrics = self.metrics(validation["workload"])
            metrics["scope"] = "frozen_development"
            source["validation_run_id"] = validation["id"]
        return self._register(plan["scheduler"], run["checkpoint"], name=name, parent_id=parent_id,
            source=source, metrics=metrics, training=dict(tasks=source_tasks, updates=updates),
            recipe_id=recipe_id, learner_config=plan.get("learner_config", {}), seed=plan.get("seed", 0))

    def capture_training_checkpoint(self, job, run, receipt):
        checkpoint = receipt.get("checkpoint")
        if not checkpoint:
            raise ValueError("真实训练更新缺少检查点")
        snapshot = receipt.get("policy") or {}
        learner = snapshot.get("learner") or {}
        plan_path = job.get("protocol") or job.get("protocol_path")
        plan = json.loads(self._local(plan_path).read_bytes())
        recipe = self._ensure_recipe(plan_path, plan, job.get("name"), job.get("grader_command"))
        return self._register(plan["scheduler"], checkpoint,
            name=job.get("name", "训练") + " · 更新" + str(receipt.get("update_index", learner.get("policy_version", ""))),
            parent_id=job.get("parent_id"),
            source=dict(kind="training_checkpoint", job_id=job["id"], run_id=run.get("id") if isinstance(run, dict) else run,
                        checkpoint_sha256=checkpoint["sha256"]),
            training=dict(tasks=(job.get("progress") or {}).get("finished"), updates=learner.get("policy_version")),
            recipe_id=recipe["id"], learner_config=plan.get("learner_config", {}), seed=plan.get("seed", 0))

    def busy(self):
        from seampact.policy_training import ACTIVE
        manager = getattr(self.platform, "policy_training", None)
        if manager and manager.busy:
            return dict(can_switch=False, reason="训练任务正在运行或收尾，请等待完整结算")
        active = [job for job in self.store.list("policy_training") if job.get("status") in ACTIVE]
        if active:
            return dict(can_switch=False, reason="训练正在进行，请先在结算块后暂停")
        execution = self.platform.execution
        with execution.condition:
            if (execution.tasks.active or execution.queue or execution.running or execution.elastic_active
                    or execution._has_held() or execution.workload and execution.workload["status"] in {"RUNNING", "DRAINING"}):
                return dict(can_switch=False, reason="任务或实例尚未收尾，请等待完成并释放实例")
        if self.store.tasks_with_status("RUNNING", "CANCELLING", summary=True):
            return dict(can_switch=False, reason="任务正在准备或执行，请等待结束")
        return dict(can_switch=True, reason=None)

    @contextmanager
    def admission_boundary(self):
        """Match scheduler -> deployment -> project lock order for control changes.

        Chat admission already uses deployment -> project. Taking the project
        lock first here would deadlock against a simultaneous chat submission.
        """
        execution = self.platform.execution
        deployments = getattr(self.platform, "deployments", None) or getattr(execution, "deployments", None)
        deployment_lock = getattr(deployments, "lock", None)
        chat_lock = getattr(getattr(self.platform, "chat", None), "_start_lock", self.lock)
        with execution.condition, deployment_lock if deployment_lock is not None else nullcontext(), chat_lock:
            yield

    def save_current(self, body):
        execution = self.platform.execution
        with self.admission_boundary():
            status = self.busy()
            if not status["can_switch"]:
                raise ValueError(status["reason"])
            config, policy = execution.config, execution.policy
            checkpoint, temporary = None, None
            learner = getattr(policy, "learner", None)
            if learner:
                if learner.rollout:
                    raise ValueError("请先结算当前训练块，再保存策略版本")
                temporary = self.root / (uuid.uuid4().hex + ".pt")
                policy.save(temporary)
                checkpoint = dict(path=str(temporary), sha256=_sha(temporary))
            try:
                return self._register(config, checkpoint, name=body.get("name"), notes=body.get("notes", ""),
                    source=dict(kind="current", session=execution.session),
                    learner_config=learner.config if learner else None, seed=learner.seed if learner else 0)
            finally:
                if temporary:
                    temporary.unlink(missing_ok=True)

    def select(self, body):
        record = self.get_internal(body.get("version_id"))
        with self.admission_boundary():
            status = self.busy()
            if not status["can_switch"]:
                raise ValueError(status["reason"])
            reference = {key: record.get(key) for key in ("id", "name", "parent_id", "kind", "checkpoint_sha256")}
            self.platform.execution.configure(record["config"], policy_version=reference)
        return dict(active=reference)

    def _summary(self, record):
        result = {key: copy.deepcopy(record.get(key)) for key in ("id", "name", "notes", "kind", "created_at", "parent_id",
            "source", "metrics", "training", "recipe_id", "checkpoint_sha256", "feature_schema")}
        result.update(compatible=record.get("feature_schema") == FEATURE_SCHEMA, error=None)
        checkpoint = record.get("checkpoint")
        if checkpoint and not Path(checkpoint["path"]).is_file():
            result.update(compatible=False, error="保存的策略工件不存在")
        return result

    def snapshot(self):
        versions = [self._summary(row) for row in self.store.list("policy_version")]
        recipes = [{key: copy.deepcopy(row.get(key)) for key in ("id", "name", "project_id", "training_tasks", "protocol_sha256")}
                   for row in self.store.list("policy_recipe")]
        active = self.platform.execution.policy_context()
        jobs = getattr(self.platform, "policy_training", None)
        return dict(versions=versions, active=active if active.get("id") else None, recipes=recipes,
                    jobs=jobs.snapshot().get("jobs", []) if jobs else [], busy=self.busy())

    def detail(self, identity):
        record = self.get_internal(identity)
        tasks = []
        with self.store.lock:
            rows = self.store.db.execute("""SELECT id,data FROM documents WHERE kind='task'
                AND (json_extract(data,'$.policy_version.id')=? OR json_extract(data,'$.execution.policy_version.id')=?)
                ORDER BY rowid DESC LIMIT 30""", (identity, identity)).fetchall()
        for task_id, raw in rows:
            task = json.loads(raw)
            result = task.get("objective_result") or {}
            tasks.append(dict(id=task_id, status=task.get("status"), started_at=task.get("started_at"),
                response=task.get("response"), latency_ms=task.get("latency_ms"),
                quality=result.get("quality"), delivered=result.get("delivered"),
                policy_version=task.get("policy_version") or (task.get("execution") or {}).get("policy_version")))
        # Historical experiments did not bind version labels to tasks. Keep the
        # verified source relation explicit instead of relabeling training data
        # as if every task had run with the final checkpoint.
        source = record.get("source") or {}
        for key, relation in (("validation_run_id", "frozen_validation"), ("run_id", "source_training")):
            run = self.store.get("office_pilot", source.get(key)) if source.get(key) else None
            if not run:
                continue
            known = {task["id"] for task in tasks}
            for task_id in run.get("task_ids", [])[-30:]:
                if task_id in known:
                    continue
                task = self.store.get("task", task_id)
                if task:
                    tasks.append(dict(id=task_id, status=task.get("status"), started_at=task.get("started_at"),
                        latency_ms=task.get("latency_ms"), relation=relation,
                        quality=(task.get("objective_result") or {}).get("quality")))
        return dict(version=record, tasks=tasks)
