"""Frozen CRAG code + real Workflow SDK + generated finite R handoff.

This probe never runs arbitrary notebook cells, downloads data, or calls a
model/search endpoint. Original Event definitions, prompt templates, complete
workflow methods, decorators and annotations execute under the installed SDK.
Index construction/retrieval, LLM, Tavily and summary query are controlled
external endpoints. These are mechanism checks, not autonomous discovery,
original retrieval quality, complete dependency closure, or production admission.
"""

import argparse
import ast
import asyncio
import copy
import hashlib
import importlib.metadata
import importlib.util
import inspect
import json
import platform
import shutil
import tempfile
import zipfile
from pathlib import Path

from seampact.build import build_package
from seampact.inspection import Inspector
from seampact.runtime import Coordinator, OwnerRuntime
from seampact.store import Store

PATH = "docs/examples/workflow/corrective_rag_pack.ipynb"
SYMBOL = "CorrectiveRAGWorkflow.eval_relevance"
FROZEN_REVISION = "6dd2f3cdd9ce7ff927ea8d46ccee08875bb00f95"
FROZEN_SHA256 = "287398bf70bd31513627a8ea83a3ffb37e25e60b882e690efa88b51e21361dfc"


def sdk_namespace():
    """Import only optional CPU SDK dependencies; --help works without them."""
    from llama_index.core import Document, PromptTemplate
    from llama_index.core.base.base_retriever import BaseRetriever
    from llama_index.core.base.llms.types import CompletionResponse
    from llama_index.core.schema import NodeWithScore, TextNode
    from llama_index.core.workflow import Context, Event, StartEvent, StopEvent, Workflow, step

    return dict(Document=Document, PromptTemplate=PromptTemplate,
                BaseRetriever=BaseRetriever, CompletionResponse=CompletionResponse,
                NodeWithScore=NodeWithScore, TextNode=TextNode, Context=Context,
                Event=Event, StartEvent=StartEvent, StopEvent=StopEvent,
                Workflow=Workflow, step=step)


class ControlledEndpoints:
    """Only original external endpoints are replaced, never Context or @step."""
    def __init__(self, sdk, case):
        self.sdk, self.case = sdk, case
        self.trace = []
        self.ctx = None
        self.relevance_calls = 0
        self.index = self.Index(self)

    class Model:
        def __init__(self, control, generation=1):
            self.control, self.generation = control, generation

        def __deepcopy__(self, memo):
            # Preserve the deliberately shared endpoint handle if the SDK store
            # copies values. This is a test provider property, not an SDK claim.
            return self

        async def acomplete(self, prompt):
            control = self.control
            is_relevance = prompt.startswith("As a grader,")
            control.trace.append(["acomplete", self.generation, prompt])
            if is_relevance:
                control.relevance_calls += 1
                position = control.relevance_calls
                if control.case.get("fail_at") == position:
                    raise ValueError("controlled original endpoint failure")
                if control.case.get("cancel"):
                    raise asyncio.CancelledError()
                response = control.case["answers"][position - 1]
                if control.case.get("changing_model"):
                    await control.ctx.store.set("llm", self.__class__(control, self.generation + 1))
            else:
                response = "controlled transformed query"
            return control.sdk["CompletionResponse"](text=response)

    class Index:
        def __init__(self, control):
            self.control = control

        def __deepcopy__(self, memo):
            return self

        def as_retriever(self, **kwargs):
            self.control.trace.append(["as_retriever", kwargs])
            return self

        def retrieve(self, query):
            self.control.trace.append(["retrieve", query])
            return self.control.nodes()

    class Search:
        def __init__(self, control):
            self.control = control

        def __deepcopy__(self, memo):
            return self

        def search(self, query, max_results):
            self.control.trace.append(["search", query, max_results])
            return [self.control.sdk["Document"](text="controlled web context")]

    class Summary:
        def __init__(self, control, text):
            self.control, self.text = control, text

        def as_query_engine(self):
            self.control.trace.append(["summary_as_query_engine"])
            return self

        def query(self, query):
            self.control.trace.append(["summary_query", query])
            return "summary: " + self.text

    def nodes(self):
        return [self.sdk["NodeWithScore"](
            node=self.sdk["TextNode"](text=text, id_=f"controlled-node-{index}"), score=1.0)
            for index, text in enumerate(self.case["texts"])]

    def globals(self):
        control = self

        def openai(**kwargs):
            control.trace.append(["OpenAI", kwargs])
            return control.Model(control)

        def tavily(**kwargs):
            # The fixture has no real credential; recording the constructor
            # confirms the original prepare method supplied its input unchanged.
            assert kwargs == {"api_key": "controlled-not-a-real-key"}
            control.trace.append(["TavilyToolSpec", {"api_key": "<controlled-fixture>"}])
            return control.Search(control)

        class VectorStoreIndex:
            @staticmethod
            def from_documents(documents):
                control.trace.append(["vector_from_documents", [d.text for d in documents]])
                return control.index

        class SummaryIndex:
            @staticmethod
            def from_documents(documents):
                texts = [d.text for d in documents]
                control.trace.append(["summary_from_documents", texts])
                return control.Summary(control, "\n".join(texts))

        return dict(OpenAI=openai, TavilyToolSpec=tavily,
                    VectorStoreIndex=VectorStoreIndex, SummaryIndex=SummaryIndex)


def compile_workflow(events_source, workflow_source, namespace, control, timeout=15):
    """Only skip imports; retain every original executable definition verbatim."""
    namespace = dict(namespace, **control.globals())
    for filename, source in (("<frozen-crag-events>", events_source),
                             ("<frozen-crag-workflow>", workflow_source)):
        tree = ast.parse(source)
        tree.body = [n for n in tree.body if not isinstance(n, (ast.Import, ast.ImportFrom))]
        exec(compile(tree, filename, "exec"), namespace)
    return namespace, namespace["CorrectiveRAGWorkflow"](timeout=timeout, verbose=False)


def step_metadata(owner):
    result = {}
    for name in ("ingest", "prepare_for_retrieval", "retrieve", "eval_relevance",
                 "extract_relevant_texts", "transform_query", "query_result"):
        method = getattr(owner, name)
        metadata = getattr(method, "_step_config", None)
        if metadata is None:
            raise AssertionError("original @step metadata absent: " + name)
        result[name] = dict(signature=str(inspect.signature(method)),
                           accepted_events=[n.__name__ for n in metadata.accepted_events],
                           return_types=[n.__name__ for n in metadata.return_types],
                           context_parameter=metadata.context_parameter)
    return result


def event_copy_semantics(event_class):
    values = ["yes"]
    event = event_class(relevant_results=values)
    aliases = event.relevant_results is values
    values.append("no")
    return dict(list_argument_alias=aliases,
                input_append_observed_by_event=event.relevant_results == values,
                event_values=list(event.relevant_results))


async def run(events_source, source, root, case, sdk, adapter=None):
    control = ControlledEndpoints(sdk, case)
    namespace, owner = compile_workflow(events_source, source, sdk, control)
    ctx = sdk["Context"](owner)
    control.ctx = ctx
    journal = Store(root / "ledger.db")
    runtime = OwnerRuntime(Coordinator(journal, automatic=False))
    bindings, held = [], 0
    metadata = step_metadata(owner)
    event_semantics = event_copy_semantics(namespace["RelevanceEvalEvent"])
    try:
        if adapter is not None:
            bindings = adapter.install_loop_handoffs(runtime, owner, PATH, "cell:10")

        async def controller():
            try:
                profile = case["profile"]
                if profile == "single-step":
                    await ctx.store.set("query_str", "controlled question")
                    await ctx.store.set("llm", control.Model(control))
                    result = await owner.eval_relevance(ctx, namespace["RetrieveEvent"](
                        retrieved_nodes=control.nodes()))
                    saved = await ctx.store.get("relevancy_results")
                    return dict(outcome="RETURN", result=list(result.relevant_results),
                                saved=list(saved), result_matches_store=result.relevant_results == saved,
                                event_list_aliases_store=result.relevant_results is saved)
                if profile == "query-workflow":
                    result = await owner.run(ctx=ctx, query_str="controlled question",
                        index=control.index, retriever_kwargs={"similarity_top_k": 3},
                        tavily_ai_apikey="controlled-not-a-real-key")
                    saved = await ctx.store.get("relevancy_results")
                    return dict(outcome="RETURN", result=result, saved=list(saved),
                                stored_query=await ctx.store.get("query_str"))
                assert profile == "ingest-workflow"
                result = await owner.run(ctx=ctx, documents=[sdk["Document"](text="controlled ingest")])
                return dict(outcome="RETURN", result_is_controlled_index=result is control.index)
            except BaseException as error:
                if not isinstance(error, (ValueError, asyncio.CancelledError)):
                    raise
                return dict(outcome=type(error).__name__, message=str(error),
                            saved=await ctx.store.get("relevancy_results", default=None))

        task = asyncio.create_task(controller())
        try:
            for _ in range(1500):
                enabled = [c for c in runtime.snapshot()["calls"].values() if c["physical"] == "ENABLED"]
                if enabled:
                    assert control.relevance_calls == held
                    await asyncio.sleep(.005)
                    assert control.relevance_calls == held
                    held += len(enabled)
                    for call in enabled:
                        runtime.call("authorize", call["id"])
                if task.done():
                    break
                await asyncio.sleep(.002)
            if not task.done():
                raise AssertionError("SDK workflow did not continue after the generated handoff")
            outcome = await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        state = runtime.snapshot()
        return dict(outcome=outcome, endpoint_trace=control.trace,
                    step_metadata=metadata, event_constructor_semantics=event_semantics,
                    calls=list(state["calls"].values()),
                    withheld_occurrences=held, bindings=bindings, used=state["used"])
    finally:
        runtime.close()
        journal.close()


async def probe(root, output):
    sdk = sdk_namespace()
    inspector = Inspector.freeze(root, output / "snapshots", "crag-sdk-mechanism", [PATH],
        SYMBOL, "original Event/step/Context continuation and finite sequential R handoff; controlled external endpoints")
    raw = inspector.data(PATH)
    assert hashlib.sha256(raw).hexdigest() == FROZEN_SHA256, "frozen notebook bytes changed"
    notebook = json.loads(raw)
    events_source = "".join(notebook["cells"][8]["source"])
    original = "".join(notebook["cells"][10]["source"])
    candidate = dict(boundaries=[dict(id="relevance-item", path=PATH, unit="cell:10",
        symbol=SYMBOL, mode="R", loop_index=0)],
        analysis="manually selected SDK mechanism probe; not autonomous partition discovery")
    package = build_package(inspector, candidate, output, "crag-sdk-sequential-draft")
    with zipfile.ZipFile(output / package["archive"]) as archive:
        archive.extractall(output / "package")
    spec = importlib.util.spec_from_file_location("crag_sdk_generated_adapter", output / "package/adapter.py")
    adapter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(adapter)
    patched_book = json.loads((output / "package/patched" / PATH).read_text(encoding="utf-8"))
    patched = "".join(patched_book["cells"][10]["source"])
    profiles = [
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
    cases = []
    for case in profiles:
        case_root = output / case["name"]
        for kind in ("native", "adapted"):
            (case_root / kind).mkdir(parents=True)
        native = await run(events_source, original, case_root / "native", case, sdk)
        adapted = await run(events_source, patched, case_root / "adapted", case, sdk, adapter)
        assert native["outcome"] == adapted["outcome"], case["name"] + " output mismatch"
        assert native["endpoint_trace"] == adapted["endpoint_trace"], case["name"] + " endpoint order mismatch"
        assert native["step_metadata"] == adapted["step_metadata"], case["name"] + " @step interface mismatch"
        assert native["event_constructor_semantics"] == adapted["event_constructor_semantics"], case["name"] + " event copy mismatch"
        assert adapted["withheld_occurrences"] == len(adapted["calls"])
        assert all(c["starts"] == 1 and c["physical"] == "RETIRED" for c in adapted["calls"])
        assert adapted["used"] == 0
        expected_calls = 0 if case["profile"] == "ingest-workflow" else len(case["texts"])
        if case.get("fail_at"):
            expected_calls = case["fail_at"]
        if case.get("cancel"):
            expected_calls = 1
        assert len(adapted["calls"]) == expected_calls, case["name"] + " activation bypassed handoff"
        if case.get("changing_model"):
            generations = [event[1] for event in adapted["endpoint_trace"] if event[0] == "acomplete"]
            assert generations[:len(case["texts"])] == list(range(1, len(case["texts"]) + 1))
        cases.append(dict(name=case["name"], profile=case["profile"], status="PASS", native=native, adapted=adapted))
    return dict(schema="crag-sdk-probe/1", status="PASS", source=dict(path=PATH,
        frozen_revision=FROZEN_REVISION, notebook_sha256=FROZEN_SHA256,
        event_unit="cell:8", workflow_unit="cell:10", original_imports="explicit SDK/test-endpoint namespace"),
        sdk={name: importlib.metadata.version(name) for name in ("llama-index-core", "llama-index-workflows")},
        python=platform.python_version(), package=dict(archive=package["archive"],
            sha256=package["sha256"], generated_adapter_used=True),
        transformations=package["transformations"], cases=cases,
        profiles=dict(single_step="original decorated eval_relevance with real Context/store/Event/Node types",
            query_workflow="complete original query control flow through real Workflow SDK; controlled external endpoints",
            ingest_workflow="original ingest branch through real Workflow SDK; controlled index endpoint"),
        original_algorithm="all workflow method bodies retained except verified sequential loop-body extraction",
        controlled_endpoints=["OpenAI", "TavilyToolSpec", "VectorStoreIndex", "SummaryIndex"],
        partition_origin="manually selected mechanism probe; not autonomous discovery evidence",
        sdk_workflow="PASS_CONTROLLED_PROFILE", live_model_quality="NOT_RUN", original_retrieval_quality="NOT_RUN",
        complete_dependency_closure="UNKNOWN", parallel_admitted=False, production_admitted=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_root")
    parser.add_argument("output")
    args = parser.parse_args()
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(dir=output) as temporary:
            scratch = Path(temporary).resolve()
            assert scratch != output and scratch.is_relative_to(output)
            report = asyncio.run(probe(Path(args.source_root).resolve(), scratch))
            archive = scratch / report["package"]["archive"]
            saved_archive = output / archive.name
            shutil.copyfile(archive, saved_archive)
            digest = hashlib.sha256(saved_archive.read_bytes()).hexdigest()
            assert digest == report["package"]["sha256"], "preserved package hash mismatch"
            (output / (archive.name + ".sha256")).write_text(
                digest + "  " + archive.name + "\n", encoding="utf-8")
    except Exception as error:
        # A failed rerun must not leave an earlier PASS as its current result.
        failure = dict(schema="crag-sdk-probe/1", status="FAIL", error_type=type(error).__name__,
            error=str(error), sdk_workflow="NOT_COMPLETED", live_model_quality="NOT_RUN",
            parallel_admitted=False, production_admitted=False)
        (output / "crag-sdk-result.json").write_text(json.dumps(failure, ensure_ascii=False, indent=2), encoding="utf-8")
        raise
    (output / "crag-sdk-result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(dict(status=report["status"], cases=len(report["cases"]),
        sdk_workflow=report["sdk_workflow"], live_model_quality="NOT_RUN", parallel_admitted=False)))


if __name__ == "__main__":
    main()
