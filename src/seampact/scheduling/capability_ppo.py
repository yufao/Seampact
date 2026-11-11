"""Optional candidate PPO with strict new-objective checkpoint identity.

No data, evaluator, or reward numbers are supplied here. Updating requires one
complete, measured workload and its real event settlements. Interactive chats
without those inputs can use rules or frozen inference, but cannot train.
"""

import copy
import math
import os
import uuid
from pathlib import Path

from seampact.capability_contract import digest
from seampact.scheduling.capability_scheduler import CANDIDATE_DIM, FEATURE_SCHEMA, OBSERVATION_DIM
from seampact.scheduling.learners import Learner

CHECKPOINT = "capability-ppo-checkpoint/1"


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
        self.learner = Learner("ppo", OBSERVATION_DIM, CANDIDATE_DIM, settings, seed=seed)
        self.training = training

    def choose(self, observation, candidates):
        if len(candidates) < 2:
            raise ValueError("单合法动作直接执行，不产生 PPO 选择")
        return self.learner.act(observation, [row["features"] for row in candidates],
                                [True] * len(candidates), training=self.training)

    def update_workload(self, decisions, settlements, summary):
        """Settle once, then clip-update one complete finite workload.

        settlements.before_first accounts for uncontrollable prefix utility;
        settlements.after_decisions contains utility deltas through the next
        choice (the last includes task completion and actual release tails).
        Their sum must equal the reported objective, so costs cannot be deducted
        again at aggregation. These values must come from the real ledger and
        the named task evaluator, never from action labels or a simulator.
        """
        if not self.training:
            raise ValueError("冻结推理策略不能更新")
        if (not summary.get("training_ready") or summary.get("objective_id") != self.identity["objective_id"]
                or not summary.get("complete") or summary.get("policy_config_id") != self.identity["config_id"]
                or not summary.get("session")):
            raise ValueError("工作负载尚未完整结算、评价或计量缺失，不能更新")
        rewards = settlements.get("after_decisions")
        prefix = settlements.get("before_first")
        if (not isinstance(rewards, list) or len(rewards) != len(decisions)
                or any(type(value) not in (int, float) or not math.isfinite(value) for value in [prefix, *rewards])):
            raise ValueError("结算须提供首动作前分项及每个真实选择之后的有限回报")
        if not math.isclose(prefix + sum(rewards), summary["utility"], rel_tol=1e-7, abs_tol=1e-9):
            raise ValueError("事件回报之和与完整工作负载效用不一致")
        episode_id = uuid.uuid4().hex
        transitions = []
        for index, decision in enumerate(decisions):
            if (decision.get("policy_config_id", decision.get("config_id")) != self.identity["config_id"]
                    or decision.get("feature_schema") != FEATURE_SCHEMA or len(decision["candidates"]) < 2
                    or decision.get("session") != summary["session"]):
                raise ValueError("决策配置、特征或真实候选集合与策略不匹配")
            selection = decision["selection"]
            following = decisions[index + 1] if index + 1 < len(decisions) else None
            transitions.append(dict(state=decision["observation"],
                candidates=[candidate["features"] for candidate in decision["candidates"]],
                legal=[True] * len(decision["candidates"]), index=selection["index"],
                reward=rewards[index], old_log_prob=selection["log_prob"], value=selection["value"],
                policy_version=selection["policy_version"], episode_id=episode_id,
                next_state=following["observation"] if following else None,
                next_candidates=[candidate["features"] for candidate in following["candidates"]] if following else [],
                next_legal=[True] * len(following["candidates"]) if following else [],
                terminated=following is None, truncated=False))
        # Validate the full set before mutating the fresh on-policy rollout.
        if any(row["policy_version"] != self.learner.policy_version for row in transitions):
            raise ValueError("工作负载包含旧策略决策，不能继续训练")
        if self.learner.rollout:
            raise ValueError("上一个完整工作负载尚未结算")
        for transition in transitions:
            self.learner.observe(transition)
        result = self.learner.update(force=True)
        return dict(result, decisions=len(decisions), before_first=prefix, utility=summary["utility"])

    def save(self, path):
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + "." + uuid.uuid4().hex + ".partial")
        try:
            self.learner.save(temporary, checkpoint_metadata=self.identity)
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        return str(destination)

    def load(self, path, *, optimizer=False):
        payload = self.learner.torch.load(Path(path), map_location=self.learner.device, weights_only=True)
        if payload.get("checkpoint_metadata") != self.identity:
            raise ValueError("检查点不是同一能力调度目标与特征身份；拒绝旧目标或旧 schema")
        return self.learner.load(path, optimizer=optimizer)

    def snapshot(self):
        return dict(identity=copy.deepcopy(self.identity), training=self.training, learner=self.learner.snapshot())
