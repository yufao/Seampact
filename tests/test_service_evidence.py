import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

from seampact.server import make_server
from seampact.service import Platform


def controlled_report():
    return dict(schema="mmct-sdk-probe/1", status="PASS", cases=[dict(status="PASS")],
                sdk={"autogen-agentchat": "0.7.5", "autogen-core": "0.7.5",
                     "autogen-ext": "0.7.5", "Pillow": "12.3.0"},
                production_admitted=True, prompt="PRIVATE_REPORT_PROMPT")


class ServiceEvidenceTests(unittest.TestCase):
    def test_live_evidence_projection_does_not_mutate_runtime_or_source_admission(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence_root = root / "evidence"
            evidence_root.mkdir()
            report_path = evidence_root / "mmct-sdk-result.json"
            raw = json.dumps(controlled_report()).encode()
            report_path.write_bytes(raw)
            sources = {identity: dict(root=str(root), files=[], entry="native", name=identity)
                       for identity in ("mmct-image", "crag-text", "abc-policy", "disp-offline")}
            platform = Platform(root / "state", sources, evidence_root=evidence_root)
            try:
                original_runtime = platform.runtime.snapshot()
                value = platform.state()
                self.assertEqual(value["evidence"]["records"][0]["status"], "PASS")
                self.assertFalse(value["evidence"]["production_admitted"])
                self.assertEqual(report_path.read_bytes(), raw)
                self.assertEqual(platform.runtime.snapshot(), original_runtime)
                self.assertNotIn("PRIVATE_REPORT_PROMPT", json.dumps(value))
                for source in value["sources"]:
                    for field in ("native", "adapted", "parallel", "deployment"):
                        self.assertEqual(source[field], "NOT_RUN")
                self.assertEqual({p["credential_mode"] for p in value["profiles"]},
                                 {"windows_dpapi" if os.name == "nt" else "private_environment"})
                report_path.write_text("{broken", encoding="utf-8")
                self.assertEqual(platform.state()["evidence"]["records"][0]["status"], "INVALID")
                report_path.unlink()
                self.assertEqual(platform.state()["evidence"]["records"][0]["status"], "NOT_RUN")
                self.assertEqual(platform.runtime.snapshot(), original_runtime)
            finally:
                platform.close()

    def test_http_state_exposes_sanitized_evidence_and_default_missing_records(self):
        with tempfile.TemporaryDirectory() as temporary:
            platform = Platform(Path(temporary) / "state")
            server = make_server(platform, 0, require_session=False)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/api/state") as response:
                    value = json.load(response)
                self.assertEqual(value["evidence"]["summary"]["NOT_RUN"], 5)
                self.assertFalse(value["evidence"]["production_admitted"])
                self.assertTrue(all(record["sha256"] is None for record in value["evidence"]["records"]))
            finally:
                server.shutdown()
                server.server_close()
                thread.join()
                platform.close()

    def test_cli_documents_explicit_read_only_evidence_root(self):
        result = subprocess.run([sys.executable, "-B", "-m", "seampact", "--help"],
                                capture_output=True, text=True, check=True)
        self.assertIn("--evidence", result.stdout)
        self.assertIn("read-only", result.stdout)
