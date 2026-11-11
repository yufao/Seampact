"""Original CLI completion -> transport -> event observation, without a wrapper."""

import base64
import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from seampact.hosting.deployment import encode_port, decode_port
from seampact.runtime import Coordinator, OwnerRuntime
from seampact.scenes.call_artifacts import CallArtifacts
from seampact.scenes.evaluation_evidence import passive_value, passive_evaluation_snapshot, verify_passive_evaluation, PassiveEvidenceGap
from seampact.scenes.multisource_review_scene import MultiSourceReviewScene
from seampact.source.inspection import Inspector
from seampact.store import Store
from seampact.units import UnitCatalog


class CallArtifactTests(unittest.TestCase):
    def test_cli_receipt_is_passive_and_retains_exact_bytes_without_source_hooks(self):
        original = subprocess.CompletedProcess(["original", "--input", "in"], 0, b"out\xff", b"err\xfe")
        original.seampact_completion = dict(task_status="UNKNOWN", contract_declared=False)
        result = passive_value(original)
        self.assertEqual(base64.b64decode(result["stdout"]["$bytes"]), b"out\xff")
        self.assertEqual(result["completion"]["task_status"], "UNKNOWN")
        original.seampact_completion["task_status"] = "changed"
        self.assertEqual(result["completion"]["task_status"], "UNKNOWN")
        invoked = []
        class Process(subprocess.CompletedProcess):
            def __getattribute__(self, name):
                invoked.append(name)
                raise AssertionError("source hook")
        with self.assertRaises(PassiveEvidenceGap):
            passive_value(Process([], 0))
        self.assertEqual(invoked, [])
        original.owner = object()
        with self.assertRaises(PassiveEvidenceGap):
            passive_value(original)

    def test_real_catalog_cli_artifact_enters_observation_only_after_this_original_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = ("import argparse\nimport numpy as np\n"
                "p=argparse.ArgumentParser()\np.add_argument('--input')\np.add_argument('--output')\n"
                "a=p.parse_args()\nnp.savez(a.output, score=np.array(0.25), imgsize=np.array([1,1]))\n")
            (root / "original.py").write_text(source, encoding="utf-8")
            (root / "input.bin").write_bytes(b"original input")
            contract = dict(schema="native-completion/1", format="npz", input_parameter="input",
                output_parameter="output", required_keys=["score", "imgsize"], output_scope="fresh-parent/1",
                evidence=[dict(path="original.py", start=1, end=len(source.splitlines()))])
            candidate = dict(entries=[dict(id="cli", kind="cli", path="original.py", symbol="__main__",
                command=["python", "original.py", "--input", "{input}", "--output", "{output}"],
                completion=contract, boundary_ids=[])], boundaries=[])
            inspector = Inspector.freeze(root, root / "snapshots", "source", ["original.py"], "test", "static")
            inspector.request["source_id"] = "source"
            store = Store(":memory:")
            runtime = OwnerRuntime(Coordinator(store))
            self.addCleanup(runtime.close)
            self.addCleanup(store.close)
            catalog = UnitCatalog(root, store, runtime)
            unit = catalog.install(inspector, candidate, "analysis", "installation")[0]
            self.assertEqual(unit["status"], "READY", unit.get("reason"))
            class Context:
                def __init__(self):
                    self.calls, self.model_calls = [], []
                    self.catalog = SimpleNamespace(unit=lambda uid: unit)
                def record_failure_evidence(self, evidence, output_artifacts=()):
                    self.failure_evidence = evidence
                def invoke(self, uid, arguments):
                    self.calls.append(dict(unit_id=uid, arguments=copy.deepcopy(arguments), status="RETURN"))
                    return decode_port(json.loads(json.dumps(encode_port(catalog.invoke(uid, arguments)))))
            context = Context()
            declaration = dict(format="npz", worker_root=root.as_posix(), platform_root=str(root),
                filename="result.npz", fields=["score", "imgsize"])
            # Linux worker paths are used by real runs; Windows local fixtures
            # share the same path syntax with their original CLI process.
            if sys.platform == "win32":
                declaration["worker_root"] = "/" + root.as_posix()
                self.skipTest("original Linux CLI path mapping is checked on Linux")
            config = dict(schema="review-events/1", checks=[dict(id="image", stage="intake",
                input_version="image:1", calls=[dict(unit_id=unit["id"],
                    arguments=dict(input=str(root / "input.bin"), output={"$output": "output"}),
                    output_artifacts=dict(output=declaration))],
                observation=dict(kind="image_score", value={"$result": ["image", 0, "artifacts", "output", "values", "score"]}))])
            scene = MultiSourceReviewScene(config, context)
            event = scene.reset(dict(request="Check image authenticity."), 1)
            self.assertEqual(context.calls, [])
            self.assertEqual(event["scene_observation"]["observation_records"], [])
            event = scene.step(next(c for c in event["candidates"] if c["id"] == "image"))
            record = event["scene_observation"]["observation_records"][0]
            self.assertEqual((record["value"], record["input_version"], record["cost_refs"]), (.25, "image:1", ["unit_call:0"]))
            payload = scene.results["image"][0]
            self.assertEqual(payload["completion"]["task_status"], "COMPLETE")
            artifact = payload["artifacts"]["output"]
            self.assertEqual(artifact["arrays"]["score"]["shape"], [])
            snapshot = passive_evaluation_snapshot(scene._terminal(), context.calls, destination=root / "evidence")
            self.assertEqual(snapshot["terminal"]["evidence"]["original_results"]["image"][0]["artifacts"]["output"]["values"]["score"], .25)
            stored_path = snapshot["terminal"]["evidence"]["original_results"]["image"][0]["artifacts"]["output"]["path"]
            self.assertEqual(stored_path, snapshot["artifacts"][0]["path"])
            self.assertNotEqual(stored_path, artifact["path"])
            linked = root / "linked-evidence"
            linked.mkdir()
            (linked / (artifact["sha256"] + ".npz")).symlink_to(artifact["path"])
            with self.assertRaisesRegex(PassiveEvidenceGap, "independent"):
                passive_evaluation_snapshot(scene._terminal(), context.calls, destination=linked)
            # A stale/tampered file cannot reuse the old successful receipt.
            helper = CallArtifacts(dict(output=declaration))
            helper.paths["output"] = dict(path=Path(artifact["path"]), root=root, worker=artifact["worker_path"])
            Path(artifact["path"]).write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "differ"):
                helper.capture(payload)
            verify_passive_evaluation(snapshot)
            Path(snapshot["artifacts"][0]["path"]).write_bytes(b"changed retained file")
            with self.assertRaises(PassiveEvidenceGap):
                verify_passive_evaluation(snapshot)
            unknown = dict(payload, completion=dict(contract_declared=False))
            with self.assertRaisesRegex(ValueError, "complete original CLI"):
                helper.capture(unknown)


if __name__ == "__main__":
    unittest.main()
