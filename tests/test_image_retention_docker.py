"""Opt-in local Docker verification using the ordinary Platform and Worker.

Requires existing python:3.11-slim and busybox images; never pulls or prunes.
Set SEAMPACT_DOCKER_TESTS=1. This is software validation, not a model experiment.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from seampact.hosting import backends
from seampact.hosting.images import Images, PreparationFailed
from seampact.store import Store
from seampact.source.inspection import Inspector
from seampact.server import make_server
from seampact.service import Platform


@unittest.skipUnless(os.environ.get("SEAMPACT_DOCKER_TESTS") == "1", "opt-in real Docker check")
class RealDockerRetention(unittest.TestCase):
    def command(self, *args):
        result = subprocess.run(["docker", *args], capture_output=True, text=True, encoding="utf-8", timeout=180)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        return result.stdout.strip()

    def test_reference_loss_aborts_without_runtime_repair(self):
        with tempfile.TemporaryDirectory(prefix="seampact-reference-loss-") as temporary:
            store = Store(Path(temporary) / "state.sqlite3")
            calls = []
            def run(argv, timeout):
                calls.append(argv)
                return backends.run(argv, timeout)
            platform = SimpleNamespace(store=store, deployments=SimpleNamespace(live_instances=lambda group: []),
                                       managed_services=SimpleNamespace(leases={}))
            images = Images(platform, run)
            image = json.loads(self.command("image", "inspect", "--format", "{{json .Id}}", "busybox:latest"))
            scope = None
            try:
                with patch.object(images, "storage", return_value=dict(basis="reference-only validation")):
                    scope = images.prepare(dict(images=[image], comparison_units=[dict(id="loss", run_ids=["native"])], reserve_bytes=0))
                self.assertEqual(scope["status"], "READY_TO_RUN", scope.get("failure"))
                images.begin(scope["id"])
                self.command("rm", scope["references"][image]["id"])
                with self.assertRaisesRegex(PreparationFailed, "REFERENCE_MISSING"):
                    images.verify(scope["id"])
                self.assertEqual(images.get(scope["id"])["failure"]["phase"], "RUNTIME")
                self.assertFalse(any(c[1] in {"pull", "build", "load"} for c in calls))
                self.assertEqual(images.inspect(image)["Id"], image)
            finally:
                if scope:
                    images.close(scope["id"])
                store.close()

    def test_real_worker_release_restart_archive_and_volume_creation(self):
        baseline = set(self.command("image", "ls", "-q", "--no-trunc").splitlines())
        baseline_tags = set(self.command("image", "ls", "--format", "{{.Repository}}:{{.Tag}}").splitlines())
        self.command("image", "inspect", "python:3.11-slim", "busybox:latest")
        with tempfile.TemporaryDirectory(prefix="seampact-retention-") as temporary:
            root = Path(temporary)
            platform = Platform(root / "platform")
            server = make_server(platform, host="0.0.0.0", port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            owned_images, owned_tags, scope_ids = set(), set(), []
            try:
                source = root / "original"; source.mkdir()
                (source / "original.py").write_text("def score(value: int) -> int:\n    return value + 7\n", encoding="utf-8")
                inspector = Inspector.freeze(source, root / "snapshots", "request", ["original.py"], "score", "result")
                inspector.request["source_id"] = "fixture"
                candidate = dict(entries=[dict(id="score", path="original.py", symbol="score", kind="function",
                    boundary_ids=["score-W"])], boundaries=[dict(id="score-W", path="original.py", symbol="score", mode="W")])
                unit = platform.catalog.install(inspector, candidate, "analysis", "install")[0]
                platform.store.save("harness", "fixture", dict(id="fixture", status="CONNECTED", installation_id="install"))
                group = unit["runtime_group"]
                backend = platform.deployments.configure_backend(dict(kind="docker", coordinator_url=
                    "http://host.docker.internal:" + str(server.server_port), publish_host="127.0.0.1",
                    advertise_host="127.0.0.1", operation_timeout_s=60, start_timeout_s=60))
                artifact = platform.deployments.export(group, dict(environment=dict(python="3.11", requirements=[], packages=[])))
                built = backends.build(artifact["folder"], artifact["image"], artifact["environment"], backend,
                    dependency_image=artifact["build_reuse"]["dependency_image"])
                if os.environ.get("SEAMPACT_DOCKER_RECEIPT"):
                    Path(os.environ["SEAMPACT_DOCKER_RECEIPT"]).write_text(json.dumps(built, ensure_ascii=False, indent=2), encoding="utf-8")
                owned_tags.update({artifact["image"], built.get("dependency_image"), built.get("base_build_reference")} - {None})
                self.assertEqual(built["status"], "BUILT", built.get("reason"))
                image = built["image_id"]
                owned_images.update({image, built["dependency_reuse"].get("dependency_image_id")} - {None})
                artifact.update(build=built, publish=backends.publish(artifact["image"], backend, image_id=image))
                platform.store.save("deployment", group, artifact)
                # Docker Desktop stores layers in a Linux VM, not the Windows
                # client drive. Use an independent read-only statvfs probe for
                # this test's preparation receipt; production storage() still
                # requires its verifiable host-path mapping.
                engine = platform.images.engine()
                probe = json.loads(self.command("run", "--rm", "--pull=never", "--network=none", "--runtime=runc",
                    "--mount", "type=bind,source=" + engine["root"] + ",target=/probe,readonly", "--entrypoint", "python",
                    "python:3.11-slim", "-c", "import os,json;s=os.statvfs('/probe');print(json.dumps({'free_bytes':s.f_bavail*s.f_frsize}))"))
                self.assertGreater(probe["free_bytes"], 1024 ** 3)
                with patch.object(platform.images, "storage", return_value=dict(probe, basis="independent daemon statvfs probe")):
                    scope = platform.images.prepare(dict(group_ids=[group], run_ids=["run"],
                        comparison_units=[dict(id="pair", run_ids=["run"])], reserve_bytes=1024 ** 3))
                scope_ids.append(scope["id"])
                self.assertEqual(scope["status"], "READY_TO_RUN", scope.get("failure"))
                platform.images.begin(scope["id"], "run")
                instances = []
                for generation in range(2):
                    record = platform.deployments.start(group, {}, run_id="run")
                    instances.append(record["id"])
                    deadline = time.monotonic() + 60
                    while time.monotonic() < deadline:
                        record = platform.deployments.instances[record["id"]]
                        if record["state"] == "READY":
                            break
                        time.sleep(0.2)
                    self.assertEqual(record["state"], "READY", record.get("reason"))
                    self.assertEqual(record["image_id"], image)
                    result, facts = platform.deployments._call(record, unit, dict(value=generation), ())
                    self.assertEqual(result, generation + 7)
                    platform.deployments.drain_stop(group, dict(instance_id=record["id"]), run_id="run")
                    deadline = time.monotonic() + 60
                    while time.monotonic() < deadline and platform.deployments.live_instances(group):
                        time.sleep(0.2)
                    self.assertFalse(platform.deployments.live_instances(group))
                    platform.images.verify(scope["id"])
                self.assertNotEqual(*instances)
                self.assertEqual(platform.images.close(scope["id"])["status"], "CLOSED")
                # Backup/restore an isolated tiny image, never delete the base.
                volume = root / "volume"; volume.mkdir()
                (volume / "Dockerfile").write_text('FROM busybox:latest\nVOLUME /payload\nCMD ["true"]\n', encoding="utf-8")
                tag = "seampact-retention-check:" + uuid.uuid4().hex
                self.command("build", "--pull=false", "--network=none", "-t", tag, str(volume))
                volume_id = json.loads(self.command("image", "inspect", "--format", "{{json .Id}}", tag))
                owned_images.add(volume_id); owned_tags.add(tag)
                name = "seampact-volume-probe-" + uuid.uuid4().hex
                try:
                    cid = self.command("create", "--pull=never", "--runtime=runc", "--network=none", "--restart=no",
                        "--no-healthcheck", "--entrypoint", "/never-run", "--tmpfs", "/payload:ro,size=65536,mode=000", "--name", name, volume_id)
                    info = json.loads(self.command("inspect", "--format", '{{json .}}', cid))
                    self.assertEqual((info["State"]["Status"], info["State"]["Pid"]), ("created", 0))
                    self.assertEqual(info["Mounts"], [])
                    self.assertEqual(info["HostConfig"]["Tmpfs"], {"/payload": "ro,size=65536,mode=000"})
                finally:
                    self.command("rm", name)
                with patch.object(platform.images, "storage", return_value=probe):
                    held_volume = platform.images.prepare(dict(images=[volume_id], comparison_units=[dict(id="volume", run_ids=["probe"])], reserve_bytes=0))
                scope_ids.append(held_volume["id"])
                self.assertEqual(held_volume["status"], "READY_TO_RUN", held_volume.get("failure"))
                self.assertEqual(platform.images.close(held_volume["id"])["status"], "CLOSED")
                tiny = root / "tiny"; tiny.mkdir()
                (tiny / "Dockerfile").write_text('FROM busybox:latest\nLABEL io.seampact.validation="' + uuid.uuid4().hex + '"\n', encoding="utf-8")
                tiny_tag = "seampact-retention-check:" + uuid.uuid4().hex
                self.command("build", "--pull=false", "--network=none", "-t", tiny_tag, str(tiny))
                tiny_id = json.loads(self.command("image", "inspect", "--format", "{{json .Id}}", tiny_tag))
                owned_images.add(tiny_id); owned_tags.add(tiny_tag)
                backup = platform.images.backup(dict(images=[tiny_id], directory=str(root), reserve_bytes=0))
                platform.images.delete(dict(image_id=tiny_id, tags=[tiny_tag], confirm_unused=True))
                self.assertIsNone(platform.images.inspect(tiny_id))
                with patch.object(platform.images, "storage", return_value=probe):
                    restored = platform.images.restore(dict(backup_id=backup["id"]))
                self.assertEqual(restored["restore_status"], "VERIFIED")
                self.assertEqual(platform.images.inspect(tiny_id)["Id"], tiny_id)
                print("REAL_DOCKER: two registered workers invoked and released; same image ID; archive restored; VOLUME tmpfs probe verified")
            finally:
                for record in list(platform.deployments.instances.values()):
                    handle = record.get("handle") or {}
                    if handle.get("container"):
                        subprocess.run(["docker", "stop", "-t", "2", handle.get("container_id") or handle["container"]], capture_output=True)
                        subprocess.run(["docker", "rm", handle.get("container_id") or handle["container"]], capture_output=True)
                for identity in scope_ids:
                    platform.images.close(identity)
                server.shutdown(); server.server_close()
                platform.close()
                # Only this test's new image IDs/tags; no prune, no force.
                for tag in owned_tags - baseline_tags:
                    subprocess.run(["docker", "image", "rm", "--no-prune", tag], capture_output=True)
                for image in owned_images - baseline:
                    subprocess.run(["docker", "image", "rm", "--no-prune", image], capture_output=True)
