"""Unavailable source evidence must not abort a paid agent tool loop."""

import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from seampact.analysis import AnalysisJobs
from seampact.inspection import Inspector
from seampact.store import Store


SOURCE = "def native(value):\n    return value\n"


class ControlledProfiles:
    def profile(self, provider):
        return dict(provider=provider, base_url="https://controlled.invalid", model="controlled-model")

    def key(self, provider):
        return "controlled-test-key"


def call(identity, name, arguments):
    return dict(id=identity, type="function",
                function=dict(name=name, arguments=json.dumps(arguments)))


def response(calls):
    return dict(choices=[dict(finish_reason="tool_calls",
        message=dict(role="assistant", content=None, tool_calls=calls))])


class ControlledTransport:
    def __init__(self, replies):
        self.replies, self.requests = list(replies), []

    def send(self, url, key, body=None):
        self.requests.append(copy.deepcopy(body))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def finished(store, identity):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        job = store.get("analysis", identity)
        if job.get("finished_at"):
            return job
        time.sleep(.01)
    raise AssertionError("controlled analysis did not finish")


class AnalysisSourceErrorTests(unittest.TestCase):
    def test_missing_or_denied_extension_is_durable_unknown_and_agent_can_refine(self):
        for denied in (False, True):
            with self.subTest(denied=denied), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / "native.py").write_text(SOURCE, encoding="utf-8")
                inspector = Inspector.freeze(root, root / "snapshots", "request", ["native.py"],
                                             "native", "original result")
                original_request = copy.deepcopy(inspector.request)
                candidate = dict(boundaries=[dict(id="native", path="native.py", symbol="native", mode="W")],
                                 missing=["unavailable.py remains UNKNOWN"])
                transport = ControlledTransport([
                    response([call("missing", "add_source", {"path": "unavailable.py"}),
                              call("read", "read_file", {"path": "native.py"})]),
                    response([call("submit", "submit_candidate", {"candidate": candidate})]),
                ])
                store = Store(root / "state.sqlite3")
                try:
                    store.save("request", "request", inspector.request, "scope_frozen")
                    jobs = AnalysisJobs(store, ControlledProfiles(), transport)
                    if denied:
                        error = PermissionError(13, "PRIVATE_ERROR_CONTENT", "/private/absolute/source.py")
                        with patch.object(inspector, "add_source", side_effect=error):
                            job = finished(store, jobs.start(inspector, "glm"))
                    else:
                        job = finished(store, jobs.start(inspector, "glm"))
                    self.assertEqual(job["status"], "COMPLETED")
                    self.assertEqual(job["rounds"], 2)
                    self.assertEqual(len(transport.requests), 2)
                    self.assertEqual(inspector.request, original_request)
                    self.assertEqual(store.get("request", "request"), original_request)
                    tools = [m for m in job["messages"] if m["role"] == "tool"]
                    self.assertEqual([m["tool_call_id"] for m in tools], ["missing", "read", "submit"])
                    unavailable = json.loads(tools[0]["content"])
                    self.assertEqual(unavailable["schema"], "source-tool-error/1")
                    self.assertEqual(unavailable["status"], "UNKNOWN")
                    self.assertEqual(unavailable["reason"], "source_permission_denied" if denied else "source_artifact_missing")
                    self.assertFalse(unavailable["admission_granted"])
                    self.assertNotIn(str(root), tools[0]["content"])
                    self.assertNotIn("/private/absolute", tools[0]["content"])
                    self.assertNotIn("PRIVATE_ERROR_CONTENT", tools[0]["content"])
                    # The next request includes both the error and successful read;
                    # it is a new agent round, not a replay of the paid request.
                    next_tools = [m for m in transport.requests[1]["messages"] if m["role"] == "tool"]
                    self.assertEqual(next_tools, tools[:2])
                    self.assertIn("return value", tools[1]["content"])
                    self.assertFalse(job["submitted"]["report"]["admitted"])
                finally:
                    store.close()

    def test_transport_oserror_remains_failed_without_paid_replay(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "native.py").write_text(SOURCE, encoding="utf-8")
            inspector = Inspector.freeze(root, root / "snapshots", "request", ["native.py"], "native", "result")
            store = Store(root / "state.sqlite3")
            transport = ControlledTransport([OSError("PRIVATE_TRANSPORT_CONTENT")])
            try:
                job = finished(store, AnalysisJobs(store, ControlledProfiles(), transport).start(inspector, "glm"))
                self.assertEqual(job["status"], "FAILED")
                self.assertEqual(job["error"], "OSError")
                self.assertTrue(job["pending_response"])
                self.assertEqual(len(transport.requests), 1)
                self.assertFalse(any(m["role"] == "tool" for m in job["messages"]))
            finally:
                store.close()

    def test_extension_journal_oserror_stops_before_a_further_paid_round(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("native.py", "dependency.py"):
                (root / name).write_text(SOURCE, encoding="utf-8")
            inspector = Inspector.freeze(root, root / "snapshots", "request", ["native.py"], "native", "result")
            store = Store(root / "state.sqlite3")
            transport = ControlledTransport([response([call("extend", "add_source", {"path": "dependency.py"})])])
            original_save = store.save
            def save(kind, identity, value, event):
                if event == "source_extended":
                    raise OSError("PRIVATE_JOURNAL_CONTENT")
                return original_save(kind, identity, value, event)
            try:
                with patch.object(store, "save", side_effect=save):
                    job = finished(store, AnalysisJobs(store, ControlledProfiles(), transport).start(inspector, "glm"))
                self.assertEqual(job["status"], "FAILED")
                self.assertEqual(job["error"], "OSError")
                self.assertFalse(job["pending_response"])
                self.assertEqual(len(transport.requests), 1)
                self.assertFalse(any(m["role"] == "tool" for m in job["messages"]))
                self.assertEqual(job["messages"][-1]["tool_calls"][0]["id"], "extend")
            finally:
                store.close()
