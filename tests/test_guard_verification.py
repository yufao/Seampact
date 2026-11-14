from contextlib import nullcontext
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock
import threading

from seampact.capability_catalog import CapabilityCatalog
from seampact.capability_diagnostics import invoke as diagnose
from seampact.capability_generation import GenerationJobs, GenerationRepairable
from seampact.guard_contract import verification_error

FACT = {"guard": {"kind": "fact_support"}}
SELF = {"guard": {"kind": "input_safety"}}
INPUT = {"references": ["source"]}


def fact(label):
    return {"output": {"status": "ok", "claim_count": 1, "claims": [
        {"claim_text": "claim", "label": label, "labels_by_reference": [label]}]}}


class GuardVerificationTests(TestCase):
    def test_checks_accept_both_outcomes_and_reject_incomplete_results(self):
        for label in ("Entailment", "Contradiction"):
            self.assertIsNone(verification_error(FACT, INPUT, fact(label)))
        invalid = {"output": {"status": "unavailable", "claim_count": 0, "claims": [],
                              "unavailable_reason": "no_claims"}}
        self.assertIn("no_claims", verification_error(FACT, INPUT, invalid))
        self.assertIsNotNone(verification_error(FACT, {"references": []}, fact("Entailment")))
        for blocked in (False, True):
            result = {"output": {"failed": False, "is_blocked": blocked,
                                  "decision": "block" if blocked else "allow"}}
            self.assertIsNone(verification_error(SELF, {}, result))
        self.assertIsNotNone(verification_error(SELF, {}, {"output": {"failed": True}}))
        self.assertIsNotNone(verification_error(SELF, {}, {"output": {"failed": False, "is_blocked": False,
                                                                       "decision": "block"}}))
        self.assertIsNone(verification_error({}, {}, invalid))

    def test_catalog_only_opens_unverified_guard_after_valid_result(self):
        unit = {"id": "u", "status": "READY", "reason": None, "parameters": {"type": "object"},
                "runtime_group": "g", "capability": FACT,
                "generation_verification": {"status": "NEEDS_INPUT"}}
        catalog = object.__new__(CapabilityCatalog)
        catalog.unit = Mock(return_value=unit)
        catalog._group = Mock(return_value={"id": "g", "contract": {"digest": "d"}})
        catalog._slot = Mock(return_value=nullcontext())
        catalog.lock = threading.RLock()
        catalog.store = SimpleNamespace(save=Mock())
        invalid = {"output": {"status": "unavailable", "claims": [], "claim_count": 0}}
        router = SimpleNamespace(backend=lambda: {"start_timeout_s": 1},
                                 invoke_placed=Mock(return_value=(invalid, {"call_id": "first"})))
        catalog.router = router
        self.assertEqual(catalog.invoke("u", INPUT)["output"], invalid["output"])
        self.assertEqual(unit["generation_verification"]["status"], "FAILED")
        router.invoke_placed.return_value = (fact("Contradiction"), {"call_id": "second"})
        catalog.invoke("u", INPUT)
        self.assertEqual(unit["generation_verification"]["status"], "SUCCEEDED")
        router.invoke_placed.return_value = (invalid, {"call_id": "third"})
        catalog.invoke("u", INPUT)
        self.assertEqual(unit["generation_verification"]["status"], "SUCCEEDED")

    def test_failed_generation_sample_retains_actual_result_and_requests_repair(self):
        cap = dict(FACT, id="check", effects=[])
        unit = {"id": "u", "entry_id": "check"}
        output = {"output": {"status": "unavailable", "claim_count": 0, "claims": [],
                             "unavailable_reason": "no_claims"}, "call": {"call_id": "actual"}}
        router = SimpleNamespace(live_instances=lambda _: [])
        catalog = SimpleNamespace(router=router, unit=lambda _: unit, invoke=lambda *a, **k: output,
                                  begin_verification_batch=Mock(), end_verification_batch=Mock())
        jobs = object.__new__(GenerationJobs)
        jobs.platform = SimpleNamespace(capabilities=catalog)
        jobs.store = SimpleNamespace(get=lambda *a: {})
        jobs._verification_pending = {}
        jobs._check = lambda _: None
        jobs._save = Mock()
        jobs._await = lambda call, _: call()
        jobs.closed = threading.Event()
        job = {"id": "job", "gaps": []}
        sample = {"capability": "check", "input": INPUT, "provenance": []}
        with self.assertRaisesRegex(GenerationRepairable, "no_claims"):
            jobs._verify_calls({"capabilities": [cap]}, {"examples": [sample]},
                               {"group_id": "g", "capabilities": ["u"]}, job,
                               threading.Event(), SimpleNamespace(request={}))
        self.assertEqual(job["first_call"]["status"], "FAILED")
        self.assertEqual(job["first_call"]["calls"][0]["result"], output)
        self.assertEqual(job["first_call"]["calls"][0]["status"], "FAILED")
        catalog.end_verification_batch.assert_called_once()
        self.assertEqual(catalog.end_verification_batch.call_args.args[1], "FAILED")

    def test_explicit_diagnostic_preserves_invalid_worker_result(self):
        invalid = {"output": {"status": "unavailable", "claim_count": 0, "claims": [],
                              "unavailable_reason": "no_claims"}, "call": {"call_id": "actual"}}
        unit = {"id": "u", "runtime_group": "g", "capability": FACT}
        catalog = SimpleNamespace(unit=lambda _: unit, _group=lambda _: {"id": "g", "bindings": {}},
                                  invoke=lambda *a, **k: invalid)
        router = SimpleNamespace(lock=threading.RLock(), affinity={}, live_instances=lambda _: [],
                                 release_task=Mock())
        store = SimpleNamespace(save=Mock())
        platform = SimpleNamespace(execution=SimpleNamespace(workload=None), capabilities=catalog,
                                   deployments=router, store=store)
        result = diagnose(platform, {"unit_id": "u", "input": INPUT})
        self.assertEqual(result["output"], invalid["output"])
        self.assertEqual(result["diagnostic"]["status"], "FAILED")
        self.assertIn("no_claims", result["diagnostic"]["verification"]["reason"])
        self.assertEqual(store.save.call_args.args[2]["result"], invalid)

    def test_all_declared_examples_gate_one_unit_even_with_concurrent_success(self):
        unit = {"id": "u", "entry_id": "check", "capability": FACT,
                "generation_verification": {"status": "SUCCEEDED"}}
        catalog = object.__new__(CapabilityCatalog)
        catalog.unit = lambda _: unit
        catalog.lock = threading.RLock()
        catalog._verification_batches = set()
        catalog.store = SimpleNamespace(save=Mock())
        catalog.router = SimpleNamespace(live_instances=lambda _: [])
        calls = []
        failed = [True]

        def invoke(identity, arguments, **kwargs):
            calls.append(arguments)
            self.assertFalse(kwargs["auto_verify"])
            # A separate successful diagnostic during the batch must not open it.
            catalog.record_verification(identity, "SUCCEEDED")
            self.assertNotEqual(unit["generation_verification"]["status"], "SUCCEEDED")
            if len(arguments["references"]) == 1 and failed[0]:
                return {"output": {"status": "unavailable", "claims": [], "claim_count": 0}}
            labels = ["Entailment"] * len(arguments["references"])
            return {"output": {"status": "ok", "claims": [{"claim_text": "claim", "label": "Entailment",
                                                             "labels_by_reference": labels}], "claim_count": 1}}

        catalog.invoke = invoke
        jobs = object.__new__(GenerationJobs)
        jobs.platform = SimpleNamespace(capabilities=catalog)
        jobs.store = SimpleNamespace(get=lambda *a: {})
        jobs._verification_pending = {}
        jobs._check = lambda _: None
        jobs._save = Mock()
        jobs._await = lambda fn, _: fn()
        jobs.closed = threading.Event()
        manifest = {"capabilities": [dict(FACT, id="check", effects=[])]}
        samples = [{"capability": "check", "input": {"references": refs}, "provenance": []}
                   for refs in (["one", "two"], ["one"])]
        installation = {"group_id": "g", "capabilities": ["u"]}
        job = {"id": "job", "gaps": []}
        with self.assertRaises(GenerationRepairable):
            jobs._verify_calls(manifest, {"examples": samples}, installation, job,
                               threading.Event(), SimpleNamespace(request={}))
        self.assertEqual([len(x["references"]) for x in calls], [2, 1])
        self.assertEqual([row["status"] for row in job["first_call"]["calls"]], ["SUCCEEDED", "FAILED"])
        self.assertEqual(unit["generation_verification"]["status"], "FAILED")
        failed[0] = False
        calls.clear()
        jobs._verify_calls(manifest, {"examples": samples}, installation, job,
                           threading.Event(), SimpleNamespace(request={}))
        self.assertEqual([len(x["references"]) for x in calls], [2, 1])
        self.assertEqual(job["first_call"]["status"], "SUCCEEDED")
        self.assertEqual(unit["generation_verification"]["status"], "SUCCEEDED")
