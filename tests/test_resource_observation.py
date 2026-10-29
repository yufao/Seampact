"""Read-only device observation: unknown values, isolation, and saved evidence."""

import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from seampact import resource_observation as observation


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, duration):
        self.now += duration


class ResourceObservationChecks(unittest.TestCase):
    def test_csv_unknown_values_and_uuid_isolation(self):
        device_csv = "GPU-a, Example A, 100, N/A, , 0\nGPU-b, Example B, 200, 50, 20, 10\nGPU-secret, Private, 1, 1, 1, 1\n"
        apps_csv = "GPU-a, 11, N/A\nGPU-a, 12, 8\nGPU-b, N/A, N/A\nGPU-secret, 99, 1\n"
        with patch.object(observation.subprocess, "run", side_effect=[
            subprocess.CompletedProcess([], 0, device_csv),
            subprocess.CompletedProcess([], 0, apps_csv),
        ]) as run:
            item = observation.sample(["GPU-a", "GPU-b", "GPU-missing"], [11])
        self.assertEqual([d["gpu_uuid"] for d in item["devices"]], ["GPU-a", "GPU-b", "GPU-missing"])
        first, second, missing = item["devices"]
        self.assertIsNone(first["memory_used_mib"])
        self.assertIsNone(first["utilization_gpu_percent"])
        self.assertEqual(first["power_draw_w"], 0)
        self.assertEqual(second["memory_used_mib"], 50)
        self.assertEqual(missing["status"], "UNKNOWN")
        self.assertIsNone(missing["memory_total_mib"])
        self.assertEqual([p["pid_relation"] for p in first["process_observation"]["processes"]],
                         ["observed", "unexpected"])
        self.assertEqual(second["process_observation"]["processes"][0]["pid_relation"], "unknown")
        self.assertNotIn("GPU-secret", json.dumps(item))
        for call in run.call_args_list:
            self.assertIn("--id=GPU-a,GPU-b,GPU-missing", call.args[0])
            self.assertFalse(call.kwargs.get("shell", False))
        self.assertIn("--query-compute-apps=gpu_uuid,pid,used_gpu_memory", run.call_args_list[1].args[0])
        self.assertEqual(first["scope"], "device")
        self.assertIn("utc", first["process_observation"])

    def test_command_failure_and_unavailable_stay_unknown(self):
        for failed in (subprocess.CompletedProcess([], 1, "", "private machine detail"),
                       OSError("private path"), subprocess.TimeoutExpired("nvidia-smi", 10)):
            with self.subTest(failure=type(failed).__name__):
                with patch.object(observation.subprocess, "run") as run:
                    if isinstance(failed, Exception):
                        run.side_effect = failed
                    else:
                        run.return_value = failed
                    item = observation.sample(["GPU-a"], [11])
                device = item["devices"][0]
                self.assertTrue(all(device[field] is None for field in observation.METRICS))
                self.assertEqual(device["process_observation"]["status"], "UNKNOWN")
                self.assertEqual(device["process_observation"]["expected_pids"][0]["status"], "unknown")
                self.assertNotIn("private", json.dumps(item))

    def test_partial_csv_and_empty_process_list_never_claim_exclusivity(self):
        with patch.object(observation.subprocess, "run", side_effect=[
            subprocess.CompletedProcess([], 0, "GPU-a, broken\n"),
            subprocess.CompletedProcess([], 0, ""),
        ]):
            item = observation.sample(["GPU-a"], [11])
        device = item["devices"][0]
        self.assertEqual(device["status"], "UNKNOWN")
        self.assertEqual(device["process_observation"]["status"], "OK")
        self.assertEqual(device["process_observation"]["processes"], [])
        self.assertEqual(device["process_observation"]["expected_pids"][0]["status"], "unknown")

    def test_integrals_are_device_scoped_and_do_not_bridge_missing_values_or_gaps(self):
        samples = []
        # Valid first edge integrates 10 -> 30 MiB and 20 -> 40 W over 2 s.
        for stamp, memory, power in [(0, 10, 20), (2, 30, 40), (4, None, 60),
                                     (6, 50, None), (20, 90, 100)]:
            samples.append({"devices": [{"gpu_uuid": "GPU-a", "monotonic_s": stamp,
                "memory_total_mib": 100, "memory_used_mib": memory,
                "utilization_gpu_percent": None, "power_draw_w": power}]})
        result = observation.summarize(samples, ["GPU-a", "GPU-missing"], 0, 20, 2, "COMPLETED")
        first, missing = result["devices"]
        self.assertEqual(first["peak_sampled_memory_mib"], 90)
        self.assertEqual(first["memory_mib_s"], 40)
        self.assertEqual(first["device_energy_j_estimate"], 160)
        self.assertEqual(first["metrics"]["memory_used_mib"]["valid_time_coverage"], 0.1)
        self.assertEqual(first["metrics"]["power_draw_w"]["valid_time_coverage"], 0.2)
        self.assertEqual(first["metrics"]["utilization_gpu_percent"]["valid_time_coverage"], 0)
        self.assertIsNone(missing["memory_mib_s"])
        self.assertIsNone(missing["peak_sampled_memory_mib"])
        self.assertEqual(first["attribution"], "UNVERIFIED_SHARED_DEVICE")

    def test_cli_writes_header_samples_summary_and_refuses_overwrite(self):
        clock = Clock()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "observations.jsonl"
            args = ["--run-id", "run-1", "--arm", "baseline", "--gpu-uuid", "GPU-a",
                    "--duration-s", "2", "--interval-s", "1", "--output", str(output)]
            def query_result(command, **kwargs):
                raw = "GPU-a, Example, 100, 10, 50, 20\n" if "--query-gpu=" in " ".join(command) else ""
                return subprocess.CompletedProcess(command, 0, raw)
            console = io.StringIO()
            with patch.object(observation.time, "monotonic", clock.monotonic), \
                 patch.object(observation.time, "sleep", clock.sleep), \
                 patch.object(observation.subprocess, "run", side_effect=query_result) as run, \
                 contextlib.redirect_stdout(console):
                self.assertEqual(observation.main(args), 0)
                original = output.read_bytes()
                self.assertEqual(observation.main(args), 2)
                self.assertEqual(output.read_bytes(), original)
            items = [json.loads(line) for line in original.decode().splitlines()]
            self.assertEqual([item["type"] for item in items], ["header", "sample", "sample", "sample", "summary"])
            self.assertEqual(items[0]["pairing"], "MANUAL_RUN_ID_NO_AUTOMATIC_TRAINING_WINDOW")
            self.assertEqual(items[-1]["devices"][0]["memory_mib_s"], 20)
            self.assertEqual(items[-1]["devices"][0]["device_energy_j_estimate"], 40)
            self.assertEqual(run.call_count, 6)
            self.assertNotIn("Example", console.getvalue())
            self.assertNotIn("GPU-a", console.getvalue())

    def test_ctrl_c_keeps_samples_and_interrupted_summary(self):
        clock = Clock()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "interrupted.jsonl"
            args = ["--run-id", "r", "--arm", "a", "--gpu-uuid", "GPU-a", "--duration-s", "5",
                    "--interval-s", "1", "--output", str(output)]
            with patch.object(observation.time, "monotonic", clock.monotonic), \
                 patch.object(observation.time, "sleep", side_effect=KeyboardInterrupt), \
                 patch.object(observation.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "")), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(observation.main(args), 130)
            items = [json.loads(line) for line in output.read_text().splitlines()]
            self.assertEqual([item["type"] for item in items], ["header", "sample", "summary"])
            self.assertEqual(items[-1]["status"], "INTERRUPTED")
            self.assertIsNone(items[-1]["devices"][0]["device_energy_j_estimate"])

    def test_cli_rejects_nonfinite_or_unreasonable_sampling_parameters(self):
        for value in ("nan", "inf", "0", "-1"):
            with self.subTest(duration=value), self.assertRaises(Exception):
                observation.positive(value)
        for value in ("0.01", "3601"):
            with self.subTest(interval=value), self.assertRaises(Exception):
                observation.interval(value)
        with self.assertRaises(Exception):
            observation.gpu_uuid("0")


if __name__ == "__main__":
    unittest.main()
