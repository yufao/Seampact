"""Candidate-scoring RL learners; execution constraints belong to the caller.

PyTorch is imported only when constructing a neural learner.  Every transition
contains the candidate features and mask that existed at that decision, rather
than looking them up again in the current capability pool.
"""

import copy
import math
import random
from collections import deque
from pathlib import Path


ALGORITHMS = ("double_dqn", "ppo", "discrete_sac")
_COMMON = dict(hidden_sizes=[64, 64], learning_rate=0.0003, gamma=0.99,
               batch_size=32, max_grad_norm=0.5, device="cpu")
DEFAULTS = {
    "double_dqn": dict(_COMMON, replay_capacity=10000, learning_starts=32,
                       gradient_steps=1, epsilon_start=0.2, epsilon_end=0.05,
                       epsilon_decay_steps=1000, target_update_interval=100),
    "ppo": dict(_COMMON, rollout_steps=64, epochs=4, gae_lambda=0.95,
                clip_ratio=0.2, value_coefficient=0.5, entropy_coefficient=0.01),
    "discrete_sac": dict(_COMMON, replay_capacity=10000, learning_starts=32,
                         gradient_steps=1, temperature=0.1, target_tau=0.005),
}


def default_config(algorithm):
    if algorithm not in ALGORITHMS:
        raise ValueError("unknown learner: " + str(algorithm))
    return copy.deepcopy(DEFAULTS[algorithm])


def learner_options():
    return [dict(algorithm=name, requires="torch", defaults=default_config(name))
            for name in ALGORITHMS]


def _finite(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(name + " must be a finite number")
    return float(value)


def _vector(value, size, name):
    if not isinstance(value, (list, tuple)) or len(value) != size:
        raise ValueError(name + " has the wrong feature dimension")
    return [_finite(item, name) for item in value]


class Learner:
    """One run's learner, with variable-size legal candidate sets.

    ``act`` returns the selected index and the metadata needed by ``observe``.
    A truncated transition still needs its actual final observation and legal
    candidates for bootstrapping. Waiting without a decision is not a terminal
    transition. PPO callers must finish outstanding decisions before updating.
    """

    def __init__(self, algorithm, observation_dim, candidate_dim, config=None, seed=0):
        if algorithm not in ALGORITHMS:
            raise ValueError("unknown learner: " + str(algorithm))
        if type(observation_dim) is not int or observation_dim < 1:
            raise ValueError("observation_dim must be a positive integer")
        if type(candidate_dim) is not int or candidate_dim < 1:
            raise ValueError("candidate_dim must be a positive integer")
        if type(seed) is not int:
            raise ValueError("seed must be an integer")
        self.algorithm = algorithm
        self.observation_dim, self.candidate_dim = observation_dim, candidate_dim
        self.config = default_config(algorithm)
        unknown = set(config or {}) - set(self.config)
        if unknown:
            raise ValueError("unknown learner settings: " + ", ".join(sorted(unknown)))
        self.config.update(copy.deepcopy(config or {}))
        self._validate_config()
        try:
            import torch
            from torch import nn
        except ImportError as exc:
            raise RuntimeError("Neural RL needs the optional PyTorch dependency") from exc
        self.torch, self.nn = torch, nn
        self.device = torch.device(self.config["device"])
        if self.device.type not in ("cpu", "cuda"):
            raise ValueError("learner device must be cpu or cuda")
        if self.device.type == "cuda" and not torch.cuda.is_available():
            raise ValueError("CUDA is unavailable for the requested learner")
        self.seed = seed
        self.random = random.Random(seed)
        self.generator = torch.Generator(device=self.device).manual_seed(seed)
        self.steps, self.updates, self.policy_version = 0, 0, 0
        self.replay = deque(maxlen=self.config.get("replay_capacity", 1))
        self.rollout = []
        self.last_stats = {}
        # fork_rng keeps separate runs from changing one another's initialisation.
        devices = [self.device.index or 0] if self.device.type == "cuda" else []
        with torch.random.fork_rng(devices=devices):
            torch.manual_seed(seed)
            self._build_networks()

    def _validate_config(self):
        cfg = self.config
        widths = cfg["hidden_sizes"]
        if not isinstance(widths, (list, tuple)) or not widths or any(type(x) is not int or x < 1 for x in widths):
            raise ValueError("hidden_sizes must contain positive integers")
        cfg["hidden_sizes"] = list(widths)
        positive_ints = ("batch_size", "replay_capacity", "learning_starts", "gradient_steps",
                         "epsilon_decay_steps", "target_update_interval", "rollout_steps", "epochs")
        for key in positive_ints:
            if key in cfg and (type(cfg[key]) is not int or cfg[key] < 1):
                raise ValueError(key + " must be a positive integer")
        for key in ("learning_rate", "max_grad_norm"):
            if _finite(cfg[key], key) <= 0:
                raise ValueError(key + " must be positive")
        for key in ("gamma", "gae_lambda", "epsilon_start", "epsilon_end", "target_tau"):
            if key in cfg and not 0 <= _finite(cfg[key], key) <= 1:
                raise ValueError(key + " must be between zero and one")
        for key in ("temperature", "value_coefficient", "entropy_coefficient"):
            if key in cfg and _finite(cfg[key], key) < 0:
                raise ValueError(key + " must be nonnegative")
        if "clip_ratio" in cfg and not 0 < _finite(cfg["clip_ratio"], "clip_ratio") < 1:
            raise ValueError("clip_ratio must be between zero and one")
        if "replay_capacity" in cfg and cfg["replay_capacity"] < max(cfg["batch_size"], cfg["learning_starts"]):
            raise ValueError("replay_capacity must fit batch_size and learning_starts")

    def _mlp(self, width):
        layers = []
        for hidden in self.config["hidden_sizes"]:
            layers.extend((self.nn.Linear(width, hidden), self.nn.Tanh()))
            width = hidden
        layers.append(self.nn.Linear(width, 1))
        return self.nn.Sequential(*layers).to(self.device)

    def _build_networks(self):
        width = self.observation_dim + self.candidate_dim
        rate = self.config["learning_rate"]
        self.networks, self.optimizers = {}, {}
        if self.algorithm == "double_dqn":
            self.networks["q"] = self._mlp(width)
            self.networks["target_q"] = copy.deepcopy(self.networks["q"])
            self.optimizers["q"] = self.torch.optim.Adam(self.networks["q"].parameters(), lr=rate)
        elif self.algorithm == "ppo":
            self.networks["actor"] = self._mlp(width)
            self.networks["value"] = self._mlp(width)
            parameters = list(self.networks["actor"].parameters()) + list(self.networks["value"].parameters())
            self.optimizers["policy"] = self.torch.optim.Adam(parameters, lr=rate)
        else:
            for name in ("actor", "q1", "q2"):
                self.networks[name] = self._mlp(width)
            for name in ("q1", "q2"):
                self.networks["target_" + name] = copy.deepcopy(self.networks[name])
            self.optimizers["actor"] = self.torch.optim.Adam(self.networks["actor"].parameters(), lr=rate)
            parameters = list(self.networks["q1"].parameters()) + list(self.networks["q2"].parameters())
            self.optimizers["critic"] = self.torch.optim.Adam(parameters, lr=rate)
        for name, network in self.networks.items():
            if name.startswith("target_"):
                network.requires_grad_(False)

    def _decision(self, observation, candidates, legal, allow_empty=False):
        state = _vector(observation, self.observation_dim, "observation")
        if not isinstance(candidates, (list, tuple)):
            raise ValueError("candidates must be a feature list")
        features = [_vector(row, self.candidate_dim, "candidate") for row in candidates]
        if not isinstance(legal, (list, tuple)) or len(legal) != len(features) or any(type(x) is not bool for x in legal):
            raise ValueError("legal must contain one boolean per candidate")
        if not allow_empty and not any(legal):
            raise ValueError("there is no legal decision; waiting is not an action")
        return state, features, list(legal)

    def _batch(self, decisions):
        torch = self.torch
        width = max(1, max(len(row[1]) for row in decisions))
        states = torch.tensor([row[0] for row in decisions], dtype=torch.float32, device=self.device)
        candidates = torch.zeros((len(decisions), width, self.candidate_dim), device=self.device)
        masks = torch.zeros((len(decisions), width), dtype=torch.bool, device=self.device)
        for index, (_, rows, legal) in enumerate(decisions):
            if rows:
                candidates[index, :len(rows)] = torch.tensor(rows, dtype=torch.float32, device=self.device)
                masks[index, :len(rows)] = torch.tensor(legal, dtype=torch.bool, device=self.device)
        return states, candidates, masks

    def _scores(self, name, states, candidates):
        expanded = states.unsqueeze(1).expand(-1, candidates.shape[1], -1)
        return self.networks[name](self.torch.cat((expanded, candidates), dim=-1)).squeeze(-1)

    def _values(self, states, candidates, masks):
        weights = masks.unsqueeze(-1).to(candidates.dtype)
        pooled = (candidates * weights).sum(dim=1) / weights.sum(dim=1).clamp_min(1)
        return self.networks["value"](self.torch.cat((states, pooled), dim=-1)).squeeze(-1)

    def _distribution(self, logits, masks):
        # Finite masking also handles terminal rows with no next legal action.
        logits = logits.masked_fill(~masks, self.torch.finfo(logits.dtype).min)
        log_probs = self.torch.log_softmax(logits, dim=-1)
        log_probs = self.torch.where(masks, log_probs, self.torch.zeros_like(log_probs))
        probabilities = self.torch.softmax(logits, dim=-1) * masks.to(logits.dtype)
        probabilities = probabilities / probabilities.sum(dim=-1, keepdim=True).clamp_min(1e-20)
        return probabilities, log_probs

    def act(self, observation, candidates, legal, training=True):
        decision = self._decision(observation, candidates, legal)
        states, features, masks = self._batch([decision])
        torch = self.torch
        with torch.no_grad():
            if self.algorithm == "double_dqn":
                scores = self._scores("q", states, features)[0]
                greedy = int(scores.masked_fill(~masks[0], -torch.inf).argmax().item())
                indices = [index for index, allowed in enumerate(legal) if allowed]
                fraction = min(1.0, self.steps / self.config["epsilon_decay_steps"])
                epsilon = (self.config["epsilon_start"] + fraction *
                           (self.config["epsilon_end"] - self.config["epsilon_start"])) if training else 0.0
                index = self.random.choice(indices) if self.random.random() < epsilon else greedy
                probability = epsilon / len(indices) + (1 - epsilon if index == greedy else 0)
                return dict(index=index, log_prob=math.log(probability), value=float(scores[index].item()),
                            epsilon=epsilon, policy_version=self.policy_version)
            logits = self._scores("actor", states, features)
            probabilities, log_probs = self._distribution(logits, masks)
            index = (int(torch.multinomial(probabilities[0], 1, generator=self.generator).item()) if training
                     else int(probabilities[0].argmax().item()))
            value = (self._values(states, features, masks)[0] if self.algorithm == "ppo" else
                     (probabilities * (torch.minimum(self._scores("q1", states, features),
                                                     self._scores("q2", states, features)) -
                                       self.config["temperature"] * log_probs)).sum(dim=-1)[0])
            return dict(index=index, log_prob=float(log_probs[0, index].item()), value=float(value.item()),
                        policy_version=self.policy_version)

    def observe(self, transition):
        row = copy.deepcopy(transition)
        state, candidates, legal = self._decision(row.get("state"), row.get("candidates"), row.get("legal"))
        index = row.get("index")
        if type(index) is not int or not 0 <= index < len(candidates) or not legal[index]:
            raise ValueError("the recorded action was not legal")
        reward = _finite(row.get("reward"), "reward")
        terminated, truncated = row.get("terminated", False), row.get("truncated", False)
        if type(terminated) is not bool or type(truncated) is not bool:
            raise ValueError("terminated and truncated must be booleans")
        if terminated:
            next_state = [0.0] * self.observation_dim
            next_candidates, next_legal = [], []
        else:
            next_state, next_candidates, next_legal = self._decision(
                row.get("next_state"), row.get("next_candidates"), row.get("next_legal"))
        row.update(state=state, candidates=candidates, legal=legal, index=index, reward=reward,
                   next_state=next_state, next_candidates=next_candidates, next_legal=next_legal,
                   terminated=terminated, truncated=truncated)
        if self.algorithm == "ppo":
            if row.get("policy_version") != self.policy_version:
                raise ValueError("PPO only accepts the current policy's fresh rollout")
            row["old_log_prob"] = _finite(row.get("old_log_prob"), "old_log_prob")
            row["value"] = _finite(row.get("value"), "value")
            self.rollout.append(row)
        else:
            self.replay.append(row)
        self.steps += 1

    def _transition_batch(self, rows):
        current = self._batch([(row["state"], row["candidates"], row["legal"]) for row in rows])
        following = self._batch([(row["next_state"], row["next_candidates"], row["next_legal"]) for row in rows])
        actions = self.torch.tensor([row["index"] for row in rows], dtype=self.torch.long, device=self.device)
        rewards = self.torch.tensor([row["reward"] for row in rows], dtype=self.torch.float32, device=self.device)
        bootstrap = self.torch.tensor([not row["terminated"] for row in rows], dtype=self.torch.float32,
                                      device=self.device)
        return current, following, actions, rewards, bootstrap

    def _step(self, name, loss, networks):
        optimizer = self.optimizers[name]
        optimizer.zero_grad(set_to_none=True)
        loss.backward()
        parameters = [parameter for network in networks for parameter in network.parameters()]
        self.torch.nn.utils.clip_grad_norm_(parameters, self.config["max_grad_norm"])
        optimizer.step()

    def update(self, force=False):
        if self.algorithm == "ppo":
            if not self.rollout or (len(self.rollout) < self.config["rollout_steps"] and not force):
                return dict(updated=False, rollout_size=len(self.rollout), updates=self.updates)
            stats = self._update_ppo()
        else:
            minimum = max(self.config["batch_size"], self.config["learning_starts"])
            if len(self.replay) < minimum:
                return dict(updated=False, replay_size=len(self.replay), updates=self.updates)
            values = []
            for _ in range(self.config["gradient_steps"]):
                rows = self.random.sample(list(self.replay), self.config["batch_size"])
                values.append(self._update_dqn(rows) if self.algorithm == "double_dqn" else self._update_sac(rows))
            stats = {key: sum(item[key] for item in values) / len(values) for key in values[0]}
        self.last_stats = dict(stats, updated=True, steps=self.steps, updates=self.updates,
                               policy_version=self.policy_version)
        return copy.deepcopy(self.last_stats)

    def _update_dqn(self, rows):
        torch = self.torch
        current, following, actions, rewards, bootstrap = self._transition_batch(rows)
        states, candidates, masks = current
        next_states, next_candidates, next_masks = following
        values = self._scores("q", states, candidates).gather(1, actions.unsqueeze(1)).squeeze(1)
        with torch.no_grad():
            online = self._scores("q", next_states, next_candidates).masked_fill(~next_masks, -torch.inf)
            selected = online.argmax(dim=1)
            target = self._scores("target_q", next_states, next_candidates).gather(1, selected.unsqueeze(1)).squeeze(1)
            expected = rewards + self.config["gamma"] * bootstrap * target
        loss = torch.nn.functional.smooth_l1_loss(values, expected)
        self._step("q", loss, [self.networks["q"]])
        self.updates += 1
        self.policy_version += 1
        if self.updates % self.config["target_update_interval"] == 0:
            self.networks["target_q"].load_state_dict(self.networks["q"].state_dict())
        return dict(q_loss=float(loss.item()), mean_q=float(values.detach().mean().item()))

    def _update_sac(self, rows):
        torch = self.torch
        current, following, actions, rewards, bootstrap = self._transition_batch(rows)
        states, candidates, masks = current
        next_states, next_candidates, next_masks = following
        alpha = self.config["temperature"]
        with torch.no_grad():
            next_probs, next_logs = self._distribution(self._scores("actor", next_states, next_candidates), next_masks)
            minimum = torch.minimum(self._scores("target_q1", next_states, next_candidates),
                                    self._scores("target_q2", next_states, next_candidates))
            next_value = (next_probs * (minimum - alpha * next_logs)).sum(dim=1)
            expected = rewards + self.config["gamma"] * bootstrap * next_value
        q1 = self._scores("q1", states, candidates).gather(1, actions.unsqueeze(1)).squeeze(1)
        q2 = self._scores("q2", states, candidates).gather(1, actions.unsqueeze(1)).squeeze(1)
        critic_loss = torch.nn.functional.mse_loss(q1, expected) + torch.nn.functional.mse_loss(q2, expected)
        self._step("critic", critic_loss, [self.networks["q1"], self.networks["q2"]])
        probs, logs = self._distribution(self._scores("actor", states, candidates), masks)
        with torch.no_grad():
            minimum = torch.minimum(self._scores("q1", states, candidates), self._scores("q2", states, candidates))
        actor_loss = (probs * (alpha * logs - minimum)).sum(dim=1).mean()
        self._step("actor", actor_loss, [self.networks["actor"]])
        tau = self.config["target_tau"]
        with torch.no_grad():
            for name in ("q1", "q2"):
                for target, source in zip(self.networks["target_" + name].parameters(), self.networks[name].parameters()):
                    target.mul_(1 - tau).add_(source, alpha=tau)
        self.updates += 1
        self.policy_version += 1
        return dict(critic_loss=float(critic_loss.item()), actor_loss=float(actor_loss.item()),
                    entropy=float(-(probs.detach() * logs.detach()).sum(dim=1).mean().item()), temperature=alpha)

    def _update_ppo(self):
        torch = self.torch
        rows = self.rollout
        current, following, actions, rewards, bootstrap = self._transition_batch(rows)
        states, candidates, masks = current
        with torch.no_grad():
            next_values = self._values(*following)
        old_values = torch.tensor([row["value"] for row in rows], dtype=torch.float32, device=self.device)
        old_logs = torch.tensor([row["old_log_prob"] for row in rows], dtype=torch.float32, device=self.device)
        advantages = torch.zeros_like(rewards)
        future = 0.0
        for index in reversed(range(len(rows))):
            row = rows[index]
            continuous = not row["terminated"] and not row["truncated"] and index + 1 < len(rows)
            if continuous:
                following_row = rows[index + 1]
                continuous = (row.get("episode_id") == following_row.get("episode_id") and
                              row["next_state"] == following_row["state"] and
                              row["next_candidates"] == following_row["candidates"] and
                              row["next_legal"] == following_row["legal"])
            delta = rewards[index] + self.config["gamma"] * bootstrap[index] * next_values[index] - old_values[index]
            future = delta + self.config["gamma"] * self.config["gae_lambda"] * future * float(continuous)
            advantages[index] = future
        returns = advantages + old_values
        if len(rows) > 1:
            advantages = (advantages - advantages.mean()) / advantages.std(unbiased=False).clamp_min(1e-8)
        stats = []
        for _ in range(self.config["epochs"]):
            order = torch.randperm(len(rows), generator=self.generator, device=self.device)
            for start in range(0, len(rows), self.config["batch_size"]):
                selection = order[start:start + self.config["batch_size"]]
                batch_states, batch_candidates, batch_masks = states[selection], candidates[selection], masks[selection]
                probabilities, log_probs = self._distribution(self._scores("actor", batch_states, batch_candidates), batch_masks)
                selected_logs = log_probs.gather(1, actions[selection].unsqueeze(1)).squeeze(1)
                ratio = (selected_logs - old_logs[selection]).exp()
                direct = ratio * advantages[selection]
                clipped = ratio.clamp(1 - self.config["clip_ratio"], 1 + self.config["clip_ratio"]) * advantages[selection]
                policy_loss = -torch.minimum(direct, clipped).mean()
                values = self._values(batch_states, batch_candidates, batch_masks)
                value_loss = torch.nn.functional.mse_loss(values, returns[selection])
                entropy = -(probabilities * log_probs).sum(dim=1).mean()
                loss = (policy_loss + self.config["value_coefficient"] * value_loss -
                        self.config["entropy_coefficient"] * entropy)
                self._step("policy", loss, [self.networks["actor"], self.networks["value"]])
                self.updates += 1
                stats.append(dict(policy_loss=float(policy_loss.item()), value_loss=float(value_loss.item()),
                                  entropy=float(entropy.item()), approximate_kl=float((old_logs[selection] - selected_logs).mean().item()),
                                  clip_fraction=float(((ratio - 1).abs() > self.config["clip_ratio"]).float().mean().item())))
        self.rollout = []
        self.policy_version += 1
        return {key: sum(row[key] for row in stats) / len(stats) for key in stats[0]}

    def snapshot(self):
        return dict(algorithm=self.algorithm, observation_dim=self.observation_dim, candidate_dim=self.candidate_dim,
                    config=copy.deepcopy(self.config), seed=self.seed, steps=self.steps, updates=self.updates,
                    policy_version=self.policy_version, replay_size=len(self.replay), rollout_size=len(self.rollout),
                    last_stats=copy.deepcopy(self.last_stats), frozen_action_selection="greedy_legal")

    def save(self, path):
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(format_version=1, **self.snapshot(),
                       networks={name: network.state_dict() for name, network in self.networks.items()},
                       optimizers={name: optimizer.state_dict() for name, optimizer in self.optimizers.items()},
                       replay=list(self.replay) if self.algorithm != "ppo" else [],
                       random_state=self.random.getstate(), generator_state=self.generator.get_state())
        # PPO rollout is deliberately not portable to a newly loaded policy run.
        self.torch.save(payload, destination)
        return str(destination)

    def load(self, path, optimizer=True):
        payload = self.torch.load(Path(path), map_location=self.device, weights_only=True)
        if (payload.get("format_version") != 1 or payload.get("algorithm") != self.algorithm or
                payload.get("observation_dim") != self.observation_dim or payload.get("candidate_dim") != self.candidate_dim):
            raise ValueError("checkpoint algorithm or feature dimensions are incompatible")
        if payload["config"]["hidden_sizes"] != self.config["hidden_sizes"]:
            raise ValueError("checkpoint network shape is incompatible")
        if optimizer and payload["config"] != self.config:
            raise ValueError("resume with optimizer requires the saved learner configuration")
        for name, network in self.networks.items():
            network.load_state_dict(payload["networks"][name])
        self.replay.clear()
        self.rollout = []
        self.steps, self.updates, self.policy_version = 0, 0, 0
        self.last_stats = {}
        if optimizer:
            for name, instance in self.optimizers.items():
                instance.load_state_dict(payload["optimizers"][name])
            self.steps, self.updates = payload["steps"], payload["updates"]
            self.policy_version = payload["policy_version"]
            self.last_stats = copy.deepcopy(payload.get("last_stats", {}))
            self.random.setstate(payload["random_state"])
            self.generator.set_state(payload["generator_state"].cpu())
            if self.algorithm != "ppo":
                for row in payload["replay"]:
                    self.replay.append(copy.deepcopy(row))
        return self.snapshot()
