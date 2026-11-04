"""Task cost uses event time even when the scheduler processes a batch late."""
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from seampact.scheduling.joint import TaskRun, Window


class TaskCostClock(unittest.TestCase):
    def window(self, count=1, horizon=None):
        window = object.__new__(Window)
        window.tasks = [TaskRun(i, {"id": str(i)}, str(i), None, 0) for i in range(count)]
        window.start = window.clock = 0.0
        window.horizon_at = horizon
        window.done_at = window.closing = window.execution_error = None
        window.levels = (count, 0, 0, 0)
        window.ledger = dict(task_seconds=0.0, charged_task_seconds=0.0, instance_seconds=0.0,
                             root_seconds=0.0, unknown_root_seconds=0.0, parallel_seconds=0.0)
        window.config = dict(costs=dict(task_seconds=2.0, instance_seconds=0.0))
        window.trajectory = []
        window.unattributed = dict(task_time=0.0, instance_time=0.0)
        window._release_task = Mock()
        window.truncated = []
        return window

    def test_late_completion_corrects_prior_charge(self):
        window = self.window()
        task = window.tasks[0]
        task.arrived, task.status = 0.0, "RUNNING"
        window._advance(5.0)
        self.assertEqual(window.ledger["task_seconds"], 5.0)
        with patch("seampact.scheduling.joint.threading.Thread"):
            window._handle(("task_event", task, {"kind": "terminal"}, 1.0))
        window._charge_task_time(5.0)
        self.assertEqual(window.ledger["task_seconds"], 1.0)
        self.assertEqual(window.ledger["charged_task_seconds"], 1.0)
        self.assertEqual(window.unattributed["task_time"], -2.0)
        window._advance(7.0)
        self.assertEqual(window.unattributed["task_time"], -2.0)

    def test_late_arrival_includes_capacity_wait(self):
        window = self.window()
        window._advance(5.0)
        self.assertEqual(window.ledger["task_seconds"], 0.0)
        window.tasks[0].arrived = 1.0
        window.tasks[0].status = "WAITING_CAPACITY"
        window._charge_task_time(5.0)
        self.assertEqual(window.ledger["task_seconds"], 4.0)
        self.assertEqual(window.unattributed["task_time"], -8.0)

    def test_horizon_clips_reward_but_not_measured_completion(self):
        window = self.window(horizon=3.0)
        task = window.tasks[0]
        task.arrived, task.done, task.ended = 1.0, 8.0, 8.0
        window._advance(10.0)
        self.assertEqual(window.ledger["task_seconds"], 7.0)
        self.assertEqual(window.ledger["charged_task_seconds"], 2.0)
        self.assertEqual(window.unattributed["task_time"], -4.0)

    def test_failure_and_abandonment_use_event_timestamps(self):
        window = self.window(count=2)
        window.closing = "stopped"
        for task in window.tasks:
            task.arrived, task.status = 0.0, "RUNNING"
        window._advance(10.0)
        window._handle(("task_error", window.tasks[0], ValueError("fixture"), 2.0))
        window._handle(("task_abandoned", window.tasks[1], 4.0))
        window._charge_task_time(10.0)
        self.assertEqual(window.ledger["task_seconds"], 6.0)
        self.assertEqual(window.unattributed["task_time"], -12.0)

    def test_closed_capacity_wait_ends_at_horizon(self):
        window = self.window(horizon=3.0)
        task = window.tasks[0]
        task.arrived, task.status = 1.0, "WAITING_CAPACITY"
        with patch("seampact.scheduling.joint.time.time", return_value=8.0):
            window._close("horizon")
        window._advance(10.0)
        self.assertEqual(task.ended, 3.0)
        self.assertEqual(window.ledger["task_seconds"], 2.0)

    def test_partition_changes_neither_measured_nor_total_reward(self):
        for times in ([10.0], [1.0, 2.0, 3.0, 7.0, 10.0]):
            window = self.window(count=2)
            a, b = window.tasks
            a.arrived, a.done, a.ended = 0.0, 2.0, 2.0
            b.arrived = 3.0  # Still unresolved, censored at the reporting endpoint.
            for at in times:
                window._advance(at)
            self.assertEqual(window.ledger["task_seconds"], 9.0)
            self.assertEqual(window.unattributed["task_time"], -18.0)


if __name__ == "__main__":
    unittest.main()
