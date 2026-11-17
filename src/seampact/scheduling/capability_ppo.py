"""Candidate PPO with separate objective critics and Pareto scalarization.

Measured component returns stay unweighted. Smooth Tchebycheff coefficients
compose only the actor advantages and remain frozen throughout each PPO update.
"""

import copy
import math
import os
import uuid
from pathlib import Path

from seampact.capability_contract import digest
from seampact.scheduling.capability_scheduler import CANDIDATE_DIM, FEATURE_SCHEMA, OBSERVATION_DIM
from seampact.scheduling.learners import Learner

CHECKPOINT = "capability-ppo-checkpoint/2"
PARETO_STATE = "capability-ppo-pareto-state/1"
COMPONENTS = ("quality", "latency", "ram", "vram", "api")


def _finite(value, name, *, nonnegative=False):
    if (type(value) not in (int, float) or not math.isfinite(value)
            or (nonnegative and value < 0)):
        raise ValueError(name + " 须为有限" + ("非负数" if nonnegative else "数值"))
    return float(value)


class CandidatePPO:
    def __init__(self, config_id, objective, *, config=None, seed=0, training=False):
        from seampact.scheduling.unified_objective import freeze_objective
        self.objective = freeze_objective(objective)
        if self.objective is None:
            raise ValueError("PPO 须有显式统一目标及任务评价器，未知质量不能训练")
        self.identity = dict(schema=CHECKPOINT, config_id=config_id, objective_id=digest(self.objective),
                             feature_schema=FEATURE_SCHEMA)
        settings = dict(config or {})
        if settings.get("gamma", 1.0) != 1.0 or settings.get("gae_lambda", 1.0) != 1.0:
            raise ValueError("有限完整工作负载使用 gamma=1、gae_lambda=1")
        settings.update(gamma=1.0, gae_lambda=1.0)
        self.pareto = copy.deepcopy(self.objective.get("pareto"))
        self.components = list(COMPONENTS) if self.pareto else []
        if self.pareto:
            if "value_components" in settings and settings["value_components"] != self.components:
                raise ValueError("Pareto 价值头须对应质量、时间、RAM、VRAM 与 token 数")
            settings["value_components"] = self.components
        elif "value_components" in settings:
            raise ValueError("固定标量目标不配置 Pareto 价值头")
        self.learner = Learner("ppo", OBSERVATION_DIM, CANDIDATE_DIM, settings, seed=seed)
        self.training = training
        self.settled_sessions, self.settled_intervals = set(), set()
        self.coefficients = copy.deepcopy(self.pareto["preference"]) if self.pareto else {}
        self.coefficient_version = 0
        self.last_scalarization = None

    def choose(self, observation, candidates):
        if len(candidates) < 2:
            raise ValueError("单合法动作直接执行，不产生 PPO 选择")
        selection = self.learner.act(observation, [row["features"] for row in candidates],
                                     [True] * len(candidates), training=self.training)
        if self.pareto:
            selection.update(coefficient_snapshot=copy.deepcopy(self.coefficients),
                             coefficient_version=self.coefficient_version)
            selection["value"] = self._compose(selection["component_values"], self.coefficients)
        return selection

    @staticmethod
    def _compose(values, weights):
        return weights["quality"] * values["quality"] - sum(weights[term] * values[term] for term in COMPONENTS[1:])

    def _component_settlements(self, components, decisions=None):
        if not isinstance(components, dict) or set(components) != set(COMPONENTS):
            raise ValueError("Pareto 区间结算须包括全部五个原始分项")
        checked, length = {}, decisions
        for term in COMPONENTS:
            row = components[term]
            rewards = row.get("after_decisions") if isinstance(row, dict) else None
            if (not isinstance(row, dict) or set(row) != {"before_first", "after_decisions"}
                    or not isinstance(rewards, list) or (length is not None and len(rewards) != length)):
                raise ValueError("分项结算须与全部真实选择逐一对应")
            length = len(rewards)
            checked[term] = dict(before_first=_finite(row["before_first"], term, nonnegative=True),
                after_decisions=[_finite(value, term, nonnegative=True) for value in rewards])
        return checked

    def interval_weights(self, component_settlements, normalizer, completed_tasks):
        """Pure chain-rule weights for STCH of the measured mean return vector.

        The quality bound and nonnegative physical costs determine the ideal
        point (1, 0, 0, 0, 0); no resource budget is introduced.
        """
        if not self.pareto:
            raise ValueError("当前目标未启用 Pareto 标量化")
        components = self._component_settlements(component_settlements)
        normalizer = _finite(normalizer, "normalizer")
        if normalizer <= 0 or type(completed_tasks) is not int or completed_tasks < 0:
            raise ValueError("须提供正归一化分母与真实非负完成任务数")
        preference, smoothing = self.pareto["preference"], self.pareto["smoothing"]
        means, gaps, value = None, None, None
        weights = copy.deepcopy(self.coefficients)
        if completed_tasks:
            means = {term: _finite((row["before_first"] + sum(row["after_decisions"])) *
                                  normalizer / completed_tasks, "mean." + term, nonnegative=True)
                     for term, row in components.items()}
            if means["quality"] > 1 + 1e-7:
                raise ValueError("原生质量均值须位于 [0,1]")
            means["quality"] = min(1.0, means["quality"])
            gaps = dict(means, quality=1.0 - means["quality"])
            logits = {term: _finite(preference[term] * gaps[term] / smoothing, "weighted_gap." + term)
                      for term in COMPONENTS}
            maximum = max(logits.values())
            exponentials = {term: math.exp(logit - maximum) for term, logit in logits.items()}
            total = sum(exponentials.values())
            weights = {term: preference[term] * exponentials[term] / total for term in COMPONENTS}
            value = _finite(smoothing * (maximum + math.log(total)), "scalarization_value")
        receipt = dict(schema="smooth-tchebycheff-receipt/1", applied=bool(completed_tasks),
            means=means, gaps=gaps, ideal=dict(quality=1.0, latency=0.0, ram=0.0, vram=0.0, api=0.0),
            preference=copy.deepcopy(preference), smoothing=smoothing, coefficients=copy.deepcopy(weights),
            scalarization_value=value, normalizer=normalizer, completed_tasks=completed_tasks,
            coefficient_version=self.coefficient_version)
        return dict(weights=weights, receipt=receipt)

    def _validate_pareto(self, decisions, settlements, summary):
        components = self._component_settlements(summary.get("component_settlements"), len(decisions))
        planned = self.interval_weights(components, summary.get("normalizer"), summary.get("completed_tasks"))
        if (summary.get("optimization_weights") != planned["weights"]
                or summary.get("scalarization") != planned["receipt"]):
            raise ValueError("Pareto 系数及凭据须由本次真实分项均值重新计算")
        for position, actual in [(None, settlements["before_first"]), *enumerate(settlements["after_decisions"])]:
            values = {term: row["before_first"] if position is None else row["after_decisions"][position]
                      for term, row in components.items()}
            if not math.isclose(self._compose(values, planned["weights"]), actual, rel_tol=1e-7, abs_tol=1e-9):
                raise ValueError("标量结算须由本批次冻结系数组合原始分项")
        for decision in decisions:
            selection = decision.get("selection", {})
            snapshot = selection.get("coefficient_snapshot")
            if (not isinstance(snapshot, dict) or set(snapshot) != set(COMPONENTS)
                    or snapshot != self.coefficients or type(selection.get("coefficient_version")) is not int
                    or selection["coefficient_version"] != self.coefficient_version):
                raise ValueError("工作负载包含旧系数状态决策，不能继续训练")
            for term, value in snapshot.items():
                _finite(value, "coefficient_snapshot." + term, nonnegative=True)
            values = selection.get("component_values")
            if not isinstance(values, dict) or set(values) != set(COMPONENTS):
                raise ValueError("决策须保存全部分项价值")
            for term, value in values.items():
                _finite(value, "component_values." + term)
        return components, planned

    def update_workload(self, decisions, settlements, summary):
        if not self.training:
            raise ValueError("冻结推理策略不能更新")
        if (not summary.get("training_ready") or summary.get("objective_id") != self.identity["objective_id"]
                or not summary.get("complete") or summary.get("policy_config_id") != self.identity["config_id"]
                or not summary.get("session")):
            raise ValueError("工作负载尚未完整结算、评价或计量缺失，不能更新")
        if summary["session"] in self.settled_sessions:
            raise ValueError("工作负载已经结算，不能重复更新")
        self._validate_settlements(decisions, settlements, summary)
        return self._update(decisions, settlements, summary, final=True)

    def update_interval(self, decisions, settlements, summary, *, bootstrap=None, final=False):
        if not self.training:
            raise ValueError("冻结推理策略不能更新")
        if (not summary.get("training_ready") or summary.get("objective_id") != self.identity["objective_id"]
                or summary.get("policy_config_id") != self.identity["config_id"] or not summary.get("session")
                or not summary.get("interval_id")):
            raise ValueError("连续区间的真实评分、计量或策略身份缺失，不能更新")
        identity = (summary["session"], summary["interval_id"])
        if identity in self.settled_intervals:
            raise ValueError("连续区间已经结算，不能重复更新")
        if not final and (not bootstrap or not bootstrap.get("candidates")):
            raise ValueError("持续服务的更新需要下一个真实选择的观测及合法候选以 bootstrap")
        self._validate_settlements(decisions, settlements, summary)
        result = self._update(decisions, settlements, summary, bootstrap=bootstrap, final=final)
        self.settled_intervals.add(identity)
        return result

    @staticmethod
    def _validate_settlements(decisions, settlements, summary):
        rewards, prefix = settlements.get("after_decisions"), settlements.get("before_first")
        if (not isinstance(rewards, list) or len(rewards) != len(decisions)
                or any(type(value) not in (int, float) or not math.isfinite(value) for value in [prefix, *rewards])
                or not math.isclose(prefix + sum(rewards), summary["utility"], rel_tol=1e-7, abs_tol=1e-9)):
            raise ValueError("真实事件回报与完整区间效用不一致")

    def _update(self, decisions, settlements, summary, *, bootstrap=None, final):
        rewards, prefix = settlements["after_decisions"], settlements["before_first"]
        components, planned = self._validate_pareto(decisions, settlements, summary) if self.pareto else ({}, None)
        if not decisions:
            if final and not summary.get("interval_id"):
                self.settled_sessions.add(summary["session"])
            result = dict(updated=False, decisions=0, reason="本区间没有多候选选择", before_first=prefix,
                          utility=summary["utility"])
            if planned:
                result["scalarization"] = dict(planned["receipt"], applied=False)
            return result
        episode_id, transitions = uuid.uuid4().hex, []
        for index, decision in enumerate(decisions):
            if (decision.get("policy_config_id", decision.get("config_id")) != self.identity["config_id"]
                    or decision.get("feature_schema") != FEATURE_SCHEMA or len(decision["candidates"]) < 2
                    or decision.get("session") != summary["session"]):
                raise ValueError("决策配置、特征或真实候选集合与策略不匹配")
            selection = decision["selection"]
            following = decisions[index + 1] if index + 1 < len(decisions) else bootstrap
            last = index + 1 == len(decisions)
            transitions.append(dict(state=decision["observation"],
                candidates=[candidate["features"] for candidate in decision["candidates"]],
                legal=[True] * len(decision["candidates"]), index=selection["index"],
                reward=rewards[index], old_log_prob=selection["log_prob"], value=selection["value"],
                policy_version=selection["policy_version"], episode_id=episode_id,
                next_state=following["observation"] if following else None,
                next_candidates=[candidate["features"] for candidate in following["candidates"]] if following else [],
                next_legal=[True] * len(following["candidates"]) if following else [],
                terminated=last and final, truncated=last and not final))
            if summary.get("normalizer") is not None:
                normalizer = _finite(summary["normalizer"], "normalizer")
                if normalizer <= 0:
                    raise ValueError("normalizer 须为正数")
                transitions[-1]["block_normalizer"] = normalizer
            if self.pareto:
                transitions[-1].update(component_rewards={term: components[term]["after_decisions"][index]
                                                          for term in COMPONENTS},
                    component_values=copy.deepcopy(selection["component_values"]),
                    component_weights={term: planned["weights"][term] * (1 if term == "quality" else -1)
                                       for term in COMPONENTS})
        if self.learner.rollout:
            raise ValueError("上一个完整工作负载尚未结算")
        transitions = [self.learner.validate_transition(row) for row in transitions]
        for transition in transitions:
            self.learner.observe(transition)
        result = self.learner.update(force=True)
        if final and not summary.get("interval_id"):
            self.settled_sessions.add(summary["session"])
        result = dict(result, decisions=len(decisions), before_first=prefix, utility=summary["utility"],
                      terminated=final, bootstrapped=not final)
        if planned:
            receipt = copy.deepcopy(planned["receipt"])
            if result["updated"] and summary["completed_tasks"]:
                self.coefficients = copy.deepcopy(planned["weights"])
                self.coefficient_version += 1
                receipt["coefficient_version"] = self.coefficient_version
                self.last_scalarization = copy.deepcopy(receipt)
            else:
                receipt["applied"] = False
            result["scalarization"] = receipt
        return result

    def _pareto_state(self):
        return dict(schema=PARETO_STATE, coefficients=copy.deepcopy(self.coefficients),
            coefficient_version=self.coefficient_version, last_scalarization=copy.deepcopy(self.last_scalarization),
            settled_sessions=sorted(self.settled_sessions), settled_intervals=sorted(self.settled_intervals))

    def _validated_pareto_state(self, state):
        expected = {"schema", "coefficients", "coefficient_version", "last_scalarization", "settled_sessions", "settled_intervals"}
        if not isinstance(state, dict) or set(state) != expected or state["schema"] != PARETO_STATE:
            raise ValueError("Pareto 检查点缺失完整系数及结算状态")
        state = copy.deepcopy(state)
        if (type(state["coefficient_version"]) is not int or state["coefficient_version"] < 0
                or not isinstance(state["coefficients"], dict) or set(state["coefficients"]) != set(COMPONENTS)):
            raise ValueError("Pareto 检查点系数或版本无效")
        state["coefficients"] = {term: _finite(value, term, nonnegative=True) for term, value in state["coefficients"].items()}
        if (not isinstance(state["settled_sessions"], list)
                or any(not isinstance(item, str) or not item for item in state["settled_sessions"])
                or not isinstance(state["settled_intervals"], list)
                or any(not isinstance(item, (list, tuple)) or len(item) != 2
                       or any(not isinstance(value, str) or not value for value in item)
                       for item in state["settled_intervals"])):
            raise ValueError("Pareto 检查点已结算身份无效")
        receipt = state["last_scalarization"]
        if receipt is None:
            if state["coefficient_version"] or state["coefficients"] != self.pareto["preference"]:
                raise ValueError("Pareto 检查点初始系数状态不一致")
        else:
            if (not isinstance(receipt, dict) or receipt.get("coefficient_version") != state["coefficient_version"]
                    or not state["coefficient_version"] or receipt.get("coefficients") != state["coefficients"]
                    or receipt.get("preference") != self.pareto["preference"] or receipt.get("smoothing") != self.pareto["smoothing"]
                    or receipt.get("applied") is not True):
                raise ValueError("Pareto 检查点最后系数凭据不一致")
            means = receipt.get("means")
            if not isinstance(means, dict) or set(means) != set(COMPONENTS):
                raise ValueError("Pareto 检查点均值分项缺失")
            normalizer, count = receipt.get("normalizer"), receipt.get("completed_tasks")
            if type(count) is not int or count <= 0:
                raise ValueError("Pareto 检查点完成任务数无效")
            normalizer = _finite(normalizer, "normalizer")
            if normalizer <= 0:
                raise ValueError("Pareto 检查点归一化分母无效")
            reconstructed = {term: dict(before_first=_finite(value, term, nonnegative=True) * count / normalizer,
                                        after_decisions=[]) for term, value in means.items()}
            expected_receipt = self.interval_weights(reconstructed, normalizer, count)["receipt"]
            if set(receipt) != set(expected_receipt):
                raise ValueError("Pareto 检查点标量化凭据字段无效")
            if not math.isclose(_finite(receipt["scalarization_value"], "scalarization_value"),
                                expected_receipt["scalarization_value"], rel_tol=1e-12, abs_tol=1e-14):
                raise ValueError("Pareto 检查点标量化值不一致")
            for term in COMPONENTS:
                for key in ("coefficients", "gaps", "ideal"):
                    values = receipt.get(key)
                    if (not isinstance(values, dict) or set(values) != set(COMPONENTS)
                            or not math.isclose(_finite(values[term], key, nonnegative=True), expected_receipt[key][term],
                                                rel_tol=1e-12, abs_tol=1e-14)):
                        raise ValueError("Pareto 检查点标量化公式不一致")
        return state

    def save(self, path):
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + "." + uuid.uuid4().hex + ".partial")
        try:
            metadata = copy.deepcopy(self.identity)
            if self.pareto:
                metadata["pareto_state"] = self._pareto_state()
            self.learner.save(temporary, checkpoint_metadata=metadata)
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        return str(destination)

    def load(self, path, *, optimizer=False):
        payload = self.learner.torch.load(Path(path), map_location=self.learner.device, weights_only=True)
        metadata = payload.get("checkpoint_metadata") or {}
        identity = {key: value for key, value in metadata.items() if key != "pareto_state"}
        if identity != self.identity:
            raise ValueError("检查点不是同一能力调度目标与特征身份；拒绝旧目标或旧 schema")
        state = self._validated_pareto_state(metadata.get("pareto_state")) if self.pareto else None
        if not self.pareto and "pareto_state" in metadata:
            raise ValueError("固定目标检查点不能包含 Pareto 状态")
        result = self.learner.load(path, optimizer=optimizer)
        if state is not None:
            self.coefficients, self.coefficient_version = state["coefficients"], state["coefficient_version"]
            self.last_scalarization = state["last_scalarization"]
            self.settled_sessions = set(state["settled_sessions"])
            self.settled_intervals = {tuple(item) for item in state["settled_intervals"]}
        return result

    def snapshot(self):
        result = dict(identity=copy.deepcopy(self.identity), training=self.training, learner=self.learner.snapshot())
        if self.pareto:
            result["pareto"] = self._pareto_state()
        return result
