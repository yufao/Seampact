"""Focused offline checks of generated packages, with execution in a child worker.

The model and image builder are replaced; no real provider or Docker build is
claimed by these checks. The generated adapter/source are never imported here.
"""
import copy
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from seampact.capability_catalog import CapabilityCatalog
from seampact.capability_contract import MANIFEST, digest, load_package, package_digest
from seampact.capability_generation import GenerationJobs, resolve_resources, write_package
from seampact.hosting.capability_hosting import CapabilityError, check_project_scope, worker_spec
from seampact.intake import RepositoryIntake
from seampact.source.inspection import Inspector
from seampact.store import Store
from seampact.project_spaces import ProjectSpaces
from seampact.resources import ResourceRegistry

SOURCE = '''def summarize(text):
    return {"words": len(text.split())}
'''
ADAPTER = '''from wordlib import summarize

class Adapter:
    def initialize(self, bindings):
        self.prefix = bindings.get("prefix", "")

    def execute(self, input, context):
        return summarize(input["text"])

    def close(self):
        pass
'''


def proposal():
    return dict(manifest=dict(name="word-tools", environment=dict(python=".".join(map(str, sys.version_info[:2])), requirements=[], packages=[]),
        implementation=dict(module="adapter", symbol="Adapter"), instance=dict(concurrency=1, replicable=False, cancel="none"),
        resources=[], capabilities=[dict(schema="capability-ir/1", id="summarize", name="统计词数", purpose="统计文本词数",
            input=dict(type="object", properties=dict(text=dict(type="string")), required=["text"], additionalProperties=False),
            output=dict(kind="json", schema=dict(type="object", properties=dict(words=dict(type="integer")), required=["words"])),
            state=dict(scope="call"), effects=[], resources=[], provenance=[dict(path="wordlib.py", start=1, end=2)])]),
        files={"adapter/__init__.py": ADAPTER}, resource_matches={},
        examples=[dict(capability="summarize", input=dict(text="alpha beta"), provenance=[dict(path="README.md", start=1, end=1)])], gaps=[])


class Profiles:
    def role_profile(self, role):
        return dict(id="generator", model="offline", base_url="http://unused.invalid", auth=dict(mode="none"))

    def role_key(self, role):
        return ""

    def public_connections(self):
        return []


class Model:
    def __init__(self, outputs):
        self.outputs = iter(outputs)
        self.bodies = []

    def send(self, url, key, body):
        self.bodies.append(body)
        output = next(self.outputs)
        return dict(choices=[dict(message=dict(role="assistant", content=None,
            tool_calls=[dict(id="tool", type="function", function=dict(name=output[0], arguments=json.dumps(output[1])))]))])


class Router:
    def __init__(self):
        self.held = []
        self.closed = threading.Event()

    def _guard(self, *args):
        pass

    def live_instances(self, group):
        return self.held

    def start_gap(self, group):
        return None

    def drain_stop(self, group, body, reason=None):
        self.held.clear()
        return dict(group_id=group, instances=[])


WORKER = '''import json, sys
from pathlib import Path
from seampact import capability_worker
capability_worker.json_request = lambda *a, **k: {"session": "test-session"}
worker = capability_worker.CapabilityWorker(Path(sys.argv[1]), "http://unused.invalid",
    source_root=Path(sys.argv[1]).parent / "package" / "source")
try:
    result = worker.invoke(dict(worker.identity(), call_id="0123456789abcdef0123456789abcdef", unit=sys.argv[2],
                                input=json.loads(sys.argv[3])), lambda *a: None)
    print(json.dumps(result))
finally:
    worker.close()
'''


class GeneratedPackageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="seampact-generation-")
        self.root = Path(self.temporary.name)
        repository = self.root / "repository"
        repository.mkdir()
        (repository / "wordlib.py").write_text(SOURCE, encoding="utf-8")
        (repository / "README.md").write_text('Example: summarize("alpha beta")\n', encoding="utf-8")
        source = RepositoryIntake(self.root / "platform" / "sources").prepare(str(repository))
        self.inspector = Inspector.freeze_repository(source, self.root / "snapshots", "request", dict(scope="complete"))
        self.ranges = {"wordlib.py": [(1, 2)], "README.md": [(1, 1)]}
        self.store = Store(self.root / "platform.sqlite3")
        self.platform = SimpleNamespace(home=self.root / "platform", store=self.store, profiles=Profiles(), resources=None)
        self.platform.projects = ProjectSpaces(self.platform.home, self.store)
        self.platform.projects.default()
        self.platform.capabilities = CapabilityCatalog(self.platform.home, self.store)
        self.router = Router()
        self.platform.capabilities.router = self.router
        self.modules = set(sys.modules)

    def tearDown(self):
        self.store.db.close()
        self.temporary.cleanup()
        self.assertNotIn("adapter", set(sys.modules) - self.modules)
        self.assertNotIn("wordlib", set(sys.modules) - self.modules)

    def write(self, value=None, ranges=None):
        return write_package(self.root / "package", value or proposal(), self.inspector, ranges or self.ranges, dict(model="offline"))

    def test_compiles_and_freezes_exact_source_and_generator_identity(self):
        value = proposal()
        value["manifest"].update(source=dict(id="forged", version="forged"), generator=dict(template="forged"), files=[])
        manifest, checks = self.write(value)
        self.assertEqual(manifest["source"], dict(id=self.inspector.request["source_id"], version=self.inspector.request["source_version"]))
        self.assertEqual(manifest["generator"]["template"], "repository-python-adapter/1")
        self.assertEqual((self.root / "package" / "source" / "wordlib.py").read_text(), SOURCE)
        self.assertEqual(load_package(self.root / "package"), manifest)
        self.assertEqual(checks["semantic_equivalence"], "NOT_CLAIMED")

    def test_rejects_path_escape_unread_evidence_and_invalid_symbol(self):
        for mutate, expected in [
            (lambda p: p["files"].update({"adapter/../../escape.py": "pass"}), "安全相对路径"),
            (lambda p: p["manifest"]["capabilities"][0]["provenance"][0].update(end=30), "实际行"),
            (lambda p: p["files"].update({"adapter/__init__.py": ADAPTER.replace("import summarize", "import missing")}), "符号不存在"),
            (lambda p: p["files"].update({"adapter/__init__.py": ADAPTER.replace("def close(self):", "def close(self, x):")}), "方法签名"),
        ]:
            with self.subTest(expected=expected):
                value = proposal()
                mutate(value)
                with self.assertRaisesRegex((ValueError, SyntaxError), expected):
                    self.write(value)

    def test_parameter_matching_requires_literal_source_default(self):
        manifest, _ = self.write()
        manifest["resources"] = [dict(id="prefix", kind="parameter", description="prefix", required=True)]
        references = [dict(path="README.md", start=1, end=1)]
        bindings, gaps, _ = resolve_resources(self.platform, manifest,
            dict(prefix=dict(value="alpha beta", provenance=references)), self.inspector)
        self.assertEqual(bindings, dict(prefix=dict(form="value", value="alpha beta")))
        self.assertFalse(gaps)
        bindings, gaps, _ = resolve_resources(self.platform, manifest,
            dict(prefix=dict(value="invented", provenance=references)), self.inspector)
        self.assertEqual(bindings, {})
        self.assertEqual(gaps[0]["resource_id"], "prefix")

    def test_model_reads_repairs_then_real_child_worker_invocation(self):
        invalid = proposal()
        invalid["files"]["adapter/__init__.py"] = ADAPTER.replace("import summarize", "import absent")
        model = Model([("read_file", dict(path="wordlib.py")), ("submit_package", invalid), ("submit_package", proposal())])
        jobs = GenerationJobs(self.platform, model)
        catalog = self.platform.capabilities
        catalog.prepare = lambda group, wait=False: catalog.installation(group)

        def invoke(unit_id, arguments, on_event=None):
            unit = catalog.unit(unit_id)
            group = self.store.get("runtime_group", unit["runtime_group"])
            root, _ = catalog.package(unit["package_digest"])
            context = self.root / "worker-context"
            shutil.copytree(root, context / "package")
            spec = worker_spec(group)
            spec_file = context / "worker.json"
            spec_file.write_text(json.dumps(spec), encoding="utf-8")
            self.router.held.append(dict(id="child-worker"))
            child = subprocess.run([sys.executable, "-B", "-c", WORKER, str(spec_file), unit_id, json.dumps(arguments)],
                                   env=dict(__import__("os").environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src")),
                                   capture_output=True, text=True, timeout=15)
            self.assertEqual(child.returncode, 0, child.stderr)
            result = json.loads(child.stdout)
            self.assertEqual(result["type"], "result", result)
            return dict(output=result["output"], files=result["files"], timing=result["timing"])

        catalog.invoke = invoke
        job = jobs.start(self.inspector, wait=True)
        self.assertEqual(job["status"], "AVAILABLE", job.get("error"))
        self.assertEqual(job["repairs"], 1)
        self.assertEqual(job["first_call"]["calls"][0]["result"]["output"], dict(words=2))
        self.assertEqual(job["first_call"]["release"]["status"], "RELEASED")
        self.assertTrue(job["group_id"].startswith("cap-"))
        self.assertEqual(jobs.snapshot()[0]["package_digest"], job["package_digest"])

    def test_external_write_sample_is_not_executed(self):
        value = proposal()
        value["manifest"]["capabilities"][0]["effects"] = [dict(kind="network", access="write", target="remote API")]
        jobs = GenerationJobs(self.platform, Model([("read_file", dict(path="wordlib.py")), ("submit_package", value)]))
        self.platform.capabilities.prepare = lambda group, wait=False: self.platform.capabilities.installation(group)
        self.platform.capabilities.invoke = lambda *a: self.fail("external write must not execute")
        job = jobs.start(self.inspector, wait=True)
        self.assertEqual(job["status"], "NEEDS_INPUT", job.get("error"))
        self.assertEqual(job["first_call"]["status"], "NOT_RUN")
        self.assertIn("外部写入", job["gaps"][0]["reason"])

    def test_cancelled_provider_result_cannot_install(self):
        entered, release = threading.Event(), threading.Event()
        class SlowModel:
            def send(self, *args):
                entered.set()
                release.wait(5)
                return dict(choices=[dict(message=dict(role="assistant", content=json.dumps(proposal())))])
        jobs = GenerationJobs(self.platform, SlowModel())
        job = jobs.start(self.inspector)
        self.assertTrue(entered.wait(2))
        jobs.cancel(job["id"])
        jobs.running[job["id"]].join(2)
        release.set()
        self.assertEqual(jobs.detail(job["id"])["status"], "CANCELLED")
        self.assertEqual(self.platform.capabilities.installations(), [])

    def test_missing_resource_installs_description_without_preparing(self):
        value = proposal()
        value["manifest"]["resources"] = [dict(id="model", kind="weight", required=True, description="原模型权重")]
        jobs = GenerationJobs(self.platform, Model([("read_file", dict(path="wordlib.py")), ("submit_package", value)]))
        self.platform.capabilities.prepare = lambda *a, **k: self.fail("unbound resource must not prepare")
        job = jobs.start(self.inspector, wait=True)
        self.assertEqual(job["status"], "NEEDS_INPUT", job.get("error"))
        self.assertEqual(job["availability"]["status"], "NEEDS_INPUT")
        self.assertIn("model", job["gaps"][0]["resource_id"])

    def test_operator_config_binds_exact_checked_weights_project_and_device(self):
        self.platform.resources = ResourceRegistry(self.platform.home, self.store, self.platform.profiles)
        weight = self.root / "model.bin"
        weight.write_bytes(b"checked model")
        record = self.platform.resources.save_weight(dict(name="selected model", kind="file", path=str(weight),
                                                          sha256=digest(weight.read_bytes())))
        self.inspector.request["configuration"] = dict(project_id="default", parameters=dict(device="cuda:0"),
            resource_choices=dict(embedding=record["id"]))
        description, _ = self.write()
        description["resources"] = [dict(id=identity, kind=kind, description=identity, required=True)
            for identity, kind in (("corpus", "data"), ("index", "data"), ("device", "device"), ("model", "weight"))]
        refs = [dict(path="README.md", start=1, end=1)]
        selections = dict(corpus=dict(area="data", provenance=refs), index=dict(area="indexes", provenance=refs),
                          device=dict(configuration_key="device", provenance=refs),
                          model=dict(configuration_key="embedding", provenance=refs))
        bound, gaps, _ = resolve_resources(self.platform, description, selections, self.inspector)
        self.assertFalse(gaps)
        self.assertEqual(bound["device"], dict(form="value", value="cuda:0"))
        self.assertEqual(bound["corpus"], dict(form="project", project_id="default", area="data", read_only=True))
        self.assertEqual(bound["index"]["namespace"], package_digest(description)[:24])
        self.assertEqual(bound["model"]["resource_id"], record["id"])
        self.assertEqual(bound["model"]["sha256"], record["sha256"])
        weight.write_bytes(b"changed model")
        bound, gaps, _ = resolve_resources(self.platform, description, selections, self.inspector)
        self.assertNotIn("model", bound)
        self.assertTrue(any(item["resource_id"] == "model" for item in gaps))
        selections["corpus"]["path"] = "/arbitrary"
        bound, gaps, _ = resolve_resources(self.platform, description, selections, self.inspector)
        self.assertNotIn("corpus", bound)

    def test_project_diagnostic_scopes_index_writes_and_release(self):
        value = proposal()
        value["manifest"]["resources"] = [dict(id=identity, kind="data", description=identity, required=True)
                                              for identity in ("corpus", "index")]
        capability = value["manifest"]["capabilities"][0]
        capability["resources"] = ["corpus", "index"]
        capability["effects"] = [dict(kind="filesystem", access="write", target="resource:index")]
        refs = [dict(path="README.md", start=1, end=1)]
        value["resource_matches"] = dict(corpus=dict(area="data", provenance=refs),
                                          index=dict(area="indexes", provenance=refs))
        self.inspector.request["configuration"] = dict(project_id="default")
        jobs = GenerationJobs(self.platform, Model([("read_file", dict(path="wordlib.py")), ("submit_package", value)]))
        catalog = self.platform.capabilities
        catalog.prepare = lambda group, wait=False: catalog.installation(group)

        def invoke(unit_id, arguments, on_event=None):
            unit = catalog.unit(unit_id)
            group = self.store.get("runtime_group", unit["runtime_group"])
            check_project_scope(self.platform, group)
            with self.assertRaisesRegex(ValueError, "运行中"):
                self.platform.projects.put_file("default", None, "data", "during.txt", b"must block")
            self.router.held.append(dict(id="verification-worker"))
            return dict(output=dict(words=2), files=[])

        catalog.invoke = invoke
        job = jobs.start(self.inspector, wait=True)
        self.assertEqual(job["status"], "AVAILABLE", job.get("error"))
        self.assertEqual(job["project_lease"]["status"], "FINISHED")
        self.platform.projects.put_file("default", None, "data", "after.txt", b"allowed")
        self.assertIsNotNone(jobs._sample_gap(dict(capability, effects=[
            dict(kind="filesystem", access="write", target="resource:corpus")]),
            self.store.get("runtime_group", job["group_id"])["bindings"]))

    def test_analyzer_receives_requested_purpose_without_local_configuration(self):
        value = proposal()
        value["manifest"]["capabilities"][0]["effects"] = [dict(kind="network", access="write", target="remote API")]
        self.inspector.request["configuration"] = dict(intent="Count words using the original public library",
            project_id="local-project-selection", parameters=dict(device="local-device-selection"),
            resource_choices=dict(model="local-resource-selection"), private_note="never-send-private-configuration")
        model = Model([("read_file", dict(path="wordlib.py")), ("submit_package", value)])
        self.store.save("weight_resource", "sensitive-weight-id", dict(id="sensitive-weight-id", name="private-model-name"))
        self.platform.profiles.public_connections = lambda: [dict(id="sensitive-connection-id", model="private-connection-model")]
        jobs = GenerationJobs(self.platform, model)
        self.platform.capabilities.prepare = lambda group, wait=False: self.platform.capabilities.installation(group)
        jobs.start(self.inspector, wait=True)
        overview = json.loads(model.bodies[0]["messages"][1]["content"])
        self.assertEqual(overview["requested_capability"], self.inspector.request["configuration"]["intent"])
        self.assertNotIn("configuration", overview)
        for private in ("local-project-selection", "local-device-selection", "local-resource-selection", "never-send-private-configuration",
                        "sensitive-weight-id", "private-model-name", "sensitive-connection-id", "private-connection-model",
                        self.inspector.request["source_id"]):
            self.assertNotIn(private, json.dumps(overview))

    def test_environment_override_stays_local_and_is_normalized(self):
        self.inspector.request["configuration"] = dict(environment=dict(python="3.10", base_image="python:3.10-slim"))
        description, _ = self.write()
        self.assertEqual(description["environment"]["python"], "3.10")
        self.assertEqual(description["environment"]["base_image"], "python:3.10-slim")

    def test_explicit_improvement_reuses_verified_proposal_without_private_context(self):
        original = proposal()
        original["manifest"].update(source=dict(id="private-old-source"), generator=dict(model="private-old-model"))
        first = Model([("read_file", dict(path="wordlib.py")), ("submit_package", original)])
        jobs = GenerationJobs(self.platform, first)
        catalog = self.platform.capabilities
        catalog.prepare = lambda group, wait=False: catalog.installation(group)
        catalog.invoke = lambda *a, **k: dict(output=dict(words=2), files=[])
        prior = jobs.start(self.inspector, wait=True)
        self.assertEqual(prior["status"], "AVAILABLE", prior.get("error"))
        model = Model([("submit_package", proposal())])
        jobs.transport = model
        config = dict(intent="Preserve the word count and improve caching",
                      project_id="private-project", resource_choices=dict(model="private-resource"),
                      parameters=dict(device="private-device"))
        result = jobs.start(self.inspector, wait=True, improve_from=prior["id"], configuration=config)
        self.assertEqual(result["status"], "AVAILABLE", result.get("error"))
        overview = json.loads(model.bodies[0]["messages"][1]["content"])
        self.assertEqual(overview["requested_capability"], config["intent"])
        self.assertEqual(overview["previous_successful_proposal"]["files"], original["files"])
        self.assertNotIn("source", overview["previous_successful_proposal"]["manifest"])
        self.assertNotIn("generator", overview["previous_successful_proposal"]["manifest"])
        for private in ("private-old-source", "private-old-model", "private-project", "private-resource", "private-device"):
            self.assertNotIn(private, json.dumps(model.bodies[0]))
        self.assertNotIn("continued_from", result)
        self.assertTrue(any(item["path"] == "wordlib.py" for item in result["reads"]))

    def test_needs_input_continuation_uses_unverified_candidate_without_runtime_details(self):
        self.inspector.request["configuration"] = dict(intent="original purpose", project_id="private-project",
                                                        parameters=dict(device="private-device"))
        incomplete = proposal()
        incomplete["manifest"]["source"] = dict(id="private-old-source")
        incomplete["manifest"]["capabilities"][0]["effects"] = [dict(kind="network", access="write", target="remote API")]
        first = Model([("read_file", dict(path="wordlib.py")), ("submit_package", incomplete)])
        jobs = GenerationJobs(self.platform, first)
        catalog = self.platform.capabilities
        catalog.prepare = lambda group, wait=False: catalog.installation(group)
        catalog.invoke = lambda *a, **k: dict(output=dict(words=2), files=[])
        prior = jobs.start(self.inspector, wait=True)
        self.assertEqual(prior["status"], "NEEDS_INPUT", prior.get("error"))
        prior["first_call"]["error"] = "private-runtime-path"
        prior["resource_matches"] = [dict(path="private-binding-path")]
        self.store.save("capability_generation", prior["id"], prior)
        model = Model([("submit_package", proposal())])
        jobs.transport = model
        result = jobs.start(self.inspector, wait=True, configuration=dict(intent="Correct the incomplete candidate"))
        self.assertEqual(result["status"], "AVAILABLE", result.get("error"))
        self.assertEqual(result["continued_from"], prior["id"])
        overview = json.loads(model.bodies[0]["messages"][1]["content"])
        self.assertEqual(overview["requested_capability"], "Correct the incomplete candidate")
        self.assertEqual(self.inspector.request["configuration"]["project_id"], "private-project")
        self.assertEqual(self.inspector.request["configuration"]["parameters"]["device"], "private-device")
        continuation = next(json.loads(item["content"]) for item in model.bodies[0]["messages"]
                            if item.get("role") == "user" and "previous_candidate" in (item.get("content") or ""))
        self.assertEqual(continuation["previous_candidate"]["files"], incomplete["files"])
        self.assertIn("未完成候选", continuation["instruction"])
        self.assertTrue(any(item["path"] == "wordlib.py" for item in continuation["previous_source_reads"]))
        for private in ("private-old-source", "private-runtime-path", "private-binding-path", "private-project", "private-device"):
            self.assertNotIn(private, json.dumps(model.bodies[0]))

    def test_improvement_rejects_unverified_or_changed_prior_package(self):
        model = Model([("read_file", dict(path="wordlib.py")), ("submit_package", proposal())])
        jobs = GenerationJobs(self.platform, model)
        catalog = self.platform.capabilities
        catalog.prepare = lambda group, wait=False: catalog.installation(group)
        catalog.invoke = lambda *a, **k: dict(output=dict(words=2), files=[])
        prior = jobs.start(self.inspector, wait=True)
        self.assertEqual(prior["status"], "AVAILABLE", prior.get("error"))
        candidate = dict(prior, id="1" * 32, status="FAILED")
        self.store.save("capability_generation", candidate["id"], candidate)
        result = jobs.start(self.inspector, wait=True, improve_from=candidate["id"], configuration=dict(intent="Improve"))
        self.assertEqual(result["status"], "FAILED")
        self.assertIn("成功生成", result["error"])
        adapter = catalog.packages / prior["package_digest"] / "adapter" / "__init__.py"
        adapter.write_text("# changed", encoding="utf-8")
        result = jobs.start(self.inspector, wait=True, improve_from=prior["id"], configuration=dict(intent="Improve"))
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(len(model.bodies), 2)

    def test_precise_candidate_edit_is_revalidated_and_recoverable(self):
        first = Model([("read_file", dict(path="wordlib.py")), ("submit_package", proposal())])
        jobs = GenerationJobs(self.platform, first)
        catalog = self.platform.capabilities
        catalog.prepare = lambda group, wait=False: catalog.installation(group)
        catalog.invoke = lambda *a, **k: dict(output=dict(words=2), files=[])
        prior = jobs.start(self.inspector, wait=True)
        self.assertEqual(prior["status"], "AVAILABLE", prior.get("error"))
        edit = dict(edits=[dict(path="adapter/__init__.py", old='return summarize(input["text"])',
                                new='return summarize(input["text"] + " extra")')])
        jobs.transport = Model([("edit_package", edit)])
        result = jobs.start(self.inspector, wait=True, improve_from=prior["id"], configuration=dict(intent="Extend input"))
        self.assertEqual(result["status"], "AVAILABLE", result.get("error"))
        saved = json.loads((jobs.home / result["id"] / "accepted_proposal.json").read_text(encoding="utf-8"))
        self.assertIn('input["text"] + " extra"', saved["files"]["adapter/__init__.py"])
        self.assertEqual(jobs._successful_proposal(self.inspector,
            dict(improve_from=result["id"], source_id=result["source_id"], model=result["model"], reads=[]))["files"], saved["files"])
        ambiguous = dict(edits=[dict(path="adapter/__init__.py", old="self", new="changed")])
        jobs.transport = Model([("edit_package", ambiguous), ("edit_package", ambiguous)])
        rejected = jobs.start(self.inspector, wait=True, improve_from=result["id"], configuration=dict(intent="Revise"))
        self.assertEqual(rejected["status"], "FAILED")
        self.assertIn("唯一匹配", rejected["error"])

    def test_cancelled_diagnostic_scope_waits_for_operation_and_instance_release(self):
        jobs = GenerationJobs(self.platform)
        lease = self.platform.projects.begin_task("default", "generation_conv", "generation_lease")
        lease = self.platform.projects.finish_task(lease["task_id"], drained=False)
        job = dict(id="generation_job", status="CANCELLED", group_id="verification_group", project_lease=lease)
        self.store.save("capability_generation", job["id"], job)
        pending = threading.Event()
        jobs._verification_pending[job["id"]] = pending
        self.assertEqual(jobs.reconcile_project_leases("default"), [])
        with self.assertRaisesRegex(ValueError, "运行中"):
            self.platform.projects.put_file("default", None, "data", "pending.txt", b"blocked")
        pending.set()
        self.router.held.append(dict(id="unreleased_worker"))
        self.assertEqual(jobs.reconcile_project_leases("default"), [])
        self.router.held.clear()
        self.assertEqual(jobs.reconcile_project_leases("default"), ["generation_lease"])
        self.assertEqual(self.platform.projects.task_scope("generation_lease")["status"], "FINISHED")

    def test_invalid_resource_is_rejected_before_copying_source(self):
        value = proposal()
        value["manifest"]["resources"] = [dict(id="embedding-model", kind="weight", required=True, description="model")]
        with self.assertRaisesRegex(ValueError, "资源需要"):
            self.write(value)
        self.assertFalse((self.root / "package").exists())

    def test_terminal_validation_saves_last_proposal_and_diagnostic(self):
        value = proposal()
        value["manifest"]["resources"] = [dict(id="embedding-model", kind="weight", required=True, description="model")]
        model = Model([("read_file", dict(path="wordlib.py")), ("submit_package", value), ("submit_package", value)])
        jobs = GenerationJobs(self.platform, model)
        job = jobs.start(self.inspector, wait=True)
        self.assertEqual(job["status"], "FAILED")
        checkpoint = json.loads((jobs.home / job["id"] / "conversation.json").read_text(encoding="utf-8"))
        self.assertEqual(checkpoint["messages"][-1]["role"], "tool")
        self.assertIn("资源需要", json.loads(checkpoint["messages"][-1]["content"])["reason"])
        self.assertIn("embedding-model", checkpoint["messages"][-2]["tool_calls"][0]["function"]["arguments"])

    def test_runtime_repair_keeps_read_evidence_and_job_round_budget(self):
        model = Model([("read_file", dict(path="wordlib.py")), ("submit_package", proposal()), ("submit_package", proposal())])
        jobs = GenerationJobs(self.platform, model)
        catalog = self.platform.capabilities
        catalog.prepare = lambda group, wait=False: catalog.installation(group)
        attempts = []

        def invoke(unit_id, arguments, on_event=None):
            attempts.append(unit_id)
            if len(attempts) == 1:
                on_event(dict(kind="model_usage", call_id="first-call", data=dict(raw="private-usage")))
                raise CapabilityError("No module named source_dependency", kind="ImportError", facts={})
            on_event(dict(kind="model_usage", call_id="second-call", data=dict(tokens=3)))
            return dict(output=dict(words=2), files=[])

        catalog.invoke = invoke
        job = jobs.start(self.inspector, wait=True)
        self.assertEqual(job["status"], "AVAILABLE", job.get("error"))
        self.assertEqual(job["rounds"], 3)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(job["first_call"]["model_usage"], [dict(kind="model_usage", call_id="second-call", data=dict(tokens=3))])
        self.assertEqual(job["attempts"][0]["first_call"]["model_usage"],
                         [dict(kind="model_usage", call_id="first-call", data=dict(raw="private-usage"))])
        self.assertNotIn("private-usage", json.dumps(model.bodies))

    def test_compaction_keeps_latest_proposal_and_all_tool_reply_pairs(self):
        messages = [dict(role="system", content="rules"), dict(role="user", content="overview")]
        for index in range(3):
            value = proposal()
            value["files"]["adapter/__init__.py"] = str(index) * 70000
            arguments = json.dumps(value)
            if index == 0:
                arguments = arguments[:-3]  # an older malformed JSON submission must also shrink
            messages.append(dict(role="assistant", tool_calls=[dict(id="proposal_" + str(index), type="function",
                function=dict(name="submit_package", arguments=arguments))]))
            messages.append(dict(role="tool", tool_call_id="proposal_" + str(index),
                                 content=json.dumps(dict(status="ERROR", reason="actual diagnostic " + str(index)))))
        latest = messages[-2]["tool_calls"][0]["function"]["arguments"]
        replies = [(item["tool_call_id"], item["content"]) for item in messages if item["role"] == "tool"]
        self.assertEqual(GenerationJobs._compact(messages), 2)
        self.assertEqual(messages[-2]["tool_calls"][0]["function"]["arguments"], latest)
        self.assertEqual([(item["tool_call_id"], item["content"]) for item in messages if item["role"] == "tool"], replies)
        self.assertEqual([item["tool_calls"][0]["id"] for item in messages if item["role"] == "assistant"],
                         [identity for identity, _ in replies])
        self.assertLess(len(json.dumps(messages)), 180000)
        self.assertTrue(json.loads(messages[2]["tool_calls"][0]["function"]["arguments"])["history_compacted"])

    def test_compaction_handles_duplicate_overview_proposal_without_losing_only_full_copy(self):
        value = proposal()
        value["files"]["adapter/__init__.py"] = "x" * 80000
        messages = [dict(role="system", content="rules"), dict(role="user", content=json.dumps(dict(
            previous_attempt=dict(previous_proposal=value, failure=dict(error="real worker failure")))))]
        self.assertEqual(GenerationJobs._compact(messages), 0)
        latest = json.dumps(value)
        messages.extend([dict(role="assistant", tool_calls=[dict(id="latest", type="function",
            function=dict(name="submit_package", arguments=latest))]),
            dict(role="tool", tool_call_id="latest", content='{"status":"ERROR","reason":"new failure"}')])
        self.assertEqual(GenerationJobs._compact(messages), 1)
        overview = json.loads(messages[1]["content"])
        self.assertTrue(overview["previous_attempt"]["previous_proposal"]["history_compacted"])
        self.assertEqual(overview["previous_attempt"]["failure"]["error"], "real worker failure")
        self.assertEqual(messages[2]["tool_calls"][0]["function"]["arguments"], latest)


if __name__ == "__main__":
    unittest.main()
