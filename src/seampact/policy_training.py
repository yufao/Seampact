"""Serial, durable jobs around the existing frozen office training protocol.

Task admission, native quality, PPO settlement and safe resume points continue
to belong to office_pilot and the ordinary runtime. No evaluation arm is run.
"""
import copy
from functools import wraps
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import subprocess
import threading
import time
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import uuid

from seampact import office_pilot
from seampact.store import now


KIND = "policy_training"
ACTIVE = {"QUEUED", "STARTING", "RUNNING", "PAUSING", "REGISTERING"}
COMPONENTS = ("quality", "latency", "ram", "vram", "api")


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".partial")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class TrainingInterrupted(RuntimeError):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


class PolicyTraining:
    def __init__(self, platform, versions):
        self.platform, self.versions = platform, versions
        self.root = Path(platform.home) / "policy-training"
        self.lock = threading.RLock()
        self.thread = None
        self.active_id = None
        self.pause_requested = threading.Event()
        self.shutdown = threading.Event()
        self.abandoned = threading.Event()
        self.shutdown_deadline = None
        self.hooks = []
        self.runtime_changed = False
        self.sender = None
        # A restarted process cannot prove that an in-flight rollout was settled.
        for job in platform.store.list(KIND):
            if job.get("status") in ACTIVE:
                job.update(status="INTERRUPTED", resumable=False, finished_at=now(),
                    error="平台进程已中断；未结算的 rollout 不能从最近更新检查点直接续跑。可从已登记版本分支。")
                self._save(job)

    def _save(self, job):
        if self.abandoned.is_set():
            return
        job["updated_at"] = now()
        progress = job.setdefault("progress", {})
        progress.update(finished=job.get("completed_tasks", 0), total=job.get("planned_tasks", 0),
            updates=job.get("actual_updates", 0))
        progress.setdefault("admitted", job.get("completed_tasks", 0))
        if job.get("version_id"):
            job["result_version_id"] = job["version_id"]
        self.platform.store.save(KIND, job["id"], job)
        _write(self.root / job["id"] / "state.json", job)

    def detail(self, identity):
        job = self.platform.store.get(KIND, identity)
        if not job:
            raise ValueError("没有这个策略训练任务")
        return job

    def snapshot(self):
        fields = ("id", "name", "status", "mode", "recipe_id", "parent_id", "version_id", "run_id",
                  "created_at", "updated_at", "completed_tasks", "planned_tasks", "actual_updates",
                  "resumable", "error", "next_index", "restore_status", "restore_error", "cleanup_status", "cleanup_error")
        return dict(active_id=self.active_id, jobs=[{key: copy.deepcopy(job.get(key)) for key in (*fields, "progress", "result_version_id")}
                    for job in reversed(self.platform.store.list(KIND))])

    @property
    def busy(self):
        return bool(self.thread and self.thread.is_alive())

    def start(self, body):
        allowed = {"mode", "recipe_id", "parent_id", "name", "learner_overrides", "objective_overrides",
                   "seed", "update_after_tasks"}
        if not isinstance(body, dict) or set(body) - allowed:
            raise ValueError("训练任务含未知设置")
        mode = body.get("mode", "fresh")
        if mode not in {"fresh", "resume", "branch"}:
            raise ValueError("训练模式须为 fresh、resume 或 branch")
        parent = body.get("parent_id")
        if bool(parent) != (mode in {"resume", "branch"}):
            raise ValueError("resume/branch 须选择父策略；fresh 不使用父策略")
        parent_record = self.versions.get_internal(parent) if parent else None
        recipe = self.versions.get_recipe(body.get("recipe_id"))
        source = Path(recipe["protocol"])
        if _sha(source) != recipe["protocol_sha256"]:
            raise ValueError("已登记训练方案的冻结协议发生变化")
        plan = json.loads(source.read_bytes())
        if parent_record:
            inherited = parent_record.get("learner_config", (parent_record.get("config", {}).get("policy") or {}).get("learner_config", {}))
            plan["learner_config"] = dict(copy.deepcopy(inherited), **plan.get("learner_config", {}))
            plan.setdefault("seed", parent_record.get("seed", 0))
        if not plan.get("continuous_training") or plan.get("schema") != "seampact.office-comparison/1":
            raise ValueError("策略训练任务需要已冻结的连续 office 训练方案")
        if plan.get("local_development_only"):
            raise ValueError("开发限定方案不能用于策略训练")
        if not plan.get("development_evidence") or not plan.get("scheduler", {}).get("objective"):
            raise ValueError("训练方案尚无真实训练集校准证据与成本尺度")
        # Validate manifests, split isolation, arrival schedule and diagnostics before queuing.
        _, _, cases = office_pilot._read_plan(dict(protocol=str(source), protocol_sha256=recipe["protocol_sha256"]))
        plan = self._overrides(plan, body)
        grader = copy.deepcopy(recipe.get("grader_command"))
        if grader is not None and (not isinstance(grader, list) or not grader
                or any(not isinstance(arg, str) or not arg for arg in grader) or grader.count("{collection}") != 1):
            raise ValueError("独立表格评分命令须含且仅含一个 {collection} 参数")
        if any(cases[key].get("_source") == "spreadsheet" for key in plan["cases"]["training"]) and not grader:
            raise ValueError("训练题包含表格任务，须在冻结训练方案登记独立原生评分命令")
        identity = uuid.uuid4().hex
        name = body.get("name") or "策略训练 " + identity[:8]
        if not isinstance(name, str) or not name.strip() or len(name) > 80:
            raise ValueError("训练任务名称须为 1 至 80 字符")
        plan.pop("parent_transfer", None)
        plan.pop("policy_version_parent", None)
        if parent:
            plan["policy_version_parent"] = dict(id=parent, mode=mode)
        plan["policy_training"] = dict(job_id=identity, run_nonce=uuid.uuid4().hex,
            recipe_id=recipe["id"], recipe_protocol_sha256=recipe["protocol_sha256"], parent_id=parent, mode=mode)
        protocol = self.root / identity / "frozen-training-protocol.json"
        with self.versions.admission_boundary(), self.lock:
            self._available()
            _write(protocol, plan)
            # Revalidate the final frozen copy including the requested objective.
            office_pilot._read_plan(dict(protocol=str(protocol), protocol_sha256=_sha(protocol)))
            job = dict(id=identity, name=name.strip(), mode=mode, recipe_id=recipe["id"], parent_id=parent,
                status="QUEUED", created_at=now(), protocol=str(protocol), protocol_sha256=_sha(protocol),
                source_protocol_sha256=recipe["protocol_sha256"], grader_command=grader,
                completed_tasks=0, planned_tasks=len(plan["cases"]["training"]), actual_updates=0,
                checkpoints=[], segment_ids=[], resumable=False, settings=dict(learner_config=plan.get("learner_config", {}),
                    objective=plan["scheduler"]["objective"], seed=plan.get("seed", 0),
                    update_after_tasks=plan.get("update_after_tasks", 16)))
            self._remember_runtime(job)
            self._save(job)
            self._launch(job, resuming=False)
            return self.detail(identity)

    def _overrides(self, original, body):
        from seampact.scheduling.learners import Learner, default_config
        from seampact.scheduling.unified_objective import freeze_objective
        plan = copy.deepcopy(original)
        learner = body.get("learner_overrides", {})
        objective = body.get("objective_overrides", {})
        if not isinstance(learner, dict) or not isinstance(objective, dict) or set(objective) - {"preference", "smoothing", "scales"}:
            raise ValueError("训练参数或目标覆盖设置不正确")
        plan["learner_config"] = dict(plan.get("learner_config", {}), **learner)
        options = dict(default_config("ppo"), gamma=1.0, gae_lambda=1.0)
        options.update(plan["learner_config"])
        if set(options) - set(default_config("ppo")) - {"value_components"}:
            raise ValueError("未知 PPO learner 参数")
        if options.get("gamma", 1) != 1 or options.get("gae_lambda", 1) != 1:
            raise ValueError("完整训练工作负载要求 gamma=1、gae_lambda=1")
        Learner._validate_config(type("Settings", (), {"config": options})())
        if "seed" in body:
            if type(body["seed"]) is not int:
                raise ValueError("随机种子须为整数")
            plan["seed"] = body["seed"]
        if "update_after_tasks" in body:
            cadence = body["update_after_tasks"]
            if type(cadence) is not int or not 1 <= cadence <= len(plan["cases"]["training"]):
                raise ValueError("更新间隔须为训练题数范围内的正整数")
            plan["update_after_tasks"] = cadence
        target = plan["scheduler"]["objective"]
        if "scales" in objective and objective["scales"] != target["scales"]:
            raise ValueError("成本尺度来自冻结的真实训练校准，不能在训练任务中改写")
        if "preference" in objective or "smoothing" in objective:
            if not target.get("pareto"):
                raise ValueError("该冻结方案尚未启用五维 Pareto 目标")
            target["pareto"].update({key: copy.deepcopy(objective[key]) for key in ("preference", "smoothing") if key in objective})
            target["weights"] = dict(target["pareto"]["preference"], delivery=0)
            if "objective_weights" in plan:
                plan["objective_weights"] = copy.deepcopy(target["weights"])
        plan["scheduler"]["objective"] = freeze_objective(target)
        return plan

    def _available(self):
        if self.shutdown.is_set():
            raise ValueError("平台正在关闭，不能启动训练")
        if self.busy:
            raise ValueError("已有策略训练任务运行；请先在块边界暂停")
        status = self.versions.busy()
        if not status["can_switch"]:
            raise ValueError(status["reason"])

    def _launch(self, job, *, resuming):
        self.pause_requested.clear()
        self.runtime_changed = False
        self.active_id = job["id"]
        self.thread = threading.Thread(target=self._worker, args=(job, resuming), daemon=True,
            name="seampact-policy-training-" + job["id"][:8])
        self.thread.start()

    def pause(self, body):
        with self.versions.admission_boundary(), self.lock:
            job = self.detail(body.get("job_id", body.get("id")))
            if job["status"] == "PAUSED":
                return job
            if job["id"] != self.active_id or not self.busy or job["status"] not in ACTIVE:
                raise ValueError("只有运行中的训练任务可以请求暂停")
            job["status"] = "PAUSING"
            self.pause_requested.set()
            self._save(job)
            return job

    def resume(self, body):
        if set(body) - {"job_id", "id"}:
            raise ValueError("暂停续训必须沿用原协议，不能修改参数")
        with self.versions.admission_boundary(), self.lock:
            self._available()
            job = self.detail(body.get("job_id", body.get("id")))
            if job["status"] != "PAUSED" or not job.get("resumable"):
                raise ValueError("仅完整结算且已保存的 PAUSED 训练任务可以续跑")
            if _sha(job["protocol"]) != job["protocol_sha256"]:
                raise ValueError("续训协议已发生变化")
            if job.get("run_id"):
                series = self.platform.store.get("office_pilot_series", job["protocol_sha256"]) or {}
                saved = series.get("workloads", {}).get("training:0", {})
                if saved.get("status") != "PAUSED" or saved.get("id") != job["run_id"]:
                    raise ValueError("暂停点与原冻结序列不一致")
                checkpoint = saved.get("checkpoint") or {}
                if not checkpoint.get("path") or _sha(checkpoint["path"]) != checkpoint.get("sha256"):
                    raise ValueError("暂停检查点缺失或身份变化")
            job.update(status="QUEUED", resumable=False)
            self._remember_runtime(job)
            self._save(job)
            self._launch(job, resuming=bool(job.get("run_id")))
            return self.detail(job["id"])

    def cleanup(self, body):
        if not isinstance(body, dict) or set(body) - {"job_id", "id"}:
            raise ValueError("重新收尾只接受训练任务身份")
        with self.versions.admission_boundary(), self.lock:
            if self.shutdown.is_set():
                raise ValueError("平台正在关闭，不能重新收尾")
            if self.busy or any(row.get("status") in ACTIVE for row in self.platform.store.list(KIND)):
                raise ValueError("已有训练任务运行，不能重新收尾另一个任务")
            job = self.detail(body.get("job_id", body.get("id")))
            if job.get("status") not in {"FAILED", "INTERRUPTED"}:
                raise ValueError("仅失败或中断的训练任务可以重新收尾")
            run_id = job.get("run_id")
            run = self.platform.store.get("office_pilot", run_id) if run_id else None
            workload = self.platform.execution.workload
            if (not run or run.get("mode") != "training" or run.get("protocol_sha256") != job.get("protocol_sha256")
                    or run_id not in job.get("segment_ids", []) or not workload or workload.get("id") != run_id):
                raise ValueError("当前执行器不属于该训练任务，不能释放或覆盖其他工作负载")
            self._cleanup_failure(job, run, timeout_s=3)
            # A process can reopen a retained failed job with no live worker.
            # Restoration remains guarded by configure's task/instance checks.
            previous_changed = self.runtime_changed
            self.runtime_changed = True
            try:
                self._restore_runtime(job)
            finally:
                self.runtime_changed = previous_changed
            return self.detail(job["id"])

    def _check_shutdown(self):
        if self.abandoned.is_set() or (self.shutdown_deadline is not None and time.monotonic() >= self.shutdown_deadline):
            raise TrainingInterrupted("平台关闭时当前块尚未完整结算；保留真实更新及任务记录，未结算 rollout 不可直接续训")

    def _wait(self, seconds=.1):
        # Event.wait on a set shutdown event would spin while allowing the block to drain.
        time.sleep(max(0, min(seconds, .1)))
        self._check_shutdown()

    def _models_ready(self):
        """Probe only selected local services; never substitute a paid provider."""
        profile = self.platform.profiles.role_profile("chat")
        if not profile.get("model") or not profile.get("base_url"):
            raise RuntimeError("训练业务模型尚未配置，请选择并启动既有本地模型服务")
        if profile.get("resource_kind") == "managed":
            service_id = profile.get("managed_service_id")
            groups = self.platform.managed_services.public()
            group = next((item for item in groups if item.get("id", "").removeprefix("service:") == str(service_id).removeprefix("service:")), None)
            if not group or not any(row.get("state") == "READY" and row.get("accepting")
                    and not row.get("pending_stop") for row in group.get("instances", [])):
                raise RuntimeError("训练业务模型服务尚未启动或尚未 READY，请先启动已选本地服务")
            return
        parsed = urlsplit(profile["base_url"])
        if parsed.scheme not in {"http", "https"}:
            raise RuntimeError("当前模型连接不是 HTTP/HTTPS；训练就绪检测须使用已登记托管本地服务或本地 HTTP 服务")
        try:
            local = parsed.hostname == "localhost" or ipaddress.ip_address(parsed.hostname).is_private or ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            local = False
        if not local:
            raise RuntimeError("策略训练需要已配置的本地模型服务；当前绑定不是本地服务，未调用付费 API")
        # A model-list GET does not create an inference request.
        try:
            headers = {}
            if (profile.get("auth") or {}).get("mode") not in {None, "none"}:
                key = self.platform.profiles.role_key("chat")
                if key:
                    headers["Authorization"] = "Bearer " + key
            request = Request(profile["base_url"].rstrip("/") + "/models", headers=headers)
            with build_opener(_NoRedirect()).open(request, timeout=3) as response:
                models = json.loads(response.read(1024 * 1024)).get("data", [])
            if not any(row.get("id") == profile["model"] for row in models):
                raise RuntimeError("所选模型未出现在本地服务的模型清单中")
        except Exception as error:
            raise RuntimeError("训练业务模型不可用，请检查已选本地服务的运行状态及模型名称") from error

    def _pilot(self, job, op, **fields):
        self._check_shutdown()
        return office_pilot.handle(self.platform, dict(protocol=job["protocol"],
            protocol_sha256=job["protocol_sha256"], op=op, **fields))

    def _remember_runtime(self, job):
        execution = self.platform.execution
        job["original_config"] = copy.deepcopy(execution.config)
        context = execution.policy_context()
        job["original_policy_context"] = {key: copy.deepcopy(context[key]) for key in
            ("id", "name", "parent_id", "kind", "checkpoint_sha256", "training_job_id") if key in context}
        job["original_policy_training"] = bool(context.get("training") or getattr(execution.policy, "training", False))

    def _restore_runtime(self, job):
        if not self.runtime_changed:
            job["restore_status"] = "NOT_NEEDED"
        elif self.shutdown.is_set():
            job.update(restore_status="SKIPPED_SHUTDOWN", restore_error="平台正在关闭；保留当前任务与实例状态，不重置尚未收尾的执行器")
        else:
            try:
                # configure itself rejects active tasks and held instances. It
                # loads the original frozen inference config without restoring
                # any in-memory training rollout or changing its saved version.
                self.platform.execution.configure(job["original_config"], policy_version=job["original_policy_context"])
                job["restore_status"] = "RESTORED_FROZEN_INFERENCE"
                job.pop("restore_error", None)
            except Exception as error:
                job.update(restore_status="REQUIRES_SELECTION", restore_error="训练前策略未恢复，请待任务和实例收尾后手动选择策略：" + str(error))
        self._save(job)

    def _attach_checkpoints(self, job, run):
        policy = self.platform.execution.policy
        for name in ("update_interval", "update_workload"):
            original = getattr(policy, name, None)
            if original is None:
                continue
            @wraps(original)
            def capture(*args, _original=original, **kwargs):
                result = _original(*args, **kwargs)
                if result.get("updated") and not self.abandoned.is_set():
                    index = len(job["checkpoints"]) + 1
                    path = self.root / job["id"] / "checkpoints" / ("update-%06d.pt" % index)
                    policy.save(path)
                    receipt = dict(update_index=index, run_id=run["id"], checkpoint=dict(path=str(path), sha256=_sha(path)),
                        update=copy.deepcopy(result), policy=policy.snapshot(), captured_at=now())
                    job["checkpoints"].append(receipt)
                    job["actual_updates"] = index
                    self._save(job)
                    version = self.versions.capture_training_checkpoint(copy.deepcopy(job), copy.deepcopy(run), receipt)
                    if version:
                        receipt["version_id"] = version.get("id")
                        self._bind_version(job, version)
                    self._save(job)
                return result
            setattr(policy, name, capture)
            self.hooks.append((policy, name, original))

    def _bind_version(self, job, version=None):
        reference = dict(name=job["name"], parent_id=job.get("parent_id"), kind="ppo", training_job_id=job["id"])
        if version:
            reference.update({key: version[key] for key in ("id", "name", "checkpoint_sha256") if key in version})
        elif job.get("checkpoints"):
            receipt = job["checkpoints"][-1]
            reference.update(id=receipt.get("version_id"), checkpoint_sha256=receipt["checkpoint"]["sha256"])
        elif job["mode"] == "resume":
            parent = self.versions.get_internal(job["parent_id"])
            reference.update(id=parent["id"], checkpoint_sha256=parent.get("checkpoint_sha256"))
        self.platform.execution.bind_policy_version(reference)

    def _restore_hooks(self):
        for policy, name, original in self.hooks:
            setattr(policy, name, original)
        self.hooks.clear()

    def _worker(self, job, resuming):
        run = None
        try:
            self._check_shutdown()
            job["status"] = "STARTING"
            self._save(job)
            self._models_ready()
            plan = json.loads(Path(job["protocol"]).read_bytes())
            # Prebuild only the frozen training split; held-out tasks stay untouched.
            if not resuming:
                for case_id in plan.get("prebuild", []):
                    if case_id in plan["cases"]["training"]:
                        self._pilot(job, "prebuild", case_id=case_id)
            if self.pause_requested.is_set() and not resuming:
                job.update(status="PAUSED", resumable=True, next_index=0)
                self._save(job)
                return
            self.runtime_changed = True
            run = self._pilot(job, "resume" if resuming else "start", mode="training", batch=0)
            job.update(run_id=run["id"], status="RUNNING", started_at=job.get("started_at") or now())
            job["segment_ids"].append(run["id"])
            self._save(job)
            self._attach_checkpoints(job, run)
            self._bind_version(job)
            count = 0
            last_requested = 0
            for block in run["arrival_blocks"]:
                self._check_shutdown()
                # The prior block's models, workspace and native scores have settled.
                if count and (self.pause_requested.is_set() or self.shutdown.is_set()):
                    self._settle(job, run, prefix=True)
                    return
                self._models_ready()
                if count - last_requested >= plan.get("update_after_tasks", 16):
                    receipt = self._pilot(job, "update", run_id=run["id"])
                    _write(self.root / job["id"] / ("update-request-%s-%d.json" % (run["id"], count)), receipt)
                    if receipt.get("pending"):
                        last_requested = count
                self._run_block(job, run, block)
                count += len(block["indices"])
                job["completed_tasks"] = run.get("start_index", 0) + count
                self._save(job)
            self._settle(job, run, prefix=False)
        except TrainingInterrupted as error:
            job.update(status="INTERRUPTED", resumable=False, error=str(error), finished_at=now())
            self._cleanup_failure(job, run)
            self._save(job)
        except Exception as error:
            job.update(status="FAILED", resumable=False, error=str(error), finished_at=now())
            self._cleanup_failure(job, run)
            self._save(job)
        finally:
            self._restore_hooks()
            if not self.abandoned.is_set():
                self._restore_runtime(job)
            with self.lock:
                if self.active_id == job["id"]:
                    self.active_id = None

    def _cleanup_failure(self, job, run, *, timeout_s=10):
        """Release a failed real prefix without training it or claiming safe resume."""
        if run is None or self.abandoned.is_set():
            return
        try:
            self._check_shutdown()
            if self.sender and self.sender.is_alive():
                raise RuntimeError("派发线程尚未退出，保留现场等待收尾")
            row = self.platform.store.get("office_pilot", run["id"]) or copy.deepcopy(run)
            if row.get("status") == "COMPLETE":
                job["cleanup_status"] = "ALREADY_COMPLETE"
                job.pop("cleanup_error", None)
                return
            execution = self.platform.execution
            workload = execution.workload
            if not workload or workload.get("id") != run["id"]:
                raise RuntimeError("执行器不再对应失败训练序列，未重置其他工作负载")
            admitted = list(row.get("admitted", []))
            started = set(workload.get("started", []))
            if set(admitted) != started:
                # An admission can fail after the runtime accepted its task.
                admitted = workload["task_ids"][:len(started)]
                if set(admitted) != started:
                    raise RuntimeError("已接入任务不是原冻结序列前缀，须人工核对")
            deadline = time.monotonic() + timeout_s
            for task_id in admitted:
                while True:
                    task = self.platform.store.get("task", task_id)
                    if task and task.get("status") not in {"RUNNING", "CANCELLING"}:
                        if task.get("workspace_status") == "FINISHED":
                            break
                        try:
                            task = self._confirm_workspace(task, deadline=deadline)
                            if task.get("workspace_status") == "FINISHED":
                                break
                        except TrainingInterrupted:
                            raise
                        except RuntimeError:
                            if time.monotonic() >= deadline:
                                raise
                    if time.monotonic() >= deadline:
                        raise RuntimeError("已接入任务仍在执行；任务终结并释放实例后才可切换策略")
                    self._wait()
            while True:
                prefix = (run["id"], admitted) if len(admitted) < len(workload["task_ids"]) else None
                state = execution.finish_workload(admitted_prefix=prefix)
                if state.get("status") == "COMPLETE":
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError("已请求失败前缀释放；实例仍未确认收尾，暂不能切换策略")
                self._wait()
            row.update(status="UNSETTLED", admitted=admitted, workload=state, error=job["error"], finished_at=now())
            self.platform.store.save("office_pilot", run["id"], row)
            series = self.platform.store.get("office_pilot_series", job["protocol_sha256"]) or dict(id=job["protocol_sha256"], workloads={})
            series.setdefault("workloads", {})["training:0"] = dict(id=run["id"], status="UNSETTLED", updated=False)
            self.platform.store.save("office_pilot_series", job["protocol_sha256"], series)
            job.update(cleanup_status="RELEASED_WITHOUT_UPDATE", result=row)
            job.pop("cleanup_error", None)
            _write(self.root / job["id"] / (run["id"] + "-failed-prefix.json"), row)
        except Exception as error:
            job.update(cleanup_status="REQUIRES_DRAIN", cleanup_error=str(error))

    def _run_block(self, job, run, block):
        # Dispatch clock is independent of collection, matching timed_admissions.
        errors = []
        cancel = threading.Event()
        def send():
            origin = time.monotonic()
            for index, offset in zip(block["indices"], block["offsets_ms"]):
                try:
                    while time.monotonic() < origin + offset / 1000:
                        if cancel.is_set():
                            return
                        self._wait(origin + offset / 1000 - time.monotonic())
                    if cancel.is_set():
                        return
                    admission = self._pilot(job, "admit", run_id=run["id"])
                    if admission.get("task_id") != run["task_ids"][index]:
                        raise RuntimeError("任务派发偏离冻结训练序列")
                    job.setdefault("progress", {})["admitted"] = run.get("start_index", 0) + index + 1
                    self._save(job)
                except Exception as error:
                    errors.append(error)
                    return
        sender = threading.Thread(target=send, daemon=True, name="seampact-training-arrivals")
        self.sender = sender
        sender.start()
        try:
            for index in block["indices"]:
                task_id = run["task_ids"][index]
                while True:
                    if errors:
                        raise errors[0]
                    task = self.platform.store.get("task", task_id)
                    if task and task.get("status") not in {"RUNNING", "CANCELLING"} and task.get("workspace_status"):
                        break
                    self._wait()
                if task.get("workspace_status") != "FINISHED":
                    task = self._confirm_workspace(task)
                _write(self.root / job["id"] / "tasks" / (task_id + ".json"), task)
                evaluation = task.get("evaluation") or {}
                if evaluation.get("source_revision") and "outputs" in evaluation:
                    self._grade_spreadsheet(job, task)
                task = self.platform.store.get("task", task_id)
                objective = task.get("objective_result") or {}
                if objective.get("quality") is None:
                    raise RuntimeError("训练任务原生质量未结算；已保留题目与记录，停止后续派发")
                target = job["settings"]["objective"]
                if (target.get("pareto") or target["weights"].get("api")) and (objective.get("metrics") or {}).get("api_tokens") is None:
                    raise RuntimeError("训练任务的真实 API token 用量缺失，不能训练或标为可续跑")
            while sender.is_alive():
                self._wait()
            if errors:
                raise errors[0]
        finally:
            # No next block is created until this sender exits. On shutdown it
            # observes the same deadline and cannot continue later admissions.
            cancel.set()
            sender.join(timeout=3.5)
            if not sender.is_alive():
                self.sender = None

    def _confirm_workspace(self, task, *, deadline=None):
        for attempt in range(3):
            self._check_shutdown()
            if deadline is not None and time.monotonic() >= deadline:
                raise RuntimeError("任务工作区仍未确认释放；请待任务收尾后重新尝试")
            scope = task.get("workspace") or {}
            try:
                self.platform.chat.reconcile_task_workspace(dict(task_id=task["id"],
                    project_id=scope.get("project_id") or task.get("project_id"),
                    conversation_id=scope.get("conversation_id") or task.get("conversation_id")))
            except ValueError:
                pass
            task = self.platform.store.get("task", task["id"])
            if task.get("workspace_status") == "FINISHED":
                return task
            if attempt < 2:
                retry_at = time.monotonic() + 2
                if deadline is not None:
                    retry_at = min(retry_at, deadline)
                while time.monotonic() < retry_at:
                    self._wait(retry_at - time.monotonic())
        raise RuntimeError("任务工作区尚未确认完整释放，训练记录已保留")

    def _settle(self, job, run, *, prefix):
        while True:
            result = self._pilot(job, "end_prefix" if prefix else "drain", run_id=run["id"])
            if result.get("status") != "DRAINING":
                break
            self._wait()
        if not prefix:
            result = self._pilot(job, "finish", run_id=run["id"])
        job["result"] = result
        _write(self.root / job["id"] / (run["id"] + "-result.json"), result)
        if prefix:
            if result.get("status") != "PAUSED":
                raise RuntimeError("已接入前缀未完整结算；不能标为可续训：" + str(result.get("error")))
            job.update(status="PAUSED", resumable=True, next_index=result["next_index"], checkpoint=result["checkpoint"])
            self._save(job)
            return
        if result.get("status") != "COMPLETE":
            raise RuntimeError("训练未完成真实结算：" + str(result.get("error")))
        actual = job["actual_updates"] > 0
        if not actual:
            raise RuntimeError("训练序列没有真实策略更新，不能登记为已训练版本")
        job.update(status="REGISTERING", checkpoint=result["checkpoint"])
        self._save(job)
        version = self.versions.capture_run(result["id"], job["protocol"], job["name"], parent_id=job.get("parent_id"))
        job.update(status="COMPLETE", version_id=version["id"], resumable=False, finished_at=now())
        self._save(job)

    def _grade_spreadsheet(self, job, task):
        """Collect final delivered bytes, then invoke only the separately registered scorer."""
        from seampact.office_spreadsheet import SCHEMA, FEEDBACK_SCHEMA, SOURCE_REVISION, apply_native_score
        folder = self.root / job["id"] / "grading" / task["id"]
        (folder / "delivered").mkdir(parents=True, exist_ok=True)
        _write(folder / "task.json", task)
        evaluation, experiment = task["evaluation"], task["experiment"]
        delivered = {item["sha256"] for item in task.get("files", []) if isinstance(item.get("sha256"), str)}
        records = []
        for output in evaluation["outputs"]:
            index, identity = output["index"], output.get("sha256")
            record = dict(index=index, sha256=identity, status="MISSING_DELIVERY", path=None)
            if identity in delivered:
                path = folder / "delivered" / ("replay-" + str(index) + ".xlsx")
                raw = self.platform.capabilities.artifacts.path(identity).read_bytes()
                if hashlib.sha256(raw).hexdigest() != identity:
                    raise RuntimeError("最终交付表格的内容身份不符")
                path.write_bytes(raw)
                record.update(status="DELIVERED", path=path.relative_to(folder).as_posix())
            records.append(record)
        _write(folder / "collection.json", dict(schema=SCHEMA, task_id=task["id"], case_id=experiment["case"],
            task_status=task["status"], solution_sha256=evaluation.get("solution_sha256"), records=records,
            common_business_latency_ms=task.get("latency_ms"), replay=task.get("spreadsheet_replay"),
            api_usage=task.get("api_usage"), objective_result=task.get("objective_result"),
            execution_infrastructure_error=evaluation.get("execution_infrastructure_error"),
            native_score_status="PENDING_SEPARATE_GRADING", scoring_cost_included_in_business_cost=False))
        command = [str(folder.resolve()) if arg == "{collection}" else arg for arg in job["grader_command"]]
        with (folder / "grader.stdout.log").open("w", encoding="utf-8") as stdout, (folder / "grader.stderr.log").open("w", encoding="utf-8") as stderr:
            with subprocess.Popen(command, stdout=stdout, stderr=stderr) as process:
                deadline = time.monotonic() + 900
                try:
                    while process.poll() is None:
                        if time.monotonic() >= deadline:
                            raise RuntimeError("独立表格评分超时，质量仍未知")
                        self._wait()
                    if process.returncode:
                        raise RuntimeError("独立表格评分失败，质量仍未知；详见评分日志")
                except BaseException:
                    process.kill()
                    process.wait(timeout=3)
                    raise
        result = json.loads((folder / "native-score.json").read_bytes())
        for record in records:
            path = folder / "delivered" / ("replay-" + str(record["index"]) + ".xlsx")
            if record["status"] == "MISSING_DELIVERY":
                if path.exists() or path.is_symlink():
                    raise RuntimeError("评分期间出现未交付表格，不能接受评分")
            elif path.is_symlink() or not path.is_file() or _sha(path) != record["sha256"]:
                raise RuntimeError("评分文件与最终交付字节不同，不能接受评分")
        if (result.get("task_id") != task["id"] or result.get("case_id") != experiment["case"]
                or result.get("outputs") != evaluation["outputs"]
                or result.get("manifest_sha256") != experiment["manifest_sha256"]
                or result.get("original_manifest_sha256") != experiment["original_manifest_sha256"]
                or result.get("source_revision") != SOURCE_REVISION):
            raise RuntimeError("独立原生评分与题目、交付文件或评分器身份不符")
        feedback = dict(schema=FEEDBACK_SCHEMA, task_id=task["id"], case_id=experiment["case"],
            manifest_sha256=experiment["manifest_sha256"], original_manifest_sha256=experiment["original_manifest_sha256"],
            outputs=evaluation["outputs"], source_revision=SOURCE_REVISION,
            cases=result.get("cases"), hard=result.get("hard"), soft=result.get("soft"))
        response = apply_native_score(self.platform, feedback)
        _write(folder / "feedback.json", response)
        if response.get("quality") is None:
            raise RuntimeError("独立表格评分质量未知，不能训练")

    def request_shutdown(self):
        self.shutdown.set()
        self.pause_requested.set()
        if self.shutdown_deadline is None:
            self.shutdown_deadline = time.monotonic() + 5

    def close(self):
        self.request_shutdown()
        worker = self.thread
        if worker:
            worker.join(timeout=6)
            if worker.is_alive():
                # Blocked external work is explicitly not a safe resume point.
                identity = self.active_id
                if identity:
                    job = self.detail(identity)
                    job.update(status="INTERRUPTED", resumable=False, finished_at=now(),
                        error="平台关闭等待超时；保留已有检查点，当前未结算 rollout 不能直接续训")
                    self._save(job)
                self.abandoned.set()
