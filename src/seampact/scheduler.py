"""Stable scheduler facade: historical plan Q values and legal-call FIFO.

Neural intervention learners are owned by immutable research runs.  Ordinary
business plan selection keeps its historical table, without presenting those
old success/latency values as independent safety feedback.
"""

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
        self.fifo_dispatches = 0

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
        """The caller supplies enabled calls in their original FIFO order."""
        if not isinstance(calls, list) or not calls:
            raise ValueError("no legally enabled calls")
        if any(not isinstance(call, dict) for call in calls):
            raise ValueError("calls must be dictionaries")
        with self.lock:
            call = calls[0]
            key = _action_key("lower", call)
            token = uuid.uuid4().hex
            self.pending[token] = dict(layer="lower", action=key)
            self.last["lower"] = dict(candidate_count=len(calls), action_key=key, explored=False)
            self.fifo_dispatches += 1
            return call, token

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
            if layer == "lower":
                # A post-return ready snapshot is not a next research decision.
                # FIFO has no value target to bootstrap or policy to update.
                del self.pending[token]
                return
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

    def discard(self, token):
        """A revoked pre-start decision has no execution outcome to learn from."""
        with self.lock:
            self.pending.pop(token, None)

    def snapshot(self):
        with self.lock:
            value = dict(status="READY", policy="HISTORICAL_PLAN_Q_WITH_FIFO_DISPATCH",
                         theory_pending=False, epsilon=self.epsilon, alpha=self.alpha, gamma=self.gamma)
            last = self.last["upper"] or {}
            count = last.get("candidate_count")
            value["upper"] = dict(algorithm="historical_tabular_q", feedback="legacy_success_latency",
                updates=self.updates["upper"], state_count=len(self.q["upper"]),
                action_count=sum(len(actions) for actions in self.q["upper"].values()),
                candidate_count=count, choice_available=count is not None and count > 1,
                action_key=last.get("action_key"), explored=last.get("explored", False))
            last = self.last["lower"] or {}
            count = last.get("candidate_count")
            value["lower"] = dict(algorithm="fifo", updates=0, state_count=0, action_count=0,
                dispatches=self.fifo_dispatches, candidate_count=count,
                choice_available=count is not None and count > 1,
                action_key=last.get("action_key"), explored=False,
                historical_q_updates=self.updates["lower"])
            return value
