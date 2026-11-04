"""Closed joint windows retain real work; stale dispatch choices are rechecked."""
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from seampact.runtime import Coordinator, OwnerRuntime, ProtocolError
from seampact.scheduling.joint import Window
from seampact.store import Store


class RootRetirementTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = Store(":memory:")
        self.coordinator = Coordinator(self.store, quota=2)
        await self.coordinator.configure_roots(dict(run_id="window", call_slots=2,
                                                    boundaries=True, boundary_slots=1))

    async def asyncTearDown(self):
        self.store.close()

    async def root(self, number):
        host, task, gate = "host" + str(number), "task" + str(number), "gate" + str(number)
        await self.coordinator.register(gate, host, unit=dict(id=gate, instance_id=host))
        identity = await self.coordinator.enable_root(task, gate, dict(kind="REMOTE", instance_id=host))
        await self.coordinator.authorize_root(identity, host)
        return identity, gate, host, task

    async def test_close_withdraws_unstarted_work_without_automatically_dispatching_it(self):
        c = self.coordinator
        roots, calls = [], []
        for number in range(2):
            root, gate, host, task = await self.root(number)
            roots.append(root)
            calls.append(await c.enable(gate, host, task_id=task))
        await c.reserve(calls[0])
        pending = await c.enable_root("pending", "gate0", dict(kind="REMOTE", instance_id="host2"))

        await c.configure_roots(None)
        self.assertTrue(c.root_control["closing"])
        self.assertEqual(c.roots_used(), 2)
        self.assertEqual(c.snapshot()["call_slots"], 2)
        self.assertEqual(c._boundaries_used(), 0)
        for call in calls:
            self.assertEqual(c.state["calls"][call]["physical"], "RETIRED")
            self.assertEqual(c.state["calls"][call]["starts"], 0)
            with self.assertRaisesRegex(ProtocolError, "closed"):
                await c.wait(call)
        with self.assertRaisesRegex(ProtocolError, "closed"):
            await c.wait_root(pending)
        with self.assertRaises(ProtocolError):
            await c.enable("gate0", "host0", task_id="task0")
        with self.assertRaises(ProtocolError):
            await c.enable_root("new", "gate0", dict(kind="REMOTE", instance_id="host2"))
        self.assertEqual(await c.boundary_view("window"), [])
        await c.pump()
        self.assertTrue(all(c.state["calls"][call]["starts"] == 0 for call in calls))

        for root in roots:
            await c.finish_root(root, "ProtocolError")
        self.assertEqual(c.roots_used(), 0)
        self.assertIsNone(c.root_control)
        await c.configure_roots(None)
        await c.configure_roots(dict(run_id="next", call_slots=1, boundary_slots=1))
        new = await c.enable_root("new", "unit", dict(kind="REMOTE", instance_id="host2"))
        await c.authorize_root(new, "host2")
        await c.finish_root(new, "RETURN")
        await c.configure_roots(None)
        self.assertIsNone(c.root_control)

    async def test_close_fences_an_authorized_but_unconsumed_permit(self):
        c = self.coordinator
        root, gate, host, task = await self.root(0)
        call = await c.enable(gate, host, task_id=task)
        permit = await c.authorize_boundary(call, "window")
        self.assertEqual(await c.wait(call), permit)
        await c.configure_roots(None)
        self.assertEqual(c.state["calls"][call]["physical"], "RETIRED")
        with self.assertRaises(ProtocolError):
            await c.consume(call, permit, host)
        self.assertEqual(c.state["calls"][call]["starts"], 0)
        self.assertEqual(c.roots_used(), 1)
        await c.finish_root(root, "ProtocolError")
        self.assertIsNone(c.root_control)

    async def held_call_survives_close(self, unknown):
        c = self.coordinator
        root, gate, host, task = await self.root(0)
        call = await c.enable(gate, host, task_id=task)
        permit = await c.authorize_boundary(call, "window")
        await c.consume(call, permit, host)
        if unknown:
            await c.cancel(call)
        phase = "UNKNOWN" if unknown else "STARTED"
        await c.configure_roots(None)
        await c.configure_roots(None)
        self.assertEqual(c.state["calls"][call]["physical"], phase)
        self.assertEqual(c.state["calls"][call]["starts"], 1)
        self.assertEqual(c.roots_used(), 1)
        self.assertEqual(c._boundaries_used(), 1)
        self.assertTrue(c.host_busy(host))
        with self.assertRaisesRegex(ProtocolError, "owns"):
            await c.configure_roots(dict(run_id="next", call_slots=2, boundary_slots=2))
        await c.finish(call, permit, "CancelledError" if unknown else "RETURN")
        self.assertEqual(c._boundaries_used(), 0)
        self.assertEqual(c.roots_used(), 1)
        await c.finish_root(root, "RETURN")
        self.assertIsNone(c.root_control)
        self.assertEqual(c.roots_used(), 0)
        await c.configure_roots(dict(run_id="next", call_slots=1, boundary_slots=1))

    async def test_running_call_keeps_its_reservation_and_allows_reconfigure_only_after_retirement(self):
        await self.held_call_survives_close(unknown=False)

    async def test_unknown_call_keeps_its_reservation_until_a_real_completion(self):
        await self.held_call_survives_close(unknown=True)

    async def test_root_receipt_does_not_discard_an_inner_reservation(self):
        c = self.coordinator
        root, gate, host, task = await self.root(0)
        call = await c.enable(gate, host, task_id=task)
        permit = await c.authorize_boundary(call, "window")
        await c.consume(call, permit, host)
        await c.configure_roots(None)
        await c.finish_root(root, "RETURN")
        self.assertEqual(c.roots_used(), 1)
        self.assertTrue(c.host_busy(host))
        self.assertIsNotNone(c.root_control)
        with self.assertRaises(ProtocolError):
            await c.enable(gate, host, task_id=task)
        await c.finish(call, permit, "RETURN")
        self.assertEqual(c.roots_used(), 0)
        self.assertIsNone(c.root_control)

    async def test_unknown_root_blocks_reconfiguration_even_without_an_inner_call(self):
        c = self.coordinator
        root, gate, host, task = await self.root(0)
        await c.remote_unknown("group", host)
        await c.finish_root(root, "ProtocolError")
        await c.configure_roots(None)
        self.assertEqual(c.state["roots"][root]["state"], "UNKNOWN")
        self.assertEqual(c.roots_used(), 1)
        with self.assertRaises(ProtocolError):
            await c.enable(gate, host, task_id=task)
        with self.assertRaises(ProtocolError):
            await c.configure_roots(dict(run_id="next", call_slots=2))

    async def test_contract_rejection_only_withdraws_unstarted_call(self):
        c = self.coordinator
        root, gate, host, task = await self.root(0)
        call = await c.enable(gate, host, task_id=task)
        self.assertTrue(await c.cancel_enabled(call))
        self.assertEqual(c.state["calls"][call]["physical"], "RETIRED")
        with self.assertRaises(ProtocolError):
            await c.wait(call)
        await c.finish_root(root, "cancelled")
        root, gate, host, task = await self.root(1)
        call = await c.enable(gate, host, task_id=task)
        permit = await c.authorize_boundary(call, "window")
        self.assertFalse(await c.cancel_enabled(call))
        self.assertEqual(c.state["calls"][call]["physical"], "AUTHORIZED")
        await c.consume(call, permit, host)
        await c.finish(call, permit, "RETURN")
        await c.finish_root(root, "RETURN")


class BoundaryRecheckTests(unittest.TestCase):
    def test_close_before_owner_join_withdraws_a_real_waiting_callable(self):
        store = Store(":memory:")
        runtime = OwnerRuntime(Coordinator(store, quota=1))
        enabled, ran, errors = threading.Event(), [], []
        try:
            runtime.call("configure_roots", dict(run_id="window", call_slots=1, boundaries=True,
                                                boundary_slots=1, notify=enabled.set))
            runtime.call("register", "gate", "owner", unit=dict(id="unit", instance_id="host"))
            root = runtime.call("enable_root", "task", "unit", dict(kind="REMOTE", instance_id="host"))
            runtime.call("authorize_root", root, "host")
            original = runtime.wrap(lambda: ran.append(True), "gate", "owner")
            enabled.clear()

            def owner():
                try:
                    with runtime.context("task"):
                        original()
                except ProtocolError as error:
                    errors.append(error)
                finally:
                    runtime.call("finish_root", root, "RETURN" if ran else "ProtocolError")

            thread = threading.Thread(target=owner, daemon=True)
            thread.start()
            self.assertTrue(enabled.wait(2), "owner never reached the controlled boundary")
            runtime.call("configure_roots", None)
            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(ran, [])
            self.assertEqual(len(errors), 1)
            self.assertEqual(runtime.snapshot()["roots_used"], 0)
            runtime.call("configure_roots", dict(run_id="next", call_slots=1))
        finally:
            runtime.close()
            store.close()

    def test_cancel_or_invalidate_during_scoring_does_not_close_the_window(self):
        for change in ("cancel", "invalidate"):
            with self.subTest(change=change):
                store = Store(":memory:")
                runtime = OwnerRuntime(Coordinator(store, quota=2))
                try:
                    runtime.call("configure_roots", dict(run_id="window", call_slots=2,
                                                        boundaries=True, boundary_slots=1))
                    calls, contexts = [], {}
                    for number in range(2):
                        host, task, gate = "host" + str(number), "task" + str(number), "gate" + str(number)
                        runtime.call("register", gate, host, unit=dict(id=gate, instance_id=host))
                        root = runtime.call("enable_root", task, gate, dict(kind="REMOTE", instance_id=host))
                        runtime.call("authorize_root", root, host)
                        calls.append(runtime.call("enable", gate, host, task_id=task))
                        contexts[task] = SimpleNamespace(task=dict(id=task))
                    window = Window.__new__(Window)
                    window.runtime, window.wid, window.by_context = runtime, "window", contexts
                    window.catalog = SimpleNamespace(require_boundary=lambda uid, call, root, gate: {
                        "contract_digest": "fixture", "scope": "test-registered-gate", "boundary_id": gate["id"]})
                    window.config = dict(dispatch_scope="roots_and_boundaries", age_guard_ms=10**15)
                    window._state = lambda: ({}, runtime.call("root_view", "window"))
                    records = []
                    window._record = lambda **record: records.append(record)
                    changed = False

                    def choose(*args):
                        nonlocal changed
                        if not changed:
                            changed = True
                            runtime.call(change, calls[0] if change == "cancel" else "gate0")
                        return 0, "test", 0.0, 0.0

                    window._choose = choose
                    window._boundary_stage()
                    self.assertEqual([r["choice"] for r in records], ["recheck_refused", "test"])
                    self.assertEqual(runtime.snapshot()["calls"][calls[0]]["starts"], 0)
                    self.assertEqual(runtime.snapshot()["calls"][calls[1]]["physical"], "AUTHORIZED")
                    if change == "invalidate":
                        with self.assertRaises(ProtocolError):
                            runtime.call("wait", calls[0])
                finally:
                    runtime.close()
                    store.close()


if __name__ == "__main__":
    unittest.main()
