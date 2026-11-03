"""Frozen connection credentials reach only the selected Docker worker process."""

import copy
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from seampact.configuration.dependencies import DependencyBindings, EnvironmentGap
from seampact.hosting import backends
from seampact.hosting.deployment import Deployments
from seampact.hosting.image_build import worker_dockerfile
from seampact.source.inspection import Inspector
from seampact.worker import HostProfiles


class WorkerCredentialTests(unittest.TestCase):
    def bindings(self, auth=None):
        code = "def call(key):\n    return Client(api_key=key)\n"
        inspector = SimpleNamespace(root=Path.cwd(), data=lambda _: code.encode(),
                                    request=dict(source_id="source", source_version="frozen"))
        candidate = dict(entries=[dict(id="call", path="original.py", kind="function", symbol="call",
            fixed_arguments=dict(key={"$dependency": dict(id="service", field="credential")}))],
            dependencies=[dict(id="service", kind="service", configurable=True, required=True,
                entry_ids=["call"], read_time="call", owner_scope="original client",
                evidence=[dict(path="original.py", start=1, end=2)],
                mappings=[dict(entry_id="call", location="entry.fixed_arguments", parameter="key", field="credential")])])
        connection = dict(id="connection-1", config_digest="frozen-connection",
                          auth=auth or dict(mode="saved", credential_version="v1"))
        self.current, self.key_reads = copy.deepcopy(connection), []
        self.key = "private-worker-test-value"
        def key(_):
            self.key_reads.append(True)
            return self.key
        profiles = SimpleNamespace(lock=threading.RLock(), connection_profile=lambda _: copy.deepcopy(self.current),
                                   connection_key=key)
        binding = DependencyBindings(SimpleNamespace(profiles=profiles), inspector, candidate)
        binding.selected["service"] = dict(definition_digest=binding.rows["service"]["definition_digest"], connection=connection)
        return binding

    def test_saved_credential_matches_worker_lookup_and_does_not_enter_snapshot(self):
        binding = self.bindings()
        before = json.dumps(binding.snapshot(), sort_keys=True)
        values = binding.worker_environment(["call"])
        self.assertEqual(set(values), {"SEAMPACT_CREDENTIAL_CONNECTION_1"})
        with patch.dict(os.environ, values):
            host = HostProfiles(binding.selected)
            self.assertEqual(host.connection_key("connection-1"), self.key)
        self.assertEqual(json.dumps(binding.snapshot(), sort_keys=True), before)
        self.assertNotIn(self.key, before)

    def test_unrelated_group_does_not_read_or_receive_credentials(self):
        binding = self.bindings()
        self.assertEqual(binding.worker_environment(["unrelated"]), {})
        self.assertEqual(self.key_reads, [])
        binding.registry = None
        self.assertEqual(binding.worker_environment([]), {})

    def test_missing_or_rotated_credential_cannot_reuse_a_cached_key(self):
        binding = self.bindings()
        binding.keys["service"] = "old-cached-key"
        self.current["auth"]["credential_version"] = "v2"
        with self.assertRaisesRegex(ValueError, "select and rebind"):
            binding.worker_environment(["call"])
        self.assertEqual(self.key_reads, [])
        self.current["auth"]["credential_version"] = "v1"
        self.key = ""
        with self.assertRaises(EnvironmentGap):
            binding.worker_environment(["call"])

    def test_environment_auth_uses_its_original_name_and_none_needs_no_key(self):
        binding = self.bindings(dict(mode="environment", env="EXAMPLE_API_KEY"))
        self.assertEqual(binding.worker_environment(["call"]), {"EXAMPLE_API_KEY": self.key})
        binding = self.bindings(dict(mode="none"))
        self.assertEqual(binding.worker_environment(["call"]), {})
        self.assertEqual(self.key_reads, [])

    def test_real_child_receives_private_override_without_changing_parent_or_receipt(self):
        name, value = "SEAMPACT_WORKER_TEST_CREDENTIAL", "private-child-value"
        previous = os.environ.get(name)
        result = backends.run([sys.executable, "-c", "import os; print(os.environ['" + name + "']); print(os.environ['SEAMPACT_OTHER_CREDENTIAL'])"],
                              10, environment={name: value, "SEAMPACT_OTHER_CREDENTIAL": value + "-suffix"})
        self.assertEqual(result["returncode"], 0)
        self.assertIn("[private credential]", result["stdout"])
        self.assertNotIn(value, json.dumps(result))
        self.assertNotIn("-suffix", result["stdout"])
        self.assertEqual(os.environ.get(name), previous)

    def test_docker_command_has_names_only_and_timeout_output_is_redacted(self):
        record = dict(name="example", group_id="group", id="instance", generation=1)
        config = dict(kind="docker", docker_network="example", coordinator_url="http://platform:8880",
                      operation_timeout_s=10, pass_env=["EXAMPLE_API_KEY"])
        secret = "private-launch-value"
        def launch(argv, **kwargs):
            self.assertEqual(kwargs["env"]["EXAMPLE_API_KEY"], secret)
            self.assertEqual(argv.count("EXAMPLE_API_KEY"), 1)
            self.assertNotIn(secret, argv)
            return subprocess.CompletedProcess(argv, 0, "container-id", "")
        with patch.object(backends, "docker_available", return_value=None), patch.object(backends.subprocess, "run", side_effect=launch):
            result = backends.start(record, "image", ".", config, credential_env={"EXAMPLE_API_KEY": secret})
        self.assertEqual(result["status"], "APPLIED")
        self.assertNotIn(secret, json.dumps(result))
        error = subprocess.TimeoutExpired(["example"], 1, output=secret.encode(), stderr=secret.encode())
        with patch.object(backends.subprocess, "run", side_effect=error):
            result = backends.run(["example"], 1, environment={"EXAMPLE_API_KEY": secret})
        self.assertTrue(result["timeout"])
        self.assertNotIn(secret, json.dumps(result))


class WorkerEnvironmentSelectionTests(unittest.TestCase):
    def test_omitted_build_environment_keeps_selected_requirements_and_explicit_override(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "tool.py").write_text("def call(): return 1\n", encoding="utf-8")
            (root / "requirements.txt").write_text("openai==1.78.1\n", encoding="utf-8")
            inspector = Inspector.freeze(root, root / "snapshots", "r", ["tool.py", "requirements.txt"], "call", "result")
            selected = dict(python="3.11", base_image="python:3.11-slim", requirements=["requirements.txt"],
                            packages=[], project="", pip_extra_index_url="")
            inspector.request["runtime_environment"] = copy.deepcopy(selected)
            manager = object.__new__(Deployments)
            environment = manager._environment(None, SimpleNamespace(inspector=inspector))
            self.assertEqual(environment["requirements"], ["requirements.txt"])
            folder = root / "worker"
            folder.mkdir()
            plan = worker_dockerfile(folder, inspector, environment, "assert True")
            self.assertEqual(plan["installation"]["early"][0]["inputs"], ["requirements.txt"])
            explicit = manager._environment(dict(selected, requirements=[]), SimpleNamespace(inspector=inspector))
            self.assertEqual(explicit["requirements"], [])
            self.assertEqual(inspector.request["runtime_environment"], selected)


if __name__ == "__main__":
    unittest.main()
