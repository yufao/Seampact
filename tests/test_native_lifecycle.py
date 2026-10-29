"""Source lifecycle evidence and original owner execution in real processes.

These validate the finite source subset; they are not HF/model-pool performance
evidence. Framework APIs without an audited summary deliberately stay UNKNOWN.
"""

import copy
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from seampact.deployment import Deployments
from seampact.inspection import Inspector
from seampact.lifecycle import RULE, build_lifecycle_evidence
from seampact.runtime import Coordinator, OwnerRuntime, ProtocolError
from seampact.store import Store
from seampact.units import UnitCatalog


HELPER = """
def dot(left: list[float], right: list[float]) -> float:
    return sum([a * b for a, b in zip(left, right)])
"""
SOURCE = """
from helper import dot

class NativeScore:
    def __init__(self, weights: list[float], bias: float):
        self.weights = weights
        self.bias = bias

    def predict(self, values: list[float]) -> float:
        return dot(values, self.weights) + self.bias
"""


def native_child(path):
    spec = json.loads(Path(path).read_text(encoding="utf-8"))
    inspector = Inspector(spec["root"], spec["snapshots"], spec["request"])
    store = Store(":memory:")
    runtime = OwnerRuntime(Coordinator(store))
    try:
        catalog = UnitCatalog(Path(path).parent, store, runtime)
        unit = catalog.install(inspector, spec["candidate"], "analysis", "install")[0]
        results = [catalog.invoke(unit["id"], {"values": values}) for values in [[3.0, 4.0], [1.0, 2.0]]]
        group = store.get("runtime_group", unit["runtime_group"])
        snapshot = runtime.snapshot()
        print(json.dumps(dict(pid=os.getpid(), results=results, lifecycle=group["contract"]["lifecycle"],
            owners=[row["owner"] for row in snapshot["gates"].values()],
            calls=[row["physical"] for row in snapshot["calls"].values()],
            source_state=[record["owner"].weights for record in catalog.bindings[unit["id"]].owners.values()])))
    finally:
        runtime.close()
        store.close()


class NativeLifecycleTests(unittest.TestCase):
    def freeze(self, source=SOURCE, helper=HELPER, extras=None):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        files = {"original.py": textwrap.dedent(source).lstrip(), "helper.py": textwrap.dedent(helper).lstrip(), **(extras or {})}
        for name, content in files.items():
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        inspector = Inspector.freeze(root, root / "snapshots", "request", list(files), "NativeScore.predict", "original result")
        inspector.request["source_id"] = "source"
        entry = dict(id="predict", path="original.py", symbol="NativeScore.predict", kind="method",
            constructor=dict(symbol="NativeScore", arguments=dict(weights=[1.0, 2.0], bias=0.5)), boundary_ids=["predict-W"])
        inspector.request["configuration"] = dict(entries=dict(predict=dict(
            constructor_arguments=copy.deepcopy(entry["constructor"]["arguments"]))))
        candidate = dict(entries=[entry], boundaries=[dict(id="predict-W", path="original.py", symbol="NativeScore.predict", mode="W")])
        return root, inspector, candidate

    def report(self, inspector, candidate):
        store = Store(":memory:")
        try:
            return UnitCatalog(inspector.root, store, None).inspect_candidate(inspector, candidate)
        finally:
            store.close()

    def lifecycle(self, inspector, candidate):
        report = self.report(inspector, candidate)
        self.assertTrue(all(row["status"] == "READY" or row.get("host_condition") for row in report["entries"]),
                        [row.get("reason") for row in report["entries"]])
        return report["runtime_groups"]["groups"][0]["contract"]["lifecycle"]

    def test_frozen_owner_and_import_call_coverage_are_contract_bound(self):
        _, inspector, candidate = self.freeze()
        lifecycle = self.lifecycle(inspector, candidate)
        self.assertEqual(lifecycle["replicate"]["status"], "SUPPORTED")
        self.assertEqual(lifecycle["reclaim"]["status"], "SUPPORTED")
        evidence = lifecycle["source_evidence"]
        self.assertEqual(evidence["rule"], RULE)
        self.assertEqual({row["path"] for row in evidence["members"][0]["source_coverage"]}, {"original.py", "helper.py"})
        self.assertTrue(any(row["target"] == "dot" for row in evidence["members"][0]["call_coverage"]))
        changed = copy.deepcopy(candidate)
        changed["entries"][0]["constructor"]["arguments"]["bias"] = 2.0
        inspector.request["configuration"]["entries"]["predict"]["constructor_arguments"]["bias"] = 2.0
        other = self.lifecycle(inspector, changed)["source_evidence"]
        self.assertNotEqual(evidence["binding_digest"], other["binding_digest"])
        self.assertNotEqual(evidence["digest"], other["digest"])

    def test_request_mutation_and_alias_mutation_are_not_read_only(self):
        for body in ("self.bias = values[0]\n        return self.bias", "state = self.weights\n        state[0] = values[0]\n        return self.bias",
                     "state = self.weights\n        state += values\n        return self.bias"):
            with self.subTest(body=body):
                source = textwrap.dedent(SOURCE).lstrip().replace("return dot(values, self.weights) + self.bias", body)
                _, inspector, candidate = self.freeze(source)
                lifecycle = self.lifecycle(inspector, candidate)
                self.assertEqual(lifecycle["reclaim"]["status"], "UNKNOWN")
                reason = lifecycle["source_evidence"]["members"][0]["reason"]
                self.assertTrue("mutation" in reason or "AugAssign" in reason, reason)

    def test_module_initialization_and_transitive_import_effects_are_covered(self):
        for helper, expected in (("import os\n" + textwrap.dedent(HELPER), "external import"),
                                 (textwrap.dedent(HELPER) + "\nprint('effect')\n", "module initialization effect")):
            with self.subTest(expected=expected):
                _, inspector, candidate = self.freeze(helper=helper)
                lifecycle = self.lifecycle(inspector, candidate)
                self.assertEqual(lifecycle["replicate"]["status"], "UNKNOWN")
                self.assertIn(expected, lifecycle["source_evidence"]["members"][0]["reason"])

    def test_framework_name_eval_and_candidate_permission_flags_do_not_grant_lifecycle(self):
        _, inspector, candidate = self.freeze("import transformers\n" + textwrap.dedent(SOURCE))
        candidate["runtime_groups"] = [dict(entries=["predict"], lifecycle=dict(reclaim=True, replicate=True),
            model_is_read_only=True, trusted=True)]
        lifecycle = self.lifecycle(inspector, candidate)
        self.assertEqual(lifecycle["replicate"]["status"], "UNKNOWN")
        reason = lifecycle["source_evidence"]["members"][0]["reason"]
        self.assertIn("version-bound lifecycle/effect summary: transformers", reason)
        _, inspector, candidate = self.freeze(SOURCE.replace("return dot(values, self.weights) + self.bias", "self.weights.eval()\n        return self.weights.generate(values)"))
        lifecycle = self.lifecycle(inspector, candidate)
        self.assertEqual(lifecycle["reclaim"]["status"], "UNKNOWN")
        self.assertIn("no exact source/framework lifecycle summary", lifecycle["source_evidence"]["members"][0]["reason"])

    def test_owner_finalizer_background_work_and_open_ports_remain_unknown(self):
        for source in (SOURCE + "\n    def __del__(self):\n        print('cleanup')\n",
                       SOURCE.replace("return dot(values, self.weights) + self.bias", "yield self.bias"),
                       SOURCE.replace("values: list[float]", "values: list")):
            _, inspector, candidate = self.freeze(source)
            report = self.report(inspector, candidate)
            lifecycle = report["runtime_groups"]["groups"][0]["contract"]["lifecycle"]
            self.assertEqual(lifecycle["reclaim"]["status"], "UNKNOWN")

    def test_persisted_iterators_are_state_even_without_attribute_assignment_in_request(self):
        source = SOURCE.replace("self.weights = weights", "self.weights = zip(weights, weights)")
        _, inspector, candidate = self.freeze(source)
        lifecycle = self.lifecycle(inspector, candidate)
        self.assertEqual(lifecycle["replicate"]["status"], "UNKNOWN")
        self.assertIn("iterator may escape", lifecycle["source_evidence"]["members"][0]["reason"])

    def test_unbounded_loops_in_initialization_or_requests_need_a_lifetime_summary(self):
        for source in (SOURCE.replace("self.bias = bias", "self.bias = bias\n        while True:\n            pass"),
                       SOURCE.replace("return dot(values, self.weights) + self.bias", "while True:\n            pass")):
            with self.subTest(source=source):
                _, inspector, candidate = self.freeze(source)
                lifecycle = self.lifecycle(inspector, candidate)
                self.assertEqual(lifecycle["replicate"]["status"], "UNKNOWN")
                self.assertIn("termination/lifetime summary", lifecycle["source_evidence"]["members"][0]["reason"])

    def test_shadowed_future_import_is_not_a_standard_library_summary(self):
        _, inspector, candidate = self.freeze("from __future__ import annotations\n" + textwrap.dedent(SOURCE),
                                              extras={"__future__.py": "annotations = None\nprint('import effect')\n"})
        lifecycle = self.lifecycle(inspector, candidate)
        self.assertEqual(lifecycle["replicate"]["status"], "UNKNOWN")
        self.assertIn("shadowed future import", lifecycle["source_evidence"]["members"][0]["reason"])

    def test_mutable_defaults_and_builtin_callbacks_do_not_hide_effects(self):
        helpers = (
            "def dot(left: list[float], right: list[float], saved=[0.0]):\n    saved[0] = left[0]\n    return 1.0\n",
            "def effect(value):\n    print(value)\n    return value\n\ndef dot(left: list[float], right: list[float]):\n    return sum(sorted(left, key=effect))\n",
        )
        for helper in helpers:
            with self.subTest(helper=helper):
                _, inspector, candidate = self.freeze(helper=helper)
                lifecycle = self.lifecycle(inspector, candidate)
                self.assertEqual(lifecycle["reclaim"]["status"], "UNKNOWN")
                self.assertTrue(any(word in lifecycle["source_evidence"]["members"][0]["reason"]
                                    for word in ("mutation", "untracked value/callback")))

    def test_source_and_binding_digests_use_frozen_import_bytes(self):
        root, inspector, candidate = self.freeze()
        evidence = self.lifecycle(inspector, candidate)["source_evidence"]
        (root / "helper.py").write_text("print('changed live source')\n", encoding="utf-8")
        self.assertEqual(self.lifecycle(inspector, candidate)["source_evidence"]["digest"], evidence["digest"])
        _, other, changed = self.freeze(helper=HELPER.replace("a * b", "a * b + 1"))
        self.assertNotEqual(self.lifecycle(other, changed)["source_evidence"]["digest"], evidence["digest"])
        rows = [dict(entry_id="predict", entry=candidate["entries"][0],
                     check=self.report(inspector, candidate)["entries"][0])]
        imported = inspector.snapshots / inspector.files["helper.py"]["sha256"]
        imported.write_bytes(b"print('changed snapshot')\n")
        rebuilt = build_lifecycle_evidence(inspector, rows, ["predict"])
        self.assertEqual(rebuilt["checks"]["request_isolation"]["status"], "UNKNOWN")
        self.assertIn("frozen source digest changed", rebuilt["members"][0]["reason"])

    def test_equal_constructor_values_keep_distinct_original_owners(self):
        root, inspector, candidate = self.freeze(SOURCE + "\n    def predict_again(self, values: list[float]) -> float:\n        return dot(values, self.weights) + self.bias\n")
        second = copy.deepcopy(candidate["entries"][0])
        second.update(id="predict-again", symbol="NativeScore.predict_again", boundary_ids=["predict-again-W"])
        candidate["entries"].append(second)
        candidate["boundaries"].append(dict(id="predict-again-W", path="original.py", symbol="NativeScore.predict_again", mode="W"))
        inspector.request["configuration"]["entries"]["predict-again"] = copy.deepcopy(
            inspector.request["configuration"]["entries"]["predict"])
        store = Store(":memory:")
        runtime = OwnerRuntime(Coordinator(store))
        try:
            catalog = UnitCatalog(root, store, runtime)
            units = catalog.install(inspector, candidate, "analysis", "install")
            for unit in units:
                self.assertEqual(catalog.invoke(unit["id"], {"values": [3.0, 4.0]}), 11.5)
            binding = catalog.bindings[units[0]["id"]]
            self.assertEqual(len(binding.owners), 2)
            self.assertEqual(len({record["id"] for record in binding.owners.values()}), 2)
        finally:
            runtime.close()
            store.close()

    def test_original_w_owner_runs_in_separate_real_processes_and_exits(self):
        root, inspector, candidate = self.freeze()
        spec = root / "process-spec.json"
        spec.write_text(json.dumps(dict(root=str(root), snapshots=str(inspector.snapshots), request=inspector.request,
            candidate=candidate)), encoding="utf-8")
        results = []
        for _ in range(2):
            process = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--native-child", str(spec)],
                capture_output=True, text=True, timeout=30)
            self.assertEqual(process.returncode, 0, process.stderr)
            result = json.loads(process.stdout.splitlines()[-1])
            self.assertEqual(result["results"], [11.5, 5.5])
            self.assertEqual(len(set(result["owners"])), 1)
            self.assertEqual(result["source_state"], [[1.0, 2.0]])
            self.assertEqual(result["calls"], ["RETIRED", "RETIRED"])
            self.assertEqual(result["lifecycle"]["reclaim"]["status"], "SUPPORTED")
            results.append(result)
        self.assertNotEqual(results[0]["owners"], results[1]["owners"])


class OwnerDrainTests(unittest.TestCase):
    def test_no_calls_is_not_owner_lease_end_and_idle_route_never_moves_owner(self):
        store = Store(":memory:")
        runtime = OwnerRuntime(Coordinator(store))
        try:
            # The actual coordinator has no active calls; the task lease alone
            # still forbids process release between two roots of one task.
            deployments = Deployments.__new__(Deployments)
            deployments.lock = threading.RLock()
            deployments.platform = SimpleNamespace(runtime=runtime)
            record = dict(id="instance", group_id="group", state="DRAINING", in_flight=0,
                          pending_stop=dict(reason="idle_retire/1"))
            deployments.instances = {"instance": record}
            deployments.affinity = {("task", "group"): "instance"}
            self.assertFalse(deployments._drained(record))
            with self.assertRaises(ProtocolError):
                deployments._route("group", "unit", "task")
            self.assertEqual(deployments.affinity[("task", "group")], "instance")
            deployments.release_task("task")
            self.assertTrue(deployments._drained(record))
        finally:
            runtime.close()
            store.close()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--native-child":
        native_child(sys.argv[2])
    else:
        unittest.main()
