"""Evaluate every API-proposed CRAG binding under a frozen real SDK profile.

The evaluator consumes an existing package and experiment record unchanged. It
does not propose boundaries, rebuild candidates, contact APIs, or grant admission.
"""
import argparse
import asyncio
import functools
import importlib.metadata
import importlib.util
import json
import platform
import tempfile
from collections import Counter
from pathlib import Path

from seampact.evaluation import PackageRejected, canonical_hash, inspect_package, match_discovery_record
from seampact import __version__
from seampact.inspection import digest
from seampact.handoff import install_method_proxy
from seampact.runtime import Coordinator, OwnerRuntime, ProtocolError
from seampact.store import Store
from probe_crag_sdk import (PATH, FROZEN_SHA256, ControlledEndpoints, compile_workflow,
                            event_copy_semantics, sdk_namespace, step_metadata)


STEPS = {"ingest", "prepare_for_retrieval", "retrieve", "eval_relevance",
         "extract_relevant_texts", "transform_query", "query_result"}
PROFILES = [
    dict(name="single-empty", profile="single-step", texts=[], answers=[]),
    dict(name="single-changing-context", profile="single-step", texts=["first", "second", "third"],
         answers=[" YES ", " NO ", " YES "], changing_model=True),
    dict(name="single-original-error", profile="single-step", texts=["first", "second", "third"],
         answers=[" YES ", " NO ", " YES "], fail_at=2),
    dict(name="single-native-cancellation", profile="single-step", texts=["first"], answers=[" YES "], cancel=True),
    dict(name="workflow-all-relevant", profile="query-workflow", texts=["first", "second"], answers=[" YES ", " YES "]),
    dict(name="workflow-mixed-search", profile="query-workflow", texts=["first", "second", "third"],
         answers=[" YES ", " NO ", " YES "], changing_model=True),
    dict(name="workflow-empty", profile="query-workflow", texts=[], answers=[]),
    dict(name="workflow-original-error", profile="query-workflow", texts=["first", "second", "third"],
         answers=[" YES ", " NO ", " YES "], fail_at=2),
    dict(name="workflow-ingest", profile="ingest-workflow", texts=[], answers=[]),
]


def profile_bindings(package):
    bindings = package.manifest["candidate"]["boundaries"]
    for boundary in bindings:
        symbol = boundary.get("symbol", "")
        if boundary["path"] != PATH or boundary.get("unit", "file") != "cell:10" or not symbol.startswith("CorrectiveRAGWorkflow."):
            raise PackageRejected("profile cannot map every submitted boundary")
        if boundary["mode"] == "R":
            if symbol != "CorrectiveRAGWorkflow.eval_relevance" or boundary.get("loop_index", 0) != 0:
                raise PackageRejected("profile has no oracle for this R boundary")
        elif boundary["mode"] not in {"W", "H"} or isinstance(boundary.get("native_wiring"), dict) or symbol.split(".")[-1] not in STEPS:
            raise PackageRejected("profile cannot bind every submitted callable")
    return bindings


def observe_native_steps(owner, bindings, counter):
    """Transparent reference observers, without permits or altered algorithms."""
    for boundary in bindings:
        if boundary["mode"] == "R":
            continue
        name = boundary["symbol"].split(".")[-1]
        original = getattr(owner, name)
        def observed(original, identity):
            @functools.wraps(original)
            async def call(*args, **kwargs):
                counter[identity] += 1
                return await original(*args, **kwargs)
            return call
        install_method_proxy(owner, name, observed(original, boundary["id"]))


def install_all(runtime, owner, adapter, bindings, bypass):
    owner_id = "crag-native-sdk-owner"
    originals = {b["id"]: getattr(owner, b["symbol"].split(".")[-1])
                 for b in bindings if b["mode"] != "R"}
    proxies = adapter.attach(runtime, originals, owner_id)
    gate_to_boundary = {}
    for boundary in bindings:
        if boundary["mode"] == "R":
            continue
        identity = boundary["id"]
        if identity in bypass:
            continue  # Deliberate negative control; never the successful path.
        runtime.call("register", identity, owner_id)
        install_method_proxy(owner, boundary["symbol"].split(".")[-1], proxies[identity])
        gate_to_boundary[identity] = identity
    for hook in adapter.install_loop_handoffs(runtime, owner, PATH, "cell:10", owner_id):
        gate_to_boundary[hook["gate"]] = hook["id"]
    return gate_to_boundary


async def run_case(events_source, source, root, case, sdk, bindings, adapter=None, bypass=()):
    control = ControlledEndpoints(sdk, case)
    namespace, owner = compile_workflow(events_source, source, sdk, control)
    counts = Counter()
    journal = Store(root / "ledger.db")
    runtime = OwnerRuntime(Coordinator(journal, automatic=False))
    gates, held = {}, []
    try:
        before_metadata = step_metadata(owner)
        before_registry = sorted(owner._get_steps())
        if adapter:
            observe_native_steps(owner, bindings, counts)
            gates = install_all(runtime, owner, adapter, bindings, set(bypass))
        else:
            observe_native_steps(owner, bindings, counts)
        metadata = step_metadata(owner)
        assert metadata == before_metadata, "SDK method signatures or step metadata changed at installation"
        registry = sorted(owner._get_steps())
        assert registry == before_registry, "native SDK registration projection changed"
        context = sdk["Context"](owner)
        control.ctx = context

        async def controller():
            try:
                if case["profile"] == "single-step":
                    await context.store.set("query_str", "controlled question")
                    await context.store.set("llm", control.Model(control))
                    result = await owner.eval_relevance(context, namespace["RetrieveEvent"](retrieved_nodes=control.nodes()))
                    saved = await context.store.get("relevancy_results")
                    return dict(outcome="RETURN", result=list(result.relevant_results), saved=list(saved),
                                event_list_aliases_store=result.relevant_results is saved)
                if case["profile"] == "query-workflow":
                    result = await owner.run(ctx=context, query_str="controlled question", index=control.index,
                        retriever_kwargs={"similarity_top_k": 3}, tavily_ai_apikey="controlled-not-a-real-key")
                    return dict(outcome="RETURN", result=result,
                                saved=list(await context.store.get("relevancy_results")),
                                stored_query=await context.store.get("query_str"))
                result = await owner.run(ctx=context, documents=[sdk["Document"](text="controlled ingest")])
                return dict(outcome="RETURN", result_is_controlled_index=result is control.index)
            except (ValueError, asyncio.CancelledError) as error:
                return dict(outcome=type(error).__name__, message=str(error),
                            saved=await context.store.get("relevancy_results", default=None))

        task = asyncio.create_task(controller())
        try:
            for _ in range(6000):
                state = runtime.snapshot()
                waiting = [c for c in state["calls"].values() if c["physical"] == "ENABLED"]
                if waiting and state["used"] == 0:
                    # No permitted native body is active. External endpoint
                    # trace must remain unchanged while the next permit is held.
                    previous = list(control.trace)
                    await asyncio.sleep(.005)
                    if not bypass:
                        assert previous == control.trace, "endpoint progressed before permit"
                    call = waiting[0]
                    try:
                        runtime.call("authorize", call["id"])
                    except ProtocolError:
                        latest = runtime.snapshot()["calls"][call["id"]]
                        if latest["logical"] != "CANCELLED" or latest["starts"] != 0:
                            raise
                    else:
                        held.append(call["id"])
                if task.done():
                    break
                await asyncio.sleep(.002)
            if not task.done():
                raise AssertionError("SDK did not complete through installed bindings")
            outcome = await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        # The SDK can publish its terminal result before its worker cleanup
        # completes. Await gate cancellation/retirement before closing the ledger.
        for _ in range(1000):
            state = runtime.snapshot()
            if all(c["physical"] == "RETIRED" for c in state["calls"].values()):
                await asyncio.sleep(.01)
                break
            await asyncio.sleep(.002)
        state = runtime.snapshot()
        calls = list(state["calls"].values())
        entered = dict(counts)
        if adapter:
            counts = Counter(gates[c["gate"]] for c in calls)
        else:
            for boundary in bindings:
                if boundary["mode"] == "R":
                    counts[boundary["id"]] = control.relevance_calls
        return dict(outcome=outcome, endpoint_trace=control.trace, step_metadata=metadata,
                    sdk_registered_steps=registry,
                    event_semantics=event_copy_semantics(namespace["RelevanceEvalEvent"]),
                    boundary_calls={b["id"]: counts[b["id"]] for b in bindings},
                    body_entries={b["id"]: (control.relevance_calls if b["mode"] == "R" else entered.get(b["id"], 0)) for b in bindings},
                    calls=calls, withheld_occurrences=len(held), used=state["used"])
    finally:
        runtime.close()
        journal.close()


def compare(native, adapted, allowable_noop=()):
    for key in ("outcome", "endpoint_trace", "step_metadata", "sdk_registered_steps", "event_semantics"):
        if native[key] != adapted[key]:
            raise AssertionError("paired " + key + " mismatch")
    assert adapted["withheld_occurrences"] == sum(c["permit"] is not None for c in adapted["calls"]), "unobserved admission"
    assert all((c["starts"] == 1 and c["physical"] == "RETIRED") or
               (c["starts"] == 0 and c["physical"] == "RETIRED" and c["logical"] == "CANCELLED")
               for c in adapted["calls"]), "non-single or unretired call"
    assert adapted["used"] == 0, "execution slot was not retired"
    for identity, entered in native["body_entries"].items():
        actual = adapted["body_entries"][identity]
        if actual != entered:
            assert identity in allowable_noop and actual == 0, "native body coverage changed: " + identity
            # The exception is only for a pinned source guard returning None
            # before any endpoint/state effect, canceled by the original SDK.
            relevant = [c for c in adapted["calls"] if c["gate"] == identity]
            assert len(relevant) == 1 and relevant[0]["logical"] == "CANCELLED", "cleanup did not cancel the no-op invocation"
    return [identity for identity, count in native["boundary_calls"].items()
            if count != adapted["boundary_calls"].get(identity)]


async def evaluate(package, record, scratch):
    provenance = match_discovery_record(package, record)
    if provenance["source_id"] != "crag-text":
        raise PackageRejected("CRAG experiment record required")
    bindings = profile_bindings(package)
    original_bytes = package.payloads["source/" + PATH]
    if digest(original_bytes) != FROZEN_SHA256:
        raise PackageRejected("native profile notebook identity mismatch")
    original_book = json.loads(original_bytes)
    patched_book = json.loads(package.payloads.get("patched/" + PATH, original_bytes))
    events = "".join(original_book["cells"][8]["source"])
    original = "".join(original_book["cells"][10]["source"])
    patched = "".join(patched_book["cells"][10]["source"])
    folder = scratch / "package"
    folder.mkdir()
    # Only engine-owned adapter and checked manifest are imported. Original
    # code is handled separately by the known frozen native profile above.
    for name in ("adapter.py", "manifest.json"):
        (folder / name).write_bytes(package.payloads[name] if name in package.payloads else
                                  json.dumps(package.manifest, ensure_ascii=False).encode())
    spec = importlib.util.spec_from_file_location("evaluated_crag_adapter", folder / "adapter.py")
    adapter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(adapter)
    sdk = sdk_namespace()
    cases, reference, coverage = [], {}, Counter()
    for case in PROFILES:
        case_root = scratch / case["name"]
        for kind in ("native", "adapted"):
            (case_root / kind).mkdir(parents=True)
        native = await run_case(events, original, case_root / "native", case, sdk, bindings)
        adapted = await run_case(events, patched, case_root / "adapted", case, sdk, bindings, adapter)
        noop_symbols = {"CorrectiveRAGWorkflow.ingest"} if case["profile"] == "query-workflow" else \
                       {"CorrectiveRAGWorkflow.prepare_for_retrieval"} if case["profile"] == "ingest-workflow" else set()
        allowable_noop = {b["id"] for b in bindings if b["symbol"] in noop_symbols}
        missing = compare(native, adapted, allowable_noop)
        assert not missing, "native activation bypassed binding: " + ",".join(missing)
        coverage.update(adapted["body_entries"])
        reference[case["name"]] = native
        cases.append(dict(name=case["name"], profile=case["profile"], status="PASS", native=native, adapted=adapted,
                          allowed_terminal_noop=sorted(allowable_noop),
                          terminal_cleanup_differences=[identity for identity in native["body_entries"]
                              if native["body_entries"][identity] != adapted["body_entries"][identity]]))
    assert all(coverage[b["id"]] > 0 for b in bindings), "unexercised submitted boundary"
    target = next((b["id"] for b in bindings if b["symbol"] == "CorrectiveRAGWorkflow.prepare_for_retrieval"), None)
    assert target, "profile requires a preparation binding for the bypass control"
    case = next(c for c in PROFILES if c["name"] == "workflow-all-relevant")
    bypass_root = scratch / "negative-control"
    bypass_root.mkdir()
    bypass = await run_case(events, patched, bypass_root, case, sdk, bindings, adapter, bypass=(target,))
    allowable_noop = {b["id"] for b in bindings if b["symbol"] == "CorrectiveRAGWorkflow.ingest"}
    missing = compare(reference[case["name"]], bypass, allowable_noop)
    assert missing == [target], "deliberately omitted hook was not detected"
    return dict(schema="api-package-profile/1", status="PASS", profile="crag-real-sdk-controlled-endpoints/2",
                package=package.receipt(), provenance=provenance, cases=cases,
                coverage={b["id"]: coverage[b["id"]] for b in bindings},
                profiles_sha256=canonical_hash(PROFILES),
                observation_contract=dict(logical_invocation_coverage="exact",
                    normal_body_entry_coverage="exact",
                    terminal_cleanup_exception="SDK-canceled no-op StartEvent branch, pinned source returning None before effects",
                    timing="queue wait is separate; not a timing-equivalence claim"),
                negative_control=dict(status="BYPASS_DETECTED", omitted_binding=target,
                                      output_and_endpoint_trace_equal=True, missing_occurrences=missing),
                sdk={name: importlib.metadata.version(name) for name in ("llama-index-core", "llama-index-workflows")},
                python=platform.python_version(), controlled_endpoints=["OpenAI", "TavilyToolSpec", "VectorStoreIndex", "SummaryIndex"],
                complete_dependency_closure="UNKNOWN", full_source_execution="NOT_RUN",
                live_model_quality="NOT_RUN", parallel_admitted=False, production_admitted=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("record", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    try:
        record = json.loads(args.record.read_text(encoding="utf-8"))
        package = inspect_package(args.archive, record["package"]["sha256"])
        with tempfile.TemporaryDirectory(dir=args.output) as temporary:
            report = asyncio.run(evaluate(package, record, Path(temporary)))
    except Exception as error:
        report = dict(schema="api-package-profile/1", status="FAIL", error_type=type(error).__name__,
                      error=str(error), production_admitted=False, parallel_admitted=False,
                      live_model_quality="NOT_RUN")
    report["core_version"] = __version__
    report["evaluator_sha256"] = digest(Path(__file__).read_bytes())
    report["core_source_sha256"] = {name: digest((Path(__file__).resolve().parents[1] / "src/seampact" / name).read_bytes())
                                   for name in ("evaluation.py", "handoff.py", "runtime.py")}
    (args.output / "crag-api-package-result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(dict(status=report["status"], cases=len(report.get("cases", [])),
                         coverage=report.get("coverage"), error=report.get("error")), ensure_ascii=False))
    raise SystemExit(0 if report["status"] == "PASS" else 1)


if __name__ == "__main__":
    main()
