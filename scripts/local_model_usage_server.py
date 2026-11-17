"""Private vLLM per-request usage receipts; no prompts, text or token IDs persist.

Opt in on the model host (repository root must be on PYTHONPATH)::

    SEAMPACT_USAGE_RECEIPT_DIR=/private/var/model-usage \
      vllm serve ... --middleware scripts.local_model_usage_server.UsageReceiptMiddleware

Send a unique X-Request-Id for each POST to /v1/chat/completions or
/v1/completions. The response advertises X-Seampact-Usage-Receipt. GET that
same-origin path to retrieve the receipt, including after a disconnected client.
The alias /seampact/usage/{id} passes through the original /v1 authentication.
The directory and endpoint belong on the existing private model service only.

``completed`` means all generate() calls returned finished RequestOutputs with
known token-count semantics. Counts match vLLM API token usage, including encoder
prompt tokens and all completion choices. DELTA adds lengths per choice;
CUMULATIVE and FINAL_ONLY replace them. No tokenizer or text-length estimates are
used. Counts in a partial/running receipt are observations, never final costs.
Cancellation/abort may discard engine outputs not yet yielded by generate();
there is no abort-final-usage API here. Those receipts stay incomplete, even if
the last observed counts look plausible. Worker death can leave a running receipt
on disk; that also does not establish final usage. This does not measure rejected
speculative tokens, physical GPU work or other engine-internal accounting.

Only explicitly enabled services are changed. This module has no vLLM dependency
at import time and does not modify generation, sampling or abort algorithms.
"""

from __future__ import annotations

import contextvars
import functools
import json
import logging
import os
import re
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA = "seampact.local_model_usage.v1"
RECEIPT_PREFIX = "/seampact/usage/"
_GUARDED_PREFIX = "/v1/seampact/usage/"
_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_TRACKED_PATHS = {"/v1/chat/completions", "/v1/completions"}
_CURRENT: contextvars.ContextVar[UsageReceipt | None] = contextvars.ContextVar(
    "seampact_usage_receipt", default=None
)
_LOG = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class EngineUsage:
    """Lengths only: never retain prompt/output text or the token ID arrays."""

    output_kind: str | None
    expected_choices: int | None
    prompt_tokens: int | None = None
    completion_lengths: dict[int, int] = field(default_factory=dict)
    finished_choices: set[int] = field(default_factory=set)
    finished: bool = False
    invalid: bool = False

    def observe(self, output: Any) -> None:
        prompt_ids = getattr(output, "prompt_token_ids", None)
        if prompt_ids is not None:
            self.prompt_tokens = len(prompt_ids)
            encoder_ids = getattr(output, "encoder_prompt_token_ids", None)
            if encoder_ids is not None:
                self.prompt_tokens += len(encoder_ids)
        if getattr(output, "error", None) is not None:
            self.invalid = True
        for choice in getattr(output, "outputs", ()):
            index = getattr(choice, "index", None)
            ids = getattr(choice, "token_ids", None)
            if not isinstance(index, int) or index < 0 or ids is None:
                self.invalid = True
                continue
            if self.output_kind == "DELTA":
                self.completion_lengths[index] = self.completion_lengths.get(index, 0) + len(ids)
            elif self.output_kind in {"CUMULATIVE", "FINAL_ONLY"}:
                self.completion_lengths[index] = len(ids)
            reason = getattr(choice, "finish_reason", None)
            if reason in {"stop", "length"}:
                self.finished_choices.add(index)
            elif reason is not None:
                self.invalid = True
        self.finished = self.finished or bool(getattr(output, "finished", False))

    @property
    def completion_tokens(self) -> int | None:
        return sum(self.completion_lengths.values()) if self.completion_lengths else None

    @property
    def complete(self) -> bool:
        return (
            self.finished
            and not self.invalid
            and self.prompt_tokens is not None
            and self.output_kind in {"DELTA", "CUMULATIVE", "FINAL_ONLY"}
            and self.expected_choices is not None
            and set(self.completion_lengths) == set(range(self.expected_choices))
            and self.finished_choices == set(range(self.expected_choices))
        )


@dataclass
class UsageReceipt:
    request_id: str
    start: str = field(default_factory=_now)
    end: str | None = None
    engines: list[EngineUsage] = field(default_factory=list)

    def snapshot(self) -> dict[str, Any]:
        complete = self.end is not None and bool(self.engines) and all(
            engine.complete for engine in self.engines
        )
        observed = any(
            engine.prompt_tokens is not None or engine.completion_tokens is not None
            for engine in self.engines
        )
        prompt = self._sum_known("prompt_tokens")
        completion = self._sum_known("completion_tokens")
        return {
            "schema": SCHEMA,
            "request_id": self.request_id,
            "start": self.start,
            "end": self.end,
            "status": "running" if self.end is None else (
                "completed" if complete else "partial" if observed else "unknown"
            ),
            "final_usage_complete": complete,
            "usage": {
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "total_tokens": prompt + completion if prompt is not None and completion is not None else None,
            },
            "source": "vllm.RequestOutput.token_ids",
        }

    def _sum_known(self, name: str) -> int | None:
        values = [getattr(engine, name) for engine in self.engines]
        return sum(values) if values and all(value is not None for value in values) else None


class ReceiptStore:
    def __init__(self, directory: str | Path):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.active: dict[str, UsageReceipt] = {}

    def reserve(self, request_id: str) -> UsageReceipt:
        receipt = UsageReceipt(request_id)
        path = self.directory / (request_id + ".json")
        # O_EXCL also rejects reuse across API workers and service restarts.
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(receipt.snapshot(), stream, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        self.active[request_id] = receipt
        return receipt

    def save(self, receipt: UsageReceipt) -> None:
        temporary: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.directory, prefix=".usage-", delete=False
            ) as stream:
                temporary = stream.name
                json.dump(receipt.snapshot(), stream, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.directory / (receipt.request_id + ".json"))
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)

    def get(self, request_id: str) -> dict[str, Any] | None:
        if not _ID.fullmatch(request_id):
            return None
        if request_id in self.active:
            return self.active[request_id].snapshot()
        try:
            return json.loads((self.directory / (request_id + ".json")).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None


def _wrap_engine(engine: Any, store: ReceiptStore) -> None:
    if getattr(engine, "_seampact_usage_wrapped", False):
        return
    original = engine.generate

    @functools.wraps(original)
    def generate(*args: Any, **kwargs: Any):
        receipt = _CURRENT.get()
        if receipt is None:
            return original(*args, **kwargs)
        params = kwargs.get("sampling_params", args[1] if len(args) > 1 else None)
        mode = getattr(getattr(params, "output_kind", None), "name", None)
        choices = getattr(params, "n", None)
        observation = EngineUsage(mode, choices if isinstance(choices, int) and choices > 0 else None)
        receipt.engines.append(observation)

        async def counted():
            iterator = original(*args, **kwargs)
            try:
                async for output in iterator:
                    observation.observe(output)
                    yield output
            finally:
                try:
                    # Preserve vLLM's generator-close/abort cleanup on early exits.
                    await iterator.aclose()
                finally:
                    if receipt.end is not None:
                        _save_safely(store, receipt)

        return counted()

    engine.generate = generate
    engine._seampact_usage_wrapped = True


def _save_safely(store: ReceiptStore, receipt: UsageReceipt) -> None:
    try:
        store.save(receipt)
    except OSError as error:
        # Never log exception messages, which may contain paths or request data.
        _LOG.error("Usage receipt persistence failed: %s", type(error).__name__)


async def _json_response(send: Any, status: int, value: dict[str, Any]) -> None:
    body = json.dumps(value, separators=(",", ":")).encode("utf-8")
    await send({"type": "http.response.start", "status": status, "headers": [
        (b"content-type", b"application/json"), (b"cache-control", b"no-store"),
        (b"content-length", str(len(body)).encode("ascii")),
    ]})
    await send({"type": "http.response.body", "body": body})


class _ReceiptEndpoint:
    def __init__(self, store: ReceiptStore):
        self.store = store

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        receipt = self.store.get(scope["path_params"]["request_id"])
        await _json_response(send, 200 if receipt else 404, receipt or {"error": "receipt_not_found"})


class UsageReceiptMiddleware:
    def __init__(self, app: Any):
        self.app = app
        directory = os.environ.get("SEAMPACT_USAGE_RECEIPT_DIR")
        self.store = ReceiptStore(directory) if directory else None
        self._registered = False

    def _install(self, application: Any) -> None:
        if not self._registered:
            application.add_route(
                _GUARDED_PREFIX + "{request_id}", _ReceiptEndpoint(self.store), methods=["GET"]
            )
            self._registered = True
        _wrap_engine(application.state.engine_client, self.store)

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if self.store is None or scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        self._install(scope["app"])
        path = scope["path"].removeprefix(scope.get("root_path", ""))
        if path.startswith(RECEIPT_PREFIX):
            # Keep the advertised alias behind vLLM's original /v1 auth middleware.
            forwarded = dict(scope)
            forwarded["path"] = scope.get("root_path", "") + _GUARDED_PREFIX + path[len(RECEIPT_PREFIX):]
            forwarded["raw_path"] = forwarded["path"].encode("ascii", errors="replace")
            await self.app(forwarded, receive, send)
            return
        if scope.get("method") != "POST" or path not in _TRACKED_PATHS:
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers", ()))
        request_id = headers.get(b"x-request-id", b"").decode("ascii", errors="replace") or "seampact-" + uuid.uuid4().hex
        if not _ID.fullmatch(request_id):
            await _json_response(send, 400, {"error": "invalid_request_id"})
            return
        try:
            receipt = self.store.reserve(request_id)
        except FileExistsError:
            await _json_response(send, 409, {"error": "request_id_already_used"})
            return
        except OSError:
            await _json_response(send, 503, {"error": "usage_receipt_unavailable"})
            return
        token = _CURRENT.set(receipt)

        async def advertised_send(message: Any) -> None:
            if message["type"] == "http.response.start":
                message = dict(message)
                message["headers"] = list(message.get("headers", ())) + [
                    (b"x-seampact-usage-receipt", (RECEIPT_PREFIX + request_id).encode("ascii")),
                ]
            await send(message)

        try:
            await self.app(scope, receive, advertised_send)
        finally:
            receipt.end = _now()
            _save_safely(self.store, receipt)
            self.store.active.pop(request_id, None)
            _CURRENT.reset(token)
