"""The native admission route preserves an explicitly unbounded pilot deadline."""

import hashlib
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from seampact.office_experiment import start_native_task


class NativeDeadline(unittest.TestCase):
    def test_explicit_pilot_deadline_including_none_and_manifest_default(self):
        manifest = dict(protocol=dict(max_model_turns=12, timeout_s=900),
                        cases=[dict(id="case", suite="workspace", user_task_id="user", split="training")])
        environment = SimpleNamespace(prompt="Native request", tool_specs=lambda: [], execute=Mock(), identity={})
        platform = SimpleNamespace(capabilities=SimpleNamespace(public=lambda: []),
            deployments=SimpleNamespace(lock=threading.RLock()), projects=SimpleNamespace(lock=threading.RLock()),
            chat=SimpleNamespace(start=Mock(return_value=dict(task_id="task"))))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            content = json.dumps(manifest).encode()
            path.write_bytes(content)
            body = dict(manifest=str(path), manifest_sha256=hashlib.sha256(content).hexdigest(),
                        case_id="case", project_id="project")
            with patch("seampact.office_experiment.source_identity", return_value=dict(revision="fixture")), \
                 patch("seampact.office_experiment.AgentDojoOfficeTask", return_value=environment), \
                 patch("seampact.office_application.build_hooks", return_value={}):
                for pilot, expected in ((None, 900), (dict(mode="app_config"), 900),
                    (dict(mode="app_config", deadline_s=None, execution_timeout_s=None), None),
                    (dict(mode="app_config", deadline_s=30, execution_timeout_s=120), 30)):
                    with self.subTest(pilot=pilot):
                        start_native_task(platform, body, internal_task_id="task", pilot_config=pilot)
                        request = platform.chat.start.call_args.args[0]
                        self.assertEqual(request["deadline_s"], expected)
                        self.assertEqual(platform.chat.start.call_args.kwargs["runtime_options"]["max_model_turns"], 12)
                        if pilot is not None and "execution_timeout_s" in pilot:
                            self.assertIn("execution_timeout_s", request)
                            self.assertEqual(request["execution_timeout_s"], pilot["execution_timeout_s"])
                        else:
                            self.assertNotIn("execution_timeout_s", request)


if __name__ == "__main__":
    unittest.main()
