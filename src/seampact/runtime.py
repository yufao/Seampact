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
        self.state = dict(schema="runtime/4", quota=quota, gates={}, calls={}, roots={})

    def _persist(self, event):
        if self.failed:
            raise ProtocolError("journal unavailable; admission stopped")
        try:
            self.store.save("runtime", "local", self.state, event)
        except Exception:
            self.failed = True
            raise

    def snapshot(self):
        value = copy.deepcopy(self.state)
        value["used"] = self.used()
        value["legal_actions"] = [c["id"] for c in self._ready()]
        value["roots_used"] = self.roots_used()
        value["call_slots"] = (self.root_control or {}).get("call_slots")
        value["policy"] = "tabular-q-learning" if self.policy else ("fifo" if self.automatic else "manual")
        return value

    def used(self):
        return sum(c["slots"] for c in self.state["calls"].values()
                   if c["physical"] in {"RESERVED", "AUTHORIZED", "STARTED", "UNKNOWN"} and not c.get("root_id"))

    def roots_used(self):
        # Slots belong to the admitting window; a closed window's hosts were drained before it ended.
        run_id = (self.root_control or {}).get("run_id")
        return sum(1 for r in self.state["roots"].values() if r["state"] in {"ACTIVE", "UNKNOWN"} and r["run_id"] == run_id)

    def _local_held(self):
        return any(r["state"] in {"ACTIVE", "UNKNOWN"} and r["host"] == "local" for r in self.state["roots"].values())

    def host_busy(self, host):
        if any(r["state"] in {"ACTIVE", "UNKNOWN"} and r["host"] == host for r in self.state["roots"].values()):
            return True
        # This process runs a local root and a call outside any root one at a time.
        return host == "local" and any(
            not c.get("root_id") and not (c.get("unit") or {}).get("instance_id")
            and c["physical"] in {"RESERVED", "AUTHORIZED", "STARTED", "UNKNOWN"} for c in self.state["calls"].values())

    def _covering_root(self, task_id, unit):
        if task_id is None:
            return None
        host = (unit or {}).get("instance_id") or "local"
        return next((r for r in self.state["roots"].values()
                     if r["state"] == "ACTIVE" and r["task_id"] == task_id and r["host"] == host), None)

    def owner_busy(self, owner):
        return any(c["owner"] == owner and c["physical"] in {"RESERVED", "AUTHORIZED", "STARTED", "UNKNOWN"}
                   for c in self.state["calls"].values())

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
            generation=generation, slots=slots, active=True, scope="native-boundary", unit=unit)
        self._persist("gate_registered")

    async def label(self, identity, unit):
        self.state["gates"][identity]["unit"] = unit

    async def enable(self, gate, owner, generation=1, task_id=None):
        g = self.state["gates"].get(gate)
        if not g or not g["active"] or (owner, generation) != (g["owner"], g["generation"]):
            raise ProtocolError("inactive gate or stale native-owner generation")
        identity = uuid.uuid4().hex
        root = self._covering_root(task_id, g.get("unit"))
        self.state["calls"][identity] = dict(id=identity, gate=gate, owner=owner,
            generation=generation, slots=g["slots"], physical="ENABLED",
            logical="WAITING", starts=0, receipt=None, permit=None,
            task_id=task_id, unit=g.get("unit"), semantic_key=(g.get("unit") or {}).get("semantic_key") or
                (g.get("unit") or {}).get("id", gate.rsplit(":",1)[-1]),
            root_id=root["id"] if root else None,
            enabled_at=time.time(), started_at=None, latency_ms=None)
        self.waiters[identity] = asyncio.get_running_loop().create_future()
        self._persist("call_enabled")
        self._notify_roots()
        await self.pump()
        return identity

    async def reserve(self, identity):
        c = self.state["calls"][identity]
        g = self.state["gates"][c["gate"]]
        if c["physical"] != "ENABLED" or not g["active"]:
            raise ProtocolError("call not legally reservable")
        if c.get("root_id"):
            if self.state["roots"][c["root_id"]]["state"] != "ACTIVE":
                raise ProtocolError("the covering root request is no longer active")
            if self._boundary_controlled(c) and self._boundaries_used() + c["slots"] > self.root_control["boundary_slots"]:
                raise ProtocolError("managed boundary execution slots exhausted")
        elif self.used() + c["slots"] > self.state["quota"]:
            raise ProtocolError("managed quota exhausted")
        elif not (c.get("unit") or {}).get("instance_id") and self._local_held():
            raise ProtocolError("this process is running a root request")
        if self.owner_busy(c["owner"]):
            raise ProtocolError("local native owner is non-reentrant until independently verified")
        c["physical"] = "RESERVED"
        self._persist("reserved")

    async def authorize(self, identity):
        c = self.state["calls"][identity]
        if c["physical"] == "ENABLED":
            await self.reserve(identity)
        g = self.state["gates"][c["gate"]]
        if c["physical"] != "RESERVED" or not g["active"]:
            raise ProtocolError("reservation invalidated before authorization")
        c.update(physical="AUTHORIZED", permit=uuid.uuid4().hex)
        self._persist("authorized")
        waiter = self.waiters.get(identity)
        if waiter and not waiter.done():
            waiter.set_result(c["permit"])
        return c["permit"]

    async def wait(self, identity):
        # Shield the coordinator's waiter from cancellation in the native owner.
        return await asyncio.shield(self.waiters[identity])

    async def consume(self, identity, permit, owner, generation=1):
        c = self.state["calls"][identity]
        if c["physical"] != "AUTHORIZED" or c["logical"] != "WAITING" or c["permit"] != permit:
            raise ProtocolError("permit already consumed, cancelled or absent")
        if (owner, generation) != (c["owner"], c["generation"]):
            raise ProtocolError("stale owner cannot consume a permit")
        c.update(physical="STARTED", starts=1, started_at=time.time())
        self._persist("started")

    async def finish(self, identity, permit, receipt):
        c = self.state["calls"][identity]
        if c["physical"] == "RETIRED":
            return False
        if c["physical"] not in {"STARTED", "UNKNOWN"} or c["permit"] != permit:
            raise ProtocolError("invalid physical completion receipt")
        c["physical"] = "RETIRED"
        if c["logical"] == "WAITING":
            c["logical"] = "COMPLETED"
        c["receipt"] = receipt
        c["latency_ms"] = (time.time() - c["enabled_at"]) * 1000
        token = self.decisions.pop(identity, None)
        if self.policy and token:
            following = self._ready()
            self.policy.feedback(token, receipt == "RETURN", c["latency_ms"], following, self._observation())
        self._persist("physically_retired")
        self._notify_roots()
        await self.pump()
        return True

    async def cancel(self, identity, prestart_fence=False):
        c = self.state["calls"][identity]
        if c["logical"] != "WAITING":
            return
        c["logical"] = "CANCELLED"
        if c["physical"] in {"ENABLED", "RESERVED"} or (
                c["physical"] == "AUTHORIZED" and prestart_fence):
            c["physical"] = "RETIRED"
        elif c["physical"] in {"AUTHORIZED", "STARTED"}:
            c["physical"] = "UNKNOWN"
        waiter = self.waiters.get(identity)
        if waiter and not waiter.done():
            waiter.cancel()
        self._persist("logically_cancelled")
        await self.pump()

    async def invalidate(self, gate):
        self.state["gates"][gate]["active"] = False
        for c in self.state["calls"].values():
            if c["gate"] == gate and c["physical"] in {"ENABLED", "RESERVED"}:
                c.update(physical="RETIRED", logical="REJECTED")
                w = self.waiters.get(c["id"])
                if w and not w.done():
                    w.set_exception(ProtocolError("gate invalidated"))
        self._persist("catalog_invalidated")
        await self.pump()

    async def detach_source(self, source_id):
        self.detached_sources.add(source_id)
        for gate in self.state["gates"].values():
            if (gate.get("unit") or {}).get("source_id") != source_id:
                continue
            gate["active"] = False
            for call in self.state["calls"].values():
                if call["gate"] == gate["id"] and call["physical"] in {"ENABLED", "RESERVED", "AUTHORIZED"}:
                    call.update(physical="RETIRED", logical="REJECTED", receipt="SOURCE_DETACHED")
                    token = self.decisions.pop(call["id"], None)
                    if token and self.policy:
                        self.policy.discard(token)
                    waiter = self.waiters.get(call["id"])
                    if waiter and not waiter.done():
                        waiter.set_exception(ProtocolError("Harness source is detached"))
        self._persist("source_detached")
        await self.pump()

    async def restore_source(self, source_id):
        # Prior gates remain inactive; the catalog creates a fresh binding generation.
        self.detached_sources.discard(source_id)

    async def remote_unknown(self, group_id, instance_id):
        # A root on this host may have lost its response before any inner call was recorded.
        for r in self.state["roots"].values():
            if r["state"] == "ACTIVE" and r["host"] == instance_id:
                r["outcome_unknown"] = True
        for call in self.state["calls"].values():
            unit = call.get("unit") or {}
            if (unit.get("runtime_group"), unit.get("instance_id")) != (group_id, instance_id):
                continue
            if call["physical"] in {"AUTHORIZED", "STARTED"}:
                call["physical"] = "UNKNOWN"
                self.state["gates"][call["gate"]]["active"] = False
            elif call["physical"] in {"ENABLED", "RESERVED"}:
                call.update(physical="RETIRED", logical="REJECTED", receipt="REMOTE_RESPONSE_UNKNOWN")
                waiter = self.waiters.get(call["id"])
                if waiter and not waiter.done():
                    waiter.set_exception(ProtocolError("remote response unavailable"))
        self._persist("remote_outcome_unknown")
        await self.pump()

    # Root requests of a joint window. The window's dispatcher chooses among
    # legal roots; authorize_root re-checks slots, host and placement atomically.

    async def configure_roots(self, control):
        if control is None:
            if self.root_control is not None:
                for identity, r in list(self.state["roots"].items()):
                    if r["state"] == "ENABLED" and r["run_id"] == self.root_control["run_id"]:
                        await self.cancel_root(identity, "joint window closed")
            self.root_control = None
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
                                 boundaries=control.get("boundaries", False), boundary_slots=boundary_slots)

    def _boundary_controlled(self, call):
        root = self.state["roots"].get(call.get("root_id"), {})
        return bool(self.root_control and self.root_control.get("boundaries")
                    and root.get("run_id") == self.root_control["run_id"])

    def _boundaries_used(self):
        return sum(c["slots"] for c in self.state["calls"].values() if self._boundary_controlled(c)
                   and c["physical"] in {"RESERVED", "AUTHORIZED", "STARTED", "UNKNOWN"})

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
        control = self.root_control
        if not control:
            raise ProtocolError("no joint window is admitting root requests")
        if not task_id or target.get("kind") not in {"LOCAL", "REMOTE"}:
            raise ProtocolError("a root request needs its task identity and frozen placement")
        identity = uuid.uuid4().hex
        self.state["roots"][identity] = dict(id=identity, run_id=control["run_id"], task_id=task_id, unit_id=unit_id,
            target=copy.deepcopy(target), state="ENABLED", host=None, enabled_at=time.time(),
            granted_at=None, finished_at=None, receipt=None, duration_ms=None)
        self.root_waiters[identity] = asyncio.get_running_loop().create_future()
        self._persist("root_enabled")
        self._notify_roots()
        return identity

    async def wait_root(self, identity):
        waiter = self.root_waiters[identity]
        try:
            return await asyncio.shield(waiter)
        finally:
            if waiter.done():
                self.root_waiters.pop(identity, None)

    async def authorize_root(self, identity, host):
        r, control = self.state["roots"].get(identity), self.root_control
        if not r or not control or r["state"] != "ENABLED" or r["run_id"] != control["run_id"]:
            raise ProtocolError("root request is not dispatchable")
        target = r["target"]
        if (host == "local") != (target["kind"] == "LOCAL") or target.get("instance_id") not in (None, host):
            raise ProtocolError("dispatch host differs from the frozen placement or task affinity")
        if self.roots_used() + 1 > control["call_slots"]:
            raise ProtocolError("root call slots exhausted")
        if self.host_busy(host):
            raise ProtocolError("the host is still running another root request or local call")
        r.update(state="ACTIVE", host=host, granted_at=time.time())
        self._persist("root_authorized")
        waiter = self.root_waiters.get(identity)
        if waiter and not waiter.done():
            waiter.set_result(dict(root_id=identity, host=host, wait_ms=(r["granted_at"] - r["enabled_at"]) * 1000))
        return host

    async def finish_root(self, identity, receipt):
        r = self.state["roots"][identity]
        if r["state"] != "ACTIVE":
            raise ProtocolError("root request is not active")
        unknown = r.get("outcome_unknown") or any(c.get("root_id") == identity and c["physical"] == "UNKNOWN"
                                                  for c in self.state["calls"].values())
        finished = time.time()
        # An UNKNOWN inner result keeps the root's slot and host occupied.
        r.update(state="UNKNOWN" if unknown else "RETIRED", finished_at=finished, receipt=receipt,
                 duration_ms=(finished - r["granted_at"]) * 1000)
        self._persist("root_retired")
        self._notify_roots()
        await self.pump()

    async def cancel_root(self, identity, reason):
        r = self.state["roots"].get(identity)
        if not r or r["state"] != "ENABLED":
            return False
        r.update(state="CANCELLED", finished_at=time.time(), receipt=reason)
        waiter = self.root_waiters.get(identity)
        if waiter and not waiter.done():
            waiter.set_exception(ProtocolError(reason))
        self._persist("root_cancelled")
        self._notify_roots()
        return True

    async def root_view(self, run_id):
        return [copy.deepcopy(r) for r in self.state["roots"].values() if r["run_id"] == run_id]

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
        ready = []
        local_held = self._local_held()
        for c in self.state["calls"].values():
            if (c["physical"] != "ENABLED" or not self.state["gates"][c["gate"]]["active"]
                    or self.owner_busy(c["owner"])):
                continue
            if c.get("root_id"):
                if (self.state["roots"][c["root_id"]]["state"] == "ACTIVE"
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
                    busy_owners=len({c["owner"] for c in self.state["calls"].values()
                                     if c["physical"] in {"AUTHORIZED", "STARTED"}}))


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
