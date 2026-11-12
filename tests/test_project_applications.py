"""Project eligibility and admission snapshots without providers or workers."""

import copy
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from seampact.chat import ChatJobs
from seampact.project_applications import OFFICE_INTERFACES, ProjectApplications, TaskCapabilityCatalog
from seampact.project_spaces import ProjectSpaces
from seampact.store import Store


def unit(role="sanitize", group="shared", identity=None, ready=True):
    entry, inputs, outputs = OFFICE_INTERFACES[role]
    return dict(id=identity or group + ":" + entry, entry_id=entry, runtime_group=group,
                tool_name=entry + "_" + group, package=group, status="READY", package_digest=group,
                capability=dict(purpose=role, input=dict(type="object", properties={name: {} for name in inputs}),
                                output=dict(type="object", properties={name: {} for name in outputs})),
                availability=dict(status="AVAILABLE" if ready else "NOT_PREPARED", reason=None),
                dispatch=dict(status="COLD_STARTABLE" if ready else "UNAVAILABLE"))


class Bound:
    def __init__(self, *args):
        pass
    def role_profile(self, role):
        return dict(base_url="http://example.invalid", model="test")
    def role_key(self, role):
        return ""
    def public(self):
        return {"chat": {"model": "test"}}
    def resources(self):
        return {}


class ProjectApplicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "state.sqlite3")
        self.addCleanup(self.store.close)
        self.projects = ProjectSpaces(self.temp.name, self.store)
        self.project = self.projects.default()["id"]
        self.other = self.projects.create(dict(name="Other"))["id"]
        self.units = [unit(), unit("privacy", "private")]
        self.groups = dict(shared=dict(bindings={}), private=dict(bindings={
            "data": dict(form="project", project_id=self.other)}))
        self.catalog = SimpleNamespace(public=lambda: copy.deepcopy(self.units),
                                       _group=lambda identity: self.groups[identity], router=None)
        self.platform = SimpleNamespace(store=self.store, projects=self.projects, capabilities=self.catalog)
        self.apps = ProjectApplications(self.platform)

    def test_empty_disables_all_and_other_project_is_not_offerable(self):
        initial = self.apps.view(self.project)
        self.assertEqual(initial["application"]["eligible_capability_ids"], [self.units[0]["id"]])
        self.assertFalse(next(row for row in initial["libraries"] if row["group_id"] == "private")["project_eligible"])
        with self.assertRaisesRegex(ValueError, "其他项目"):
            self.apps.configure(self.project, dict(enabled_groups=["private"]))
        disabled = self.apps.configure(self.project, dict(enabled_groups=[]))
        self.assertEqual(disabled["application"]["eligible_capability_ids"], [])
        self.assertFalse(disabled["runtime_actions"])
        self.assertEqual(self.apps.configuration(self.other)["revision"], 0)
        self.assertEqual(self.apps.view(self.other)["application"]["eligible_capability_ids"],
                         sorted(row["id"] for row in self.units))

    def test_preset_reuses_interfaces_and_reports_disabled_checks(self):
        selected = self.apps.configure(self.project, dict(use_office_preset=True))
        self.assertEqual(selected["application"]["effective_bindings"], dict(sanitize=self.units[0]["id"]))
        sentinel_selector = object()
        self.platform.execution = SimpleNamespace(optional_selector=lambda *args: sentinel_selector)
        options = self.apps.runtime_options("task", selected["application"])
        self.assertIs(options["optional_hooks"], sentinel_selector)
        self.assertIn("sanitize:input_received", [hook.name for hook in options["hooks"]])
        disabled = self.apps.configure(self.project, dict(enabled_groups=[]))
        self.assertEqual(disabled["application"]["binding_status"][0]["status"], "DISABLED")
        self.assertEqual(self.apps.runtime_options("task", disabled["application"]), {})
        self.units[0]["dispatch"]["status"] = "UNAVAILABLE"
        self.apps.configure(self.project, dict(enabled_groups=["shared"]))
        with self.assertRaisesRegex(ValueError, "尚不可调用"):
            self.apps.runtime_options("task", self.apps.snapshot(self.project, self.catalog.public())[0])

    def test_ambiguous_preset_and_invalid_interfaces_do_not_bind(self):
        self.units.append(unit(group="second"))
        self.groups["second"] = dict(bindings={})
        preset = self.apps.view(self.project)["preset"]
        self.assertNotIn("sanitize", preset["bindings"])
        self.assertIn("sanitize", preset["ambiguous_roles"])
        self.units[0]["capability"]["output"] = dict(type="object", properties={})
        with self.assertRaisesRegex(ValueError, "不匹配"):
            self.apps.configure(self.project, dict(bindings=dict(sanitize=self.units[0]["id"])))

    def test_task_view_keeps_installed_identity_after_configuration_and_catalog_change(self):
        snapshot, selected = self.apps.snapshot(self.project, self.catalog.public())
        frozen = TaskCapabilityCatalog(self.catalog, selected)
        name, identity = self.units[0]["tool_name"], self.units[0]["id"]
        self.apps.configure(self.project, dict(enabled_groups=[]))
        self.units[0]["id"] = "replacement:sanitize_text"
        self.assertEqual(frozen.name_to_id(name), identity)
        self.assertEqual(frozen.tools_for_project(self.project)[0]["function"]["name"], name)
        self.assertEqual(snapshot["eligible_capability_ids"], [identity])
        with self.assertRaises(ValueError):
            frozen.name_to_id("unadvertised_tool")

    def test_chat_admission_freezes_config_and_explicit_runtime_stays_authoritative(self):
        jobs = ChatJobs(self.store, None, self.catalog, projects=self.projects, applications=self.apps)
        captured = []
        def capture(*args, **kwargs):
            captured.append((args, kwargs))
        class NoWorker:
            def __init__(self, target, args, kwargs, **unused):
                self.target, self.args, self.kwargs = target, args, kwargs
            def start(self):
                capture(*self.args, **self.kwargs)
        with patch("seampact.chat.FrozenProfiles", Bound), patch("seampact.chat.threading.Thread", NoWorker):
            receipt = jobs.start(dict(project_id=self.project, messages=[dict(role="user", content="hello")]))
            self.apps.configure(self.project, dict(enabled_groups=[]))
            task = self.store.get("task", receipt["task_id"])
            self.assertEqual(task["application"]["revision"], 0)
            self.assertEqual([row["id"] for row in task["capability_pool"]], [self.units[0]["id"]])
            self.assertEqual(captured[0][0][3].name_to_id(self.units[0]["tool_name"]), self.units[0]["id"])
            self.projects.finish_task(receipt["task_id"])
            native = jobs.start(dict(project_id=self.project, messages=[dict(role="user", content="native")]),
                                runtime_options=dict(allowed_capabilities=[]))
            self.assertNotIn("application", self.store.get("task", native["task_id"]))
            self.assertEqual(captured[-1][1]["runtime_options"], dict(allowed_capabilities=[]))
            self.projects.finish_task(native["task_id"])
            self.apps.configure(self.project, dict(enabled_groups=["shared"], use_office_preset=True))
            selector = object()
            self.platform.execution = SimpleNamespace(optional_selector=lambda *args: selector)
            simulated = jobs.start(dict(project_id=self.project, messages=[dict(role="user", content="simulated")]),
                                   runtime_options=dict(allowed_capabilities=[]), apply_project_application=True)
            options = captured[-1][1]["runtime_options"]
            self.assertEqual(options["allowed_capabilities"], [])
            self.assertIs(options["optional_hooks"], selector)
            self.assertIn("sanitize:input_received", [hook.name for hook in options["hooks"]])
            self.assertEqual(self.store.get("task", simulated["task_id"])["application"]["effective_bindings"],
                             dict(sanitize=self.units[0]["id"]))
            self.assertEqual(captured[-1][0][3].tools(), [])
            self.projects.finish_task(simulated["task_id"])


if __name__ == "__main__":
    unittest.main()
