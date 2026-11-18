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
from seampact.office_development import start_development_task
from seampact.office_privacy import privacy_cases, start_privacy_task
from seampact.office_spreadsheet import start_spreadsheet_task


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
                for pilot, expected, maximum in ((None, 900, 12), (dict(mode="app_config"), 900, 12),
                    (dict(mode="app_config", deadline_s=None, execution_timeout_s=None), None, 12),
                    (dict(mode="app_config", deadline_s=30, execution_timeout_s=120), 30, 12),
                    (dict(mode="app_config", max_model_turns=None), 900, None),
                    (dict(mode="app_config", max_model_turns=37), 900, 37),
                    (dict(mode="app_config", max_model_turns=160), 900, 160)):
                    with self.subTest(pilot=pilot):
                        start_native_task(platform, body, internal_task_id="task", pilot_config=pilot)
                        request = platform.chat.start.call_args.args[0]
                        self.assertEqual(request["deadline_s"], expected)
                        self.assertEqual(platform.chat.start.call_args.kwargs["runtime_options"]["max_model_turns"], maximum)
                        if pilot is not None and "execution_timeout_s" in pilot:
                            self.assertIn("execution_timeout_s", request)
                            self.assertEqual(request["execution_timeout_s"], pilot["execution_timeout_s"])
                        else:
                            self.assertNotIn("execution_timeout_s", request)

    def test_other_office_sources_use_one_explicit_limit_and_reject_invalid_values_before_admission(self):
        platform = SimpleNamespace(capabilities=SimpleNamespace(public=lambda: []),
            deployments=SimpleNamespace(lock=threading.RLock()), projects=SimpleNamespace(lock=threading.RLock()),
            code_execution=SimpleNamespace(image="fixture-python"), chat=SimpleNamespace(start=Mock()))
        spreadsheet = dict(id="spreadsheet", instruction="Copy values", instruction_type="cell", answer_position="A1",
                           family_id="sheet-family", original_manifest_sha256="b" * 64)
        inputs = [dict(name="input.xlsx", data=b"fixture", sha256="c" * 64)]
        development = dict(id="development", family_id="development-family", _source="office_summedits", _grading={},
                           _manifest=dict(manifest_sha256="d" * 64), agent_input=dict(request="Inspect text", documents={}))
        admissions = (
            (start_spreadsheet_task, dict(manifest="fixture", manifest_sha256="a" * 64, case_id="spreadsheet", project_id="project")),
            (start_privacy_task, dict(case_id=privacy_cases()[0]["id"], project_id="project")),
            (start_development_task, dict(case=development, project_id="project")),
        )
        with patch("seampact.office_spreadsheet.load_case", return_value=(spreadsheet, inputs)), \
             patch("seampact.office_application.build_hooks", return_value=dict(hooks=())), \
             patch("seampact.office_development.build_hooks", return_value={}):
            for start, body in admissions:
                for supplied, expected in (({}, None), (dict(max_model_turns=None), None),
                                           (dict(max_model_turns=37), 37), (dict(max_model_turns=160), 160)):
                    with self.subTest(source=start.__name__, supplied=supplied):
                        pilot = dict(mode="app_config", deadline_s=None, **supplied)
                        start(platform, body, internal_task_id="task", pilot_config=pilot)
                        runtime = platform.chat.start.call_args.kwargs["runtime_options"]
                        self.assertEqual(runtime["max_model_turns"], expected)
                for invalid in (False, 0, -1, 1.5, "15"):
                    with self.subTest(source=start.__name__, invalid=invalid):
                        platform.chat.start.reset_mock()
                        with self.assertRaisesRegex(ValueError, "max_model_turns"):
                            start(platform, body, internal_task_id="task",
                                  pilot_config=dict(mode="app_config", deadline_s=None, max_model_turns=invalid))
                        platform.chat.start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
