import asyncio
import json
import tempfile
import unittest
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts.local_model_usage_server import (
    EngineUsage, ReceiptStore, SCHEMA, UsageReceiptMiddleware,
)


class Kind(Enum):
    DELTA = 1
    CUMULATIVE = 2
    FINAL_ONLY = 3


def output(lengths, *, finished=False, reason=None, prompt=True):
    return SimpleNamespace(
        prompt_token_ids=[1, 2] if prompt else None,
        encoder_prompt_token_ids=[3] if prompt else None,
        outputs=[SimpleNamespace(index=i, token_ids=[9] * count, finish_reason=reason)
                 for i, count in lengths.items()],
        finished=finished, error=None,
        prompt="PRIVATE_PROMPT", text="PRIVATE_REASONING",
    )


class Engine:
    def __init__(self, outputs, kind=Kind.CUMULATIVE, wait=False):
        self.outputs, self.kind, self.wait = outputs, kind, wait
        self.first = asyncio.Event()
        self.closed = False

    async def generate(self, prompt, sampling_params, request_id):
        try:
            for item in self.outputs:
                yield item
                self.first.set()
                if self.wait:
                    await asyncio.Event().wait()
        finally:
            self.closed = True


class App:
    """Minimal ASGI router/auth gate, without installing model or web packages."""
    def __init__(self, engine):
        self.state = SimpleNamespace(engine_client=engine)
        self.endpoint = None

    def add_route(self, path, endpoint, methods):
        self.route, self.endpoint = path, endpoint

    async def __call__(self, scope, receive, send):
        if scope["path"].startswith("/v1") and b"authorization" not in dict(scope["headers"]):
            await send({"type": "http.response.start", "status": 401, "headers": []})
            await send({"type": "http.response.body", "body": b""})
        elif scope["path"].startswith("/v1/seampact/usage/"):
            scope["path_params"] = {"request_id": scope["path"].rsplit("/", 1)[-1]}
            await self.endpoint(scope, receive, send)
        else:
            await send({"type": "http.response.start", "status": 200, "headers": []})
            params = SimpleNamespace(output_kind=self.state.engine_client.kind, n=1)
            async for _ in self.state.engine_client.generate("PRIVATE_PROMPT", params, "engine-id"):
                pass
            await send({"type": "http.response.body", "body": b""})


class UsageReceiptTests(unittest.IsolatedAsyncioTestCase):
    async def invoke(self, middleware, app, *, method="POST", path="/v1/chat/completions", auth=True, request_id="seampact-test"):
        messages = []
        headers = [(b"x-request-id", request_id.encode())]
        if auth:
            headers.append((b"authorization", b"Bearer private"))
        scope = {"type": "http", "method": method, "path": path, "headers": headers, "app": app}

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            messages.append(message)

        await middleware(scope, receive, send)
        return messages

    def test_output_modes_and_choices(self):
        for mode, second, total in [("DELTA", 3, 5), ("CUMULATIVE", 5, 5), ("FINAL_ONLY", 5, 5)]:
            with self.subTest(mode=mode):
                usage = EngineUsage(mode, 2)
                if mode != "FINAL_ONLY":
                    usage.observe(output({0: 2}))
                usage.observe(output({0: second}, reason="stop"))
                usage.observe(output({1: 4}, finished=True, reason="length"))
                self.assertEqual(usage.prompt_tokens, 3)
                self.assertEqual(usage.completion_tokens, total + 4)
                self.assertTrue(usage.complete)

    def test_aborted_or_unknown_mode_never_complete(self):
        for kind, reason, prompt in [("DELTA", "abort", True), (None, "stop", True), ("CUMULATIVE", "stop", False)]:
            usage = EngineUsage(kind, 1)
            usage.observe(output({0: 5}, finished=True, reason=reason, prompt=prompt))
            self.assertFalse(usage.complete)

    async def test_terminal_receipt_handshake_persistence_and_auth(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {"SEAMPACT_USAGE_RECEIPT_DIR": directory}):
            app = App(Engine([output({0: 7}, finished=True, reason="stop")]))
            middleware = UsageReceiptMiddleware(app)
            messages = await self.invoke(middleware, app)
            advertised = dict(messages[0]["headers"])[b"x-seampact-usage-receipt"]
            self.assertEqual(advertised, b"/seampact/usage/seampact-test")
            receipt = ReceiptStore(directory).get("seampact-test")
            self.assertEqual(receipt["schema"], SCHEMA)
            self.assertEqual(receipt["status"], "completed")
            self.assertTrue(receipt["final_usage_complete"])
            self.assertEqual(receipt["usage"], {"prompt_tokens": 3, "completion_tokens": 7, "total_tokens": 10})
            self.assertIsNotNone(receipt["end"])
            persisted = (Path(directory) / "seampact-test.json").read_text()
            self.assertNotIn("PRIVATE", persisted)
            self.assertNotIn("Bearer", persisted)
            denied = await self.invoke(middleware, app, method="GET", path=advertised.decode(), auth=False)
            self.assertEqual(denied[0]["status"], 401)
            recovered = await self.invoke(middleware, app, method="GET", path=advertised.decode())
            self.assertEqual(json.loads(recovered[1]["body"]), receipt)
            duplicate = await self.invoke(middleware, app)
            self.assertEqual(duplicate[0]["status"], 409)

    async def test_cancellation_keeps_observed_partial_and_closes_engine(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {"SEAMPACT_USAGE_RECEIPT_DIR": directory}):
            engine = Engine([output({0: 4})], kind=Kind.DELTA, wait=True)
            app = App(engine)
            middleware = UsageReceiptMiddleware(app)
            task = asyncio.create_task(self.invoke(middleware, app))
            await asyncio.wait_for(engine.first.wait(), timeout=2)
            self.assertEqual(middleware.store.get("seampact-test")["status"], "running")
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            receipt = ReceiptStore(directory).get("seampact-test")
            self.assertEqual(receipt["status"], "partial")
            self.assertFalse(receipt["final_usage_complete"])
            self.assertEqual(receipt["usage"]["completion_tokens"], 4)
            self.assertTrue(engine.closed)

    async def test_no_output_is_unknown_not_zero(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {"SEAMPACT_USAGE_RECEIPT_DIR": directory}):
            app = App(Engine([]))
            middleware = UsageReceiptMiddleware(app)
            await self.invoke(middleware, app)
            receipt = ReceiptStore(directory).get("seampact-test")
            self.assertEqual(receipt["status"], "unknown")
            self.assertFalse(receipt["final_usage_complete"])
            self.assertEqual(receipt["usage"], dict.fromkeys(["prompt_tokens", "completion_tokens", "total_tokens"]))

    async def test_disabled_middleware_does_not_wrap_engine(self):
        with patch.dict("os.environ", {"SEAMPACT_USAGE_RECEIPT_DIR": ""}):
            app = App(Engine([]))
            middleware = UsageReceiptMiddleware(app)
            await self.invoke(middleware, app)
            self.assertIsNone(middleware.store)
            self.assertIsNone(app.endpoint)
            self.assertFalse(hasattr(app.state.engine_client, "_seampact_usage_wrapped"))


if __name__ == "__main__":
    unittest.main()
