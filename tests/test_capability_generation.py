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
from seampact.capability_contract import MANIFEST, digest, load_package
from seampact.capability_generation import GenerationJobs, resolve_resources, write_package
from seampact.hosting.capability_hosting import worker_spec
from seampact.intake import RepositoryIntake
from seampact.source.inspection import Inspector
from seampact.store import Store

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
    return dict(manifest=dict(name="word-tools", environment=dict(python="3.11", requirements=[], packages=[]),
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


if __name__ == "__main__":
    unittest.main()
