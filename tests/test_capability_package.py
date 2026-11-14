"""A generated capability package through its whole life.

The offline checks run the ordinary Platform, instance manager and worker in
one process: install, bind, export, register, call with events and files,
cancel, drain and close. The opt-in class repeats the path with a real image
and container (set SEAMPACT_DOCKER_TESTS=1).
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from seampact.capability_contract import (
    IR_SCHEMA, MANIFEST, PACKAGE_SCHEMA, ContractError, check_manifest, check_schema, digest, load_package,
    package_digest, scan_files, validate_value,
)
from seampact.capability_worker import CapabilityWorker, make_worker_server
from seampact.hosting import capability_hosting
from seampact.hosting.capability_hosting import CapabilityError
from seampact.hosting.deployment import Deployments, IDENTITY_PROTOCOL
from seampact.runtime import ProtocolError
from seampact.server import make_server
from seampact.service import Platform

ADAPTER = '''"""Generated adapter of the example package."""
import time

from wordlib import load_vocabulary


class WordTools:
    def initialize(self, bindings):
        self.greeting = bindings["greeting"]
        self.vocabulary = load_vocabulary()
        self.calls = 0

    def execute(self, input, context):
        self.calls += 1
        context.emit("progress", {"stage": "counting"})
        words = input["text"].split()
        if input.get("fail"):
            raise RuntimeError("requested failure")
        return {"greeting": self.greeting, "words": len(words),
                "known": sum(word in self.vocabulary for word in words), "calls": self.calls}

    def export_report(self, input, context):
        lines = sorted(set(input["text"].split()))
        (context.output_dir / "report.txt").write_text("\\n".join(lines), encoding="utf-8")
        return {"lines": len(lines)}

    def wait(self, input, context):
        context.emit("waiting", None)
        deadline = time.monotonic() + input["seconds"]
        while time.monotonic() < deadline:
            if input.get("cooperate", True):
                context.stop_if_cancelled()
            time.sleep(0.02)
        return {"waited": True}

    def close(self):
        self.vocabulary = None
'''

WORDLIB = '''from pathlib import Path


def load_vocabulary():
    return set(Path(__file__).with_name("vocabulary.txt").read_text(encoding="utf-8").split())
'''

TEXT = dict(type="object", properties=dict(text=dict(type="string"), fail=dict(type="boolean")),
            required=["text"], additionalProperties=False)


def write_example_package(root, concurrency=1, replicable=False):
    """A package as a generator would write it: adapter, reused source and manifest."""
    root = Path(root)
    (root / "adapter").mkdir(parents=True)
    (root / "source").mkdir()
    (root / "adapter" / "__init__.py").write_text(ADAPTER, encoding="utf-8")
    (root / "source" / "wordlib.py").write_text(WORDLIB, encoding="utf-8")
    (root / "source" / "vocabulary.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    provenance = [dict(path="wordlib.py", start=4, end=5)]
    manifest = dict(schema=PACKAGE_SCHEMA, name="word-tools", source=dict(id="example-source", version="v1"),
        generator=dict(template="python-adapter/1"),
        environment=dict(python=".".join(map(str, sys.version_info[:2])), requirements=[], packages=[]),
        implementation=dict(module="adapter", symbol="WordTools"),
        instance=dict(concurrency=concurrency, replicable=replicable, cancel="cooperative"),
        resources=[dict(id="greeting", kind="parameter", required=True, description="结果中使用的问候语")],
        capabilities=[
            dict(schema=IR_SCHEMA, id="count_words", name="统计词数", purpose="统计文本词数及词表内词数",
                 input=TEXT, output=dict(kind="json", schema=dict(type="object", required=["words", "known"],
                     properties=dict(words=dict(type="integer"), known=dict(type="integer"),
                                     greeting=dict(type="string"), calls=dict(type="integer")))),
                 state=dict(scope="instance"), resources=["greeting"], provenance=provenance),
            dict(schema=IR_SCHEMA, id="export_report", name="导出词表", purpose="把文本中的不同词写成文件",
                 method="export_report", input=TEXT, output=dict(kind="files"), state=dict(scope="call"),
                 effects=[dict(kind="filesystem", access="write", target="调用输出目录")]),
            dict(schema=IR_SCHEMA, id="wait", name="等待", purpose="等待指定秒数", method="wait",
                 input=dict(type="object", properties=dict(seconds=dict(type="number", minimum=0, maximum=30),
                                                           cooperate=dict(type="boolean")), required=["seconds"]),
                 output=dict(kind="json"), state=dict(scope="call"))],
        files=scan_files(root))
    (root / MANIFEST).write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def wait_for(condition, timeout=60, message="condition not reached"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = condition()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(message)


class ContractTests(unittest.TestCase):
    def test_interface_schema_is_only_what_is_enforced(self):
        check_schema(TEXT)
        with self.assertRaises(ContractError):
            check_schema(dict(type="object", patternProperties={}))
        validate_value(dict(text="a"), TEXT)
        for bad in (dict(), dict(text=1), dict(text="a", other=1), dict(text="a", fail=1)):
            with self.assertRaises(ContractError):
                validate_value(bad, TEXT)
        with self.assertRaises(ContractError):
            validate_value(True, dict(type="integer"))

    def test_package_identity_covers_code_source_and_description(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "package"
            manifest = write_example_package(root)
            self.assertEqual(load_package(root), manifest)
            identity = package_digest(manifest)
            changed = json.loads(json.dumps(manifest))
            changed["capabilities"][0]["purpose"] += "。"
            self.assertNotEqual(package_digest(changed), identity)
            (root / "source" / "vocabulary.txt").write_text("alpha\n", encoding="utf-8")
            with self.assertRaisesRegex(ContractError, "身份不一致"):
                load_package(root)
            (root / "source" / "vocabulary.txt").write_text("alpha\nbeta\n", encoding="utf-8")
            (root / "adapter" / "extra.py").write_text("", encoding="utf-8")
            with self.assertRaisesRegex(ContractError, "清单之外"):
                load_package(root)

    def test_description_must_be_complete_and_grounded(self):
        with tempfile.TemporaryDirectory() as temporary:
            manifest = write_example_package(Path(temporary) / "package")
            for change in (lambda m: m["capabilities"][0].pop("state"),
                           lambda m: m["capabilities"][0].update(resources=["absent"]),
                           lambda m: m["capabilities"][0]["provenance"][0].update(path="missing.py"),
                           lambda m: m["capabilities"][1].update(method="execute"),
                           lambda m: m["implementation"].update(module="other"),
                           lambda m: m["instance"].pop("cancel")):
                broken = json.loads(json.dumps(manifest))
                change(broken)
                with self.assertRaises(ContractError):
                    check_manifest(broken)


class IdleReplicaSafety(unittest.TestCase):
    def test_idle_replica_rechecks_busy_affinity_and_last_ready_before_draining(self):
        controller = Deployments.__new__(Deployments)
        controller.lock = threading.RLock()
        controller.affinity = {}
        first = dict(id="first", group_id="group", state="READY", accepting=True,
                     in_flight=0, released=False, pending_stop=None)
        second = dict(id="second", group_id="group", state="READY", accepting=True,
                      in_flight=0, released=False, pending_stop=None)
        controller.instances = {"first": first, "second": second}
        group = dict(contract=dict(replicas=dict(replicable=True),
                                   operations=dict(drain_stop=dict(status="SUPPORTED"))))
        controller.platform = SimpleNamespace(store=SimpleNamespace(get=lambda kind, identity: group))
        controller._guard = lambda *args: None
        controller._instances = lambda group_id, states=None: [row for row in controller.instances.values()
            if row["group_id"] == group_id and (states is None or row["state"] in states)]
        events = []
        controller._event = lambda item, kind, **facts: events.append((item["id"], kind))
        controller._advance_stops = lambda: None
        controller._notify = lambda *args: self.fail("fixture has no endpoint to notify")
        controller._view = lambda item: dict(item)
        request = dict(instance_id="first", idle_replica=True)

        first["in_flight"] = 1
        self.assertEqual(controller.drain_stop("group", request)["status"], "SKIPPED")
        first["in_flight"] = 0
        controller.affinity[("task", "group")] = "first"
        self.assertEqual(controller.drain_stop("group", request)["status"], "SKIPPED")
        controller.affinity.clear()
        second.update(state="STARTING", accepting=False)
        self.assertEqual(controller.drain_stop("group", request)["status"], "SKIPPED")
        self.assertEqual(first["state"], "READY")
        self.assertFalse(events)

        second.update(state="READY", accepting=True)
        outcome = controller.drain_stop("group", request)
        self.assertEqual([row["id"] for row in outcome["instances"]], ["first"])
        self.assertEqual(first["state"], "DRAINING")
        self.assertFalse(first["accepting"])
        self.assertEqual(second["state"], "READY")
        self.assertEqual(events, [("first", "drain_requested")])


class PackageLifecycle(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="seampact-capability-")
        self.root = Path(self.directory.name)
        self.platform = Platform(self.root / "platform")
        self.server = make_server(self.platform, 0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.coordinator = "http://127.0.0.1:" + str(self.server.server_port)
        self.platform.deployments.configure_backend(dict(kind="docker", coordinator_url=self.coordinator,
                                                         image_access="preloaded"))
        self.modules, self.paths = set(sys.modules), list(sys.path)
        self.workers = []

    def tearDown(self):
        for worker, server in self.workers:
            server.shutdown()
            server.server_close()
            worker.close()
        self.server.shutdown()
        self.server.server_close()
        self.platform.close()
        for name in set(sys.modules) - self.modules:
            if name in {"adapter", "wordlib"}:
                del sys.modules[name]
        sys.path[:] = self.paths
        self.directory.cleanup()

    def install(self, concurrency=1, replicable=False):
        write_example_package(self.root / "generated", concurrency, replicable)
        installation = self.platform.capabilities.install(self.root / "generated")
        self.group = installation["group_id"]
        return installation

    def reported_image(self):
        """Stand in for the image build; everything else is the ordinary path."""
        deployments = self.platform.deployments
        artifact = deployments.export(self.group, {})
        deployments.build(self.group, dict(operator_reported_image="example/word-tools:1"))
        deployments.publish(self.group, {})
        wait_for(lambda: self.platform.store.get("deployment", self.group)["publish"]["status"] != "RUNNING")
        return artifact

    def start_worker(self, artifact):
        worker = CapabilityWorker(Path(artifact["folder"]) / "worker.json", self.coordinator,
                                  instance_id=uuid.uuid4().hex, generation=1)
        server = make_worker_server(worker, "127.0.0.1", 0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.workers.append((worker, server))
        record = self.platform.deployments.attach("http://127.0.0.1:" + str(server.server_port))
        return worker, record

    def units(self):
        return {unit["entry_id"]: unit for unit in self.platform.capabilities.public()}

    def test_missing_resource_is_reported_and_withholds_start(self):
        installation = self.install()
        self.assertEqual(installation["availability"]["status"], "NEEDS_INPUT")
        self.assertIn("greeting", installation["availability"]["reason"])
        unit = self.units()["count_words"]
        self.assertEqual(unit["status"], "NEEDS_INPUT")
        self.assertEqual(self.platform.capabilities.tools(), [])
        with self.assertRaisesRegex(ValueError, "greeting"):
            self.platform.capabilities.invoke(unit["id"], dict(text="alpha"))
        with self.assertRaisesRegex(ValueError, "greeting"):
            self.platform.deployments.start(self.group, {})
        with self.assertRaises(ContractError):
            self.platform.capabilities.bind(self.group, dict(greeting=dict(form="path", path="/x")))
        bound = self.platform.capabilities.bind(self.group, dict(greeting=dict(form="value", value="hi")))
        self.assertEqual(bound["availability"]["status"], "NOT_PREPARED")
        self.assertNotEqual(bound["installation_id"], installation["installation_id"])
        self.assertEqual({unit["status"] for unit in self.units().values()}, {"READY"})

    def test_install_initialize_call_deliver_and_close(self):
        self.install()
        self.platform.capabilities.bind(self.group, dict(greeting=dict(form="value", value="hi")))
        artifact = self.reported_image()
        folder = Path(artifact["folder"])
        self.assertEqual(sorted(path.name for path in (folder / "core" / "seampact").iterdir()),
                         ["__init__.py", "capability_contract.py", "capability_worker.py"])
        self.assertIn("seampact.capability_worker", (folder / "Dockerfile").read_text(encoding="utf-8"))
        capabilities = self.platform.capabilities
        self.assertEqual(capabilities.installation(self.group)["availability"]["status"], "AVAILABLE")
        units = self.units()
        self.assertEqual({unit["dispatch"]["status"] for unit in units.values()}, {"COLD_STARTABLE"})
        tools = {tool["function"]["name"]: tool["function"] for tool in capabilities.tools()}
        self.assertEqual(tools[units["count_words"]["tool_name"]]["parameters"], TEXT)

        worker, record = self.start_worker(artifact)
        self.assertEqual(IDENTITY_PROTOCOL, worker.health()["identity_protocol"])
        self.assertEqual((record["state"], record["profile"]), ("READY", capability_hosting.WORKER_PROFILE))
        self.assertEqual(worker.state, "NOT_RUN")

        events = []
        first = capabilities.invoke(units["count_words"]["id"], dict(text="alpha gamma"), on_event=events.append)
        self.assertEqual(first["output"], dict(greeting="hi", words=2, known=1, calls=1))
        self.assertEqual([event["kind"] for event in events], ["call_started", "progress"])
        self.assertEqual(events[1]["data"], dict(stage="counting"))
        self.assertEqual((first["call"]["initialization_before"], first["call"]["initialization_after"],
                          first["call"]["startup_kind"]), ("NOT_RUN", "BOUND", "COLD_INITIALIZE"))
        second = capabilities.invoke(units["count_words"]["id"], dict(text="beta"))
        self.assertEqual((second["output"]["calls"], second["call"]["startup_kind"]), (2, "WARM_REUSE"))
        self.assertEqual(second["call"]["instance_id"], record["id"])

        report = capabilities.invoke(units["export_report"]["id"], dict(text="b a b"))
        self.assertEqual([item["name"] for item in report["files"]], ["report.txt"])
        delivered = Path(report["files"][0]["artifact"])
        self.assertEqual(delivered.read_text(encoding="utf-8"), "a\nb")
        self.assertEqual(digest(delivered.read_bytes()), report["files"][0]["sha256"])
        self.assertEqual(worker.calls, {})

        with self.assertRaises(ContractError):
            capabilities.invoke(units["count_words"]["id"], dict(text=3))
        with self.assertRaises(CapabilityError) as failure:
            capabilities.invoke(units["count_words"]["id"], dict(text="a", fail=True))
        self.assertEqual(failure.exception.kind, "RuntimeError")
        self.assertIn("requested failure", failure.exception.trace)
        self.assertEqual(self.platform.deployments.instances[record["id"]]["state"], "READY")
        self.assertEqual(self.platform.deployments.instances[record["id"]]["in_flight"], 0)

        self.platform.deployments.drain_stop(self.group, {})
        wait_for(lambda: worker.closed, message="adapter was not closed after draining")
        self.assertEqual(worker.closed, dict(status="CLOSED"))
        self.assertFalse(worker.accepting)

    def test_export_releases_controller_lock_and_rejects_changed_installation(self):
        self.install()
        entered, release = threading.Event(), threading.Event()
        package = self.platform.capabilities.package
        result = {}

        def paused_package(identity):
            if threading.current_thread().name == "test-package-export":
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("test export wait expired")
            return package(identity)

        def export():
            try:
                result["artifact"] = self.platform.deployments.export(self.group, {})
            except Exception as error:
                result["error"] = error

        with patch.object(self.platform.capabilities, "package", side_effect=paused_package):
            worker = threading.Thread(target=export, name="test-package-export")
            worker.start()
            self.assertTrue(entered.wait(2))
            try:
                available = self.platform.deployments.lock.acquire(timeout=0.5)
                if available:
                    self.platform.deployments.lock.release()
                self.platform.capabilities.bind(self.group, dict(greeting=dict(form="value", value="changed")))
            finally:
                release.set()
                worker.join(5)
        self.assertTrue(available, "package I/O must not hold the controller lock")
        self.assertFalse(worker.is_alive())
        self.assertIsInstance(result.get("error"), ValueError)
        self.assertIn("安装已改变", str(result["error"]))
        self.assertNotIn("artifact", result)
        self.assertIsNone(self.platform.store.get("deployment", self.group))

    def test_concurrency_limit_and_cancellation_states(self):
        self.install()
        self.platform.capabilities.bind(self.group, dict(greeting=dict(form="value", value="hi")))
        worker, record = self.start_worker(self.reported_image())
        capabilities, units = self.platform.capabilities, self.units()
        started, outcome = [], {}

        def run(key, seconds, cooperate):
            try:
                outcome[key] = capabilities.invoke(units["wait"]["id"], dict(seconds=seconds, cooperate=cooperate),
                                                   on_event=started.append)
            except Exception as error:
                outcome[key] = error

        thread = threading.Thread(target=run, args=("cooperative", 20, True))
        thread.start()
        call_id = wait_for(lambda: [e for e in started if e["kind"] == "waiting"])[0]["call_id"]
        # The instance itself refuses work beyond its declared concurrency.
        with self.assertRaisesRegex(ProtocolError, "并发上限"):
            capability_hosting.call(self.platform.deployments, self.platform.deployments.instances[record["id"]],
                                    units["count_words"], dict(text="a"))
        self.assertEqual(capabilities.cancel(call_id), dict(requested=True, mode="cooperative", reason=None))
        thread.join(30)
        self.assertIsInstance(outcome["cooperative"], CapabilityError)
        self.assertEqual(outcome["cooperative"].kind, "Cancelled")
        self.assertEqual(outcome["cooperative"].facts["cancel"], dict(requested=True, confirmed=True))
        self.assertFalse(capabilities.cancel(call_id)["requested"])

        # Work that ignores the request still completes: requested, never confirmed.
        del started[:]
        thread = threading.Thread(target=run, args=("ignoring", 1.5, False))
        thread.start()
        call_id = wait_for(lambda: [e for e in started if e["kind"] == "waiting"])[0]["call_id"]
        self.assertTrue(capabilities.cancel(call_id)["requested"])
        self.assertEqual(worker.in_flight, 1)
        thread.join(30)
        self.assertEqual(outcome["ignoring"]["output"], dict(waited=True))
        self.assertEqual(outcome["ignoring"]["cancel"], dict(requested=True, confirmed=False))

    def test_replica_capacity_and_task_affinity_are_reserved_atomically(self):
        self.install(replicable=True)
        self.platform.capabilities.bind(self.group, dict(greeting=dict(form="value", value="hi")))
        artifact = self.reported_image()
        _, first = self.start_worker(artifact)
        _, second = self.start_worker(artifact)
        capabilities, units = self.platform.capabilities, self.units()
        waiting, same_started = threading.Event(), threading.Event()
        results, errors = {}, []

        def invoke(key, task_id, unit, args, on_event=None):
            try:
                with self.platform.runtime.context(task_id):
                    results[key] = capabilities.invoke(unit, args, on_event=on_event)
            except Exception as error:
                errors.append(error)

        def events(event):
            if event["kind"] == "waiting":
                results["first_instance"] = event["instance_id"]
                waiting.set()

        first_call = threading.Thread(target=invoke, args=("first", "same-task", units["wait"]["id"],
            dict(seconds=1.5), events))
        same_call = threading.Thread(target=invoke, args=("same", "same-task", units["count_words"]["id"],
            dict(text="alpha"), lambda event: same_started.set() if event["kind"] == "call_started" else None))
        other_call = threading.Thread(target=invoke, args=("other", "other-task", units["count_words"]["id"],
            dict(text="beta")))
        first_call.start()
        self.assertTrue(waiting.wait(5))
        same_call.start()
        self.assertFalse(same_started.wait(0.15), "an affinity-bound call must wait for its busy instance")
        other_call.start()
        other_call.join(3)
        self.assertFalse(other_call.is_alive(), "an idle replica should serve a different task")
        self.assertNotEqual(results["other"]["call"]["instance_id"], results["first_instance"])
        first_call.join(5)
        same_call.join(5)
        self.assertFalse(first_call.is_alive() or same_call.is_alive())
        self.assertFalse(errors)
        self.assertEqual(results["same"]["call"]["instance_id"], results["first_instance"])
        self.assertEqual({first["id"], second["id"]},
                         {results["first_instance"], results["other"]["call"]["instance_id"]})

    def test_drain_waits_for_an_already_reserved_capability_call(self):
        self.install()
        self.platform.capabilities.bind(self.group, dict(greeting=dict(form="value", value="hi")))
        worker, record = self.start_worker(self.reported_image())
        entered, resume = threading.Event(), threading.Event()
        real_call, outcome = capability_hosting.call, {}

        def pause_before_worker(*args, **kwargs):
            entered.set()
            if not resume.wait(5):
                raise RuntimeError("test reservation was not resumed")
            return real_call(*args, **kwargs)

        def invoke():
            try:
                outcome["result"] = self.platform.capabilities.invoke(
                    self.units()["count_words"]["id"], dict(text="alpha"))
            except Exception as error:
                outcome["error"] = error

        with patch.object(capability_hosting, "call", side_effect=pause_before_worker):
            thread = threading.Thread(target=invoke)
            thread.start()
            self.assertTrue(entered.wait(5))
            try:
                result = self.platform.deployments.drain_stop(self.group, {"instance_id": record["id"]})
                self.assertEqual(result["instances"][0]["state"], "DRAINING")
                self.assertTrue(worker.accepting, "a reserved call has not reached the worker yet")
            finally:
                resume.set()
                thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertNotIn("error", outcome)
        self.assertEqual(outcome["result"]["output"]["words"], 1)

    def test_simultaneous_cold_capability_calls_share_one_start(self):
        self.install()
        self.platform.capabilities.bind(self.group, dict(greeting=dict(form="value", value="hi")))
        artifact, unit = self.reported_image(), self.units()["wait"]["id"]
        gate, starts, results = threading.Barrier(2), [], []

        def start_once(placement, unit_id, *, bind_affinity=True):
            self.assertFalse(bind_affinity)
            starts.append(placement["group_id"])
            time.sleep(0.1)
            _, record = self.start_worker(artifact)
            return record

        def call(task):
            try:
                gate.wait(3)
                with self.platform.runtime.context(task):
                    results.append(self.platform.capabilities.invoke(unit, dict(seconds=0.2)))
            except Exception as error:
                results.append(error)

        with patch.object(self.platform.deployments, "_ensure", side_effect=start_once):
            threads = [threading.Thread(target=call, args=(f"cold-{i}",)) for i in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(8)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(len(starts), 1)
        self.assertEqual(len(results), 2)
        self.assertTrue(all(isinstance(item, dict) and item["output"]["waited"] for item in results), results)

    def test_completed_task_cleanup_does_not_wait_for_another_tasks_shared_worker_call(self):
        self.install()
        self.platform.capabilities.bind(self.group, dict(greeting=dict(form="value", value="hi")))
        self.start_worker(self.reported_image())
        capabilities, units = self.platform.capabilities, self.units()
        events, other_started = [], threading.Event()
        with self.platform.runtime.context("finished-task"):
            completed = capabilities.invoke(units["count_words"]["id"], dict(text="alpha"), on_event=events.append)

        def other():
            with self.platform.runtime.context("other-task"):
                capabilities.invoke(units["wait"]["id"], dict(seconds=0.8),
                    on_event=lambda event: other_started.set() if event["kind"] == "waiting" else None)

        thread = threading.Thread(target=other)
        thread.start()
        self.assertTrue(other_started.wait(5))
        task = dict(id="finished-task", workspace_controller=self.platform.chat._workspace_controller,
                    calls=[completed["call"]], events=[dict(kind="capability_event", event=e) for e in events])
        try:
            self.assertTrue(self.platform.chat._workspace_drained("finished-task", capabilities, task=task))
            self.assertTrue(thread.is_alive(), "the other task must still be using the instance")
        finally:
            thread.join(5)
        self.assertFalse(thread.is_alive())

    def test_changed_package_bytes_are_refused_by_the_worker(self):
        self.install()
        self.platform.capabilities.bind(self.group, dict(greeting=dict(form="value", value="hi")))
        artifact = self.reported_image()
        target = Path(artifact["folder"]) / "package" / "adapter" / "__init__.py"
        target.write_text(target.read_text(encoding="utf-8") + "\n# edited\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "身份不一致"):
            CapabilityWorker(Path(artifact["folder"]) / "worker.json", self.coordinator)

    def test_regenerated_package_replaces_its_predecessor(self):
        self.install()
        capabilities = self.platform.capabilities
        first = capabilities.bind(self.group, dict(greeting=dict(form="value", value="hi")))
        self.reported_image()
        generated = self.root / "generated"
        (generated / "adapter" / "__init__.py").write_text(ADAPTER + "\n# regenerated\n", encoding="utf-8")
        manifest = json.loads((generated / MANIFEST).read_text(encoding="utf-8"))
        manifest["files"] = scan_files(generated)
        (generated / MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")
        second = capabilities.install(generated)
        self.assertEqual(second["group_id"], first["group_id"])
        self.assertNotEqual(second["package_digest"], first["package_digest"])
        # Bindings of still-declared resources carry over; the old image no longer applies.
        self.assertTrue(second["resources"][0]["bound"])
        self.assertEqual(second["availability"]["status"], "NOT_PREPARED")
        current = {unit["id"] for unit in capabilities.public()}
        self.assertEqual(current, set(second["capabilities"]))
        self.assertEqual({self.platform.store.get("capability", unit)["status"] for unit in first["capabilities"]},
                         {"SUPERSEDED"})

    def test_installation_cannot_change_under_a_held_instance(self):
        self.install()
        capabilities = self.platform.capabilities
        capabilities.bind(self.group, dict(greeting=dict(form="value", value="hi")))
        self.start_worker(self.reported_image())
        for change in (lambda: capabilities.bind(self.group, dict(greeting=dict(form="value", value="hello"))),
                       lambda: capabilities.install(self.root / "generated"),
                       lambda: capabilities.detach(self.group)):
            with self.assertRaisesRegex(ValueError, "排空"):
                change()


@unittest.skipUnless(os.environ.get("SEAMPACT_DOCKER_TESTS") == "1", "opt-in real Docker check")
class RealDockerPackage(unittest.TestCase):
    def docker(self, *args):
        result = subprocess.run(["docker", *args], capture_output=True, text=True, encoding="utf-8", timeout=300)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        return result.stdout.strip()

    def tags(self):
        return set(self.docker("image", "ls", "--format", "{{.Repository}}:{{.Tag}}").splitlines())

    def test_built_image_serves_calls_and_release_is_confirmed(self):
        existing = self.tags()
        with tempfile.TemporaryDirectory(prefix="seampact-capability-docker-") as temporary:
            root = Path(temporary)
            platform = Platform(root / "platform")
            server = make_server(platform, host="0.0.0.0", port=0)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                platform.deployments.configure_backend(dict(kind="docker", publish_host="127.0.0.1",
                    coordinator_url="http://host.docker.internal:" + str(server.server_port),
                    advertise_host="127.0.0.1", operation_timeout_s=120, start_timeout_s=120))
                write_example_package(root / "generated")
                capabilities = platform.capabilities
                group = capabilities.install(root / "generated", dict(greeting=dict(form="value", value="hi")))["group_id"]
                prepared = capabilities.prepare(group, wait=True)
                artifact = platform.store.get("deployment", group)
                self.assertEqual(prepared["availability"]["status"], "AVAILABLE", prepared["preparation"])
                units = {unit["entry_id"]: unit for unit in capabilities.public()}
                self.assertEqual(units["count_words"]["dispatch"]["status"], "COLD_STARTABLE")

                events = []
                first = capabilities.invoke(units["count_words"]["id"], dict(text="alpha gamma"), on_event=events.append)
                self.assertEqual(first["output"], dict(greeting="hi", words=2, known=1, calls=1))
                self.assertTrue(first["call"]["cold_instance"])
                self.assertIn("progress", [event["kind"] for event in events])
                second = capabilities.invoke(units["count_words"]["id"], dict(text="beta"))
                self.assertEqual((second["output"]["calls"], second["call"]["startup_kind"]), (2, "WARM_REUSE"))
                self.assertEqual(second["call"]["instance_id"], first["call"]["instance_id"])
                report = capabilities.invoke(units["export_report"]["id"], dict(text="b a b"))
                self.assertEqual(Path(report["files"][0]["artifact"]).read_text(encoding="utf-8"), "a\nb")

                record = platform.deployments.instances[first["call"]["instance_id"]]
                container = record["handle"]["container"]
                self.assertEqual(record["image_id"], artifact["build"]["image_id"])
                platform.deployments.drain_stop(group, {})
                wait_for(lambda: not platform.deployments.live_instances(group), timeout=120,
                         message="instance release was not confirmed")
                self.assertEqual((record["state"], record["released"]), ("STOPPED", True))
                self.assertEqual(self.docker("ps", "-a", "--filter", "name=^/" + container + "$", "--format", "{{.ID}}"), "")
                # A released package starts a fresh instance on the next call.
                third = capabilities.invoke(units["count_words"]["id"], dict(text="alpha"))
                self.assertEqual(third["output"]["calls"], 1)
                self.assertNotEqual(third["call"]["instance_id"], first["call"]["instance_id"])
                platform.deployments.drain_stop(group, {})
                wait_for(lambda: not platform.deployments.live_instances(group), timeout=120)
            finally:
                for group_id in [g["id"] for g in platform.store.list("runtime_group")]:
                    for item in platform.deployments.live_instances(group_id):
                        container = (item.get("handle") or {}).get("container")
                        if container:
                            subprocess.run(["docker", "rm", "-f", container], capture_output=True)
                # Remove only references this check created; shared images stay.
                for tag in sorted(self.tags() - existing):
                    if tag.startswith(("seampact-capability-", "seampact-environment:", "seampact-build-input:")):
                        subprocess.run(["docker", "image", "rm", tag], capture_output=True)
                server.shutdown()
                server.server_close()
                platform.close()


if __name__ == "__main__":
    unittest.main()
