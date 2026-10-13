import ast
import asyncio
import importlib.util
import json
import tempfile
import types
import unittest
import zipfile
from pathlib import Path

from seampact.build import build_package
from seampact.handoff import ToolListHandoff
from seampact.inspection import Inspector
from seampact.lowering import UnsupportedLowering, lower_loop, verify_lowering
from seampact.runtime import Coordinator, OwnerRuntime, ProtocolError
from seampact.store import Store
from seampact.verification import check_candidate


SOURCE='''class Native:
    async def evaluate(self, ctx, nodes):
        query = await ctx.get("query")
        results = []
        for node in nodes:
            model = await ctx.get("model")
            response = await model.complete(node, query)
            results.append(response.strip())
        await ctx.set("results", results)
        return results
'''


def native_class(source):
    namespace={}
    exec(compile(source,"<frozen-mechanism-fixture>","exec"),namespace)
    return namespace["Native"]


class Context:
    def __init__(self,fail=None,blocked=False):
        self.trace=[]
        self.models=0
        self.results=None
        self.fail=fail
        self.blocked=blocked
        self.entered=asyncio.Event()
    async def get(self,key):
        self.trace.append(["get",key])
        if key=="query":
            return "q"
        self.models+=1
        number=self.models
        ctx=self
        class Model:
            async def complete(self,node,query):
                ctx.trace.append(["complete",number,node,query])
                if ctx.fail==node:
                    raise ValueError("original failure")
                if ctx.blocked:
                    ctx.entered.set()
                    await asyncio.Event().wait()
                return " " + str(node) + ":" + str(number) + " "
        return Model()
    async def set(self,key,value):
        self.trace.append(["set",key,list(value)])
        self.results=value


class TracedIterator:
    def __init__(self,ctx,values,fail_at=None):
        self.ctx,self.values,self.fail_at=ctx,list(values),fail_at
        self.index=0
    def __iter__(self):
        self.ctx.trace.append(["iter"])
        return self
    def __next__(self):
        i=self.index
        self.ctx.trace.append(["next",i])
        if self.fail_at==i:
            raise RuntimeError("original iterator failure")
        if i==len(self.values):
            raise StopIteration
        self.index+=1
        return self.values[i]


class LoweringTests(unittest.TestCase):
    def test_method_local_imports_keep_the_original_namespace(self):
        cases = (
            ("import math", "math.sqrt(node)"),
            ("import math as arithmetic", "arithmetic.sqrt(node)"),
            ("from math import sqrt as root", "root(node)"),
            ("import xml.etree.ElementTree", "xml.etree.ElementTree.Element(str(node)).tag"),
        )
        for statement, expression in cases:
            source = "class Native:\n    async def evaluate(self, nodes):\n        " + statement + "\n        results = []\n        for node in nodes:\n            results.append(" + expression + ")\n        return results\n"
            with self.subTest(statement=statement):
                lowered = lower_loop(source, "Native.evaluate")
                self.assertEqual(asyncio.run(native_class(source)().evaluate([4, 9])),
                                 asyncio.run(native_class(lowered["source"])().evaluate([4, 9])))

    def test_hidden_local_bindings_are_not_misclassified_as_globals(self):
        prefixes = (
            "if nodes:\n            import math as hidden",
            "try:\n            raise ValueError('native')\n        except ValueError as hidden:\n            pass",
            "hidden = 7\n        try:\n            raise ValueError('native')\n        except ValueError as hidden:\n            pass",
            "match nodes:\n            case [hidden, *_]:\n                pass",
        )
        for prefix in prefixes:
            source = "class Native:\n    async def evaluate(self, nodes):\n        " + prefix + "\n        results = []\n        for node in nodes:\n            results.append(hidden)\n        return results\n"
            with self.subTest(prefix=prefix), self.assertRaises(UnsupportedLowering):
                lower_loop(source, "Native.evaluate")
        # A delete makes this a function-local name even without any Store node.
        source = "hidden = 7\nclass Native:\n    async def evaluate(self, nodes):\n        results = []\n        for node in nodes:\n            results.append(hidden)\n        del hidden\n        return results\n"
        with self.assertRaises(UnsupportedLowering):
            lower_loop(source, "Native.evaluate")

    def test_per_item_context_mutable_alias_and_iterator_order(self):
        lowered=lower_loop(SOURCE,"Native.evaluate")
        async def run(source):
            ctx=Context()
            result=await native_class(source)().evaluate(ctx,TracedIterator(ctx,["a","b","c"]))
            self.assertIs(result,ctx.results)
            return result,ctx.trace
        self.assertEqual(asyncio.run(run(SOURCE)),asyncio.run(run(lowered["source"])))
        self.assertTrue(lowered["verification"]["structural_inverse"])
        self.assertFalse(lowered["recipe"]["parallel_admitted"])

    def test_empty_iterator_keeps_no_body_reads(self):
        async def run(source):
            ctx=Context()
            result=await native_class(source)().evaluate(ctx,TracedIterator(ctx,[]))
            self.assertEqual(ctx.models,0)
            return result,ctx.trace
        self.assertEqual(asyncio.run(run(SOURCE)),asyncio.run(run(lower_loop(SOURCE,"Native.evaluate")["source"])))

    def test_original_failure_stops_before_later_items_and_write(self):
        async def run(source,iterator_failure):
            ctx=Context(fail=None if iterator_failure else "b")
            with self.assertRaises((ValueError,RuntimeError)) as error:
                await native_class(source)().evaluate(ctx,TracedIterator(ctx,["a","b","c"],1 if iterator_failure else None))
            self.assertIsNone(ctx.results)
            self.assertNotIn(["complete",3,"c","q"],ctx.trace)
            return type(error.exception).__name__,str(error.exception),ctx.trace
        for iterator_failure in (False,True):
            self.assertEqual(asyncio.run(run(SOURCE,iterator_failure)),
                             asyncio.run(run(lower_loop(SOURCE,"Native.evaluate")["source"],iterator_failure)))

    def test_cancellation_keeps_no_continuation(self):
        async def run(source):
            ctx=Context(blocked=True)
            task=asyncio.create_task(native_class(source)().evaluate(ctx,["a","b"]))
            await asyncio.wait_for(ctx.entered.wait(),1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertIsNone(ctx.results)
            return ctx.trace
        self.assertEqual(asyncio.run(run(SOURCE)),asyncio.run(run(lower_loop(SOURCE,"Native.evaluate")["source"])))

    def test_inverse_detects_body_callsite_outside_change_and_recipe_forgery(self):
        lowered=lower_loop(SOURCE,"Native.evaluate")
        for altered in (lowered["source"].replace("response.strip()","response.upper()"),
                        lowered["source"].replace("return results","return []"),
                        lowered["source"].replace("await self.","self.",1)):
            self.assertFalse(verify_lowering(SOURCE,altered,lowered["recipe"])["structural_inverse"])
        forged=dict(lowered["recipe"],incoming=[])
        self.assertFalse(verify_lowering(SOURCE,lowered["source"],forged)["structural_inverse"])
        tree=ast.parse(lowered["source"])
        tree.body[0].body[-1].args.args[0].annotation=ast.Call(func=ast.Name(id="side_effect",ctx=ast.Load()),args=[],keywords=[])
        self.assertFalse(verify_lowering(SOURCE,ast.unparse(tree),lowered["recipe"])["structural_inverse"])

    def test_unsupported_control_carry_liveout_scope_and_reflection(self):
        variants=[
            SOURCE.replace('results.append(response.strip())','break'),
            SOURCE.replace('results.append(response.strip())','return response'),
            SOURCE.replace('results.append(response.strip())','query = query + response'),
            SOURCE.replace('return results','return response'),
            SOURCE.replace('results.append(response.strip())','locals()'),
            SOURCE.replace('results.append(response.strip())','node = response'),
            SOURCE.replace('results.append(response.strip())','self = response'),
            SOURCE.replace('results = []','if nodes:\n            results = []'),
            SOURCE.replace('results = []','results = []\n        del results'),
            SOURCE.replace('results = []','global query\n        results = []'),
            SOURCE.replace('results = []','results = []\n        def observe():\n            return response'),
        ]
        for source in variants:
            with self.subTest(source=source),self.assertRaises(UnsupportedLowering):
                lower_loop(source,"Native.evaluate")

    def test_notebook_build_preserves_other_units_and_original_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            book=dict(cells=[dict(cell_type="markdown",source=["original data description"]),
                dict(cell_type="code",source=SOURCE.splitlines(True),outputs=[dict(text="untouched")]),
                dict(cell_type="code",source=["other_data = ['a', 'b']\n"],outputs=[])])
            data=json.dumps(book).encode()
            (root/"book.ipynb").write_bytes(data)
            inspector=Inspector.freeze(root,root/"snapshots","req",["book.ipynb"],"Native.evaluate","result, trace, failure")
            candidate=dict(boundaries=[dict(id="item",path="book.ipynb",unit="cell:1",symbol="Native.evaluate",mode="R")])
            report=check_candidate(inspector,candidate)
            self.assertTrue(report["boundaries"][0]["buildable"])
            self.assertFalse(report["admitted"])
            package=build_package(inspector,candidate,root/"packages","draft")
            with zipfile.ZipFile(root/"packages"/package["archive"]) as archive:
                self.assertEqual(archive.read("source/book.ipynb"),data)
                patched=json.loads(archive.read("patched/book.ipynb"))
                self.assertEqual(patched["cells"][0],book["cells"][0])
                self.assertEqual(patched["cells"][2],book["cells"][2])
                self.assertEqual(patched["cells"][1]["outputs"],book["cells"][1]["outputs"])
                manifest=json.loads(archive.read("manifest.json"))
                self.assertTrue(manifest["transformations"][0]["verification"]["structural_inverse"])
                self.assertFalse(manifest["admitted"])

    def test_generated_loop_adapter_waits_before_per_item_body(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            (root/"native.py").write_text(SOURCE,encoding="utf-8")
            inspector=Inspector.freeze(root,root/"snapshots","req",["native.py"],"Native.evaluate","result, trace")
            candidate=dict(boundaries=[dict(id="item",path="native.py",symbol="Native.evaluate",mode="R")])
            package=build_package(inspector,candidate,root/"packages","draft")
            with zipfile.ZipFile(root/"packages"/package["archive"]) as archive:
                archive.extractall(root/"package")
            spec=importlib.util.spec_from_file_location("generated_test_adapter",root/"package/adapter.py")
            adapter=importlib.util.module_from_spec(spec)
            spec.loader.exec_module(adapter)
            owner=native_class((root/"package/patched/native.py").read_text())()
            store=Store(root/"runtime.db")
            runtime=OwnerRuntime(Coordinator(store,automatic=False))
            try:
                bindings=adapter.install_loop_handoffs(runtime,owner,"native.py")
                async def run():
                    ctx=Context()
                    task=asyncio.create_task(owner.evaluate(ctx,["a","b"]))
                    for count in range(2):
                        for _ in range(200):
                            enabled=[c for c in runtime.snapshot()["calls"].values() if c["physical"]=="ENABLED"]
                            if enabled: break
                            await asyncio.sleep(.005)
                        else: self.fail("body did not reach gate")
                        self.assertEqual(ctx.models,count)
                        runtime.call("authorize",enabled[0]["id"])
                    self.assertEqual(await asyncio.wait_for(task,1),["a:1","b:2"])
                asyncio.run(run())
                self.assertEqual(runtime.snapshot()["used"],0)
                self.assertEqual(len(bindings),1)
                with self.assertRaises(ProtocolError):
                    adapter.install_loop_handoffs(runtime,owner,"native.py")
            finally:
                runtime.close()
                store.close()


HANDOFF_SOURCE='''class Tool:
    async def call(self, value):
        return value
class Owner:
    async def initialize(self):
        self.tools = []
        tool = Tool()
        self.tools.append(tool.call)
    async def intake(self):
        self.sdk = SDK(tools=self.tools)
    async def setup(self):
        await self.initialize()
        await self.intake()
'''


class HandoffTests(unittest.TestCase):
    def test_overlapping_candidates_cannot_be_installed_as_one_package(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            (root/"source.py").write_text(HANDOFF_SOURCE)
            inspector=Inspector.freeze(root,root/"snapshots","req",["source.py"],"Owner.setup","tool output")
            boundaries=[dict(id=i,path="source.py",symbol="Tool.call",mode="H") for i in ("first","alternative")]
            report=check_candidate(inspector,dict(boundaries=boundaries))
            self.assertFalse(any(row["buildable"] for row in report["boundaries"]))
            with self.assertRaises(ValueError):
                build_package(inspector,dict(boundaries=boundaries),root/"packages","ambiguous")

    def test_structured_registration_checker_and_late_anchor_rejection(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            wiring=dict(rule="tool-list-before-sdk/1",controller_path="source.py",controller_symbol="Owner",
                initializer="initialize",tools_attribute="tools",sdk_intake="intake",setup="setup")
            candidate=dict(boundaries=[dict(id="tool",path="source.py",symbol="Tool.call",mode="H",native_wiring=wiring)])
            for source,expected in [(HANDOFF_SOURCE,True),(HANDOFF_SOURCE.replace('await self.initialize()\n        await self.intake()',
                                                                                  'await self.intake()\n        await self.initialize()'),False)]:
                (root/"source.py").write_text(source)
                inspector=Inspector.freeze(root,root/"snapshots","req",["source.py"],"Owner.setup","tool output")
                report=check_candidate(inspector,candidate)
                self.assertEqual(report["boundaries"][0]["buildable"],expected)
                self.assertFalse(report["admitted"])

    def test_repeated_native_initialization_replaces_tools_and_never_double_wraps(self):
        namespace={}
        exec(HANDOFF_SOURCE,namespace)
        with tempfile.TemporaryDirectory() as temporary:
            store=Store(Path(temporary)/"runtime.db")
            runtime=OwnerRuntime(Coordinator(store))
            try:
                owner=namespace["Owner"]()
                hook=ToolListHandoff(runtime,owner,{"tool":"Tool.call"},"initialize","tools").install()
                async def run():
                    await owner.initialize()
                    first=owner.tools[0]
                    self.assertEqual(await first(3),3)
                    with self.assertRaises(ProtocolError): await hook.bind_current_tools()
                    await owner.initialize()
                    self.assertIsNot(first,owner.tools[0])
                    self.assertEqual(await owner.tools[0](7),7)
                asyncio.run(run())
                self.assertEqual([b["initialization_round"] for b in hook.bindings],[1,2])
                self.assertNotEqual(hook.bindings[0]["gate"],hook.bindings[1]["gate"])
                self.assertEqual(runtime.snapshot()["used"],0)
            finally:
                runtime.close()
                store.close()

    def test_original_annotation_namespace_is_resolved_for_sdk_intake(self):
        namespace={"types":types}
        exec('from __future__ import annotations\n'+HANDOFF_SOURCE.replace('value):','value: types.SimpleNamespace):'),namespace)
        with tempfile.TemporaryDirectory() as temporary:
            store=Store(Path(temporary)/"runtime.db")
            runtime=OwnerRuntime(Coordinator(store))
            try:
                owner=namespace["Owner"]()
                ToolListHandoff(runtime,owner,{"tool":"Tool.call"},"initialize","tools",schema_policy="resolved-types").install()
                asyncio.run(owner.initialize())
                self.assertIs(owner.tools[0].__annotations__["value"],types.SimpleNamespace)
            finally:
                runtime.close()
                store.close()
