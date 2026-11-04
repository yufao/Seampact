"""Reference lifetime, frozen starts and archives, without running a model."""
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from seampact.hosting.images import Images, PreparationFailed, ROLE, SCOPE, OWNER, IMAGE
from seampact.store import Store

CONFIGS = [b'{"architecture":"amd64","n":1}', b'{"architecture":"amd64","n":2}']
A, B = ["sha256:" + hashlib.sha256(value).hexdigest() for value in CONFIGS]


class Docker:
    def __init__(self, root):
        self.root, self.daemon = str(root), "engine-one"
        self.calls, self.containers, self.tags = [], {}, {}
        self.images = {i: dict(Id=i, Size=4096, Os="linux", Architecture="amd64", RepoTags=[], RepoDigests=[],
            Config=dict(Volumes=None, Labels={})) for i in (A, B)}
        self.unknown = False
        self.lost_response = False

    def __call__(self, argv, timeout):
        self.calls.append(argv)
        def answer(value=None, code=0, error=""):
            return dict(returncode=code, stdout=json.dumps(value) if value is not None else "", stderr=error)
        if self.unknown:
            return answer(code=1, error="permission denied")
        if argv[1] == "info":
            return answer(dict(id=self.daemon, os="linux", root=self.root, runtimes={"runc": {}}))
        if argv[1:3] == ["context", "show"]:
            return dict(returncode=0, stdout="default", stderr="")
        if argv[1:3] == ["context", "inspect"]:
            return answer("unix:///var/run/docker.sock")
        if argv[1:3] == ["image", "inspect"]:
            identity = self.tags.get(argv[-1], argv[-1])
            if identity not in self.images:
                return answer(code=1, error="No such image: missing")
            return answer(identity if argv[-2] == '{{json .Id}}' else self.images[identity])
        if argv[1:3] == ["container", "inspect"]:
            obj = next((v for k, v in self.containers.items() if k == argv[-1] or v["Id"] == argv[-1]), None)
            return answer(obj) if obj else answer(code=1, error="No such container: missing")
        if argv[1] == "create":
            if argv[-1] not in self.images:
                return answer(code=1, error="No such image")
            labels = dict(arg.split("=", 1) for n, arg in enumerate(argv) if n and argv[n - 1] == "--label")
            name = argv[argv.index("--name") + 1]
            self.containers[name] = dict(Id=hashlib.sha256(name.encode()).hexdigest(), Image=argv[-1], Labels=labels,
                Healthcheck=dict(Test=["NONE"]), Entrypoint=["/__image_reference_never_start__"],
                State=dict(Status="created", Running=False, Pid=0), Mounts=[],
                HostConfig=dict(Runtime="runc", NetworkMode="none", RestartPolicy=dict(Name="no"), Devices=[], DeviceRequests=[]))
            self.containers[name]["HostConfig"]["Tmpfs"] = dict(arg.split(":", 1) for n, arg in enumerate(argv) if n and argv[n - 1] == "--tmpfs")
            return answer(code=1 if self.lost_response else 0)
        if argv[1] == "rm":
            for name, obj in list(self.containers.items()):
                if name == argv[-1] or obj["Id"] == argv[-1]:
                    del self.containers[name]
            return answer()
        if argv[1:3] == ["image", "save"]:
            with tarfile.open(argv[argv.index("-o") + 1], "w") as tar:
                manifest = []
                for identity in argv[5:]:
                    config = CONFIGS[(A, B).index(identity)]
                    name = identity[7:] + ".json"
                    member = tarfile.TarInfo(name); member.size = len(config)
                    tar.addfile(member, io.BytesIO(config))
                    manifest.append(dict(Config=name, RepoTags=None, Layers=[]))
                data = json.dumps(manifest).encode()
                member = tarfile.TarInfo("manifest.json"); member.size = len(data)
                tar.addfile(member, io.BytesIO(data))
            return answer()
        if argv[1:3] == ["image", "load"]:
            for identity in (A, B):
                self.images[identity] = dict(Id=identity, Size=4096, Os="linux", Architecture="amd64", RepoTags=[], RepoDigests=[],
                    Config=dict(Volumes=None, Labels={}))
            return answer()
        if argv[1] == "tag":
            self.tags[argv[-1]] = argv[-2]
            return answer()
        if argv[1] == "ps":
            return dict(returncode=0, stdout="", stderr="")
        if argv[1:3] == ["image", "rm"]:
            del self.images[argv[-1]]
            return answer()
        raise AssertionError(argv)


class ImageRetention(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store = Store(self.root / "state.sqlite3")
        self.addCleanup(self.store.close)
        self.docker = Docker(self.root)
        self.instances = []
        self.platform = SimpleNamespace(store=self.store,
            deployments=SimpleNamespace(live_instances=lambda group: [i for i in self.instances if i["group_id"] == group],
                backend=lambda: dict(kind="docker", mounts=[]), _start_gap=lambda group, artifact: None),
            managed_services=SimpleNamespace(leases={}))
        self.images = Images(self.platform, self.docker)
        self.platform.images = self.images
        self.store.save("runtime_group", "g", dict(id="g", contract=dict(digest="contract")))
        self.store.save("deployment", "g", dict(id="g", build=dict(image_id=A), binding_fingerprint="binding", platform_core={}))
        self.groups = dict(g=dict(image_id=A, image=A, group_id="g"))

    def prepare(self, **extra):
        body = dict(groups=self.groups, run_ids=["run"], comparison_units=[dict(id="pair", run_ids=["native", "run"])], reserve_bytes=0)
        body.update(extra)
        # The fake daemon is a local Linux host, regardless of this test runner's OS.
        with patch.object(self.images, "storage", return_value=dict(free_bytes=10 ** 10)):
            result = self.images.prepare(body)
        return result

    def test_storage_probe_uses_deployment_identity_and_real_mapped_filesystem(self):
        mounts = [dict(Type="bind", Source="/daemon/docker", Destination=str(self.root))]
        env = dict(HOSTNAME="removed-old-container", SEAMPACT_CONTAINER_NAME="platform",
                   SEAMPACT_DOCKER_STORAGE_PATH=str(self.root))
        with patch.dict(os.environ, env), patch.object(Path, "exists", return_value=True), \
                patch.object(self.images, "_json", return_value=mounts) as inspect:
            value = self.images.storage(dict(root="/daemon/docker"), reserve=0)
        self.assertEqual(value["path"], str(self.root))
        self.assertGreater(value["free_bytes"], 0)
        self.assertEqual(inspect.call_args.args[0][-1], "platform")

    def test_storage_probe_still_rejects_an_unrelated_filesystem(self):
        mounts = [dict(Type="bind", Source="/different/daemon", Destination=str(self.root))]
        with patch.dict(os.environ, dict(SEAMPACT_CONTAINER_NAME="platform")), \
                patch.object(Path, "exists", return_value=True), patch.object(self.images, "_json", return_value=mounts):
            with self.assertRaisesRegex(PreparationFailed, "DOCKER_STORAGE_UNKNOWN"):
                self.images.storage(dict(root="/daemon/docker"), str(self.root), reserve=0)

    def test_same_frozen_image_survives_multiple_real_consumer_releases(self):
        scope = self.prepare()
        self.assertEqual(scope["status"], "READY_TO_RUN")
        self.images.begin(scope["id"], "run")
        for generation in range(2):
            with self.images.launch("g", "run") as frozen:
                self.assertEqual(frozen["image_id"], A)
                self.assertIn("check_ms", frozen)
                self.instances.append(dict(group_id="g", id=str(generation)))
            self.instances.clear()
            self.assertEqual(len(self.docker.containers), 1)
        ref = next(iter(self.docker.containers.values()))
        self.assertFalse(ref["State"]["Running"])
        self.assertEqual(ref["HostConfig"]["DeviceRequests"], [])
        self.assertEqual(self.store.list("runtime_instance"), [])
        self.assertEqual(self.images.close(scope["id"])["status"], "CLOSED")
        self.assertIn(A, self.docker.images)
        self.assertEqual(self.docker.containers, {})

    def test_two_scopes_own_independent_references(self):
        first, second = self.prepare(), self.prepare(run_ids=["other"])
        self.assertEqual(len(self.docker.containers), 2)
        self.images.close(first["id"])
        self.assertEqual(len(self.docker.containers), 1)
        self.images.verify(second["id"])
        with self.assertRaises(PreparationFailed):
            with self.images.launch("g", "run"):
                pass

    def test_close_blocks_new_launches_and_waits_for_submission(self):
        scope = self.prepare()
        entered, finish = threading.Event(), threading.Event()
        errors = []
        def submit():
            try:
                with self.images.launch("g", "run"):
                    entered.set(); finish.wait(5)
            except Exception as error:
                errors.append(error)
        worker = threading.Thread(target=submit); worker.start()
        self.assertTrue(entered.wait(5))
        try:
            self.assertEqual(self.images.close(scope["id"])["status"], "CLOSING")
            self.assertTrue(self.docker.containers)
            with self.assertRaises(PreparationFailed):
                with self.images.launch("g", "run"):
                    pass
        finally:
            finish.set(); worker.join(5)
        self.assertFalse(errors)
        self.assertEqual(self.images.close(scope["id"])["status"], "CLOSED")

    def test_reference_loss_fails_before_consumer_allocation_without_repair(self):
        scope = self.prepare()
        self.images.begin(scope["id"])
        self.docker.containers.clear()
        with self.assertRaisesRegex(PreparationFailed, "REFERENCE_MISSING"):
            with self.images.launch("g", "run"):
                self.fail("must not allocate")
        failure = self.images.get(scope["id"])["failure"]
        self.assertEqual((failure["category"], failure["phase"]), ("PREPARATION_FAILED", "RUNTIME"))
        self.assertFalse(any(c[1] in {"pull", "build", "load"} for c in self.docker.calls))
        self.assertEqual(self.images.get(scope["id"])["tickets"], {})

    def test_image_loss_and_unknown_are_distinct(self):
        scope = self.prepare()
        del self.docker.images[A]
        with self.assertRaisesRegex(PreparationFailed, "IMAGE_MISSING"):
            self.images.verify(scope["id"])
        self.docker.unknown = True
        with self.assertRaisesRegex(PreparationFailed, "DOCKER_UNKNOWN"):
            self.images.inspect(A)

    def test_partial_create_failure_cleans_only_own_refs(self):
        del self.docker.images[B]
        scope = self.prepare(images=[B])
        self.assertEqual(scope["status"], "CLOSED")
        self.assertEqual(scope["failure"]["phase"], "BEFORE_RUN")
        self.assertEqual(self.docker.containers, {})
        self.assertIn(A, self.docker.images)

    def test_lost_create_response_is_reconciled_by_name(self):
        self.docker.lost_response = True
        scope = self.prepare()
        self.assertEqual(scope["status"], "READY_TO_RUN")
        self.assertEqual(len(self.docker.containers), 1)

    def test_declared_volume_is_overridden_without_persistent_mounts(self):
        self.docker.images[A]["Config"]["Volumes"] = {"/models": {}}
        result = self.prepare()
        self.assertEqual(result["status"], "READY_TO_RUN")
        self.assertEqual(next(iter(self.docker.containers.values()))["Mounts"], [])
        self.assertEqual(result["references"][A]["tmpfs"], {"/models": "ro,size=65536,mode=000"})

    def test_changed_daemon_keeps_old_references_pending(self):
        scope = self.prepare()
        self.docker.daemon = "different"
        self.assertEqual(self.images.close(scope["id"])["status"], "CLEANUP_PENDING")
        self.assertTrue(self.docker.containers)
        self.assertFalse(any(c[1] == "rm" for c in self.docker.calls))

    def test_reference_that_started_is_not_silently_deleted(self):
        scope = self.prepare()
        next(iter(self.docker.containers.values()))["State"].update(Status="running", Running=True, Pid=42)
        result = self.images.close(scope["id"])
        self.assertEqual(result["status"], "CLEANUP_PENDING")
        self.assertTrue(self.docker.containers)

    def test_real_consumer_and_lease_prevent_close(self):
        scope = self.prepare()
        self.instances.append(dict(group_id="g", id="real"))
        self.assertEqual(self.images.close(scope["id"])["status"], "CLOSING")
        self.instances.clear()
        self.platform.managed_services.leases["g"] = {"request": {}}
        self.assertEqual(self.images.close(scope["id"])["status"], "CLOSING")
        self.platform.managed_services.leases.clear()
        self.assertEqual(self.images.close(scope["id"])["status"], "CLOSED")

    def test_measuring_blocks_image_operations_even_after_failure(self):
        scope = self.prepare()
        self.images.begin(scope["id"])
        self.images._fail(scope["id"], ValueError("lost"))
        with self.assertRaises(ValueError):
            with self.images.image_operation("build"):
                self.fail()

    def test_scope_protects_artifact_between_child_windows(self):
        scope = self.prepare()
        with self.assertRaises(ValueError):
            self.images.guard_artifact("g")
        self.images.close(scope["id"])
        self.images.guard_artifact("g")

    def test_mount_change_invalidates_conditions(self):
        path = self.root / "config.json"; path.write_text("first")
        self.platform.deployments.backend = lambda: dict(kind="docker", mounts=[dict(source=str(path), target="/config", read_only=True)])
        scope = self.prepare()
        path.write_text("different content")
        with self.assertRaisesRegex(PreparationFailed, "BINDING_CHANGED"):
            self.images.verify(scope["id"])

    def test_complete_unit_survives_later_incomplete_unit(self):
        scope = self.prepare(comparison_units=[dict(id="first", run_ids=["a", "b"]), dict(id="second", run_ids=["c", "d"])])
        for name in ("a", "b", "c"):
            self.images.record_run_end(scope["id"], name, "COMPLETED")
        self.images._fail(scope["id"], ValueError("missing later"))
        self.images.record_run_end(scope["id"], "d", "PREPARATION_FAILED")
        units = self.images.get(scope["id"])["comparison_units"]
        self.assertEqual(units[0]["status"], "COMPLETED")
        self.assertNotIn("status", units[1])

    def test_restart_preserves_refs_and_does_not_resume_scope(self):
        scope = self.prepare()
        restarted = Images(self.platform, self.docker)
        self.assertEqual(restarted.get(scope["id"])["status"], "INTERRUPTED")
        with self.assertRaises(PreparationFailed):
            restarted.begin(scope["id"])
        self.assertTrue(self.docker.containers)

    def test_archive_roundtrip_reuses_ids_and_existing_valid_backup(self):
        backup = self.images.backup(dict(images=[A, B], directory=str(self.root), reserve_bytes=0))
        self.assertEqual(backup["status"], "VERIFIED_ARCHIVE")
        self.assertIsNone(backup["restored_at"])
        duplicate = self.images.backup(dict(images=[A], directory=str(self.root), reserve_bytes=0))
        self.assertEqual(duplicate["id"], backup["id"])
        self.docker.images.clear()
        with patch.object(self.images, "storage", return_value={}):
            restored = self.images.restore(dict(backup_id=backup["id"]))
        self.assertEqual(restored["restore_status"], "VERIFIED")
        self.assertEqual(set(self.docker.images), {A, B})
        self.assertEqual(self.docker.containers, {})

    def test_archive_corruption_and_full_archive_delete_confirmation(self):
        backup = self.images.backup(dict(images=[A, B], directory=str(self.root), reserve_bytes=0))
        with self.assertRaises(ValueError):
            self.images.delete(dict(backup_id=backup["id"], images=[A]))
        Path(backup["path"]).write_bytes(b"broken")
        with self.assertRaises(ValueError):
            self.images.restore(dict(backup_id=backup["id"]))
        with self.assertRaises(ValueError):
            self.images.delete(dict(backup_id=backup["id"], images=[A, B]))

    def test_restore_names_rejects_conflicts_without_overwriting(self):
        reference = "seampact-build-input:sha256-" + B[7:]
        self.store.save("deployment", "g", dict(id="g", build=dict(image_id=A, resolved_base_id=B, base_build_reference=reference)))
        backup = self.images.backup(dict(images=[A, B], directory=str(self.root), reserve_bytes=0))
        self.docker.tags[reference] = A
        restored = self.images.restore(dict(backup_id=backup["id"]))
        self.assertEqual(restored["restore_status"], "NAME_CONFLICT")
        self.assertEqual(self.docker.tags[reference], A)

    def test_import_space_is_not_archive_space(self):
        backup = self.images.backup(dict(images=[A], directory=str(self.root), reserve_bytes=0))
        del self.docker.images[A]
        with patch.object(self.images, "storage", side_effect=PreparationFailed("DOCKER_STORAGE_FULL")):
            with self.assertRaisesRegex(PreparationFailed, "STORAGE_FULL"):
                self.images.restore(dict(backup_id=backup["id"]))
        self.assertFalse(any(c[1:3] == ["image", "load"] for c in self.docker.calls))

    def test_delete_is_targeted_no_force_no_parent_prune(self):
        result = self.images.delete(dict(image_id=A, tags=[], confirm_unused=True))
        self.assertEqual(result["status"], "DELETED")
        self.assertIn(["docker", "image", "rm", "--no-prune", A], self.docker.calls)
        self.assertIn(B, self.docker.images)

    def test_delete_protected_image_is_rejected(self):
        self.prepare()
        with self.assertRaises(ValueError):
            self.images.delete(dict(image_id=A, tags=[], confirm_unused=True))

    def test_backup_metadata_can_be_registered_after_state_reset(self):
        backup = self.images.backup(dict(images=[A], directory=str(self.root), reserve_bytes=0))
        self.store.delete("image_backup", backup["id"])
        imported = self.images.import_backup(dict(metadata_path=backup["metadata_path"]))
        self.assertEqual(imported["images"], [A])
        self.assertIsNone(imported["restored_at"])

    def test_task_ticket_prevents_close_and_failure_prevents_new_warm_dispatch(self):
        scope = self.prepare()
        self.images.enter_task(scope["id"], "task", ["g"])
        self.assertEqual(self.images.close(scope["id"])["status"], "CLOSING")
        self.images._fail(scope["id"], ValueError("REFERENCE_MISSING"))
        with self.assertRaises(PreparationFailed):
            self.images.dispatch_check("g", "task", scope["id"])
        self.images.finish_task(scope["id"], "task")
        self.assertEqual(self.images.close(scope["id"])["status"], "CLOSED")

    def test_restart_reconciliation_only_removes_old_platform_tickets(self):
        scope = self.prepare()
        self.images.enter_task(scope["id"], "old-task", ["g"])
        restarted = Images(self.platform, self.docker)
        result = restarted.reconcile(dict(retention_scope_id=scope["id"], confirm_launches_settled=True))
        self.assertEqual(result["status"], "CLOSED")

    def test_manual_scope_accepts_equivalent_training_placement(self):
        scope = self.prepare()
        child = copy.deepcopy(self.groups)
        child["g"].update(kind="REMOTE", start_timeout_s=30)
        self.images.register_runs(scope["id"], ["child"], child, [dict(id="next", run_ids=["child"])])
        self.images.begin(scope["id"], "child")
        with self.assertRaises(ValueError):
            self.images.register_runs(scope["id"], ["late"], child, [dict(id="late", run_ids=["late"])])

    def test_oci_local_image_identity_is_verified_separately_from_config_digest(self):
        config = CONFIGS[0]
        oci_manifest = json.dumps(dict(schemaVersion=2, mediaType="application/vnd.oci.image.manifest.v1+json",
            config=dict(digest=A), layers=[])).encode()
        identity = "sha256:" + hashlib.sha256(oci_manifest).hexdigest()
        archive = self.root / "oci.tar"
        with tarfile.open(archive, "w") as tar:
            members = {
                "blobs/sha256/" + A[7:]: config,
                "blobs/sha256/" + identity[7:]: oci_manifest,
                "manifest.json": json.dumps([dict(Config="blobs/sha256/" + A[7:], Layers=[])]).encode(),
                "index.json": json.dumps(dict(manifests=[dict(mediaType="application/vnd.oci.image.manifest.v1+json", digest=identity)])).encode()}
            for name, value in members.items():
                member = tarfile.TarInfo(name); member.size = len(value)
                tar.addfile(member, io.BytesIO(value))
        self.images._archive_check(archive, [identity])
        with self.assertRaises(ValueError):
            self.images._archive_check(archive, [B])

    def test_scoped_stop_never_deletes_a_foreign_same_name_container(self):
        from seampact.hosting import backends
        commands = []
        def run(argv, timeout):
            commands.append(argv)
            output = '"other-instance"' if argv[1:3] == ["container", "inspect"] else "some-id"
            return dict(returncode=0, stdout=output, stderr="", output=output, duration_ms=1)
        with patch.object(backends, "docker_available", return_value=None), patch.object(backends, "run", side_effect=run):
            result = backends.stop(dict(handle=dict(container="expected-name", instance_id="owned")), dict(operation_timeout_s=10))
        self.assertEqual(result["status"], "STOP_PENDING")
        self.assertFalse(any(c[1] in {"stop", "rm"} for c in commands))


if __name__ == "__main__":
    unittest.main()
