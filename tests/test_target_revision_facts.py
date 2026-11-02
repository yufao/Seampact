"""Batch revision facts through the actual selected-interpreter JSON protocol."""

import base64
import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from seampact.source.inspection import Inspector
from seampact.analysis_support.revisions import _extract, build_review_context
from seampact.store import Store
from seampact.target_runtime import attach_target, checker_identity


class TargetRevisionFactsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.payloads = []
        self.backend = patch("seampact.target_runtime.backends.run", side_effect=self.run_target).start()
        self.addCleanup(patch.stopall)
        patch("seampact.target_runtime.backends.docker_available", return_value=None).start()

    def freeze(self, files):
        for path, text in files.items():
            (self.root / path).write_text(text, encoding="utf-8")
        inspector = Inspector.freeze(self.root, self.root / "snapshots", "request", list(files), "main", "original result")
        environment = dict(python=".".join(map(str, sys.version_info[:2])), base_image="python:test-target")
        inspector.request["runtime_environment"] = environment
        record = dict(status="READY", image_id="sha256:test-selected-target", identity=checker_identity(environment))
        inspector.request["target_runtime"] = copy.deepcopy(record)
        return attach_target(inspector, record)

    def protocol(self, payload):
        environment = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"),
                           PYTHONIOENCODING="utf-8")
        process = subprocess.run([sys.executable, "-B", "-m", "seampact.target_runtime"],
            input=json.dumps(payload), capture_output=True, text=True, encoding="utf-8", env=environment, timeout=30)
        self.assertEqual(process.returncode, 0, process.stderr)
        return process.stdout

    def run_target(self, command, timeout, stdin):
        self.assertEqual(command[:7], ["docker", "run", "--rm", "-i", "--network", "none", "sha256:test-selected-target"])
        payload = json.loads(stdin)
        self.payloads.append(payload)
        stdout = self.protocol(payload)
        return dict(returncode=0, stdout=stdout, output=stdout)

    def test_one_batch_matches_original_target_tools_and_only_queries_cache_misses(self):
        inspector = self.freeze({
            "source.py": "raise RuntimeError('source must never execute')\nimport os\n@register\ndef main():\n    return service.call()\n",
            "dynamic.py": "def invoke(name):\n    return __import__(name)\n",
            "package.json": '{"scripts":{"check":"python source.py"}}',
        })
        expected = {path: _extract(inspector, path, remote=True) for path in inspector.files}
        self.assertTrue(all(payload["method"] != "collect_source_facts" for payload in self.payloads))
        self.payloads.clear()
        context = build_review_context(None, inspector, self.store)
        self.assertEqual(len(self.payloads), 1)
        self.assertEqual(self.payloads[0]["method"], "collect_source_facts")
        self.assertEqual(set(self.payloads[0]["arguments"]["paths"]), set(inspector.files))
        for fact in context["facts"]:
            self.assertEqual({key: fact[key] for key in expected[fact["path"]]}, expected[fact["path"]])
            self.assertEqual(fact["sha256"], inspector.files[fact["path"]]["sha256"])
            self.assertEqual(fact["parser"]["identity"], inspector.target_runtime.record["identity"])
        self.assertEqual(context["reuse"]["created"], 3)
        self.assertEqual(context["reuse"]["failed"], 0)
        self.assertIn("NOT_REUSED", context["permissions"])
        build_review_context(inspector, inspector, self.store)
        self.assertEqual(len(self.payloads), 1)

        changed = self.freeze({"source.py": "import sys\ndef main():\n    return sys.version\n",
            "dynamic.py": "def invoke(name):\n    return __import__(name)\n",
            "package.json": '{"scripts":{"check":"python source.py"}}'})
        context = build_review_context(inspector, changed, self.store)
        self.assertEqual(len(self.payloads), 2)
        self.assertEqual(self.payloads[-1]["arguments"]["paths"], ["source.py"])
        self.assertEqual(set(self.payloads[-1]["files"]), set(changed.files))
        self.assertEqual(context["reuse"]["hits"], 2)
        self.assertEqual(context["reuse"]["created"], 1)

    def test_partial_parser_and_failed_file_remain_unknown_and_failure_is_not_cached(self):
        inspector = self.freeze({"good.py": "def main():\n    return 1\n", "partial.py": "def broken(:\n",
                                 "failed.ipynb": '{"cells":null}'})
        context = build_review_context(None, inspector, self.store)
        by_path = {row["path"]: row for row in context["facts"]}
        self.assertEqual(by_path["good.py"]["parse_status"], "PARSED")
        self.assertEqual(by_path["partial.py"]["parse_status"], "PARTIAL")
        self.assertIn("TARGET_PARSER_INCOMPLETE", by_path["partial.py"]["unknown"])
        self.assertEqual(by_path["failed.ipynb"]["parse_status"], "UNAVAILABLE")
        self.assertEqual(by_path["failed.ipynb"]["dependency_closure"], "UNKNOWN")
        self.assertEqual(context["reuse"]["failed"], 1)
        context = build_review_context(None, inspector, self.store)
        self.assertEqual(context["reuse"]["hits"], 2)
        self.assertEqual(context["reuse"]["failed"], 1)
        self.assertEqual(self.payloads[-1]["arguments"]["paths"], ["failed.ipynb"])

    def test_protocol_rejects_wrong_selected_python_and_changed_frozen_bytes(self):
        inspector = self.freeze({"source.py": "def main():\n    return 1\n"})
        payload = dict(schema="target-source-tools/1", method="collect_source_facts", arguments=dict(paths=["source.py"]),
            request=copy.deepcopy(inspector.request),
            files={path: base64.b64encode(inspector.data(path)).decode("ascii") for path in inspector.files})
        payload["request"]["runtime_environment"]["python"] = "0.0"
        self.assertIn("error", json.loads(self.protocol(payload)))
        payload["request"] = copy.deepcopy(inspector.request)
        payload["files"]["source.py"] = base64.b64encode(b"changed").decode("ascii")
        answer = json.loads(self.protocol(payload))
        self.assertIn("error", answer)
        self.assertNotIn("result", answer)

    def test_failed_transport_and_bad_file_hash_never_become_successful_facts(self):
        inspector = self.freeze({"source.py": "def main():\n    return 1\n"})
        with patch.object(inspector.target_runtime, "call", side_effect=ValueError("unavailable")) as call:
            context = build_review_context(None, inspector, self.store)
        call.assert_called_once_with("collect_source_facts", paths=["source.py"])
        self.assertEqual(context["reuse"]["failed"], 1)
        self.assertEqual(context["reuse"]["created"], 0)
        answer = dict(schema="source-facts/1", source_version=inspector.request["source_version"], results=[
            dict(path="source.py", sha256="different", facts={})])
        with patch.object(inspector.target_runtime, "call", return_value=answer):
            context = build_review_context(None, inspector, self.store)
        self.assertEqual(context["reuse"]["failed"], 1)
        self.assertEqual(context["reuse"]["hits"], 0)


if __name__ == "__main__":
    unittest.main()
