"""Focused SQLite checks for session-scoped incremental runtime evidence."""

import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from seampact.store import Store


class RuntimeStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = Store(Path(self.directory.name) / "state.sqlite3")
        self.addCleanup(self.store.close)

    def test_sessions_order_unit_dedup_and_historical_versions(self):
        unit = {"z": "large" * 1000, "nested": [{"b": 2, "a": 1}], "a": 1}
        reordered = {"a": 1, "nested": [{"a": 1, "b": 2}], "z": "large" * 1000}
        state = {"schema": "runtime/4", "quota": 1,
                 "gates": {"g1": {"id": "g1", "unit": unit}, "g2": {"id": "g2", "unit": reordered}},
                 "calls": {"c1": {"id": "c1", "unit": copy.deepcopy(unit)},
                           "c2": {"id": "c2", "unit": copy.deepcopy(unit)}},
                 "roots": {"r1": {"id": "r1", "state": "RETIRED"}}}
        self.store.save_runtime("one", state, gates=["g1", "g2"], calls=["c1", "c2"], roots=["r1"])
        self.store.save_runtime("two", state, gates=state["gates"], calls=state["calls"], roots=state["roots"])
        for session in ("one", "two"):
            self.assertEqual(json.dumps(self.store.read_runtime(session), ensure_ascii=False),
                             json.dumps(state, ensure_ascii=False))
        units = self.store.db.execute("SELECT COUNT(*) FROM documents WHERE kind='runtime_unit'").fetchone()[0]
        # Identical ordered representations share content; reordered keys are a
        # separate version so readback preserves each original order.
        self.assertEqual(units, 2)
        state["gates"]["g1"]["unit"] = {"new": 7}
        self.store.save_runtime("one", state, gates=["g1"])
        restored = self.store.read_runtime("one")
        self.assertEqual(restored["gates"]["g1"]["unit"], {"new": 7})
        self.assertEqual(restored["calls"]["c1"]["unit"], unit)
        self.assertEqual(self.store.read_runtime("two")["gates"]["g1"]["unit"], unit)
        self.assertIsNone(self.store.read_runtime("missing"))

    def test_unordered_changed_ids_are_rejected_before_writing(self):
        state = {"schema": "runtime/4", "quota": 1,
                 "gates": {"g": {"id": "g"}}, "calls": {}, "roots": {}}
        with self.assertRaises(TypeError):
            self.store.save_runtime("one", state, gates={"g"})
        self.assertIsNone(self.store.read_runtime("one"))

    def test_single_change_writes_only_meta_and_changed_record(self):
        unit = {"blob": "x" * 100000}
        state = {"schema": "runtime/4", "quota": 1, "gates": {},
                 "calls": {str(i): {"id": str(i), "unit": unit, "physical": "ENABLED"}
                           for i in range(100)}, "roots": {}}
        self.store.save_runtime("one", state, calls=state["calls"])
        before = self.store.db.total_changes
        state["calls"]["42"]["physical"] = "STARTED"
        self.store.save_runtime("one", state, calls=["42"])
        self.assertEqual(self.store.db.total_changes - before, 2)
        self.assertEqual(self.store.read_runtime("one"), state)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM documents WHERE kind='runtime_unit'").fetchone()[0], 1)

    def test_failed_transaction_rolls_back_meta_unit_and_records(self):
        state = {"schema": "runtime/4", "quota": 1, "gates": {}, "calls": {}, "roots": {}}
        self.store.save_runtime("one", state)
        before = self.store.read_runtime("one")
        with self.store.db:
            self.store.db.execute("""CREATE TRIGGER reject_runtime_call BEFORE INSERT ON documents
                WHEN NEW.kind = 'runtime_calls:one' BEGIN SELECT RAISE(ABORT, 'injected failure'); END""")
        state["quota"] = 2
        state["calls"]["c"] = {"id": "c", "unit": {"never": "committed"}}
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.save_runtime("one", state, calls=["c"])
        self.assertEqual(self.store.read_runtime("one"), before)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM documents WHERE kind='runtime_unit'").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
