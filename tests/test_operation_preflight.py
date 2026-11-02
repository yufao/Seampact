"""Operation observations report live activity without changing it."""
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from seampact.operation_preflight import preflight


class Store:
    def __init__(self):
        self.rows = {}

    def list(self, kind):
        return list(self.rows.get(kind, {}).values())

    def get(self, kind, identity):
        return self.rows.get(kind, {}).get(identity)

    def put(self, kind, row):
        self.rows.setdefault(kind, {})[row["id"]] = row


class PreflightChecks(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.store.put("harness", dict(id="a", status="READY"))
        self.store.put("harness", dict(id="b", status="READY"))
        self.store.put("runtime_group", dict(id="ga", source_id="a"))
        self.store.put("runtime_group", dict(id="gb", source_id="b"))
        lock = threading.RLock()
        deployments = SimpleNamespace(lock=lock, instances={}, run_locks={}, controller_error=None)
        services = SimpleNamespace(instances=deployments.instances, leases={})
        catalog = SimpleNamespace(lock=threading.RLock(), source_running=lambda source_id: False)
        runtime = SimpleNamespace(snapshot=lambda: dict(calls={}, used=0))
        self.platform = SimpleNamespace(store=self.store, deployments=deployments,
            managed_services=services, catalog=catalog, runtime=runtime)

    def test_saved_images_and_stopped_instances_are_not_running(self):
        self.store.put("deployment", dict(id="ga", build=dict(status="BUILT", image_id="saved")))
        self.platform.deployments.instances["old"] = dict(id="old", group_id="ga", state="STOPPED", released=True)
        self.assertEqual(preflight(self.platform, "update")["status"], "READY")

    def test_updates_report_activity_and_service_leases_once(self):
        self.store.put("task", dict(id="t", status="RUNNING"))
        self.store.put("harness", dict(id="a", status="READY", target_runtime=dict(status="PREPARING")))
        self.store.put("deployment", dict(id="ga", build=dict(status="RUNNING")))
        self.platform.deployments.instances["s"] = dict(id="s", group_id="service:s", target_kind="model-service", state="READY", released=False)
        self.platform.managed_services.leases["service:s"] = {"owner": dict(unknown=True)}
        result = preflight(self.platform, "update")
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual({i["kind"] for i in result["items"]}, {"task", "target_prepare", "worker_build", "managed_service", "service_lease"})
        self.assertEqual(next(i for i in result["items"] if i["kind"] == "service_lease")["status"], "UNKNOWN")

    def test_source_scope_and_unknown_instance(self):
        self.store.put("analysis", dict(id="other", harness_id="b", status="RUNNING"))
        self.store.put("deployment", dict(id="gb", publish=dict(status="RUNNING")))
        self.platform.deployments.instances["x"] = dict(id="x", group_id="ga", state="UNKNOWN")
        result = preflight(self.platform, "source_environment", "a")
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual([i["kind"] for i in result["items"]], ["runtime_instance"])
        self.assertEqual(self.platform.deployments.instances["x"]["state"], "UNKNOWN")

    def test_active_instance_and_run_lock(self):
        self.platform.deployments.instances["x"] = dict(id="x", group_id="ga", state="READY")
        self.assertEqual(preflight(self.platform, "source_environment", "a")["status"], "DRAIN")
        self.platform.deployments.run_locks["ga"] = dict(run_id="r")
        self.assertEqual(preflight(self.platform, "source_environment", "a")["status"], "BLOCKED")

    def test_active_monitor_needs_stopping_only_for_update(self):
        self.platform.monitor = SimpleNamespace(status=lambda: {"current": {"id": "m", "status": "RUNNING"}})
        result = preflight(self.platform, "update")
        self.assertEqual(result["status"], "DRAIN")
        self.assertEqual(result["items"][0]["kind"], "monitor")
        self.platform.monitor.status = lambda: {"current": {"id": "m", "status": "STOPPING"}}
        self.assertEqual(preflight(self.platform, "update")["status"], "DRAIN")
        self.assertEqual(preflight(self.platform, "source_environment", "a")["status"], "READY")

    def test_root_activity_without_inner_calls_or_used_slots(self):
        for state, expected in (("ENABLED", "BLOCKED"), ("ACTIVE", "BLOCKED"),
                                ("UNKNOWN", "UNKNOWN"), ("RETIRED", "READY")):
            with self.subTest(state=state):
                self.platform.runtime.snapshot = lambda: dict(calls={}, used=0, roots={
                    "r": dict(id="r", state=state, task_id="t", run_id="run")})
                result = preflight(self.platform, "update")
                self.assertEqual(result["status"], expected)
                if state != "RETIRED":
                    self.assertEqual(result["items"][0]["kind"], "root")
                    self.assertEqual(result["items"][0]["task_id"], "t")
        self.platform.runtime.snapshot = lambda: dict(calls={}, used=0, roots={
            "r": dict(id="r", state="ACTIVE", outcome_unknown=True)})
        self.assertEqual(preflight(self.platform, "update")["status"], "UNKNOWN")

    def test_validate_operation_and_source(self):
        with self.assertRaises(ValueError):
            preflight(self.platform, "source_environment")
        with self.assertRaises(ValueError):
            preflight(self.platform, "unknown")


if __name__ == "__main__":
    unittest.main()
