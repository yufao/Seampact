"""Core admission and actual coordinator execution checks, not research results."""
import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from seampact.scheduling.residency import WorkingSets, freeze_working_sets
from seampact.runtime import Coordinator, ProtocolError
from seampact.store import Store


class ResidencyTests(unittest.TestCase):
    def spec(self, reclaimable=("a", "b"), retention="idle_retire/1", initial=()):
        scene = dict(unit_ids=["ua", "ub"])
        tasks = [dict(id="ta", unit_ids=["ua"]), dict(id="tb", unit_ids=["ub"])]
        return freeze_working_sets(scene, tasks,
            {"ua": dict(kind="REMOTE", group_id="a"), "ub": dict(kind="REMOTE", group_id="b")},
            {"memory": 8}, {"a": {"memory": 6}, "b": {"memory": 6}}, reclaimable, retention,
            initial_allocations=initial)

    def test_pool_larger_than_capacity_preserves_waiting_task(self):
        w = WorkingSets(self.spec(), {"memory": 8}, {"a": {"memory": 6}, "b": {"memory": 6}})
        self.assertTrue(w.admit("1", "ta", units=["ua"]))
        self.assertFalse(w.admit("2", "tb", units=["ub"]))
        w.release("1")
        self.assertTrue(w.admit("2", "tb", units=["ub"]))
        self.assertLess(w.after_start("b", {"a": 1, "b": 0}), 0)
        self.assertGreaterEqual(w.after_start("b", {"a": 0, "b": 0}), 0)

    def test_shared_working_set_counted_once_and_replicas_counted(self):
        w = WorkingSets(self.spec(), {"memory": 8}, {"a": {"memory": 6}, "b": {"memory": 6}})
        self.assertTrue(w.admit("1", "ta", units=["ua"]))
        self.assertTrue(w.admit("2", "ta", units=["ua"]))
        self.assertEqual(w.used({"a": 1, "b": 0}), {"memory": 6})
        self.assertLess(w.after_start("a", {"a": 1, "b": 0}), 0)

    def test_cold_pool_is_not_pinned_but_actual_unknown_and_fixed_residency_still_count(self):
        for retention in ("idle_retire/1", "fixed_resident"):
            with self.subTest(retention=retention):
                spec = self.spec(reclaimable=(), retention=retention)
                self.assertEqual(spec["pinned"], [])
                working = WorkingSets(spec, {"memory": 8}, {"a": {"memory": 6}, "b": {"memory": 6}})
                self.assertTrue(working.admit("owner", "ta", units=["ua"]))
                working.release("owner")
                # Returned logical claims do not release a physical/unknown
                # allocation: the next cold choice cannot borrow its bytes.
                self.assertFalse(working.admit("next", "tb", {"a": 1}, units=["ub"]))
                self.assertEqual(working.used({"a": 1}), {"memory": 6})
                self.assertEqual(self.spec(reclaimable=(), retention=retention, initial=["a"])["pinned"], ["a"])
                with self.assertRaisesRegex(ValueError, "工作集"):
                    self.spec(reclaimable=(), retention=retention, initial=["a", "b"])

    def test_optional_union_does_not_reserve_and_selected_closure_is_complete(self):
        demands = {g: {"memory": 1} for g in ("a", "b", "dependency")}
        spec = freeze_working_sets(dict(unit_ids=["ua", "ub"]), [dict(id="t")],
            {"ua": dict(kind="REMOTE", group_id="a", resource_dependencies=["dependency"]),
             "ub": dict(kind="REMOTE", group_id="b")}, {"memory": 2}, demands, list(demands), "idle_retire/1")
        working = WorkingSets(spec, {"memory": 2}, demands)
        self.assertTrue(working.admit("owner", "t"))
        self.assertEqual(working.used({}), {"memory": 0})
        self.assertTrue(working.admit("owner", "t", units=["ua"]))
        self.assertEqual(working.claims["owner"], {"a", "dependency"})
        self.assertFalse(working.admit("owner", "t", units=["ub"]))
        self.assertEqual(working.claims["owner"], {"a", "dependency"})
        working.release("owner")
        self.assertEqual(working.used({"a": 1, "dependency": 1}), {"memory": 2})


class BoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = Store(":memory:")
        self.c = Coordinator(self.store, quota=2)
        await self.c.configure_roots(dict(run_id="window", call_slots=2, boundaries=True, boundary_slots=1))
        self.roots = []
        for number in range(2):
            host, task, gate = "host" + str(number), "task" + str(number), "gate" + str(number)
            await self.c.register(gate, host, unit=dict(id=gate, instance_id=host))
            root = await self.c.enable_root(task, gate, dict(kind="REMOTE", instance_id=host))
            await self.c.authorize_root(root, host)
            self.roots.append(root)

    async def test_two_hosts_expose_real_boundary_choices_without_preemption(self):
        a = await self.c.enable("gate0", "host0", task_id="task0")
        b = await self.c.enable("gate1", "host1", task_id="task1")
        self.assertEqual({c["id"] for c in await self.c.boundary_view("window")}, {a, b})
        self.assertEqual(self.c.state["calls"][a]["physical"], "ENABLED")
        permit = await self.c.authorize_boundary(b, "window")
        await self.c.consume(b, permit, "host1")
        self.assertEqual(await self.c.boundary_view("window"), [])
        with self.assertRaises(ProtocolError):
            await self.c.authorize_boundary(a, "window")
        await self.c.finish(b, permit, "RETURN")
        self.assertEqual([c["id"] for c in await self.c.boundary_view("window")], [a])
        permit_a = await self.c.authorize_boundary(a, "window")
        await self.c.consume(a, permit_a, "host0")
        await self.c.finish(a, permit_a, "RETURN")
        for root in self.roots:
            await self.c.finish_root(root, "RETURN")
        self.assertEqual(self.c.roots_used(), 0)

    async def test_unknown_keeps_slots_and_same_host_root_is_not_interleaved(self):
        call = await self.c.enable("gate0", "host0", task_id="task0")
        permit = await self.c.authorize_boundary(call, "window")
        await self.c.consume(call, permit, "host0")
        await self.c.cancel(call)
        other = await self.c.enable("gate1", "host1", task_id="task1")
        self.assertEqual(await self.c.boundary_view("window"), [])
        root = await self.c.enable_root("task3", "gate0", dict(kind="REMOTE", instance_id="host0"))
        with self.assertRaises(ProtocolError):
            await self.c.authorize_root(root, "host0")
        await self.c.cancel_root(root, "test done")
        # Observe cancelled waiter to avoid an unhandled Future warning.
        with self.assertRaises(ProtocolError):
            await self.c.wait_root(root)
        await self.c.cancel(other)
        await self.c.finish(call, permit, "CancelledError")


if __name__ == "__main__":
    unittest.main()
