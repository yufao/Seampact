import asyncio
import inspect
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from seampact.analysis import AnalysisJobs
from seampact import __version__
from seampact.build import build_package
from seampact.inspection import Inspector, safe_path
from seampact.providers import completion_body
from seampact.runtime import Coordinator, OwnerRuntime, ProtocolError
from seampact.server import make_server
from seampact.service import Platform
from seampact.store import Store
from seampact.verification import check_candidate


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "test.db")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()


class ProtocolTests(Fixture):
    def test_single_consumption_generation_and_grandfathering(self):
        async def scenario():
            c = Coordinator(self.store, automatic=False)
            await c.register("tool", "native")
            identity = await c.enable("tool", "native")
            permit = await c.authorize(identity)
            await c.invalidate("tool")
            with self.assertRaises(ProtocolError):
                await c.consume(identity, permit, "native", 2)
            await c.consume(identity, permit, "native")
            with self.assertRaises(ProtocolError):
                await c.consume(identity, permit, "native")
            self.assertEqual(c.used(), 1)
            self.assertTrue(await c.finish(identity, permit, "RETURN"))
            self.assertFalse(await c.finish(identity, permit, "RETURN"))
            self.assertEqual(c.used(), 0)
            with self.assertRaises(ProtocolError):
                await c.enable("tool", "native")
        asyncio.run(scenario())

    def test_cancel_unknown_retains_quota_and_late_result_cannot_revive(self):
        async def scenario():
            c = Coordinator(self.store, automatic=False)
            await c.register("tool", "native")
            a = await c.enable("tool", "native")
            p = await c.authorize(a)
            await c.consume(a, p, "native")
            await c.cancel(a)
            b = await c.enable("tool", "native")
            self.assertEqual(c.used(), 1)
            with self.assertRaises(ProtocolError):
                await c.authorize(b)
            await c.finish(a, p, "LATE_RETURN")
            self.assertEqual(c.state["calls"][a]["logical"], "CANCELLED")
            await c.authorize(b)
            self.assertEqual(c.used(), 1)
        asyncio.run(scenario())

    def test_invalidation_blocks_old_reservation(self):
        async def scenario():
            c = Coordinator(self.store, automatic=False)
            await c.register("tool", "native")
            a = await c.enable("tool", "native")
            await c.reserve(a)
            waiting = asyncio.create_task(c.wait(a))
            await asyncio.sleep(0)
            await c.invalidate("tool")
            with self.assertRaises(ProtocolError):
                await waiting
            with self.assertRaises(ProtocolError):
                await c.authorize(a)
            self.assertEqual(c.used(), 0)
        asyncio.run(scenario())

    def test_restart_is_conservative_about_authorized_owner(self):
        async def scenario():
            c = Coordinator(self.store, automatic=False)
            await c.register("tool", "native")
            a = await c.enable("tool", "native")
            await c.authorize(a)
            recovered = Coordinator(self.store)
            self.assertEqual(recovered.state["calls"][a]["physical"], "UNKNOWN")
            self.assertEqual(recovered.used(), 1)
            self.assertFalse(recovered.state["gates"]["tool"]["active"])
        asyncio.run(scenario())

    def test_managed_quota_and_legal_actions(self):
        async def scenario():
            c = Coordinator(self.store, quota=2, automatic=False)
            await c.register("tool", "native", slots=2)
            a, b = await c.enable("tool", "native"), await c.enable("tool", "native")
            self.assertEqual(c.snapshot()["legal_actions"], [a,b])
            await c.authorize(a)
            self.assertEqual(c.snapshot()["legal_actions"], [])
            await c.cancel(a, prestart_fence=True)
            self.assertEqual(c.snapshot()["legal_actions"], [b])
        asyncio.run(scenario())


class OwnerTests(Fixture):
    def test_nested_call_is_rejected_before_resource_deadlock(self):
        runtime = OwnerRuntime(Coordinator(self.store))
        runtime.call("register", "outer", "owner")
        runtime.call("register", "inner", "owner")
        async def inner():
            self.fail("nested original must not start")
        wrapped_inner = runtime.wrap(inner, "inner", "owner")
        async def outer():
            return await wrapped_inner()
        try:
            with self.assertRaises(ProtocolError):
                asyncio.run(runtime.wrap(outer, "outer", "owner")())
            self.assertEqual(runtime.snapshot()["used"], 0)
        finally:
            runtime.close()

    def test_native_async_context_result_identity_and_continuation(self):
        runtime = OwnerRuntime(Coordinator(self.store, automatic=False))
        runtime.call("register", "tool", "native")
        touched, loops = [], []
        result = object()
        async def original(value: int):
            touched.append(value)
            loops.append(asyncio.get_running_loop())
            return result
        wrapped = runtime.wrap(original, "tool", "native")
        self.assertEqual(inspect.signature(wrapped), inspect.signature(original))
        async def scenario():
            task = asyncio.create_task(wrapped(5))
            for _ in range(100):
                snapshot = runtime.snapshot()
                if snapshot["calls"]:
                    break
                await asyncio.sleep(.002)
            self.assertEqual(touched, [])
            identity = next(iter(snapshot["calls"]))
            runtime.call("authorize", identity)
            self.assertIs(await task, result)
            self.assertIs(loops[0], asyncio.get_running_loop())
        try:
            asyncio.run(scenario())
            self.assertEqual(touched, [5])
            self.assertEqual(runtime.snapshot()["used"], 0)
        finally:
            runtime.close()

    def test_sync_preserves_thread_and_exception_object(self):
        runtime = OwnerRuntime(Coordinator(self.store))
        runtime.call("register", "tool", "native")
        owner_thread = threading.get_ident()
        error = ValueError("native business error")
        def original():
            self.assertEqual(threading.get_ident(), owner_thread)
            raise error
        try:
            with self.assertRaises(ValueError) as caught:
                runtime.wrap(original, "tool", "native")()
            self.assertIs(caught.exception, error)
            self.assertEqual(runtime.snapshot()["used"], 0)
        finally:
            runtime.close()

    def test_cancel_waiting_native_call_never_starts_original(self):
        runtime = OwnerRuntime(Coordinator(self.store, automatic=False))
        runtime.call("register", "tool", "native")
        touched = []
        async def original():
            touched.append(1)
        async def scenario():
            task = asyncio.create_task(runtime.wrap(original, "tool", "native")())
            while not runtime.snapshot()["calls"]:
                await asyncio.sleep(.002)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(touched, [])
            self.assertEqual(runtime.snapshot()["used"], 0)
        try:
            asyncio.run(scenario())
        finally:
            runtime.close()

    def test_cancel_running_local_callable_unwinds_before_retirement(self):
        runtime = OwnerRuntime(Coordinator(self.store))
        runtime.call("register", "tool", "native")
        entered, unwound = asyncio.Event(), []
        async def original():
            entered.set()
            try:
                await asyncio.sleep(30)
            finally:
                unwound.append(True)
        async def scenario():
            task = asyncio.create_task(runtime.wrap(original, "tool", "native")())
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(unwound, [True])
            self.assertEqual(runtime.snapshot()["used"], 0)
        try:
            asyncio.run(scenario())
        finally:
            runtime.close()


class SourceTests(Fixture):
    def inspector(self):
        (self.root / "original.py").write_text("raise RuntimeError('must not import')\nasync def tool(value):\n    return value\n", encoding="utf-8")
        return Inspector.freeze(self.root, self.root / "snapshots", "request", ["original.py"],
                                "original.tool", "native return, exception, continuation")

    def candidate(self):
        return dict(boundaries=[dict(id="tool",path="original.py",symbol="tool",mode="H")], admitted=True)

    def test_frozen_content_and_independent_admission(self):
        i = self.inspector()
        report = check_candidate(i, self.candidate())
        self.assertFalse(report["admitted"])
        self.assertFalse(report["boundaries"][0]["runtime_admitted"])
        self.assertEqual(report["boundaries"][0]["obligations"]["source_anchor"]["status"], "PASS")
        (self.root / "original.py").write_text("changed")
        self.assertIn("async def tool", "\n".join(i.read_file("original.py")["lines"]))

    def test_paths_secret_and_symlink_escape_are_rejected(self):
        for p in ("../outside.py", "C:/outside.py", ".env", "credentials.json", "a/../../x"):
            with self.assertRaises(ValueError):
                safe_path(self.root, p)
        with tempfile.TemporaryDirectory() as outside:
            try:
                (self.root / "escape").symlink_to(outside, target_is_directory=True)
            except OSError:
                return  # Windows may lack symlink permission; lexical cases still ran.
            with self.assertRaises(ValueError):
                safe_path(self.root, "escape/thing.py")

    def test_package_integrity_without_import_or_behavior_claim(self):
        i = self.inspector()
        package = build_package(i, self.candidate(), self.root / "packages", "test-package")
        self.assertEqual(package["status"], "BUILT_DRAFT")
        self.assertFalse(package["admitted"])
        output = self.root / "extracted"
        with zipfile.ZipFile(self.root / "packages" / package["archive"]) as z:
            z.extractall(output)
        result = subprocess.run([sys.executable,"-B",str(output/"verify.py")],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout)["runtime"], "NOT_RUN")

    def test_notebook_output_is_not_sent_to_analysis(self):
        book = dict(cells=[dict(cell_type="code",source=["def tool():\n return 1"],outputs=[{"secret":"output"}])])
        (self.root / "book.ipynb").write_text(json.dumps(book),encoding="utf-8")
        i = Inspector.freeze(self.root,self.root/"snapshots","request",["book.ipynb"],"cell:0","return")
        self.assertEqual(i.read_file("book.ipynb")["units"],["cell:0"])
        self.assertNotIn("output",json.dumps(i.read_file("book.ipynb",unit="cell:0")))

    def test_unsupported_R_does_not_inherit_W_admission(self):
        i = self.inspector()
        candidate = self.candidate()
        candidate["boundaries"][0]["mode"] = "R"
        with self.assertRaises(ValueError):
            build_package(i,candidate,self.root/"packages","unsupported")


class AnalysisTests(Fixture):
    def test_capability_maximum_full_context_and_reasoning_are_kept(self):
        messages=[dict(role="assistant",content=None,reasoning_content="full reasoning",tool_calls=[])]*100
        profile=dict(model="test",capabilities=dict(selected=dict(max_output_tokens=393216)))
        body=completion_body(profile,messages,[])
        self.assertIs(body["messages"],messages)
        self.assertEqual(body["max_tokens"],393216)
        self.assertNotIn("max_calls",body)
        self.assertNotIn("temperature",body)
        self.assertNotIn("max_tokens",completion_body(dict(model="glm"),messages,[]))

    def test_real_tool_loop_can_read_check_submit_without_self_admission(self):
        (self.root/"original.py").write_text("def tool():\n    return 1\n",encoding="utf-8")
        i=Inspector.freeze(self.root,self.root/"snapshots","req",["original.py"],"tool","return")
        candidate=dict(boundaries=[dict(id="tool",path="original.py",symbol="tool",mode="W")])
        calls=[]
        class Profiles:
            def profile(self,provider):
                return dict(model="test",base_url="https://api.deepseek.com",capabilities=dict(selected=dict(max_output_tokens=100000)))
            def key(self,provider):
                return "test-fixture-credential"
        class Transport:
            def send(self,url,key,body=None):
                calls.append(json.loads(json.dumps(body)))
                step=len(calls)
                name,args=("read_file",dict(path="original.py")) if step==1 else ("submit_candidate",dict(candidate=candidate))
                return dict(usage=dict(prompt_tokens=42,completion_tokens=3),choices=[dict(finish_reason="tool_calls",message=dict(
                    role="assistant",content=None,reasoning_content="source reasoning",tool_calls=[dict(id=str(step),type="function",
                    function=dict(name=name,arguments=json.dumps(args)))]))])
        jobs=AnalysisJobs(self.store,Profiles(),Transport())
        identity=jobs.start(i,"deepseek")
        for _ in range(100):
            j=self.store.get("analysis",identity)
            if j["status"]!="RUNNING":break
            time.sleep(.01)
        self.assertEqual(j["status"],"COMPLETED",j)
        self.assertEqual(j["rounds"],2)
        self.assertEqual(calls[1]["messages"][2]["reasoning_content"],"source reasoning")
        self.assertFalse(j["submitted"]["report"]["admitted"])


class PlatformTests(unittest.TestCase):
    def test_shared_server_requires_startup_session(self):
        import http.cookiejar
        with tempfile.TemporaryDirectory() as root:
            p=Platform(root)
            server=make_server(p,0,require_session=True)
            thread=threading.Thread(target=server.serve_forever,daemon=True)
            thread.start()
            base=f"http://127.0.0.1:{server.server_port}"
            try:
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(base+"/api/state")
                self.assertEqual(caught.exception.code,403)
                opener=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
                with opener.open(base+"/?access="+server.access_token) as response:
                    self.assertIn(b"SeamPact",response.read())
                with opener.open(base+"/api/state") as response:
                    self.assertEqual(json.load(response)["version"],__version__)
            finally:
                server.shutdown()
                server.server_close()
                thread.join()
                p.close()

    def test_teaching_native_adapted_and_http_origin_controls(self):
        with tempfile.TemporaryDirectory() as root:
            p=Platform(root)
            server=make_server(p,0,require_session=False)
            thread=threading.Thread(target=server.serve_forever,daemon=True)
            thread.start()
            base=f"http://127.0.0.1:{server.server_port}"
            try:
                with urllib.request.urlopen(base+"/api/state") as response:
                    self.assertEqual(json.load(response)["profiles"][0]["usage_policy"],"observe_only")
                req=urllib.request.Request(base+"/api/demo",data=b"{}",headers={"Content-Type":"application/json","Origin":"https://foreign.example"})
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(req)
                self.assertEqual(caught.exception.code,403)
                identity=p.demo()
                for _ in range(100):
                    result=p.store.get("demo",identity)
                    if result["status"]!="RUNNING":break
                    time.sleep(.01)
                self.assertEqual(result["status"],"PASS",result)
                self.assertEqual(len(result["calls"]),2)
                self.assertEqual(p.runtime.snapshot()["used"],0)
            finally:
                server.shutdown()
                server.server_close()
                thread.join()
                p.close()


if __name__=="__main__":
    unittest.main()
