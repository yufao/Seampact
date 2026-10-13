"""Two Q-learning policies selecting only caller-supplied legal candidates."""

import json
import math
import random
import threading
import uuid
from pathlib import Path


def _number(value):
    if isinstance(value, (int, float)) and math.isfinite(value):
        return max(0, value)
    return None


def _count(value):
    if isinstance(value, (list, tuple, set, dict)):
        return len(value)
    return _number(value)


def _bucket(value):
    if value is None:
        return "unknown"
    if value <= 0:
        return "0"
    if value <= 1:
        return "1"
    if value <= 2:
        return "2"
    if value <= 4:
        return "3-4"
    if value <= 8:
        return "5-8"
    return "9+"


def _wait_bucket(value):
    if value is None:
        return "unknown"
    if value <= 0:
        return "0"
    if value < 100:
        return "<100ms"
    if value < 1000:
        return "<1s"
    if value < 10000:
        return "<10s"
    return "10s+"


def _unit_key(value):
    if isinstance(value, str) and value:
        return value
    if isinstance(value, dict):
        for name in ("semantic_key", "unit_key", "unit_id", "symbol", "name", "id", "gate_key", "gate"):
            if value.get(name):
                return str(value[name])
    raise ValueError("candidate needs a semantic Unit key")


def _action_key(layer, candidate):
    for name in ("semantic_key", "plan_key" if layer == "upper" else "unit_key"):
        if candidate.get(name):
            return str(candidate[name])
    if layer == "lower":
        for name in ("unit_id", "unit", "symbol", "gate_key", "gate", "name"):
            if candidate.get(name):
                return _unit_key(candidate[name])
        raise ValueError("ready call needs a Unit or gate key")
    units = candidate.get("units")
    if units:
        value = dict(units=[_unit_key(unit) for unit in units])
        for name in ("method", "method_key", "granularity", "mode"):
            if candidate.get(name) is not None:
                value[name] = candidate[name]
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if candidate.get("name"):
        return str(candidate["name"])
    raise ValueError("plan needs a semantic plan key or Unit list")


def _state_key(candidates, observation):
    observation = observation or {}
    queue = observation.get("queue_length", observation.get("waiting_count", observation.get("waiting")))
    used = observation.get("used_slots", observation.get("used"))
    quota = observation.get("quota", observation.get("total_slots"))
    busy = observation.get("busy_owners", observation.get("owner_busy"))
    owners = {str(candidate["owner"]) for candidate in candidates if candidate.get("owner") is not None}
    wait = _number(observation.get("wait_ms", observation.get("waiting_ms", observation.get("mean_wait_ms"))))
    if wait is None:
        observed_waits = [_number(candidate.get("wait_ms", candidate.get("waiting_ms"))) for candidate in candidates]
        observed_waits = [value for value in observed_waits if value is not None]
        wait = max(observed_waits) if observed_waits else None
    value = dict(candidates=_bucket(len(candidates)), queue=_bucket(_count(queue)),
                 used_slots=_bucket(_number(used)), quota=_bucket(_number(quota)),
                 busy_owners=_bucket(_count(busy)),
                 candidate_owners=_bucket(len(owners) if owners else None),
                 wait=_wait_bucket(wait))
    if observation.get("task_type") is not None:
        value["task_type"] = str(observation["task_type"])
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class TwoLayerScheduler:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.file = self.directory / "q_tables.json"
        self.lock = threading.RLock()
        self.epsilon, self.alpha, self.gamma = 0.1, 0.2, 0.9
        self.q = {"upper": {}, "lower": {}}
        self.updates = {"upper": 0, "lower": 0}
        if self.file.is_file():
            saved = json.loads(self.file.read_text(encoding="utf-8"))
            self.q = saved["q"]
            self.updates = saved["updates"]
        self.pending = {}
        self.last = {"upper": None, "lower": None}

    def _select(self, layer, candidates, observation):
        if not isinstance(candidates, list) or not candidates:
            raise ValueError("no legal " + ("plans" if layer == "upper" else "enabled calls"))
        if any(not isinstance(candidate, dict) for candidate in candidates):
            raise ValueError("candidates must be dictionaries")
        with self.lock:
            state = _state_key(candidates, observation)
            keys = [_action_key(layer, candidate) for candidate in candidates]
            values = self.q[layer].get(state, {})
            explore = len(candidates) > 1 and random.random() < self.epsilon
            if explore:
                index = random.randrange(len(candidates))
            else:
                best = max(values.get(key, 0.0) for key in keys)
                index = random.choice([index for index, key in enumerate(keys) if values.get(key, 0.0) == best])
            token = uuid.uuid4().hex
            self.pending[token] = dict(layer=layer, state=state, action=keys[index])
            self.last[layer] = dict(candidate_count=len(candidates), action_key=keys[index], explored=explore)
            return candidates[index], token

    def select_plan(self, plans, observation):
        """Plans already carry the same task obligations and executable Unit calls."""
        return self._select("upper", plans, observation)

    def select_ready(self, calls, observation):
        """The caller supplies already-enabled, legally dispatchable calls."""
        return self._select("lower", calls, observation)

    def feedback(self, token, success, duration_ms, next_candidates=None, next_observation=None):
        if type(success) is not bool:
            raise ValueError("feedback success must be boolean")
        if type(duration_ms) not in (int, float) or not math.isfinite(duration_ms) or duration_ms < 0:
            raise ValueError("feedback duration must be a nonnegative number of milliseconds")
        with self.lock:
            decision = self.pending.get(token)
            if decision is None:
                raise ValueError("unknown or already completed decision")
            layer = decision["layer"]
            following = 0.0
            # None or an empty legal set has no successor action to bootstrap.
            if next_candidates is not None and (not isinstance(next_candidates, list) or
                    any(not isinstance(candidate, dict) for candidate in next_candidates)):
                raise ValueError("next candidates must be legal candidate dictionaries")
            if next_candidates:
                next_state = _state_key(next_candidates, next_observation)
                values = self.q[layer].get(next_state, {})
                following = max(values.get(_action_key(layer, candidate), 0.0) for candidate in next_candidates)
            seconds = duration_ms / 1000.0
            reward = (1.0 if success else -1.0) - seconds / (1.0 + seconds)
            values = self.q[layer].setdefault(decision["state"], {})
            previous = values.get(decision["action"], 0.0)
            values[decision["action"]] = previous + self.alpha * (reward + self.gamma * following - previous)
            self.updates[layer] += 1
            del self.pending[token]
            self._save()

    def _save(self):
        temporary = self.file.with_suffix(".tmp")
        temporary.write_text(json.dumps(dict(schema="two-layer-q/1", q=self.q, updates=self.updates),
                                        ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.file)

    def snapshot(self):
        with self.lock:
            value = dict(status="READY", policy="TWO_LAYER_TABULAR_Q_LEARNING",
                         theory_pending=False, epsilon=self.epsilon, alpha=self.alpha, gamma=self.gamma)
            for layer in ("upper", "lower"):
                last = self.last[layer] or {}
                count = last.get("candidate_count")
                value[layer] = dict(updates=self.updates[layer], state_count=len(self.q[layer]),
                    action_count=sum(len(actions) for actions in self.q[layer].values()),
                    candidate_count=count, choice_available=count is not None and count > 1,
                    action_key=last.get("action_key"), explored=last.get("explored", False))
            return value
