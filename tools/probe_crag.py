"""Frozen CRAG method + generated R package + controlled Context/LLM probes.

The original notebook is not executed. This intentionally tests an exact method
fragment with synthetic dependencies, not the complete LlamaIndex SDK workflow.
"""
import argparse
import ast
import asyncio
import copy
import hashlib
import importlib.util
import json
import shutil
import tempfile
import types
import zipfile
from pathlib import Path

from seampact.build import build_package
from seampact.inspection import Inspector
from seampact.lowering import locate
from seampact.runtime import Coordinator,OwnerRuntime
from seampact.store import Store

PATH="docs/examples/workflow/corrective_rag_pack.ipynb"
SYMBOL="CorrectiveRAGWorkflow.eval_relevance"


class ControlledContext:
    def __init__(self,fail=None,cancel=False):
        self.store=self
        self.trace=[]
        self.models=0
        self.saved=None
        self.fail=fail
        self.cancel=cancel
    async def get(self,key):
        self.trace.append(["context_get",key])
        if key=="query_str":return "controlled question"
        assert key=="llm"
        self.models+=1
        version=self.models
        ctx=self
        class Model:
            async def acomplete(self,prompt):
                ctx.trace.append(["complete",version,prompt])
                if ctx.fail==version:raise ValueError("original model failure")
                if ctx.cancel:raise asyncio.CancelledError()
                return types.SimpleNamespace(text=" YES " if version%2 else " NO ")
        return Model()
    async def set(self,key,value):
        assert key=="relevancy_results"
        self.saved=value
        self.trace.append(["context_set",key,list(value)])


class Nodes:
    def __init__(self,ctx,texts,fail_at=None):
        self.ctx,self.texts,self.fail_at=ctx,texts,fail_at
        self.index=0
    def __iter__(self):
        self.ctx.trace.append(["iterator_enter"])
        return self
    def __next__(self):
        index=self.index
        self.ctx.trace.append(["iterator_next",index])
        if self.fail_at==index:raise RuntimeError("original iterator failure")
        if index==len(self.texts):raise StopIteration
        self.index+=1
        return types.SimpleNamespace(text=self.texts[index])


def fragment_owner(source):
    """Keep exact executable method bodies; remove imports/decorators/annotations."""
    cls,method=locate(ast.parse(source),SYMBOL)
    methods=[copy.deepcopy(method)]+[copy.deepcopy(n) for n in cls.body
                if isinstance(n,ast.AsyncFunctionDef) and n.name.startswith("_seampact_body_")]
    for node in methods:
        node.decorator_list=[]
        node.returns=None
        for argument in node.args.args:argument.annotation=None
    isolated=ast.Module(body=[ast.ClassDef(name=cls.name,bases=[],keywords=[],body=methods,decorator_list=[])],type_ignores=[])
    namespace={"DEFAULT_RELEVANCY_PROMPT_TEMPLATE":types.SimpleNamespace(
        format=lambda **kw:dict(kw)),"RelevanceEvalEvent":lambda **kw:types.SimpleNamespace(**kw)}
    exec(compile(ast.fix_missing_locations(isolated),"<frozen-crag-method-fragment>","exec"),namespace)
    return namespace[cls.name]()


async def run(source,root,case,adapter=None):
    owner=fragment_owner(source)
    ctx=ControlledContext(fail=case.get("fail"),cancel=case.get("cancel",False))
    ev=types.SimpleNamespace(retrieved_nodes=Nodes(ctx,case["texts"],case.get("iterator_fail")))
    store=Store(root/"ledger.db")
    runtime=OwnerRuntime(Coordinator(store,automatic=False))
    bindings=[]
    held=0
    try:
        if adapter:
            bindings=adapter.install_loop_handoffs(runtime,owner,PATH,"cell:10")
        async def controller():
            try:
                result=await owner.eval_relevance(ctx,ev)
                assert result.relevant_results is ctx.saved
                return dict(outcome="RETURN",values=list(result.relevant_results),alias_preserved=True)
            except BaseException as error:
                if not isinstance(error,(ValueError,RuntimeError,asyncio.CancelledError)):raise
                return dict(outcome=type(error).__name__,message=str(error),context_written=ctx.saved is not None)
        task=asyncio.create_task(controller())
        for _ in range(400):
            calls=runtime.snapshot()["calls"].values()
            enabled=[c for c in calls if c["physical"]=="ENABLED"]
            if enabled:
                # An iterator item has been selected; its native Context/LLM
                # body must not enter until the engine grants this occurrence.
                assert ctx.models==held
                await asyncio.sleep(.005)
                assert ctx.models==held
                held+=len(enabled)
                for call in enabled:runtime.call("authorize",call["id"])
            if task.done():break
            await asyncio.sleep(.002)
        if not task.done():
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)
            raise AssertionError("CRAG fragment continuation timed out")
        outcome=await task
        state=runtime.snapshot()
        return dict(outcome=outcome,trace=ctx.trace,calls=list(state["calls"].values()),
                    withheld_occurrences=held,bindings=bindings,used=state["used"])
    finally:
        runtime.close()
        store.close()


async def probe(root,output):
    inspector=Inspector.freeze(root,output/"snapshots","crag-mechanism",[PATH],SYMBOL,
        "projected result values, mutable result alias, ordered Context/LLM/iterator effects, failure/cancel continuation")
    candidate=dict(boundaries=[dict(id="relevance-item",path=PATH,unit="cell:10",symbol=SYMBOL,mode="R",loop_index=0)],
        analysis="manually selected finite-rule mechanism probe; not an autonomous partition discovery result")
    package=build_package(inspector,candidate,output,"crag-sequential-draft")
    with zipfile.ZipFile(output/package["archive"]) as archive:
        archive.extractall(output/"package")
    spec=importlib.util.spec_from_file_location("crag_generated_adapter",output/"package/adapter.py")
    adapter=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(adapter)
    original="".join(json.loads(inspector.data(PATH))["cells"][10]["source"])
    patched="".join(json.loads((output/"package/patched"/PATH).read_text(encoding="utf-8"))["cells"][10]["source"])
    cases=[]
    profiles=[dict(name="empty",texts=[]),dict(name="changing-context",texts=["first","second","third"]),
        dict(name="body-failure",texts=["first","second","third"],fail=2),
        dict(name="iterator-failure",texts=["first","second","third"],iterator_fail=1),
        dict(name="native-cancellation",texts=["first","second"],cancel=True)]
    for profile in profiles:
        case_dir=output/profile["name"]
        for kind in ("native","adapted"):(case_dir/kind).mkdir(parents=True)
        native=await run(original,case_dir/"native",profile)
        adapted=await run(patched,case_dir/"adapted",profile,adapter)
        assert native["outcome"]==adapted["outcome"]
        assert native["trace"]==adapted["trace"]
        assert adapted["withheld_occurrences"]==len(adapted["calls"])
        assert all(c["starts"]==1 and c["physical"]=="RETIRED" for c in adapted["calls"])
        assert adapted["used"]==0
        cases.append(dict(name=profile["name"],status="PASS",native=native,adapted=adapted))
    return dict(schema="crag-fragment-probe/1",status="PASS",source=dict(path=PATH,unit="cell:10",
        notebook_sha256=hashlib.sha256(inspector.data(PATH)).hexdigest()),
        package=dict(archive=package["archive"],sha256=package["sha256"],generated_adapter_used=True),
        transformations=package["transformations"],cases=cases,
        scope="exact original method body + generated package; controlled Context/LLM/event/iterator",
        partition_origin="manually selected mechanism probe",sdk_workflow="NOT_RUN",live_model_quality="NOT_RUN",
        dependency_closure="UNKNOWN",parallel_admitted=False,production_admitted=False)


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("source_root")
    parser.add_argument("output")
    args=parser.parse_args()
    output=Path(args.output).resolve()
    output.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output) as temporary:
        scratch=Path(temporary).resolve()
        assert scratch.is_relative_to(output) and scratch!=output
        report=asyncio.run(probe(Path(args.source_root).resolve(),scratch))
        archive=scratch/"crag-sequential-draft.zip"
        shutil.copyfile(archive,output/archive.name)
        (output/(archive.name+".sha256")).write_text(hashlib.sha256(archive.read_bytes()).hexdigest()+"  "+archive.name+"\n",encoding="utf-8")
    (output/"crag-fragment-result.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(dict(status=report["status"],cases=len(report["cases"]),sdk_workflow="NOT_RUN",parallel_admitted=False)))
