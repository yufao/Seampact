"""Deploy readiness checks without invoking Docker or deployment."""
import ast
import unittest
from types import SimpleNamespace
from pathlib import Path


class DeployPreflightTests(unittest.TestCase):
    def check(self, observation, state):
        source = (Path(__file__).resolve().parents[1] / "deploy.sh").read_text(encoding="utf-8")
        embedded = source.split("<<'SEAMPACT_DEPLOY_PY'", 1)[1].rsplit("SEAMPACT_DEPLOY_PY", 1)[0]
        tree = ast.parse(embedded)
        function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "check_idle")
        def fail(message):
            raise RuntimeError(message)
        scope = dict(OLD={"Id": "old", "State": {"Running": True}}, fail=fail,
                     container_preflight=lambda _: observation, container_http=lambda *_: state)
        exec(compile(ast.Module(body=[function], type_ignores=[]), "deploy.sh:check_idle", "exec"), scope)
        return scope["check_idle"]()

    def test_container_probe_builds_valid_python_and_preserves_404(self):
        source = (Path(__file__).resolve().parents[1] / "deploy.sh").read_text(encoding="utf-8")
        embedded = source.split("<<'SEAMPACT_DEPLOY_PY'", 1)[1].rsplit("SEAMPACT_DEPLOY_PY", 1)[0]
        function = next(n for n in ast.parse(embedded).body if isinstance(n, ast.FunctionDef) and n.name == "container_preflight")
        def docker(*args, **kwargs):
            compile(args[-1], "container-probe", "exec")
            return SimpleNamespace(returncode=0, stdout='{ "http_status": 404 }')
        import json
        scope = dict(OLD={"Config": {"Cmd": []}}, command_option=lambda *args: "8880", docker=docker, json=json)
        exec(compile(ast.Module(body=[function], type_ignores=[]), "deploy.sh:probe", "exec"), scope)
        self.assertEqual(scope["container_preflight"]("old")["http_status"], 404)

    def test_ready_still_runs_old_guard(self):
        ready = {"http_status": 200, "body": {"operation": "update", "status": "READY", "items": []}}
        state = dict(tasks=[{"status": "RUNNING"}], analyses=[], runtime_groups=[], deployments=[], scheduler={})
        with self.assertRaisesRegex(RuntimeError, "active/queued"):
            self.check(ready, state)
        state["tasks"] = []
        self.check(ready, state)

    def test_not_ready_and_unknown_observation_refuse_update(self):
        for status in ("BLOCKED", "DRAIN", "UNKNOWN"):
            body = dict(operation="update", status=status, items=[dict(reason="activity", next_step="wait")])
            with self.subTest(status=status), self.assertRaisesRegex(RuntimeError, status):
                self.check(dict(http_status=200, body=body), {})
        with self.assertRaisesRegex(RuntimeError, "Cannot observe"):
            self.check(None, {})
        with self.assertRaisesRegex(RuntimeError, "unrecognized"):
            self.check(dict(http_status=500), {})

    def test_old_missing_endpoint_warns_and_checks_existing_state(self):
        state = dict(version="0.15.1", tasks=[], analyses=[], runtime_groups=[], deployments=[], scheduler={},
                     resources={"managed_services": []})
        self.check(dict(http_status=404), state)
        state["resources"]["managed_services"] = [{"instances": [{"state": "READY", "released": False}]}]
        with self.assertRaisesRegex(RuntimeError, "active/queued"):
            self.check(dict(http_status=404), state)
        with self.assertRaisesRegex(RuntimeError, "no recognized"):
            self.check(dict(http_status=404), dict(version="future"))



if __name__ == "__main__":
    unittest.main()
