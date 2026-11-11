"""Single control loop; native callables execute in their original caller context.

This local backend accounts for configured execution slots. It does not lease GPU
memory, prove callback purity, revoke remote effects, or migrate a live stack.
A joint window adds root requests: one business Unit invocation of a task holds one
root slot and its host from dispatch to return; gated calls inside it are admitted
under that reservation instead of taking another slot.
"""

import asyncio
import contextvars
import copy
import functools
import inspect
import time
import threading
import uuid
from concurrent.futures import TimeoutError as FutureTimeout


class ProtocolError(RuntimeError):
    pass


_native_gate = contextvars.ContextVar("seampact_native_gate", default=None)
_task_context = contextvars.ContextVar("seampact_task", default=None)
_OCCUPIED = {"RESERVED", "AUTHORIZED", "STARTED", "UNKNOWN"}


class Coordinator:
    def __init__(self, store, quota=1, automatic=True, policy=None):
        if type(quota) is not int or quota < 1:
            raise ValueError("positive managed slot quota required")
        self.store, self.automatic = store, automatic
        self.policy = policy
        self.decisions = {}
        self.waiters = {}
        self.root_waiters = {}
        self.root_control = None
        self.failed = False
        self.detached_sources = set()
        self.session_id = uuid.uuid4().hex
        self.state = dict(schema="runtime/4", quota=quota, gates={}, calls={}, roots={})
        # Dict indexes retain creation order. Historical records stay available
        # through snapshot, while admission only visits live work.
        self._active_calls = {}
        self._enabled_calls = {}
        self._occupied_calls = {}
        self._active_roots = {}
        self._held_roots = {}
        self._roots_by_run = {}
        self._calls_by_run = {}
        self._calls_by_task = {}
        self._root_children = {}
        self._root_unknowns = {}
        self._owner_counts = {}
        self._group_counts = {}
        self._run_slots = {}
        self._used_slots = 0
        self._local_calls = 0
        self._executing_owners = {}

    def _check_journal(self):
        if self.failed:
            raise ProtocolError("journal unavailable; admission stopped")

    def _persist(self, event, *, gates=(), calls=(), roots=()):
        self._check_journal()
        try:
            self.store.save_runtime(self.session_id, self.state, gates=gates,
                                    calls=calls, roots=roots, event=event)
        except Exception:
            self.failed = True
            for waiters in (self.waiters, self.root_waiters):
                for waiter in waiters.values():
                    if not waiter.done():
                        waiter.set_exception(ProtocolError("journal unavailable; admission stopped"))
            raise

    @staticmethod
    def _count(counts, key, delta):
        value = counts.get(key, 0) + delta
        if value:
            counts[key] = value
        else:
            counts.pop(key, None)

    def _sync_root(self, root):
        identity = root["id"]
        held = root["state"] in {"ACTIVE", "UNKNOWN"} or bool(self._root_children.get(identity))
        if held:
            self._held_roots[identity] = root
        else:
            self._held_roots.pop(identity, None)
        if held or root["state"] == "ENABLED":
            self._active_roots[identity] = root
        else:
            self._active_roots.pop(identity, None)

    def _call_activity(self, call, delta):
        identity, phase = call["id"], call["physical"]
        if phase == "ENABLED":
            if delta > 0:
                self._enabled_calls[identity] = call
            else:
                self._enabled_calls.pop(identity, None)
        if phase in {"ENABLED", "RESERVED"}:
            group = (call.get("unit") or {}).get("runtime_group")
            if group is not None:
                self._count(self._group_counts, group, delta)
        if phase in {"AUTHORIZED", "STARTED"}:
            self._count(self._executing_owners, call["owner"], delta)
        if phase not in _OCCUPIED:
            return
        if delta > 0:
            self._occupied_calls[identity] = call
        else:
            self._occupied_calls.pop(identity, None)
        self._count(self._owner_counts, call["owner"], delta)
        root_id = call.get("root_id")
        if root_id:
            self._count(self._root_children, root_id, delta)
            if phase == "UNKNOWN":
                self._count(self._root_unknowns, root_id, delta)
            root = self.state["roots"][root_id]
            self._count(self._run_slots, root["run_id"], delta * call["slots"])
        else:
            self._used_slots += delta * call["slots"]
            if not (call.get("unit") or {}).get("instance_id"):
                self._local_calls += delta

    def _change_call(self, call, **changes):
        self._call_activity(call, -1)
        call.update(changes)
        self._call_activity(call, 1)
        if call["physical"] == "RETIRED":
            self._active_calls.pop(call["id"], None)
        if call.get("root_id"):
            self._sync_root(self.state["roots"][call["root_id"]])

    def _index_call(self, call):
        identity = call["id"]
        self._active_calls[identity] = call
        self._calls_by_task.setdefault(call["task_id"], {})[identity] = call
        if call.get("root_id"):
            run_id = self.state["roots"][call["root_id"]]["run_id"]
            self._calls_by_run.setdefault(run_id, {})[identity] = call
        self._call_activity(call, 1)

    async def group_call_counts(self):
        return dict(self._group_counts)

    async def call_view(self, identity):
        return copy.deepcopy(self.state["calls"].get(identity))

    async def gate_view(self, identity):
        return copy.deepcopy(self.state["gates"].get(identity))

    async def retired_boundaries(self, run_id, seen_ids):
        return [copy.deepcopy(call) for identity, call in self._calls_by_run.get(run_id, {}).items()
                if identity not in seen_ids and call["physical"] == "RETIRED"
                and call.get("root_id") in self.state["roots"]]

    async def task_calls(self, task_id):
        return copy.deepcopy(list(self._calls_by_task.get(task_id, {}).values()))

    async def task_calls_by_tasks(self, task_ids):
        return copy.deepcopy({task_id: list(self._calls_by_task.get(task_id, {}).values())
                              for task_id in task_ids})

    async def status_view(self):
        return dict(used=self.used(), quota=self.state["quota"],
                    legal_actions=[c["id"] for c in self._ready()], roots_used=self.roots_used(),
                    call_slots=(self.root_control or {}).get("call_slots"),
                    policy="tabular-q-learning" if self.policy else ("fifo" if self.automatic else "manual"))

    async def active_view(self):
        value = await self.status_view()
        value.update(schema=self.state["schema"], calls=copy.deepcopy(self._active_calls),
                     roots=copy.deepcopy(self._active_roots))
        return value

    def snapshot(self):
        value = copy.deepcopy(self.state)
        value["used"] = self.used()
        value["legal_actions"] = [c["id"] for c in self._ready()]
        value["roots_used"] = self.roots_used()
        value["call_slots"] = (self.root_control or {}).get("call_slots")
        value["policy"] = "tabular-q-learning" if self.policy else ("fifo" if self.automatic else "manual")
        return value

    def used(self):
        return self._used_slots

    def roots_used(self):
        return len(self._held_roots)

    def _root_held(self, root):
        return root["id"] in self._held_roots

    def _release_closed_roots(self):
        if self.root_control and self.root_control.get("closing") and not self.roots_used():
            self.root_control = None

    def _local_held(self):
        return any(r["host"] == "local" for r in self._held_roots.values())

    def host_busy(self, host):
        if any(r["host"] == host for r in self._held_roots.values()):
            return True
        # This process runs a local root and a call outside any root one at a time.
        return host == "local" and bool(self._local_calls)

    def _covering_root(self, task_id, unit):
        if task_id is None:
            return None
        host = (unit or {}).get("instance_id") or "local"
        return next((r for r in self._held_roots.values()
                     if r["task_id"] == task_id and r["host"] == host), None)

    def owner_busy(self, owner):
        return bool(self._owner_counts.get(owner))

    async def register(self, identity, owner, generation=1, slots=1, unit=None):
        if (unit or {}).get("source_id") in self.detached_sources:
            raise ProtocolError("Harness source is detached")
        if not identity or not owner or type(generation) is not int or generation < 1:
            raise ValueError("gate and native-owner generation required")
        if type(slots) is not int or not 1 <= slots <= self.state["quota"]:
            raise ValueError("slots exceed configured managed quota")
        if identity in self.state["gates"]:
            raise ProtocolError("use a new gate identity for a new immutable version")
        self.state["gates"][identity] = dict(id=identity, owner=owner,
            generation=generation, slots=slots, active=True, scope="native-boundary", unit=copy.deepcopy(unit))
        self._persist("gate_registered", gates=(identity,))

    async def label(self, identity, unit):
        # Calls retain the previous frozen label; neither the caller's mutable
        # input nor later labels can rewrite their Unit version.
        self.state["gates"][identity]["unit"] = copy.deepcopy(unit)
        self._persist("gate_labelled", gates=(identity,))

    async def enable(self, gate, owner, generation=1, task_id=None):
        self._check_journal()
        g = self.state["gates"].get(gate)
        if not g or not g["active"] or (owner, generation) != (g["owner"], g["generation"]):
            raise ProtocolError("inactive gate or stale native-owner generation")
        identity = uuid.uuid4().hex
        root = self._covering_root(task_id, g.get("unit"))
        if root and (root["state"] != "ACTIVE" or not self.root_control or self.root_control.get("closing")
                     or root["run_id"] != self.root_control["run_id"]):
            raise ProtocolError("the covering joint window is closed")
        self.state["calls"][identity] = dict(id=identity, gate=gate, owner=owner,
            generation=generation, slots=g["slots"], physical="ENABLED",
            logical="WAITING", starts=0, receipt=None, permit=None,
            task_id=task_id, unit=g.get("unit"), semantic_key=(g.get("unit") or {}).get("semantic_key") or
                (g.get("unit") or {}).get("id", gate.rsplit(":",1)[-1]),
            root_id=root["id"] if root else None,
            enabled_at=time.time(), started_at=None, latency_ms=None)
        self.waiters[identity] = asyncio.get_running_loop().create_future()
        self._index_call(self.state["calls"][identity])
        self._persist("call_enabled", calls=(identity,))
        self._notify_roots()
        await self.pump()
        return identity

    async def reserve(self, identity):
        self._check_journal()
        c = self.state["calls"][identity]
        g = self.state["gates"][c["gate"]]
        if c["physical"] != "ENABLED" or not g["active"]:
            raise ProtocolError("call not legally reservable")
        if c.get("root_id"):
            root = self.state["roots"][c["root_id"]]
            if (root["state"] != "ACTIVE" or not self.root_control or self.root_control.get("closing")
                    or root["run_id"] != self.root_control["run_id"]):
                raise ProtocolError("the covering root request is no longer active")
            if self._boundary_controlled(c) and self._boundaries_used() + c["slots"] > self.root_control["boundary_slots"]:
                raise ProtocolError("managed boundary execution slots exhausted")
        elif self.used() + c["slots"] > self.state["quota"]:
            raise ProtocolError("managed quota exhausted")
        elif not (c.get("unit") or {}).get("instance_id") and self._local_held():
            raise ProtocolError("this process is running a root request")
        if self.owner_busy(c["owner"]):
            raise ProtocolError("local native owner is non-reentrant until independently verified")
        self._change_call(c, physical="RESERVED")
        self._persist("reserved", calls=(identity,))

    async def authorize(self, identity):
        self._check_journal()
        c = self.state["calls"][identity]
        if c["physical"] == "ENABLED":
            await self.reserve(identity)
        g = self.state["gates"][c["gate"]]
        if c["physical"] != "RESERVED" or not g["active"]:
            raise ProtocolError("reservation invalidated before authorization")
        if c.get("root_id") and (not self.root_control or self.root_control.get("closing")
                or self.state["roots"][c["root_id"]]["run_id"] != self.root_control["run_id"]
                or self.state["roots"][c["root_id"]]["state"] != "ACTIVE"):
            raise ProtocolError("the covering joint window is closed")
        self._change_call(c, physical="AUTHORIZED", permit=uuid.uuid4().hex)
        self._persist("authorized", calls=(identity,))
        waiter = self.waiters.get(identity)
        if waiter and not waiter.done():
            waiter.set_result(c["permit"])
        return c["permit"]

    async def wait(self, identity):
        # Shield the coordinator's waiter from cancellation in the native owner.
        self._check_journal()
        permit = await asyncio.shield(self.waiters[identity])
        self._check_journal()
        return permit

    async def consume(self, identity, permit, owner, generation=1):
        self._check_journal()
        c = self.state["calls"][identity]
        if c["physical"] != "AUTHORIZED" or c["logical"] != "WAITING" or c["permit"] != permit:
            raise ProtocolError("permit already consumed, cancelled or absent")
        if (owner, generation) != (c["owner"], c["generation"]):
            raise ProtocolError("stale owner cannot consume a permit")
        self._change_call(c, physical="STARTED", starts=1, started_at=time.time())
        self._persist("started", calls=(identity,))

    async def finish(self, identity, permit, receipt):
        c = self.state["calls"][identity]
        if c["physical"] == "RETIRED":
            return False
        if c["physical"] not in {"STARTED", "UNKNOWN"} or c["permit"] != permit:
            raise ProtocolError("invalid physical completion receipt")
        self._change_call(c, physical="RETIRED")
        if c["logical"] == "WAITING":
            c["logical"] = "COMPLETED"
        c["receipt"] = receipt
        c["latency_ms"] = (time.time() - c["enabled_at"]) * 1000
        token = self.decisions.pop(identity, None)
        if self.policy and token:
            following = self._ready()
            self.policy.feedback(token, receipt == "RETURN", c["latency_ms"], following, self._observation())
        self._persist("physically_retired", calls=(identity,))
        self._notify_roots()
        self._release_closed_roots()
        await self.pump()
        return True

    async def cancel(self, identity, prestart_fence=False):
        c = self.state["calls"][identity]
        if c["logical"] != "WAITING":
            return
        c["logical"] = "CANCELLED"
        if c["physical"] in {"ENABLED", "RESERVED"} or (
                c["physical"] == "AUTHORIZED" and prestart_fence):
            self._change_call(c, physical="RETIRED")
        elif c["physical"] in {"AUTHORIZED", "STARTED"}:
            self._change_call(c, physical="UNKNOWN")
        waiter = self.waiters.get(identity)
        if waiter and not waiter.done():
            waiter.cancel()
        self._persist("logically_cancelled", calls=(identity,))
        await self.pump()

    async def cancel_enabled(self, identity):
        """Withdraw only a still-enabled waiter, without touching in-flight work."""
        call = self.state["calls"].get(identity)
        if not call or call["physical"] != "ENABLED" or call["logical"] != "WAITING":
            return False
        self._change_call(call, physical="RETIRED", logical="REJECTED")
        waiter = self.waiters.get(identity)
        if waiter and not waiter.done():
            waiter.set_exception(ProtocolError("boundary contract no longer permits dispatch"))
        self._persist("boundary_rejected", calls=(identity,))
        await self.pump()
        return True

    async def invalidate(self, gate):
        self.state["gates"][gate]["active"] = False
        dirty = []
        for c in list(self._active_calls.values()):
            if c["gate"] == gate and c["physical"] in {"ENABLED", "RESERVED"}:
                self._change_call(c, physical="RETIRED", logical="REJECTED")
                dirty.append(c["id"])
                w = self.waiters.get(c["id"])
                if w and not w.done():
                    w.set_exception(ProtocolError("gate invalidated"))
        self._persist("catalog_invalidated", gates=(gate,), calls=dirty)
        await self.pump()

    async def detach_source(self, source_id):
        self.detached_sources.add(source_id)
        gates, calls = [], []
        for gate in self.state["gates"].values():
            if (gate.get("unit") or {}).get("source_id") != source_id:
                continue
            gate["active"] = False
            gates.append(gate["id"])
        detached_gates = set(gates)
        for call in list(self._active_calls.values()):
            if call["gate"] in detached_gates and call["physical"] in {"ENABLED", "RESERVED", "AUTHORIZED"}:
                self._change_call(call, physical="RETIRED", logical="REJECTED", receipt="SOURCE_DETACHED")
                calls.append(call["id"])
                token = self.decisions.pop(call["id"], None)
                if token and self.policy:
                    self.policy.discard(token)
                waiter = self.waiters.get(call["id"])
                if waiter and not waiter.done():
                    waiter.set_exception(ProtocolError("Harness source is detached"))
        self._persist("source_detached", gates=gates, calls=calls)
        await self.pump()

    async def restore_source(self, source_id):
        # Prior gates remain inactive; the catalog creates a fresh binding generation.
        self.detached_sources.discard(source_id)

    async def remote_unknown(self, group_id, instance_id):
        # A root on this host may have lost its response before any inner call was recorded.
        gates, calls, roots = {}, [], []
        for r in self._active_roots.values():
            if r["state"] == "ACTIVE" and r["host"] == instance_id:
                r["outcome_unknown"] = True
                roots.append(r["id"])
        for call in list(self._active_calls.values()):
            unit = call.get("unit") or {}
            if (unit.get("runtime_group"), unit.get("instance_id")) != (group_id, instance_id):
                continue
            if call["physical"] in {"AUTHORIZED", "STARTED"}:
                self._change_call(call, physical="UNKNOWN")
                self.state["gates"][call["gate"]]["active"] = False
                gates[call["gate"]] = None
                calls.append(call["id"])
            elif call["physical"] in {"ENABLED", "RESERVED"}:
                self._change_call(call, physical="RETIRED", logical="REJECTED", receipt="REMOTE_RESPONSE_UNKNOWN")
                calls.append(call["id"])
                waiter = self.waiters.get(call["id"])
                if waiter and not waiter.done():
                    waiter.set_exception(ProtocolError("remote response unavailable"))
        self._persist("remote_outcome_unknown", gates=list(gates), calls=calls, roots=roots)
        await self.pump()

    # Root requests of a joint window. The window's dispatcher chooses among
    # legal roots; authorize_root re-checks slots, host and placement atomically.

    async def configure_roots(self, control):
        if control is None:
            if self.root_control is not None:
                self.root_control["closing"] = True
                run_id = self.root_control["run_id"]
                for identity, r in self._roots_by_run.get(run_id, {}).items():
                    if r["state"] == "ENABLED":
                        await self.cancel_root(identity, "joint window closed")
                dirty = []
                for call in self._calls_by_run.get(run_id, {}).values():
                    if call["physical"] in {"ENABLED", "RESERVED", "AUTHORIZED"}:
                        # consume is the central no-start barrier: an issued but
                        # unconsumed permit cannot enter the original body after this step.
                        self._change_call(call, physical="RETIRED", logical="REJECTED", receipt="JOINT_WINDOW_CLOSED")
                        dirty.append(call["id"])
                        waiter = self.waiters.get(call["id"])
                        if waiter and not waiter.done():
                            waiter.set_exception(ProtocolError("joint window closed"))
                self._persist("root_window_closed", calls=dirty)
                # Keep the old limits and boundary classification until every
                # possibly executing root and inner call has actually retired.
                self._release_closed_roots()
            await self.pump()
            return
        if self.root_control is not None:
            raise ProtocolError("another joint window owns the root-request ledger")
        slots = control.get("call_slots")
        if type(slots) is not int or slots < 1:
            raise ValueError("call_slots must be a positive integer")
        boundary_slots = control.get("boundary_slots", slots)
        if type(boundary_slots) is not int or boundary_slots < 1:
            raise ValueError("boundary_slots must be a positive integer")
        self.root_control = dict(run_id=control["run_id"], call_slots=slots, notify=control.get("notify"),
                                 boundaries=control.get("boundaries", False), boundary_slots=boundary_slots, closing=False)

    def _boundary_controlled(self, call):
        root = self.state["roots"].get(call.get("root_id"), {})
        return bool(self.root_control and self.root_control.get("boundaries")
                    and root.get("run_id") == self.root_control["run_id"])

    def _boundaries_used(self):
        if not self.root_control or not self.root_control.get("boundaries"):
            return 0
        return self._run_slots.get(self.root_control["run_id"], 0)

    async def boundary_view(self, run_id):
        return [copy.deepcopy(c) for c in self._ready() if self._boundary_controlled(c)
                and self.state["roots"][c["root_id"]]["run_id"] == run_id]

    async def authorize_boundary(self, identity, run_id):
        call = self.state["calls"].get(identity)
        if (not call or not self._boundary_controlled(call)
                or self.state["roots"][call["root_id"]]["run_id"] != run_id
                or identity not in {c["id"] for c in self._ready()}):
            raise ProtocolError("boundary is no longer legally dispatchable")
        return await self.authorize(identity)

    def _notify_roots(self):
        notify = (self.root_control or {}).get("notify")
        if notify:
            notify()

    async def enable_root(self, task_id, unit_id, target):
        self._check_journal()
        control = self.root_control
        if not control or control.get("closing"):
            raise ProtocolError("no joint window is admitting root requests")
        if not task_id or target.get("kind") not in {"LOCAL", "REMOTE"}:
            raise ProtocolError("a root request needs its task identity and frozen placement")
        identity = uuid.uuid4().hex
        self.state["roots"][identity] = dict(id=identity, run_id=control["run_id"], task_id=task_id, unit_id=unit_id,
            target=copy.deepcopy(target), state="ENABLED", host=None, enabled_at=time.time(),
            granted_at=None, finished_at=None, receipt=None, duration_ms=None)
        self.root_waiters[identity] = asyncio.get_running_loop().create_future()
        root = self.state["roots"][identity]
        self._roots_by_run.setdefault(control["run_id"], {})[identity] = root
        self._sync_root(root)
        self._persist("root_enabled", roots=(identity,))
        self._notify_roots()
        return identity

    async def wait_root(self, identity):
        self._check_journal()
        waiter = self.root_waiters[identity]
        try:
            result = await asyncio.shield(waiter)
            self._check_journal()
            return result
        finally:
            if waiter.done():
                self.root_waiters.pop(identity, None)

    async def authorize_root(self, identity, host):
        self._check_journal()
        r, control = self.state["roots"].get(identity), self.root_control
        if (not r or not control or control.get("closing") or r["state"] != "ENABLED"
                or r["run_id"] != control["run_id"]):
            raise ProtocolError("root request is not dispatchable")
        target = r["target"]
        if (host == "local") != (target["kind"] == "LOCAL") or target.get("instance_id") not in (None, host):
            raise ProtocolError("dispatch host differs from the frozen placement or task affinity")
        if self.roots_used() + 1 > control["call_slots"]:
            raise ProtocolError("root call slots exhausted")
        if self.host_busy(host):
            raise ProtocolError("the host is still running another root request or local call")
        r.update(state="ACTIVE", host=host, granted_at=time.time())
        self._sync_root(r)
        self._persist("root_authorized", roots=(identity,))
        waiter = self.root_waiters.get(identity)
        if waiter and not waiter.done():
            waiter.set_result(dict(root_id=identity, host=host, wait_ms=(r["granted_at"] - r["enabled_at"]) * 1000))
        return host

    async def finish_root(self, identity, receipt):
        r = self.state["roots"][identity]
        if r["state"] != "ACTIVE":
            raise ProtocolError("root request is not active")
        unknown = r.get("outcome_unknown") or bool(self._root_unknowns.get(identity))
        finished = time.time()
        # An UNKNOWN inner result keeps the root's slot and host occupied.
        r.update(state="UNKNOWN" if unknown else "RETIRED", finished_at=finished, receipt=receipt,
                 duration_ms=(finished - r["granted_at"]) * 1000)
        self._sync_root(r)
        self._persist("root_retired", roots=(identity,))
        self._notify_roots()
        self._release_closed_roots()
        await self.pump()

    async def cancel_root(self, identity, reason):
        r = self.state["roots"].get(identity)
        if not r or r["state"] != "ENABLED":
            return False
        r.update(state="CANCELLED", finished_at=time.time(), receipt=reason)
        self._sync_root(r)
        waiter = self.root_waiters.get(identity)
        if waiter and not waiter.done():
            waiter.set_exception(ProtocolError(reason))
        self._persist("root_cancelled", roots=(identity,))
        self._notify_roots()
        return True

    async def root_view(self, run_id):
        return copy.deepcopy(list(self._roots_by_run.get(run_id, {}).values()))

    async def pump(self):
        if not self.automatic:
            return
        ready = [c for c in self._ready() if not self._boundary_controlled(c)]
        while ready:
            covered = [c for c in ready if c.get("root_id")]
            if covered:
                call = covered[0]
            elif self.policy:
                call, token = self.policy.select_ready(ready, self._observation())
                self.decisions[call["id"]] = token
            else:
                call = ready[0]
            await self.authorize(call["id"])
            ready = [c for c in self._ready() if not self._boundary_controlled(c)]

    def _ready(self):
        if self.failed:
            return []
        ready = []
        local_held = self._local_held()
        for c in self._enabled_calls.values():
            if (c["physical"] != "ENABLED" or not self.state["gates"][c["gate"]]["active"]
                    or self.owner_busy(c["owner"])):
                continue
            if c.get("root_id"):
                if (self.root_control and not self.root_control.get("closing")
                        and self.state["roots"][c["root_id"]]["run_id"] == self.root_control["run_id"]
                        and self.state["roots"][c["root_id"]]["state"] == "ACTIVE"
                        and (not self._boundary_controlled(c) or
                             self._boundaries_used() + c["slots"] <= self.root_control["boundary_slots"])):
                    ready.append(c)
            elif (self.used() + c["slots"] <= self.state["quota"]
                  and not (local_held and not (c.get("unit") or {}).get("instance_id"))):
                ready.append(c)
        return ready

    def _observation(self):
        ready = self._ready()
        return dict(waiting=len(ready), used=self.used(), quota=self.state["quota"],
                    wait_ms=max(((time.time() - c["enabled_at"]) * 1000 for c in ready), default=0),
                    busy_owners=len(self._executing_owners))


class OwnerRuntime:
    """A dedicated coordinator loop bridges native async loops and sync threads."""
    def __init__(self, coordinator):
        self.coordinator = coordinator
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run, daemon=True, name="seampact-coordinator")
        self.thread.start()

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()
        pending = asyncio.all_tasks(self.loop)
        for task in pending:
            task.cancel()
        self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        self.loop.close()

    def submit(self, method, *args, **kwargs):
        return asyncio.run_coroutine_threadsafe(getattr(self.coordinator, method)(*args, **kwargs), self.loop)

    def call(self, method, *args, **kwargs):
        if threading.current_thread() is self.thread:
            raise ProtocolError("synchronous owner cannot block the coordinator loop")
        return self.submit(method, *args, **kwargs).result()

    async def acall(self, method, *args, **kwargs):
        return await asyncio.wrap_future(self.submit(method, *args, **kwargs))

    def snapshot(self):
        async def read():
            return self.coordinator.snapshot()
        return asyncio.run_coroutine_threadsafe(read(), self.loop).result()

    def context(self, task_id):
        from contextlib import contextmanager
        @contextmanager
        def scope():
            token = _task_context.set(task_id)
            try:
                yield
            finally:
                _task_context.reset(token)
        return scope()

    def wrap(self, original, gate, owner, generation=1):
        if getattr(original,"__seampact_gate__",None) is not None:
            raise ProtocolError("callable already has a gate; nested wrapping is unsupported")
        if inspect.isgeneratorfunction(original) or inspect.isasyncgenfunction(original):
            raise ProtocolError("generator handoff is not supported by the local callable rule")
        if inspect.iscoroutinefunction(original):
            @functools.wraps(original)
            async def asynchronous(*args, **kwargs):
                if _native_gate.get() is not None:
                    raise ProtocolError("nested controlled calls require a verified parent/child resource rule")
                # Waiter creation is shielded so cancellation cannot lose its identity.
                enable = self.submit("enable", gate, owner, generation, _task_context.get())
                identity = None
                permit = None
                consumed = False
                try:
                    identity = await asyncio.shield(asyncio.wrap_future(enable))
                    permit = await self.acall("wait", identity)
                    consume = self.submit("consume", identity, permit, owner, generation)
                    try:
                        await asyncio.shield(asyncio.wrap_future(consume))
                        consumed = True
                    except asyncio.CancelledError:
                        # A queued consume may have committed. The owner has not called
                        # original yet; resolve it, then retire through that same identity.
                        consumed = await asyncio.shield(asyncio.wrap_future(consume)) is None
                        raise
                    token = _native_gate.set(gate)
                    try:
                        try:
                            result = await original(*args, **kwargs)
                        except BaseException as error:
                            await asyncio.shield(self.acall("finish", identity, permit,
                                                           type(error).__name__))
                            raise
                    finally:
                        _native_gate.reset(token)
                    await asyncio.shield(self.acall("finish", identity, permit, "RETURN"))
                    return result
                except asyncio.CancelledError:
                    if identity is None:
                        identity = await asyncio.shield(asyncio.wrap_future(enable))
                    await asyncio.shield(self.acall("cancel", identity, not consumed))
                    if consumed:
                        # Trusted local callable has unwound, or was never entered.
                        await asyncio.shield(self.acall("finish", identity, permit, "CancelledError"))
                    raise
            asynchronous.__seampact_gate__ = gate
            return asynchronous

        @functools.wraps(original)
        def synchronous(*args, **kwargs):
            if _native_gate.get() is not None:
                raise ProtocolError("nested controlled calls require a verified parent/child resource rule")
            identity = self.call("enable", gate, owner, generation, _task_context.get())
            permit = self.call("wait", identity)
            self.call("consume", identity, permit, owner, generation)
            token = _native_gate.set(gate)
            try:
                try:
                    result = original(*args, **kwargs)
                except BaseException as error:
                    self.call("finish", identity, permit, type(error).__name__)
                    raise
            finally:
                _native_gate.reset(token)
            self.call("finish", identity, permit, "RETURN")
            return result
        synchronous.__seampact_gate__ = gate
        return synchronous

    def close(self):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)
        if self.thread.is_alive():
            raise FutureTimeout("coordinator did not stop")
