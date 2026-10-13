"""Real MMCT source + AutoGen SDK + controlled provider registration probe.

Run only in the isolated optional SDK environment. This is not a live-model
quality experiment and never imports the unused heavyweight vision backends.
"""
import argparse
import asyncio
import hashlib
import importlib.metadata
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import types
import zipfile
from pathlib import Path

from seampact.handoff import ToolListHandoff
from seampact.build import build_package
from seampact.inspection import Inspector
from seampact.runtime import Coordinator, OwnerRuntime
from seampact.store import Store


def load_native(root):
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    from loguru import logger
    logger.remove()
    packages = ["mmct", "mmct.providers", "mmct.providers.base", "mmct.utils",
        "mmct.image_pipeline", "mmct.image_pipeline.utils", "mmct.image_pipeline.core",
        "mmct.image_pipeline.core.models", "mmct.image_pipeline.core.models.vit",
        "mmct.image_pipeline.core.tools", "mmct.image_pipeline.agents"]
    for name in packages:
        module=types.ModuleType(name)
        module.__path__=[str(root/name.replace(".","/"))]
        sys.modules[name]=module
        if "." in name:
            parent,child=name.rsplit(".",1)
            setattr(sys.modules[parent],child,module)
    loaded=[]
    def load(name):
        relative=name.replace(".","/")+".py"
        path=root/relative
        spec=importlib.util.spec_from_file_location(name,path)
        module=importlib.util.module_from_spec(spec)
        sys.modules[name]=module
        spec.loader.exec_module(module)
        parent,child=name.rsplit(".",1)
        setattr(sys.modules[parent],child,module)
        loaded.append(dict(path=relative,sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
        return module
    base=load("mmct.providers.base.llm_provider").BaseLLMProvider
    sys.modules["mmct.providers.base"].BaseLLMProvider=base
    load("mmct.utils.error_handler")
    load("mmct.image_pipeline.utils.safety_prompts")
    load("mmct.image_pipeline.prompts")
    config=load("mmct.image_pipeline.config").ImageAgentProviderConfig
    load("mmct.image_pipeline.core.models.vit.visual_llm")
    load("mmct.image_pipeline.core.tools.vit")
    # A Vit-only frozen profile never constructs these original optional tools.
    for suffix,name in [("recog","RecogTool"),("object_detect","ObjectDetectTool"),
                        ("ocr","OcrTool"),("critic","CriticTool")]:
        module=types.ModuleType("mmct.image_pipeline.core.tools."+suffix)
        def prohibited(self,*args,**kwargs):
            raise AssertionError("unused backend was activated outside the probe profile")
        setattr(module,name,type(name,(),{"__init__":prohibited}))
        sys.modules[module.__name__]=module
    agent=load("mmct.image_pipeline.agents.image_agent")
    return base,config,agent,loaded


def provider_class(base):
    from autogen_core import FunctionCall
    from autogen_core.models import CreateResult,RequestUsage
    from autogen_ext.models.replay import ReplayChatCompletionClient
    class ControlledProvider(base):
        def __init__(self,label,fail=False):
            self.label,self.fail=label,fail
            self.trace=[]
            self.client=ReplayChatCompletionClient([
                CreateResult(finish_reason="function_calls",content=[FunctionCall(
                    id="native-tool",name="vit_tool",arguments=json.dumps({"query":"describe"}))],
                    usage=RequestUsage(prompt_tokens=7,completion_tokens=2),cached=False),
                "TERMINATE: controlled final answer"],
                model_info=dict(vision=False,function_calling=True,json_output=False,family="unknown",structured_output=False))
        def get_autogen_client(self,**kwargs):
            return self.client
        async def generate_json(self,prompt,**kwargs):
            raise AssertionError("unexpected provider path")
        async def chat_completion(self,messages,**kwargs):
            if kwargs.get("response_format"):
                self.trace.append(dict(kind="format",messages=messages))
                return {"content":kwargs["response_format"](response="controlled final answer",
                    tokens={"input_token":7,"output_token":2})}
            self.trace.append(dict(kind="vision",label=self.label,messages=messages,temperature=kwargs.get("temperature")))
            if self.fail:
                raise RuntimeError("controlled original provider failure")
            return {"content":" controlled visual answer "}
    return ControlledProvider


async def run_case(agent_module,config,provider_class,image,label,output,adapted=False,fail=False,late=False,adapter=None):
    provider=provider_class(label,fail)
    owner=agent_module.ImageAgent(str(image),"describe",config(llm_provider=provider),
        use_critic_agent=False,tools=[agent_module.ImageQnaTools.vit],
        disable_console_log=True,use_console=False)
    store=Store(output/"runtime.sqlite3")
    runtime=OwnerRuntime(Coordinator(store,automatic=False))
    hook=None
    try:
        if adapted:
            hook=ToolListHandoff(runtime,owner,{"visual":"VitTool.vit_tool"},
                initializer="_initialize_tools",tools_attribute="tools",schema_policy="resolved-types")
            if late:
                original=owner._initialize_agents
                async def too_late():
                    result=await original()
                    await hook.bind_current_tools()
                    return result
                owner._initialize_agents=too_late
            else:
                if adapter:
                    hooks=adapter.install_tool_handoffs(runtime,owner,
                        "mmct/image_pipeline/agents/image_agent.py","ImageAgent")
                    assert len(hooks)==1
                    hook=hooks[0]
                else:
                    hook.install()
        task=asyncio.create_task(owner())
        held=False
        for _ in range(200):
            calls=runtime.snapshot()["calls"]
            enabled=[c for c in calls.values() if c["physical"]=="ENABLED"]
            if enabled:
                # Native SDK has activated the tool, but original vision code has
                # not run. This is the actual gate probe, not a logging check.
                assert not provider.trace,"native vision escaped the admission gate"
                await asyncio.sleep(.02)
                assert not provider.trace
                held=True
                for call in enabled:
                    runtime.call("authorize",call["id"])
            if task.done():
                break
            await asyncio.sleep(.005)
        if not task.done():
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)
            raise AssertionError("native SDK did not continue after permission")
        result=await task
        schema=owner.planner_agent._tools[0].schema
        state=runtime.snapshot()
        return dict(result=result.model_dump(),provider_trace=provider.trace,schema=schema,
            withheld_before_native_entry=held,calls=list(state["calls"].values()),used=state["used"],
            binding=hook.bindings if hook else [],native_entry="ImageAgent.__call__",late=late)
    finally:
        await provider.client.close()
        runtime.close()
        store.close()


async def probe(root,output):
    from PIL import Image
    base,config,agent,loaded=load_native(root)
    provider=provider_class(base)
    inspector=Inspector.freeze(root,output/"snapshots","mmct-mechanism",[f["path"] for f in loaded],
        "ImageAgent.__call__ vit-only non-stream no-critic",
        "projected original response, ordered provider effects and SDK schema; original exception handling")
    wiring=dict(rule="tool-list-before-sdk/1",controller_path="mmct/image_pipeline/agents/image_agent.py",
        controller_symbol="ImageAgent",initializer="_initialize_tools",tools_attribute="tools",
        sdk_intake="_initialize_agents",setup="setup",schema_policy="resolved-types")
    candidate=dict(boundaries=[dict(id="visual",path="mmct/image_pipeline/core/tools/vit.py",
        symbol="VitTool.vit_tool",mode="H",native_wiring=wiring)],
        analysis="manually selected registration mechanism probe; not autonomous partition discovery")
    package=build_package(inspector,candidate,output,"mmct-handoff-draft")
    with zipfile.ZipFile(output/package["archive"]) as archive:
        archive.extractall(output/"package")
    spec=importlib.util.spec_from_file_location("mmct_generated_adapter",output/"package/adapter.py")
    adapter=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(adapter)
    cases=[]
    for label,color,fail in [("red",(255,0,0),False),("blue",(0,0,255),False),("original-error",(255,0,0),True)]:
        case_dir=output/label
        case_dir.mkdir(parents=True,exist_ok=True)
        image=case_dir/"fixture.png"
        Image.new("RGB",(4,4),color).save(image)
        for kind in ("native","adapted"):
            (case_dir/kind).mkdir(exist_ok=True)
        native=await run_case(agent,config,provider,image,label,case_dir/"native",fail=fail)
        adapted=await run_case(agent,config,provider,image,label,case_dir/"adapted",adapted=True,fail=fail,adapter=adapter)
        assert native["result"]==adapted["result"]
        assert native["provider_trace"]==adapted["provider_trace"]
        assert native["schema"]==adapted["schema"]
        assert adapted["withheld_before_native_entry"]
        assert len(adapted["calls"])==1 and adapted["calls"][0]["starts"]==1
        assert adapted["calls"][0]["physical"]=="RETIRED" and adapted["used"]==0
        cases.append(dict(label=label,status="PASS",native=native,adapted=adapted))
    late_dir=output/"late-registration"
    late_dir.mkdir(exist_ok=True)
    late=await run_case(agent,config,provider,output/"red/fixture.png","red",late_dir,adapted=True,late=True)
    assert not late["calls"] and late["provider_trace"] and not late["withheld_before_native_entry"]
    return dict(schema="mmct-sdk-probe/1",status="PASS",source=loaded,
        sdk={p:importlib.metadata.version(p) for p in ("autogen-agentchat","autogen-core","autogen-ext","Pillow")},
        profile=dict(entry="ImageAgent.__call__",tools=["vit"],critic=False,stream=False,
                     provider="controlled replay",package_initializers="namespace bootstrap"),
        cases=cases,negative_control=dict(status="BYPASS_DETECTED",late_registration=late),
        package=dict(sha256=package["sha256"],generated_adapter_used=True),
        partition_origin="manually selected mechanism probe; not autonomous discovery evidence",
        admission_scope="source + real SDK + controlled provider registration/continuation only",
        live_model_quality="NOT_RUN",complete_dependency_closure="UNKNOWN",production_admitted=False)


if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("source_root")
    parser.add_argument("output")
    args=parser.parse_args()
    output=Path(args.output).resolve()
    output.mkdir(parents=True,exist_ok=True)
    # Each run gets fresh owners/ledgers. Results remain at a stable report path.
    with tempfile.TemporaryDirectory(dir=output) as temporary:
        scratch=Path(temporary).resolve()
        assert scratch.is_relative_to(output) and scratch!=output
        report=asyncio.run(probe(Path(args.source_root).resolve(),scratch))
        archive=scratch/"mmct-handoff-draft.zip"
        shutil.copyfile(archive,output/archive.name)
        (output/(archive.name+".sha256")).write_text(hashlib.sha256(archive.read_bytes()).hexdigest()+"  "+archive.name+"\n",encoding="utf-8")
        # Comparisons above use exact raw observations. Persist stable fixture
        # references, rather than deleted per-run paths in the readable report.
        serialized=json.dumps(report,ensure_ascii=False)
        for prefix in (str(scratch),scratch.as_posix()):
            serialized=serialized.replace(json.dumps(prefix)[1:-1],"<probe-fixtures>")
        report=json.loads(serialized)
    (output/"mmct-sdk-result.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps(dict(status=report["status"],cases=len(report["cases"]),
                         negative_control=report["negative_control"]["status"],live_model_quality="NOT_RUN")))
