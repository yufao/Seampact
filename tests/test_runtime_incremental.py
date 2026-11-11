"""Runtime indexes preserve admission semantics without visiting retained history."""
import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from seampact.runtime import Coordinator, ProtocolError
from seampact.store import Store


class RecordingStore(Store):
    def __init__(self):
        super().__init__(":memory:")
        self.changes = []

    def save_runtime(self, session_id, state, *, gates=(), calls=(), roots=(), event=None):
        self.changes.append((event, tuple(gates), tuple(calls), tuple(roots)))
        super().save_runtime(session_id, state, gates=gates, calls=calls, roots=roots, event=event)
        if self.read_runtime(session_id) != state:
            raise AssertionError("dirty IDs omitted a changed runtime record")


class NoHistoryScan(dict):
    def __iter__(self):
        raise AssertionError("visited all retained runtime identities")

    def values(self):
        raise AssertionError("visited all retained runtime values")

    def items(self):
        raise AssertionError("visited all retained runtime items")


class RuntimeIncrementalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = RecordingStore()
        self.addCleanup(self.store.close)
        self.c = Coordinator(self.store, quota=4, automatic=False)

    async def assert_indexes(self):
        c = self.c
        calls, roots = c.state["calls"], c.state["roots"]
        occupied = [call for call in calls.values()
                    if call["physical"] in {"RESERVED", "AUTHORIZED", "STARTED", "UNKNOWN"}]
        held = [r for r in roots.values() if r["state"] in {"ACTIVE", "UNKNOWN"}
                or any(call.get("root_id") == r["id"] for call in occupied)]
        self.assertEqual(c.used(), sum(call["slots"] for call in occupied if not call.get("root_id")))
        self.assertEqual(c.roots_used(), len(held))
        self.assertEqual(set(c._held_roots), {r["id"] for r in held})
        view = await c.active_view()
        self.assertEqual(list(view["calls"]), [identity for identity, call in calls.items()
                                              if call["physical"] != "RETIRED"])
        self.assertEqual(list(view["roots"]), [identity for identity, root in roots.items()
                                              if root["state"] == "ENABLED" or root in held])
        counts = {}
        for call in calls.values():
            group = (call.get("unit") or {}).get("runtime_group")
            if group is not None and call["physical"] in {"ENABLED", "RESERVED"}:
                counts[group] = counts.get(group, 0) + 1
        self.assertEqual(await c.group_call_counts(), counts)
        for owner in {call["owner"] for call in calls.values()}:
            self.assertEqual(c.owner_busy(owner), any(call["owner"] == owner for call in occupied))

    async def test_labels_are_frozen_persisted_versions_and_queries_are_isolated(self):
        c = self.c
        unit = dict(id="first", runtime_group="group", nested=dict(version=1))
        await c.register("gate", "owner", unit=unit)
        unit["nested"]["version"] = 99
        old = await c.enable("gate", "owner", task_id="task")
        replacement = dict(id="second", runtime_group="other", nested=dict(version=2))
        await c.label("gate", replacement)
        replacement["nested"]["version"] = 99
        new = await c.enable("gate", "owner", task_id="task")
        self.assertEqual((await c.call_view(old))["unit"]["nested"]["version"], 1)
        self.assertEqual((await c.call_view(new))["unit"]["nested"]["version"], 2)
        self.assertEqual(self.store.changes[-2], ("gate_labelled", ("gate",), (), ()))
        for view in (await c.call_view(old), await c.gate_view("gate")):
            view["unit"]["nested"]["version"] = 100
        tasks = await c.task_calls("task")
        tasks[0]["unit"]["nested"]["version"] = 100
        self.assertEqual([call["id"] for call in await c.task_calls("task")], [old, new])
        grouped = await c.task_calls_by_tasks(["task", "missing"])
        self.assertEqual([call["id"] for call in grouped["task"]], [old, new])
        self.assertEqual(grouped["missing"], [])
        grouped["task"][0]["unit"]["nested"]["version"] = 100
        self.assertEqual((await c.call_view(old))["unit"]["nested"]["version"], 1)
        self.assertIsNone(await c.call_view("missing"))
        self.assertIsNone(await c.gate_view("missing"))
        before = self.store.read_runtime(c.session_id)
        fresh = Coordinator(self.store, automatic=False)
        await fresh.register("gate", "new-owner")
        self.assertNotEqual(c.session_id, fresh.session_id)
        self.assertEqual(self.store.read_runtime(c.session_id), before)
        await self.assert_indexes()

    async def test_fifo_and_unknown_occupancy_follow_every_call_transition(self):
        c = self.c
        for number in range(2):
            await c.register(str(number), str(number), unit=dict(runtime_group="group"))
        first = await c.enable("0", "0")
        second = await c.enable("1", "1")
        self.assertEqual([call["id"] for call in c._ready()], [first, second])
        await c.reserve(second)
        await self.assert_indexes()
        permit = await c.authorize(second)
        await self.assert_indexes()
        await c.consume(second, permit, "1")
        await self.assert_indexes()
        await c.cancel(second)
        await self.assert_indexes()
        self.assertEqual(c.used(), 1)
        self.assertTrue(c.host_busy("local"))
        await c.finish(second, permit, "RETURN")
        await self.assert_indexes()
        await c.cancel(first)
        await self.assert_indexes()
        self.assertEqual(await c.group_call_counts(), {})
        self.assertEqual((await c.active_view())["calls"], {})

    async def root_and_call(self, run_id, suffix):
        c = self.c
        await c.configure_roots(dict(run_id=run_id, call_slots=1, boundaries=True, boundary_slots=1))
        host, gate, task = "host" + suffix, "gate" + suffix, "task" + suffix
        await c.register(gate, host, unit=dict(id=gate, instance_id=host, runtime_group="group"))
        root = await c.enable_root(task, gate, dict(kind="REMOTE", instance_id=host))
        await c.authorize_root(root, host)
        call = await c.enable(gate, host, task_id=task)
        permit = await c.authorize_boundary(call, run_id)
        await c.consume(call, permit, host)
        return root, call, permit, host

    async def test_window_indexes_avoid_history_scans_and_hold_retired_root_children(self):
        c = self.c
        old_root, old_call, old_permit, _ = await self.root_and_call("old", "0")
        await c.finish(old_call, old_permit, "RETURN")
        await c.finish_root(old_root, "RETURN")
        await c.configure_roots(None)
        root, call, permit, host = await self.root_and_call("current", "1")
        await c.finish_root(root, "RETURN")
        await self.assert_indexes()
        self.assertEqual(c.state["roots"][root]["state"], "RETIRED")
        self.assertTrue(c.host_busy(host))
        # Subsequent narrow queries and transitions may use identity lookup,
        # but must never enumerate either full retained ledger.
        c.state["calls"] = NoHistoryScan(c.state["calls"])
        c.state["roots"] = NoHistoryScan(c.state["roots"])
        self.assertEqual([r["id"] for r in await c.root_view("current")], [root])
        self.assertEqual([r["id"] for r in await c.retired_boundaries("old", set())], [old_call])
        self.assertEqual(await c.retired_boundaries("old", {old_call}), [])
        self.assertEqual(await c.retired_boundaries("current", set()), [])
        self.assertEqual([record["id"] for record in (await c.task_calls_by_tasks(["task1"]))["task1"]], [call])
        active = await c.active_view()
        active["calls"][call]["unit"]["id"] = "changed"
        self.assertEqual((await c.call_view(call))["unit"]["id"], "gate1")
        await c.configure_roots(None)
        self.assertIsNotNone(c.root_control)
        await c.finish(call, permit, "RETURN")
        self.assertIsNone(c.root_control)
        self.assertEqual(c.roots_used(), 0)
        self.assertEqual([r["id"] for r in await c.retired_boundaries("current", set())], [call])
        self.assertEqual((await c.active_view())["roots"], {})

    async def test_bulk_remote_invalidate_and_detach_persist_all_changed_ids(self):
        c = self.c
        for identity in ("running", "waiting", "reserved", "detached"):
            await c.register(identity, identity, unit=dict(source_id="source", runtime_group="group", instance_id="host"))
        running = await c.enable("running", "running")
        permit = await c.authorize(running)
        await c.consume(running, permit, "running")
        waiting = await c.enable("waiting", "waiting")
        reserved = await c.enable("reserved", "reserved")
        await c.reserve(reserved)
        await c.remote_unknown("group", "host")
        self.assertEqual(self.store.changes[-1], ("remote_outcome_unknown", ("running",),
                                                (running, waiting, reserved), ()))
        await self.assert_indexes()
        for call in (waiting, reserved):
            with self.assertRaises(ProtocolError):
                await c.wait(call)
        invalidated = await c.enable("waiting", "waiting")
        await c.invalidate("waiting")
        with self.assertRaises(ProtocolError):
            await c.wait(invalidated)
        detached = await c.enable("detached", "detached")
        detached_permit = await c.authorize(detached)
        await c.detach_source("source")
        await self.assert_indexes()
        self.assertEqual((await c.call_view(detached))["physical"], "RETIRED")
        self.assertEqual((await c.call_view(running))["physical"], "UNKNOWN")
        await c.finish(running, permit, "RETURN")
        await self.assert_indexes()
        self.assertEqual(c.used(), 0)
        with self.assertRaises(ProtocolError):
            await c.consume(detached, detached_permit, "detached")

    async def test_journal_failure_stops_resolved_permits_and_root_admission(self):
        c = self.c
        await c.register("gate", "owner")
        call = await c.enable("gate", "owner")
        permit = await c.authorize(call)
        with patch.object(self.store, "save_runtime", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                await c.label("gate", dict(id="label"))
        with self.assertRaises(ProtocolError):
            await c.wait(call)
        with self.assertRaises(ProtocolError):
            await c.consume(call, permit, "owner")
        with self.assertRaises(ProtocolError):
            await c.enable("gate", "owner")
        self.assertEqual(c.state["calls"][call]["physical"], "AUTHORIZED")
        self.assertEqual(c._ready(), [])

        other = Coordinator(self.store, automatic=False)
        await other.register("gate", "owner")
        await other.configure_roots(dict(run_id="window", call_slots=1))
        root = await other.enable_root("task", "unit", dict(kind="LOCAL"))
        await other.authorize_root(root, "local")
        with patch.object(self.store, "save_runtime", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                await other.label("gate", None)
        with self.assertRaises(ProtocolError):
            await other.wait_root(root)
        with self.assertRaises(ProtocolError):
            await other.enable_root("next", "unit", dict(kind="LOCAL"))

    async def test_journal_failure_wakes_already_pending_call_and_root_waiters(self):
        c = self.c
        await c.register("running", "running")
        running = await c.enable("running", "running")
        permit = await c.authorize(running)
        await c.consume(running, permit, "running")
        await c.register("waiting", "waiting")
        waiting = await c.enable("waiting", "waiting")
        await c.configure_roots(dict(run_id="window", call_slots=1))
        root = await c.enable_root("task", "unit", dict(kind="REMOTE", instance_id="host"))
        pending = [asyncio.create_task(c.wait(waiting)), asyncio.create_task(c.wait_root(root))]
        await asyncio.sleep(0)
        self.assertTrue(all(not task.done() for task in pending))
        with patch.object(self.store, "save_runtime", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                await c.label("waiting", dict(id="changed"))
        for task in pending:
            with self.assertRaisesRegex(ProtocolError, "journal unavailable"):
                await asyncio.wait_for(task, timeout=1)
        self.assertEqual(c.state["calls"][running]["physical"], "STARTED")
        self.assertEqual(c.state["calls"][waiting]["physical"], "ENABLED")
        self.assertEqual(c.state["roots"][root]["state"], "ENABLED")
        self.assertEqual(c.used(), 1)


if __name__ == "__main__":
    unittest.main()
