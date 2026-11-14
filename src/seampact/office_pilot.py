"""Frozen office arrivals around the ordinary runtime."""
import copy
import hashlib
import json
import threading
import uuid
from pathlib import Path

from seampact.office_experiment import start_native_task
from seampact.office_development import DEVELOPMENT_SOURCES
from seampact.store import now


_admission_lock = threading.Lock()


def arrival_waves(plan, ids, cases):
    """Pack adjacent task-private environments; shared project fixture changes run alone."""
    width = plan.get("arrival_width", 1)
    if type(width) is not int or width < 1:
        raise ValueError("并发派发宽度须为正整数")
    waves, wave = [], []
    for index, identity in enumerate(ids):
        case = cases[identity]
        exclusive = bool(case.get("_source") or case.get("rag_adaptation"))
        if wave and (exclusive or len(wave) == width):
            waves.append(wave)
            wave = []
        if exclusive:
            waves.append([index])
        else:
            wave.append(index)
    if wave:
        waves.append(wave)
    return waves


def scheduled_blocks(plan, split, ids, cases):
    """Each block shares a clock. Its admissions do not wait for earlier waves."""
    schedule = plan.get("arrival_schedule")
    if schedule is None:
        return [dict(indices=wave, offsets_ms=[0] * len(wave), profile="batch")
                for wave in arrival_waves(plan, ids, cases)]
    if not plan.get("continuous_training") or set(schedule) != set(plan["cases"]):
        raise ValueError("定时派发需要三个完整划分的连续服务协议")
    result, start = [], 0
    width = plan["arrival_width"]
    if type(width) is not int or width < 1:
        raise ValueError("到达宽度无效")
    for block in schedule[split]:
        offsets = block["offsets_ms"]
        if (not isinstance(offsets, list) or not offsets or any(type(x) is not int or x < 0 for x in offsets)
                or offsets != sorted(offsets) or offsets[0] != 0 or start + len(offsets) > len(ids)
                or max(offsets.count(x) for x in set(offsets)) > width):
            raise ValueError("冻结的到达时刻与题目或宽度不匹配")
        indices = list(range(start, start + len(offsets)))
        if len(indices) > 1 and any(cases[ids[i]].get("rag_adaptation") or
                                    cases[ids[i]].get("_source") == "office_rag" for i in indices):
            raise ValueError("替换共享检索资料的任务须独占派发段")
        result.append(dict(indices=indices, offsets_ms=offsets, profile=block["profile"]))
        start += len(indices)
    if start != len(ids):
        raise ValueError("冻结到达表未覆盖该划分")
    return result


def _read_plan(body):
    path = Path(body["protocol"])
    raw = path.read_bytes()
    identity = hashlib.sha256(raw).hexdigest()
    if identity != body["protocol_sha256"]:
        raise ValueError("pilot 协议内容身份已变化")
    plan = json.loads(raw)
    legacy = plan.get("schema") == "seampact.office-pilot/1"
    if plan.get("schema") not in {"seampact.office-pilot/1", "seampact.office-comparison/1"}:
        raise ValueError("pilot 协议格式不正确")
    manifest_raw = Path(plan["manifest"]).read_bytes()
    if hashlib.sha256(manifest_raw).hexdigest() != plan["manifest_sha256"]:
        raise ValueError("pilot 来源清单身份已变化")
    manifest = json.loads(manifest_raw)
    indexed = {case["id"]: case for case in manifest["cases"]}
    for source, location in plan.get("source_manifests", {}).items():
        development_source = source in DEVELOPMENT_SOURCES
        if (not development_source and source not in {"privacy", "spreadsheet", "office_rag"}) or legacy:
            raise ValueError("该协议不支持这一混合任务来源")
        raw = Path(location["manifest"]).read_bytes()
        if hashlib.sha256(raw).hexdigest() != location["manifest_sha256"]:
            raise ValueError("混合任务来源清单身份已变化")
        additional = json.loads(raw)
        if development_source:
            from seampact.office_development import grading_by_case
            references = grading_by_case(source, location, additional)
        elif source == "privacy":
            from seampact.office_privacy import all_privacy_cases
            expected = {case["id"]: case for case in all_privacy_cases()}
            if (not isinstance(additional.get("cases"), list) or not additional["cases"]
                    or any(case != expected.get(case.get("id")) for case in additional["cases"])):
                raise ValueError("合成任务实现与冻结题目不同")
        elif source == "office_rag":
            from seampact.office_synthetic_rag import checked
            grading = checked(location["grading_manifest"], location["grading_sha256"])
            if (additional.get("schema") != "seampact.office-rag-runtime/1"
                    or additional.get("grading_sha256") != location["grading_sha256"]
                    or grading.get("schema") != "seampact.office-rag-grading/1"
                    or {c["id"] for c in additional["cases"]} != set(grading["answers"])):
                raise ValueError("合成检索题与评分清单不匹配")
        elif (additional.get("schema") != "seampact.office-spreadsheetbench-v1-subset.v1"
              or additional.get("runtime", {}).get("inputs_only") is not True):
            raise ValueError("表格业务来源须使用仅含输入的运行清单")
        for case in additional["cases"]:
            if case["id"] in indexed:
                raise ValueError("混合任务来源标识重复")
            indexed[case["id"]] = dict(case, _source=source, _manifest=location)
            if development_source:
                indexed[case["id"]]["_grading"] = references[case["id"]]
    selected = plan["cases"]
    if set(selected) != {"development", "training", "evaluation"}:
        raise ValueError("pilot 需要开发、训练、评估三组固定题目")
    for split, expected in (("development", 4), ("training", 8), ("evaluation", 8)):
        ids = selected[split]
        if (not isinstance(ids, list) or not ids or any(not isinstance(i, str) or i not in indexed for i in ids)
                or legacy and (len(ids) != expected or len(set(ids)) != expected)
                or split == "evaluation" and len(set(ids)) != len(ids)):
            raise ValueError("pilot 题目数或来源清单不匹配")
        expected_split = {"development": "development_pool", "training": "train_pool", "evaluation": "evaluation"}[split]
        if any(indexed[i]["split"] != expected_split for i in ids):
            raise ValueError("pilot 题目跨越冻结的数据划分")
    evaluation = [indexed[i] for i in selected["evaluation"]]
    if legacy and (sum(bool(x.get("injection_task_id")) for x in evaluation) != 2
            or len({x.get("attack_template") for x in evaluation if x.get("injection_task_id")}) != 2):
        raise ValueError("pilot 评估须为六正常、两种不同模板攻击")
    continuous = plan.get("continuous_training", False)
    if type(continuous) is not bool or legacy and continuous:
        raise ValueError("连续训练仅用于新比较协议")
    if continuous:
        cadence = plan.get("update_after_tasks", 16)
        if type(cadence) is not int or not 1 <= cadence <= len(selected["training"]):
            raise ValueError("训练更新间隔须为合理的任务数")
        if "training_batch_size" in plan or "workload_size" in plan:
            raise ValueError("连续服务不接受按训练批次清空实例的设置")
    else:
        batch_size = plan.get("training_batch_size", 4)
        if type(batch_size) is not int or not 1 <= batch_size <= len(selected["training"]) or legacy and batch_size != 4:
            raise ValueError("训练批次须为合理的完整任务数")
    families = [{(indexed[i].get("_source", "agentdojo"), indexed[i].get("suite"), indexed[i]["family_id"]) for i in selected[split]}
                for split in ("development", "training", "evaluation")]
    if any(families[a] & families[b] for a in range(3) for b in range(a)):
        raise ValueError("pilot 数据家族跨划分")
    if not isinstance(plan.get("bindings", {}), dict) or not isinstance(plan.get("scheduler"), dict):
        raise ValueError("pilot 调度配置或指定检查格式不正确")
    selector = plan.get("development_selector", "app_config")
    if selector not in {"app_config", "scheduler"}:
        raise ValueError("开发校准的检查选择方式无效")
    if selector == "scheduler" and (plan.get("policies", {}).get("app_config") or {}).get("kind") != "rule":
        raise ValueError("开发校准调度器须使用明确的规则策略")
    if type(plan.get("local_development_only", False)) is not bool:
        raise ValueError("开发限定标记须为布尔值")
    return identity, plan, indexed


def training_batches(plan):
    return len(workload_cases(plan, "training"))


def workload_cases(plan, mode):
    """New training uses one continuous stream; historical pilots stay frozen."""
    split = "development" if mode.startswith("validation_") else mode if mode in {"training", "development"} else "evaluation"
    ids = plan["cases"][split]
    size = len(ids) if plan.get("continuous_training") or mode != "training" else plan.get("training_batch_size", 4)
    return [ids[i:i + size] for i in range(0, len(ids), size)]


def frozen_application(platform, plan, units, rag, series, *, project=None):
    """Freeze the first application's policy and guard metadata for every arm."""
    from seampact.project_applications import application_config
    calibration = (plan.get("development_evidence") or {}).get("application")
    previous = series.get("application") or calibration
    if previous is None and series.get("workloads"):
        raise ValueError("旧 pilot 工作负载未冻结应用语义，不能与新比较混用")
    if previous is not None:
        policy = previous["guard_policy"]
    elif "guard_policy" in plan:
        policy = plan["guard_policy"]
    else:
        policy = platform.project_applications.configuration(plan["project_id"]).get("guard_policy")
    candidates = ([dict(role=role, unit_id=identity) for role, identity in plan["bindings"].items()]
                  if "bindings" in plan else project["guard_candidates"])
    snapshot = application_config([*units, rag], candidates, policy, mode="app_config")
    if snapshot["operations"].get(rag["tool_name"], {}).get("kind") != "retrieve":
        raise ValueError("协议中的检索能力缺少有源码依据的检索声明，请通过分析器更新能力包")
    if calibration is not None and snapshot != calibration:
        raise ValueError("当前项目的检查候选或语义与开发校准冻结应用不同")
    if previous is not None and snapshot != previous:
        raise ValueError("当前能力语义或绑定与该 pilot 冻结应用不同")
    return previous if previous is not None else snapshot


def handle(platform, body):
    if body.get("op") == "admit":
        with _admission_lock:
            return _handle(platform, body)
    return _handle(platform, body)


def _handle(platform, body):
    allowed = {"op", "protocol", "protocol_sha256", "mode", "batch", "run_id", "case_id"}
    if set(body) - allowed:
        raise ValueError("pilot 只接受冻结协议及有限工作负载操作")
    identity, plan, cases = _read_plan(body)
    op = body.get("op")
    execution = platform.execution
    series_key = identity
    series = platform.store.get("office_pilot_series", series_key) or dict(id=series_key, workloads={})
    if op == "prebuild":
        case_id = body.get("case_id")
        if plan.get("local_development_only") and case_id not in plan["cases"]["development"]:
            raise ValueError("开发限定协议只允许预构开发题")
        if case_id not in plan.get("prebuild", []) or case_id not in cases or not cases[case_id].get("rag_adaptation"):
            raise ValueError("索引预构只接受冻结协议指定的 RAG 版本")
        if execution.workload and execution.workload["status"] in {"RUNNING", "DRAINING"}:
            raise ValueError("工作负载期间不能进行离线索引预构")
        from seampact.office_benchmark import prepare_office_rag_task
        from seampact.capability_diagnostics import invoke
        rag = plan["rag"]
        with platform.projects.lock:
            if cases[case_id].get("_source") == "office_rag":
                from seampact.office_synthetic_rag import prepare_synthetic_rag_task
                _environment, corpus = prepare_synthetic_rag_task(platform.projects, plan["project_id"],
                                                                  cases[case_id]["_manifest"], case_id)
            else:
                _environment, corpus = prepare_office_rag_task(platform.projects, plan["project_id"],
                    plan["manifest"], plan["manifest_sha256"], rag["corpus_dir"], rag["corpus_sha256"], case_id)
        result = invoke(platform, dict(unit_id=rag["capability_id"], project_id=plan["project_id"],
            input=dict(query="office reference document", mode="dense", top_k=3, rerank=False)))
        return dict(case_id=case_id, corpus=corpus, result=result)
    if op == "start":
        mode, batch = body.get("mode"), body.get("batch", 0)
        if plan.get("local_development_only") and mode != "development":
            raise ValueError("开发限定协议不允许训练、验证或评价")
        required_hooks = {"sanitize", "injection", "privacy", "secrets", "code_scan"}
        self_checks = {"self_check_input", "self_check_output"}
        optional_hooks = self_checks | {"fact_check"}
        project, eligible = platform.project_applications.snapshot(plan["project_id"], platform.capabilities.public())
        permitted = {(item["role"], item["unit_id"]) for item in project["guard_candidates"]}
        if "bindings" in plan and not set(plan["bindings"].items()) <= permitted:
            raise ValueError("协议指定的检查不在本项目已启用且可调用的候选中")
        selected = set(plan["bindings"].items()) if "bindings" in plan else permitted
        roles = {role for role, _ in selected}
        if (not required_hooks <= roles or roles - required_hooks - optional_hooks
                or bool(roles & self_checks) != (self_checks <= roles)):
            raise ValueError("办公比较须覆盖五种基础防御，自检两操作须同时提供")
        unit_ids = {identity for _, identity in selected}
        units = [unit for unit in eligible if unit["id"] in unit_ids]
        rag = platform.capabilities.unit(plan["rag"]["capability_id"])
        if rag["runtime_group"] not in project["enabled_group_ids"]:
            raise ValueError("协议中的检索能力未在本项目启用")
        if any(unit.get("status") != "READY" or (unit.get("generation_verification") or {}).get("status") != "SUCCEEDED"
               for unit in units):
            raise ValueError("pilot 所有防御能力须先完成真实首调")
        if rag.get("status") != "READY" or (mode != "development" and
                (rag.get("generation_verification") or {}).get("status") != "SUCCEEDED"):
            raise ValueError("检索能力须先完成固定绑定；训练和比较前还须完成真实首调")
        groups = {unit["runtime_group"] for unit in units}
        if len(groups) < 5 or rag["runtime_group"] in groups:
            raise ValueError("pilot 须固定至少五个防御库和一个独立检索库")
        if mode != "development" and not (plan.get("development_evidence") or {}).get("application"):
            raise ValueError("开发校准缺少冻结的应用候选记录，请用新版重新校准")
        application = frozen_application(platform, plan, units, rag, series, project=project)
        if mode not in {"development", "training", "app_config", "resource_rule", "ppo",
                        "validation_app_config", "validation_resource_rule", "validation_ppo"}:
            raise ValueError("未知 pilot 工作负载模式")
        effective_mode = mode.removeprefix("validation_")
        batches = training_batches(plan)
        if type(batch) is not int or not 0 <= batch < len(workload_cases(plan, mode)):
            raise ValueError("pilot 批次不正确")
        key = mode + ":" + str(batch)
        if key in series["workloads"]:
            raise ValueError("该冻结 pilot 工作负载已有记录，不重复运行")
        if batch > 0:
            prior = series["workloads"].get(mode + ":" + str(batch - 1))
            if not prior or prior.get("status") != "COMPLETE" or mode == "training" and not prior.get("updated"):
                raise ValueError("上一任务段须先真实完成并结算")
        if effective_mode in {"app_config", "resource_rule", "ppo"}:
            prior = series["workloads"].get("training:" + str(batches - 1))
            if not prior or prior.get("status") != "COMPLETE" or not prior.get("updated"):
                raise ValueError("比较前须完成全部训练工作负载")
        if effective_mode == "ppo" and not any(series["workloads"].get("training:" + str(i), {}).get("actual_updates",
                                     (series["workloads"].get("training:" + str(i), {}).get("update") or {}).get("updated"))
                                     for i in range(batches)):
            raise ValueError("训练工作负载均无真实策略更新，不能称为已训练 PPO")
        ids = workload_cases(plan, mode)[batch]
        split = "development" if mode.startswith("validation_") else mode if mode in {"development", "training"} else "evaluation"
        blocks = scheduled_blocks(plan, split, ids, cases)
        waves = [block["indices"] for block in blocks]
        if mode != "development" and (plan.get("development_evidence") or {}).get("pool") != execution._pool():
            raise ValueError("当前能力池与开发基线冻结身份不同")
        run_id = uuid.uuid4().hex
        task_ids = [uuid.uuid4().hex for _ in ids]
        config = copy.deepcopy(plan["scheduler"])
        policies = plan["policies"]
        config["policy"] = copy.deepcopy(policies["app_config" if effective_mode in {"development", "training", "app_config"} else "resource_rule"])
        if mode == "development":
            config["objective"] = None
        if effective_mode == "ppo":
            checkpoint = series.get("checkpoint")
            if not checkpoint:
                raise ValueError("没有冻结的训练检查点")
            config["policy"] = dict(kind="ppo", checkpoint=checkpoint["path"], checkpoint_sha256=checkpoint["sha256"],
                                    learner_config=plan.get("learner_config", {}), seed=plan.get("seed", 0))
        if not (mode == "training" and batch > 0):
            execution.configure(config)
        workload = execution.start_workload(run_id, task_ids, training=mode == "training", fresh=batch == 0,
            continuous=bool(plan.get("continuous_training") and mode == "training"),
            learner_config=plan.get("learner_config", {}) if mode == "training" and batch == 0 else None,
            seed=plan.get("seed", 0))
        row = dict(id=run_id, protocol_sha256=identity, mode=mode, batch=batch, status="RUNNING",
                   defense_groups=sorted(groups), defense_group_count=len(groups),
                   retrieval_group=rag["runtime_group"], total_group_count=len(groups) + 1,
                   case_ids=ids, task_ids=task_ids, arrival_waves=waves, admitted=[], started_at=now(), workload=workload,
                   arrival_blocks=blocks, application=copy.deepcopy(application))
        if mode == "development":
            row["development_selection"] = dict(
                selector=plan.get("development_selector", "app_config"),
                scheduler_policy=copy.deepcopy(config["policy"]))
        platform.store.save("office_pilot", run_id, row)
        series["application"] = copy.deepcopy(application)
        series["workloads"][key] = dict(id=run_id, status="RUNNING")
        platform.store.save("office_pilot_series", series_key, series)
        return row
    run_id = body.get("run_id")
    row = platform.store.get("office_pilot", run_id)
    if not row or row["protocol_sha256"] != identity:
        raise ValueError("没有匹配该冻结协议的 pilot 工作负载")
    if op == "admit":
        if row["status"] != "RUNNING" or len(row["admitted"]) >= len(row["case_ids"]):
            raise ValueError("该工作负载没有待接入题目")
        index = len(row["admitted"])
        waves = row.get("arrival_waves") or [[i] for i in range(len(row["case_ids"]))]
        wave_index = next(i for i, wave in enumerate(waves) if index in wave)
        if wave_index and index == waves[wave_index][0]:
            for prior in waves[wave_index - 1]:
                previous = platform.store.get("task", row["task_ids"][prior])
                if not previous or previous.get("workspace_status") != "FINISHED" or previous.get("status") in {"RUNNING", "CANCELLING"}:
                    raise ValueError("上一组到达的任务尚未完整收尾")
        case_id, task_id = row["case_ids"][index], row["task_ids"][index]
        case = cases[case_id]
        location = case.get("_manifest", plan)
        request = dict(manifest=location["manifest"], manifest_sha256=location["manifest_sha256"],
                       case_id=case_id, project_id=plan["project_id"])
        if case.get("rag_adaptation"):
            request["rag"] = copy.deepcopy(plan["rag"])
            if case.get("_source") == "office_rag":
                request["synthetic_rag"] = copy.deepcopy(location)
        effective_mode = row["mode"].removeprefix("validation_")
        mode = (plan.get("development_selector", "app_config") if effective_mode == "development" else
                "app_config" if effective_mode == "app_config" else
                "ppo" if effective_mode in {"training", "ppo"} else "resource_rule")
        if case.get("_source") == "privacy":
            from seampact.office_privacy import start_privacy_task
            start = start_privacy_task
            request = dict(case_id=case_id, project_id=plan["project_id"])
        elif case.get("_source") == "spreadsheet":
            from seampact.office_spreadsheet import start_spreadsheet_task
            start = start_spreadsheet_task
        elif case.get("_source") in DEVELOPMENT_SOURCES:
            from seampact.office_development import start_development_task
            start = start_development_task
            request = dict(case=case, project_id=plan["project_id"])
        else:
            start = start_native_task
        if "application" not in row:
            raise ValueError("旧 pilot 工作负载未冻结应用语义，不能继续接入新任务")
        application = copy.deepcopy(row["application"])
        application.update(mode=mode, deadline_s=plan["deadline_s"])
        if plan.get("arrival_width", 1) > 1 and start is start_native_task and not case.get("rag_adaptation"):
            application["native_environment_only"] = True
        admission = start(platform, request, internal_task_id=task_id, pilot_config=application)
        row["admitted"].append(task_id)
        platform.store.save("office_pilot", run_id, row)
        return dict(case_id=case_id, **admission)
    if op == "update":
        if row["mode"] != "training" or not plan.get("continuous_training") or row["status"] != "RUNNING":
            raise ValueError("仅连续训练支持运行中的更新请求")
        result = execution.request_update(minimum_tasks=plan.get("update_after_tasks", 16))
        return dict(run_id=run_id, **result)
    if op in {"drain", "finish"}:
        if len(row["admitted"]) != len(row["case_ids"]):
            raise ValueError("冻结清单尚未全部接入")
        state = execution.finish_workload()
        if state["status"] != "COMPLETE":
            return state
        if op == "drain":
            row.update(status="AWAITING_SCORING", workload=state)
            platform.store.save("office_pilot", run_id, row)
            return row
        if row["status"] == "COMPLETE":
            return row
        if row["mode"] == "training":
            summary = state["summary"]
            if not summary.get("training_ready"):
                row.update(status="UNSETTLED", workload=state, error=summary.get("reason"), finished_at=now())
                platform.store.save("office_pilot", run_id, row)
                return row
            if plan.get("continuous_training"):
                state = execution.finish_workload(update=True)
                row["settlement"] = dict(method="continuous measured intervals; final confirmed release tail")
            else:
                count = len(state["decision_ids"])
                total = summary["utility"]
                settlements = dict(before_first=0.0 if count else total,
                                   after_decisions=([0.0] * (count - 1) + [total]) if count else [])
                state = execution.finish_workload(settlements=settlements, update=True)
                row["settlement"] = dict(method="terminal workload utility, zero for earlier decisions", **settlements)
            if row["batch"] == training_batches(plan) - 1:
                checkpoint = Path(platform.home) / "office-pilot" / (identity + ".pt")
                execution.policy.save(checkpoint)
                series["checkpoint"] = dict(path=str(checkpoint), sha256=hashlib.sha256(checkpoint.read_bytes()).hexdigest())
        row.update(status="COMPLETE", workload=state, finished_at=now())
        key = row["mode"] + ":" + str(row["batch"])
        actual = bool((state.get("update") or {}).get("updated") or
                      any(item.get("updated") for item in state.get("updates", [])))
        series["workloads"][key] = dict(id=run_id, status="COMPLETE", updated=bool(state.get("updated")),
                                       update=state.get("update"), actual_updates=actual)
        platform.store.save("office_pilot", run_id, row)
        platform.store.save("office_pilot_series", series_key, series)
        return row
    raise ValueError("未知 pilot 工作负载操作")
