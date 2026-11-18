"""Load a registered policy for inference, exact continuation or a new run.

The registered parent is always authenticated against its own frozen config.
A branch copies validated in-memory networks into a fresh policy; it never
edits checkpoint metadata to make an old checkpoint match a new objective.
"""

import copy
import hashlib
from pathlib import Path

from seampact.capability_contract import digest
from seampact.scheduling.capability_ppo import CandidatePPO
from seampact.scheduling.capability_scheduler import (
    FEATURE_SCHEMA, freeze_config, load_policy, policy_config_id,
)
from seampact.scheduling.learners import default_config


def _settings(settings, objective):
    """Compare declarations after the defaults CandidatePPO supplies."""
    if not isinstance(settings, dict):
        raise ValueError("策略版本的 learner_config 须为对象")
    normalized = default_config("ppo")
    normalized.update(gamma=1.0, gae_lambda=1.0)
    normalized.update(copy.deepcopy(settings))
    if isinstance(normalized["hidden_sizes"], tuple):
        normalized["hidden_sizes"] = list(normalized["hidden_sizes"])
    if objective and objective.get("pareto"):
        normalized.setdefault("value_components", ["quality", "latency", "ram", "vram", "api"])
    if isinstance(normalized.get("value_components"), tuple):
        normalized["value_components"] = list(normalized["value_components"])
    return normalized


def _nonpolicy(config):
    return {key: value for key, value in config.items() if key != "policy"}


def _checkpoint(record, config):
    checkpoint = record.get("checkpoint")
    if config["policy"]["kind"] == "rule":
        if checkpoint is not None:
            raise ValueError("规则策略版本不能携带 PPO 检查点")
        return None
    if (not isinstance(checkpoint, dict) or set(checkpoint) != {"path", "sha256"}
            or not isinstance(checkpoint["path"], str)
            or not isinstance(checkpoint["sha256"], str)):
        raise ValueError("策略版本缺少冻结检查点 path 与 sha256")
    settings = config["policy"]
    if (Path(checkpoint["path"]).resolve() != Path(settings["checkpoint"]).resolve()
            or checkpoint["sha256"] != settings["checkpoint_sha256"]):
        raise ValueError("策略版本检查点与父配置不一致")
    return checkpoint


def _check_hash(checkpoint):
    if checkpoint is not None:
        actual = hashlib.sha256(Path(checkpoint["path"]).read_bytes()).hexdigest()
        if actual != checkpoint["sha256"]:
            raise ValueError("策略版本检查点文件身份已变化")


def _receipt(record, mode, checkpoint, transferred, reset, **details):
    return dict(phase=mode, parent_id=record.get("id"),
                parent_checkpoint_sha256=checkpoint["sha256"] if checkpoint else None,
                transferred=list(transferred), reset=list(reset), **details)


def _copy_network(parent, child, name):
    source = parent.learner.networks[name].state_dict()
    target = child.learner.networks[name].state_dict()
    if (source.keys() != target.keys()
            or any(source[key].shape != target[key].shape for key in source)):
        raise ValueError("策略分支的网络结构或特征/动作维度不兼容")
    child.learner.networks[name].load_state_dict(copy.deepcopy(source), strict=True)


def _component_semantics(objective):
    # Preference and smoothing compose advantages, not the five critic returns.
    return {key: value for key, value in objective.items() if key not in {"weights", "pareto"}}


def load_version_policy(record, target_config=None, learner_config=None, seed=None, mode="inference"):
    """Return the policy represented by a registry record.

    ``target_config`` supplies the destination scheduler/objective configuration;
    its policy selection does not alter the registered parent. ``learner_config``
    overrides the parent's declared learner settings for a new branch. A rule
    version can seed a fresh PPO branch, but cannot resume neural training.
    Every branch starts with a new optimizer, counters, RNG, rollout, settlement
    history and Pareto coefficients from the destination objective (preference
    for adaptive STCH; the frozen calibration derivative for fixed mode), even
    for an unchanged objective.
    """
    if mode not in {"inference", "resume", "branch"}:
        raise ValueError("策略加载 mode 须为 inference、resume 或 branch")
    if not isinstance(record, dict) or not isinstance(record.get("config"), dict):
        raise ValueError("策略版本须包含完整冻结配置")
    if record.get("feature_schema") != FEATURE_SCHEMA:
        raise ValueError("策略版本特征或动作 schema 不兼容")
    parent_config = freeze_config(record["config"])
    checkpoint = _checkpoint(record, parent_config)
    target = freeze_config(target_config, verify_policy=False) if target_config is not None else parent_config
    metadata = record.get("metadata")
    if metadata is not None:
        if not isinstance(metadata, dict):
            raise ValueError("策略版本 metadata 须为对象")
        expected = dict(config_id=policy_config_id(parent_config),
                        objective_id=digest(parent_config["objective"]), feature_schema=FEATURE_SCHEMA)
        if any(key in metadata and metadata[key] != value for key, value in expected.items()):
            raise ValueError("策略版本 metadata 与父配置身份不一致")

    is_ppo = parent_config["policy"]["kind"] == "ppo"
    parent_settings = parent_config["policy"].get("learner_config", {})
    declared_settings = record.get("learner_config", parent_settings)
    parent_seed = record.get("seed", parent_config["policy"].get("seed", 0))
    if is_ppo and (_settings(declared_settings, parent_config["objective"])
                   != _settings(parent_settings, parent_config["objective"])
                   or parent_seed != parent_config["policy"]["seed"]):
        raise ValueError("策略版本的训练配置或种子与父配置不一致")
    selected_seed = parent_seed if seed is None else seed
    if type(selected_seed) is not int:
        raise ValueError("策略版本训练 seed 须为整数")
    if learner_config is not None and not isinstance(learner_config, dict):
        raise ValueError("learner_config 须为对象")
    settings = copy.deepcopy(declared_settings)
    settings.update(copy.deepcopy(learner_config or {}))

    if mode == "inference":
        if (_nonpolicy(target) != _nonpolicy(parent_config)
                or (learner_config is not None and is_ppo
                    and _settings(settings, parent_config["objective"])
                    != _settings(parent_settings, parent_config["objective"]))
                or selected_seed != parent_seed):
            raise ValueError("冻结推理须使用该策略版本的配置；变更配置请创建分支")
        policy = load_policy(parent_config)
        policy.version_transfer = _receipt(record, mode, checkpoint,
            ["actor", "critics", "pareto"] if is_ppo and policy.pareto else
            ["actor", "value"] if is_ppo else ["rule"],
            ["optimizer", "counters", "rng", "rollout"] if is_ppo else [])
        return policy

    if mode == "resume" and not is_ppo:
        raise ValueError("规则策略没有 PPO 训练状态，不能续训；可创建全新训练分支")
    if mode == "resume" and (_nonpolicy(target) != _nonpolicy(parent_config)
            or _settings(settings, target["objective"]) != _settings(parent_settings, parent_config["objective"])
            or selected_seed != parent_seed):
        raise ValueError("精确续训须保持目标、调度非 policy 配置、learner 配置与种子相同")

    parent = None
    if is_ppo:
        # Validate every parent network, optimizer and Pareto receipt using the
        # parent's own identity before considering any destination compatibility.
        parent = CandidatePPO(policy_config_id(parent_config), parent_config["objective"],
                              config=parent_settings, seed=parent_seed, training=True)
        _check_hash(checkpoint)
        parent.load(checkpoint["path"], optimizer=True)
        _check_hash(checkpoint)
    if mode == "resume":
        parent.version_transfer = _receipt(record, mode, checkpoint,
            ["actor", "critics" if parent.pareto else "value", "optimizer", "counters", "rng"]
            + (["pareto", "settlement_history"] if parent.pareto else []), ["rollout"])
        return parent

    child = CandidatePPO(policy_config_id(target), target["objective"], config=settings,
                         seed=selected_seed, training=True)
    transferred, critics = [], "fresh"
    if parent is not None:
        _copy_network(parent, child, "actor")
        transferred.append("actor")
        if (parent.pareto and child.pareto
                and _component_semantics(parent.objective) == _component_semantics(child.objective)):
            for name in child.learner.value_network_names:
                _copy_network(parent, child, name)
            transferred.append("critics")
            critics = "inherited"
        elif not parent.pareto and not child.pareto and parent.objective == child.objective:
            _copy_network(parent, child, "value")
            transferred.append("value")
            critics = "inherited"
    reset = ["optimizer", "counters", "rng", "rollout", "settlement_history"]
    if critics == "fresh":
        reset.append("critics" if child.pareto else "value")
    if child.pareto:
        reset.append("pareto")
    child.version_transfer = _receipt(record, mode, checkpoint, transferred, reset,
                                      critics=critics, fresh=parent is None)
    _check_hash(checkpoint)
    return child
