"""Single control loop; native callables execute in their original caller context.

This local backend accounts for configured execution slots. It does not lease GPU
memory, prove callback purity, revoke remote effects, or migrate a live stack.
"""

import asyncio
import contextvars
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
        self.failed = False
        self.detached_sources = set()
        self.state = dict(schema="runtime/2", quota=quota, gates={}, calls={})

    def _persist(self, event):
        if self.failed:
            raise ProtocolError("journal unavailable; admission stopped")
        try:
            self.store.save("runtime", "local", self.state, event)
        except Exception:
            self.failed = True
            raise

    def snapshot(self):
        import copy
        value = copy.deepcopy(self.state)
        value["used"] = self.used()
        value["legal_actions"] = [c["id"] for c in value["calls"].values()
                                  if c["physical"] == "ENABLED"
                                  and value["gates"][c["gate"]]["active"]
                                  and not self.owner_busy(c["owner"])
                                  and self.used() + c["slots"] <= value["quota"]]
        value["policy"] = "tabular-q-learning" if self.policy else ("fifo" if self.automatic else "manual")
        return value

    def used(self):
        return sum(c["slots"] for c in self.state["calls"].values()
                   if c["physical"] in {"RESERVED", "AUTHORIZED", "STARTED", "UNKNOWN"})

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
        self.state["calls"][identity] = dict(id=identity, gate=gate, owner=owner,
            generation=generation, slots=g["slots"], physical="ENABLED",
            logical="WAITING", starts=0, receipt=None, permit=None,
            task_id=task_id, unit=g.get("unit"), semantic_key=(g.get("unit") or {}).get("semantic_key") or
                (g.get("unit") or {}).get("id", gate.rsplit(":",1)[-1]),
            enabled_at=time.time(), started_at=None, latency_ms=None)
        self.waiters[identity] = asyncio.get_running_loop().create_future()
        self._persist("call_enabled")
        await self.pump()
        return identity

    async def reserve(self, identity):
        c = self.state["calls"][identity]
        g = self.state["gates"][c["gate"]]
        if c["physical"] != "ENABLED" or not g["active"]:
            raise ProtocolError("call not legally reservable")
        if self.used() + c["slots"] > self.state["quota"]:
            raise ProtocolError("managed quota exhausted")
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

    async def pump(self):
        if not self.automatic:
            return
        ready = self._ready()
        while ready:
            if self.policy:
                call, token = self.policy.select_ready(ready, self._observation())
                self.decisions[call["id"]] = token
            else:
                call = ready[0]
            await self.authorize(call["id"])
            ready = self._ready()

    def _ready(self):
        return [c for c in self.state["calls"].values() if c["physical"] == "ENABLED"
                and self.state["gates"][c["gate"]]["active"] and not self.owner_busy(c["owner"])
                and self.used() + c["slots"] <= self.state["quota"]]

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
