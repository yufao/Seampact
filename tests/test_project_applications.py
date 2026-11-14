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
from seampact.guard_contract import GUARD_INTERFACES
from seampact.project_applications import ProjectApplications, TaskCapabilityCatalog
from seampact.project_spaces import ProjectSpaces
from seampact.store import Store


def unit(role="sanitize", group="shared", identity=None, ready=True):
    inputs, outputs = GUARD_INTERFACES[role]
    entry = role + "_entry"
    return dict(id=identity or group + ":" + entry, entry_id=entry, runtime_group=group,
                tool_name=entry + "_" + group, package=group, status="READY", package_digest=group,
                capability=dict(purpose=role, input=dict(type="object", properties={name: {} for name in inputs}),
                                output=dict(type="object", properties={name: {} for name in outputs})),
                availability=dict(status="AVAILABLE" if ready else "NOT_PREPARED", reason=None),
                dispatch=dict(status="COLD_STARTABLE" if ready else "UNAVAILABLE"))


def semantic_contract(item, role):
    capability = item["capability"]
    capability["guard"] = {"kind": role, "subjects": ["input" if role == "sanitize" else "message"],
                           "provenance": [{"path": "source.py", "start": 1, "end": 1}]}
    if role == "sanitize":
        capability["input"]["properties"] = {"text": {"type": "string"}}
        capability["output"].update(properties={"sanitized_text": {"type": "string"},
                                                "was_modified": {"type": "boolean"}},
                                    required=["sanitized_text", "was_modified"])
    elif role == "privacy":
        capability["input"]["properties"] = {"text": {"type": "string"},
            "entities": {"type": "array", "items": {"type": "string"}}, "anonymize": {"type": "boolean"}}
        capability["output"].update(properties={"findings": {"type": "array"}}, required=["findings"])


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
        self.platform.execution = SimpleNamespace(optional_selector=lambda *args: SimpleNamespace(
            choose_next=lambda *args: dict(kind="continue")))
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

    def test_explicit_binding_reports_disabled_checks(self):
        semantic_contract(self.units[0], "sanitize")
        selected = self.apps.configure(self.project, dict(bindings={"sanitize": self.units[0]["id"]}))
        self.assertEqual([row["unit_id"] for row in selected["application"]["guard_candidates"]], [self.units[0]["id"]])
        sentinel_selector = SimpleNamespace(choose_next=lambda *args: dict(kind="continue"))
        self.platform.execution = SimpleNamespace(optional_selector=lambda *args: sentinel_selector)
        options = self.apps.runtime_options("task", selected["application"])
        self.assertTrue(callable(options["optional_hooks"].choose_next))
        self.assertIn("sanitize:input_received", [hook.name for hook in options["hooks"]])
        disabled = self.apps.configure(self.project, dict(enabled_groups=[]))
        self.assertEqual(disabled["application"]["binding_status"][0]["status"], "DISABLED")
        self.assertEqual(self.apps.runtime_options("task", disabled["application"]), {})
        self.units[0]["dispatch"]["status"] = "UNAVAILABLE"
        self.apps.configure(self.project, dict(enabled_groups=["shared"]))
        with self.assertRaisesRegex(ValueError, "尚不可调用"):
            self.apps.runtime_options("task", self.apps.snapshot(self.project, self.catalog.public())[0])

    def test_missing_semantic_metadata_and_invalid_interfaces_do_not_bind(self):
        self.assertFalse(self.apps.view(self.project)["application"]["guard_candidates"])
        with self.assertRaisesRegex(ValueError, "不匹配"):
            self.apps.configure(self.project, dict(bindings=dict(sanitize=self.units[0]["id"])))
        semantic_contract(self.units[0], "sanitize")
        self.units[0]["capability"]["output"] = dict(type="object", properties={})
        with self.assertRaisesRegex(ValueError, "不匹配"):
            self.apps.configure(self.project, dict(bindings=dict(sanitize=self.units[0]["id"])))

    def test_semantic_guards_auto_bind_and_explicit_disable_wins(self):
        guarded = unit(group="guard")
        guarded.update(id="g", entry_id="new_guard_entry", tool_name="opaque_guard")
        semantic_contract(guarded, "sanitize")
        self.units.append(guarded)
        self.groups["guard"] = dict(bindings={})
        snapshot, _ = self.apps.snapshot(self.project, self.catalog.public())
        self.assertEqual([(row["role"], row["unit_id"]) for row in snapshot["guard_candidates"]], [("sanitize", "g")])
        self.assertEqual(snapshot["guard_candidates"][0]["guard"]["subjects"], ["input"])
        self.apps.configure(self.project, dict(bindings={"sanitize": None}))
        snapshot, _ = self.apps.snapshot(self.project, self.catalog.public())
        self.assertFalse(snapshot["guard_candidates"])
        self.assertEqual(snapshot["binding_status"][0]["status"], "DISABLED")

    def test_ambiguous_generated_guards_never_become_model_tools(self):
        for index in range(2):
            guarded = unit(group="guard" + str(index))
            guarded.update(id="g" + str(index), entry_id="semantic_guard_" + str(index),
                           tool_name="opaque_guard_" + str(index))
            semantic_contract(guarded, "sanitize")
            self.units.append(guarded)
            self.groups[guarded["runtime_group"]] = dict(bindings={})
        snapshot, _ = self.apps.snapshot(self.project, self.catalog.public())
        self.assertEqual({item["unit_id"] for item in snapshot["guard_candidates"]}, {"g0", "g1"})
        jobs = ChatJobs(self.store, None, self.catalog, projects=self.projects, applications=self.apps)
        captured = []
        class NoWorker:
            def __init__(self, target, args, kwargs, **unused):
                captured.append((args, kwargs))
            def start(self):
                pass
        with patch("seampact.chat.FrozenProfiles", Bound), patch("seampact.chat.threading.Thread", NoWorker):
            jobs.start(dict(project_id=self.project, messages=[dict(role="user", content="hello")]))
        names = captured[0][0][3].names
        self.assertNotIn("opaque_guard_0", names)
        self.assertNotIn("opaque_guard_1", names)

    def test_required_check_must_be_available_and_support_message_subject(self):
        privacy = unit("privacy", "guard")
        privacy.update(entry_id="different_entry", id="privacy-guard")
        semantic_contract(privacy, "privacy")
        privacy["capability"]["guard"]["subjects"] = ["input"]
        self.units.append(privacy)
        self.groups["guard"] = dict(bindings={})
        selected = self.apps.configure(self.project, dict(guard_policy={"required_outbound": ["privacy"]}))
        with self.assertRaisesRegex(ValueError, "必需检查"):
            self.apps.runtime_options("task", selected["application"])
        privacy["capability"]["guard"]["subjects"] = ["message"]
        selected = self.apps.view(self.project)
        self.assertTrue(self.apps.runtime_options("task", selected["application"])["hooks"])
        privacy["dispatch"]["status"] = "UNAVAILABLE"
        with self.assertRaisesRegex(ValueError, "必需检查"):
            self.apps.runtime_options("task", self.apps.view(self.project)["application"])

    def test_multiple_required_guards_need_explicit_pin_and_invalid_pin_blocks(self):
        for index in range(2):
            candidate = unit("privacy", "guard" + str(index), identity="privacy" + str(index))
            semantic_contract(candidate, "privacy")
            self.units.append(candidate)
            self.groups[candidate["runtime_group"]] = dict(bindings={})
        snapshot = self.apps.configure(self.project, dict(guard_policy={"required_outbound": ["privacy"]}))["application"]
        self.assertEqual(len(snapshot["guard_candidates"]), 2)
        with self.assertRaisesRegex(ValueError, "多个.*明确绑定"):
            self.apps.runtime_options("task", snapshot)
        snapshot = self.apps.configure(self.project, dict(bindings={"privacy": "privacy0"}))["application"]
        self.assertEqual([row["unit_id"] for row in snapshot["guard_candidates"]], ["privacy0"])
        self.units[-2]["capability"].pop("guard")
        snapshot = self.apps.view(self.project)["application"]
        self.assertEqual(snapshot["binding_status"][0]["status"], "INCOMPATIBLE")
        with self.assertRaisesRegex(ValueError, "重新分析"):
            self.apps.runtime_options("task", snapshot)

    def test_semantic_binding_requires_callable_contract_and_mount(self):
        candidate = unit("sanitize", "guard")
        candidate.update(entry_id="new", id="g")
        semantic_contract(candidate, "sanitize")
        self.units.append(candidate)
        self.groups["guard"] = dict(bindings={})
        self.assertEqual(self.apps.view(self.project)["application"]["guard_candidates"][0]["unit_id"], "g")
        for mutate in (
                lambda cap: cap["output"]["properties"]["was_modified"].update(type="string"),
                lambda cap: cap["input"].update(required=["text", "unexpected"]),
                lambda cap: cap["output"].update(required=["sanitized_text"]),
                lambda cap: cap["guard"].update(subjects=["code"])):
            original = copy.deepcopy(candidate["capability"])
            mutate(candidate["capability"])
            self.assertFalse(self.apps.view(self.project)["application"]["guard_candidates"])
            candidate["capability"] = original

    def test_old_self_check_name_and_allowed_only_are_not_semantic_guards(self):
        candidate = unit("self_check_input", "old", identity="old")
        candidate["entry_id"] = "self_check_input"
        candidate["capability"]["input"]["properties"] = {"user_message": {"type": "string"}}
        candidate["capability"]["output"] = {"type": "object", "properties": {"allowed": {"type": "boolean"}},
                                             "required": ["allowed"]}
        candidate["capability"]["guard"] = {"kind": "input_safety", "subjects": ["input"],
                                              "provenance": [{"path": "source.py", "start": 1, "end": 1}]}
        self.units.append(candidate)
        self.groups["old"] = dict(bindings={})
        self.assertFalse(self.apps.view(self.project)["application"]["guard_candidates"])
        with self.assertRaisesRegex(ValueError, "不匹配"):
            self.apps.configure(self.project, dict(bindings={"self_check_input": "old"}))

    def test_retrieval_requires_source_backed_operation_metadata(self):
        rag = unit(group="rag")
        rag.update(id="r", entry_id="search_documents", tool_name="custom_tool_name")
        rag["capability"] = dict(input=dict(required=["query"], properties={
            "query": {"type": "string"}, "mode": {"type": "string"},
            "top_k": {"type": "integer"}, "rerank": {"type": "boolean"}}),
            output=dict(kind="json", schema=dict(type="object")))
        self.groups["rag"] = dict(bindings={})
        self.units.append(rag)
        snapshot, _ = self.apps.snapshot(self.project, self.catalog.public())
        self.assertEqual(snapshot["operations"]["custom_tool_name"]["kind"], "other")
        rag["capability"]["operation"] = {"kind": "retrieve", "provenance": [{"path": "x.py", "start": 1, "end": 1}]}
        snapshot, _ = self.apps.snapshot(self.project, self.catalog.public())
        self.assertEqual(snapshot["operations"]["custom_tool_name"]["kind"], "retrieve")

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
        semantic_contract(self.units[0], "sanitize")
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
            with self.assertRaises(ValueError):
                captured[0][0][3].name_to_id(self.units[0]["tool_name"])
            self.projects.finish_task(receipt["task_id"])
            native = jobs.start(dict(project_id=self.project, messages=[dict(role="user", content="native")]),
                                runtime_options=dict(allowed_capabilities=[]), apply_project_application=False)
            self.assertNotIn("application", self.store.get("task", native["task_id"]))
            self.assertEqual(captured[-1][1]["runtime_options"], dict(allowed_capabilities=[]))
            self.projects.finish_task(native["task_id"])
            self.apps.configure(self.project, dict(enabled_groups=["shared"]))
            ordinary = jobs.start(dict(project_id=self.project, messages=[dict(role="user", content="ordinary")]),
                                  runtime_options=dict(allowed_capabilities=[]))
            self.assertIn("application", self.store.get("task", ordinary["task_id"]))
            self.assertEqual(captured[-1][1]["runtime_options"]["allowed_capabilities"], [])
            self.assertIn("sanitize:input_received", [hook.name for hook in captured[-1][1]["runtime_options"]["hooks"]])
            self.projects.finish_task(ordinary["task_id"])
            selector = SimpleNamespace(choose_next=lambda *args: dict(kind="continue"))
            self.platform.execution = SimpleNamespace(optional_selector=lambda *args: selector)
            simulated = jobs.start(dict(project_id=self.project, messages=[dict(role="user", content="simulated")]),
                                   runtime_options=dict(allowed_capabilities=[]), apply_project_application=True)
            options = captured[-1][1]["runtime_options"]
            self.assertEqual(options["allowed_capabilities"], [])
            self.assertTrue(callable(options["optional_hooks"].choose_next))
            self.assertIn("sanitize:input_received", [hook.name for hook in options["hooks"]])
            self.assertEqual(self.store.get("task", simulated["task_id"])["application"]["guard_candidates"][0]["unit_id"],
                             self.units[0]["id"])
            self.assertEqual(captured[-1][0][3].tools(), [])
            self.projects.finish_task(simulated["task_id"])


if __name__ == "__main__":
    unittest.main()
