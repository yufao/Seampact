"""Window resource accounting, explicit scope selection and real host counters."""

import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from seampact import resource_observation as observation
from seampact import resource_process


class WindowResourceChecks(unittest.TestCase):
    def test_configuration_rejects_implicit_selection_and_invalid_values(self):
        self.assertIsNone(observation.ObservationConfig.from_mapping(None))
        for config in ({"gpu_uuids": ["0"]}, {"interval_s": float("nan")},
                       {"interval_s": True}, {"interval_s": 0.01},
                       {"process_ids": [True]}, {"process_ids": [1.5]},
                       {"process_ids": [-1]}, {"include_current_process": "yes"},
                       {"cgroup_paths": ["relative"]}, {"discover_children": True},
                       {"gpu_uuids": "GPU-a"}):
            with self.subTest(keys=list(config)), self.assertRaises(ValueError):
                observation.ObservationConfig.from_mapping(config)
        result = observation.ObservationConfig.from_mapping({"gpu_uuids": ["GPU-a", "GPU-a"],
                                                             "process_ids": [10, 10]})
        self.assertEqual(result.gpu_uuids, ("GPU-a",))
        self.assertEqual(result.process_ids, (10,))

    def test_absent_configuration_has_no_queries_or_raw_files(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(observation, "sample") as gpu, \
             patch.object(observation, "process_sample") as process:
            observer = observation.WindowObserver(None, run_id="r", arm="a", runtime_dir=directory)
            observer.start()
            result = observer.finish()
            self.assertEqual(result["status"], "NOT_CONFIGURED")
            self.assertEqual(list(Path(directory).iterdir()), [])
            gpu.assert_not_called()
            process.assert_not_called()

    def test_cpu_edges_reject_missing_identity_reuse_reset_and_large_gaps(self):
        rows = []
        # Only the first two identity-A samples are a valid CPU/memory edge.
        for stamp, identity, cpu, memory in [(0, "A", 1, 10), (1, "A", 2, 30),
                (2, None, None, None), (3, "A", 9, 50), (4, "B", 1, 60),
                (5, "B", 0, None), (20, "B", 4, 100)]:
            rows.append({"monotonic_s": stamp, "identity": identity,
                         "cpu_seconds": cpu, "rss_bytes": memory})
        result = resource_process.counter_summary(rows, 20, 2.5)
        self.assertEqual(result["cpu_seconds_delta"], 1)
        self.assertEqual(result["cpu_valid_time_s"], 1)
        self.assertEqual(result["rss_byte_s"], 20)
        self.assertEqual(result["peak_sampled_rss_bytes"], 100)
        self.assertTrue(result["identity_changed"])
        self.assertEqual(result["cpu_time_coverage"], 0.05)
        unknown = resource_process.counter_summary([], 10, 2.5)
        self.assertIsNone(unknown["cpu_seconds_delta"])
        self.assertIsNone(unknown["rss_byte_s"])
        self.assertEqual(unknown["cpu_time_coverage"], 0)

    def test_dynamic_pids_explicit_only_and_deselection_is_unknown(self):
        selected = [81]
        def process(pid):
            return {"pid": pid, "scope": "process", "identity": "birth",
                    "monotonic_s": time.monotonic(), "cpu_seconds": 1, "rss_bytes": 8}
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(observation, "process_sample", side_effect=process) as query, \
             patch.object(observation, "sample") as gpu:
            observer = observation.WindowObserver({"interval_s": 10}, run_id="r", arm="a",
                runtime_dir=Path(directory) / "runtime", pid_resolver=lambda: selected)
            observer.start()
            selected.clear()
            observer.finish()
            self.assertEqual([call.args[0] for call in query.call_args_list], [81])
            gpu.assert_not_called()
            lines = [json.loads(line) for line in observer.raw_path.read_text().splitlines()]
            final_sample = [line for line in lines if line["type"] == "sample"][-1]
            self.assertEqual(final_sample["processes"][0]["reason"], "NO_LONGER_SELECTED")
            self.assertIsNone(observer.finish()["processes"][0]["cpu_seconds_delta"])

    def test_real_current_process_cpu_and_rss_raw_summary_and_lifecycle(self):
        if not (sys.platform.startswith("linux") or sys.platform == "win32"):
            self.skipTest("native process backend requires Windows or Linux")
        with tempfile.TemporaryDirectory() as directory, patch.object(observation, "query") as gpu:
            config = observation.ObservationConfig(interval_s=0.1, include_current_process=True)
            with observation.WindowObserver(config, run_id="real-own-cpu", arm="check",
                    runtime_dir=Path(directory) / "runtime") as observer:
                start_cpu = time.process_time()
                value = 1
                while time.process_time() - start_cpu < 0.12:
                    value = (value * 1103515245 + 12345) % 2**31
            result = observer.finish()
            gpu.assert_not_called()
            self.assertEqual(result["status"], "COMPLETED")
            self.assertEqual(result["window_boundary"], "PREPARATION_THROUGH_RELEASE")
            self.assertEqual(len(result["processes"]), 1)
            measured = result["processes"][0]
            self.assertEqual(measured["pid"], os.getpid())
            self.assertGreater(measured["cpu_seconds_delta"], 0)
            self.assertGreater(measured["peak_sampled_rss_bytes"], 0)
            self.assertGreater(measured["rss_byte_s"], 0)
            self.assertGreater(measured["cpu_time_coverage"], 0)
            self.assertLessEqual(measured["cpu_time_coverage"], 1)
            lines = [json.loads(line) for line in observer.raw_path.read_text().splitlines()]
            self.assertEqual(lines[0]["type"], "header")
            self.assertEqual(lines[-1]["type"], "summary")
            self.assertEqual(json.loads(observer.summary_path.read_text()), result)
            self.assertIs(observer.finish(), result)
            self.assertFalse(observer._thread.is_alive())

    def test_cgroup_scope_memory_not_rss_or_process_total(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "cpu.stat").write_text("usage_usec 1200000\nuser_usec 1000000\n")
            (path / "memory.current").write_text("200\n")
            with patch.object(resource_process.sys, "platform", "linux"):
                first = resource_process.cgroup_sample(path, 0)
                (path / "cpu.stat").write_text("usage_usec 1500000\n")
                (path / "memory.current").write_text("400\n")
                second = resource_process.cgroup_sample(path, 0)
            first["monotonic_s"], second["monotonic_s"] = 0, 1
            result = resource_process.counter_summary([first, second], 1, 2.5, cgroup=True)
            self.assertAlmostEqual(result["cpu_seconds_delta"], 0.3)
            self.assertEqual(result["memory_byte_s"], 300)
            self.assertEqual(result["peak_sampled_memory_bytes"], 400)
            self.assertNotIn("rss_byte_s", result)
            self.assertNotIn("rss_bytes", first)
            (path / "memory.current").unlink()
            with patch.object(resource_process.sys, "platform", "linux"):
                partial = resource_process.cgroup_sample(path, 0)
            self.assertIsNone(partial["memory_current_bytes"])
            self.assertIsNotNone(partial["cpu_seconds"])

    def test_phase_boundaries_survive_raw_and_summary_without_extra_sampling(self):
        with tempfile.TemporaryDirectory() as directory:
            observer = observation.WindowObserver({"interval_s": 10}, run_id="r", arm="a",
                runtime_dir=Path(directory) / "runtime")
            with patch.object(observer, "_capture", wraps=observer._capture) as capture:
                observer.start()
                marks = [observer.mark_phase(name) for name in
                         ("preparation", "workload", "release", "evaluation_tail")]
                self.assertEqual(capture.call_count, 1)
                result = observer.finish()
            self.assertEqual(result["phases"], marks)
            self.assertTrue(all(mark["elapsed_s"] >= 0 for mark in marks))
            lines = [json.loads(line) for line in observer.raw_path.read_text().splitlines()]
            self.assertEqual([line for line in lines if line["type"] == "phase"], marks)
            self.assertEqual([line for line in lines if line["type"] == "sample"][-1]["phase"],
                             "evaluation_tail")
            with self.assertRaises(RuntimeError):
                observer.mark_phase("late")

    def test_final_capture_failure_preserves_history_and_idempotent_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            observer = observation.WindowObserver({"interval_s": 10}, run_id="r", arm="a",
                runtime_dir=Path(directory) / "runtime").start()
            with patch.object(observer, "_capture", side_effect=RuntimeError("private detail")):
                result = observer.finish("FAILED")
                self.assertIs(observer.finish(), result)
            self.assertEqual(result["sample_count"], 1)
            self.assertEqual(result["sampling_error"], "FINAL_SAMPLING_FAILED")
            self.assertEqual(result["status"], "FAILED")
            lines = [json.loads(line) for line in observer.raw_path.read_text().splitlines()]
            self.assertEqual([line["type"] for line in lines], ["header", "sample", "summary"])
            self.assertNotIn("private detail", observer.raw_path.read_text())
            self.assertEqual(json.loads(observer.summary_path.read_text()), result)

    def test_stuck_worker_does_not_block_finish_or_append_after_summary(self):
        entered, release = threading.Event(), threading.Event()
        calls = 0
        def resolver():
            nonlocal calls
            calls += 1
            if calls > 1:
                entered.set()
                release.wait(5)
            return []
        with tempfile.TemporaryDirectory() as directory:
            observer = observation.WindowObserver({"interval_s": 0.1}, run_id="r", arm="a",
                runtime_dir=Path(directory) / "runtime", pid_resolver=resolver).start()
            try:
                self.assertTrue(entered.wait(2))
                started = time.monotonic()
                result = observer.finish()
                self.assertLess(time.monotonic() - started, 3)
                self.assertEqual(result["sampling_error"], "SAMPLING_STOP_TIMEOUT")
                original = observer.raw_path.read_bytes()
            finally:
                release.set()
                observer._thread.join(timeout=2)
            self.assertFalse(observer._thread.is_alive())
            self.assertEqual(observer.raw_path.read_bytes(), original)
            self.assertIs(observer.finish(), result)

    def test_exception_window_is_closed_and_failed(self):
        with tempfile.TemporaryDirectory() as directory:
            observer = observation.WindowObserver({}, run_id="r", arm="a",
                runtime_dir=Path(directory) / "runtime")
            with self.assertRaisesRegex(RuntimeError, "work failed"):
                with observer:
                    raise RuntimeError("work failed")
            self.assertEqual(observer.finish()["status"], "FAILED")
            self.assertTrue(observer._stream.closed)


if __name__ == "__main__":
    unittest.main()
