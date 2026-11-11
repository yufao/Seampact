"""NVIDIA utility access and same-image configure; no Docker daemon required."""
import ast
import copy
import datetime
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from urllib.parse import parse_qs, quote, urlsplit
import uuid


SOURCE = Path(__file__).resolve().parents[1] / "deploy.sh"
EMBEDDED = SOURCE.read_text(encoding="utf-8").split("<<'SEAMPACT_DEPLOY_PY'", 1)[1].rsplit("SEAMPACT_DEPLOY_PY", 1)[0]
FUNCTIONS = [node for node in ast.parse(EMBEDDED).body if isinstance(node, ast.FunctionDef)]
GPU = "GPU-11111111-2222-3333-4444-555555555555"
OTHER = "GPU-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def helpers(selection, reply=None):
    calls = []

    def fail(message):
        raise RuntimeError(message)

    def docker(*args, **kwargs):
        calls.append(args)
        return reply or SimpleNamespace(returncode=0, stdout=GPU + "\n", stderr="")

    names = {"gpu_observation", "add_gpu_observation", "probe_gpu_observation", "inspect_environment"}
    scope = dict(CONFIG=dict(gpu_observe=selection), re=re, fail=fail, docker=docker,
                 private_error=lambda *args: "private-probe.txt")
    exec(compile(ast.Module(body=[node for node in FUNCTIONS if node.name in names], type_ignores=[]), str(SOURCE), "exec"), scope)
    return scope, calls


class GPUObservationTests(unittest.TestCase):
    def test_absent_setting_preserves_existing_compute_configuration(self):
        scope, calls = helpers(None)
        spec = dict(Env=["KEPT=value"], HostConfig=dict(DeviceRequests=[dict(Capabilities=[["gpu", "compute"]])]))
        original = copy.deepcopy(spec)
        scope["add_gpu_observation"](spec)
        scope["probe_gpu_observation"](spec, "local-image")
        self.assertEqual(spec, original)
        self.assertFalse(calls)

    def test_explicit_uuid_probe_has_no_mounts_download_or_compute(self):
        scope, calls = helpers(GPU)
        spec = dict(User="1000:1000", Env=["KEPT=value"], HostConfig=dict(GroupAdd=[991], Mounts=[dict(Target="/data")]))
        scope["add_gpu_observation"](spec)
        self.assertEqual(spec["HostConfig"]["DeviceRequests"], [dict(Driver="nvidia", DeviceIDs=[GPU], Capabilities=[["gpu", "utility"]])])
        self.assertIn("KEPT=value", spec["Env"])
        self.assertIn("NVIDIA_DRIVER_CAPABILITIES=utility", spec["Env"])
        scope["probe_gpu_observation"](spec, "sha256:local-image")
        args = calls[0]
        self.assertIn("--pull=never", args)
        self.assertEqual(args[args.index("--network") + 1], "none")
        self.assertEqual(args[args.index("--gpus") + 1], '"device=' + GPU + '",capabilities=utility')
        self.assertNotIn("--mount", args)
        self.assertNotIn("-v", args)
        self.assertNotIn("--privileged", args)
        self.assertEqual(args[args.index("--user") + 1], "1000:1000")

    def test_all_and_multiple_uuid_probes_are_validated(self):
        for selected in ("all", GPU + "," + OTHER):
            scope, calls = helpers(selected, SimpleNamespace(returncode=0, stdout=GPU + "\n" + OTHER + "\n", stderr=""))
            spec = dict(Env=[], HostConfig={})
            scope["add_gpu_observation"](spec)
            scope["probe_gpu_observation"](spec, "local-image")
            scope["probe_gpu_observation"](spec, "local-image", container="candidate")
            self.assertEqual(calls[-1][:3], ("exec", "candidate", "nvidia-smi"))
            self.assertNotIn("compute", spec["HostConfig"]["DeviceRequests"][0]["Capabilities"][0])

    def test_invalid_selection_and_compute_replacement_are_rejected(self):
        for selected in ("1", "device=" + GPU, GPU + ",", "unrecognized"):
            scope, calls = helpers(selected)
            with self.subTest(selected=selected), self.assertRaises(RuntimeError):
                scope["add_gpu_observation"](dict(HostConfig={}))
            self.assertFalse(calls)
        scope, calls = helpers(GPU)
        spec = dict(HostConfig=dict(DeviceRequests=[dict(Driver="nvidia", Capabilities=[["gpu", "compute"]])]))
        with self.assertRaisesRegex(RuntimeError, "cannot be replaced"):
            scope["add_gpu_observation"](spec)
        self.assertFalse(calls)

    def test_failed_or_wrong_device_probe_does_not_claim_success(self):
        for reply in (SimpleNamespace(returncode=1, stdout="", stderr="no NVIDIA driver"),
                      SimpleNamespace(returncode=0, stdout=OTHER + "\n", stderr=""),
                      SimpleNamespace(returncode=0, stdout="unexpected output", stderr="")):
            scope, _ = helpers(GPU, reply)
            with self.subTest(reply=reply), self.assertRaisesRegex(RuntimeError, "could not be verified"):
                scope["probe_gpu_observation"](dict(HostConfig={}), "local-image")


class ConfigureLifecycleTests(unittest.TestCase):
    def exercise(self, directory, fail_probe=False):
        root = Path(directory).resolve()
        data = root / "data"
        data.mkdir()
        database = data / "platform.sqlite3"
        database.write_bytes(b"same-image state fixture")
        socket = root / "daemon.sock"
        socket.touch()
        image_id = "sha256:" + "1" * 64
        mount = dict(Type="bind", Name=None, Source=str(data), Destination="/data", RW=True, Propagation="rprivate")
        old = dict(Id="a" * 64, Name="/platform", Image=image_id,
            Config=dict(Image="platform:current", User="1000:1000", Env=["KEPT=value"],
                Entrypoint=["python", "-B", "-m", "seampact"], Cmd=["serve", "--home", "/data"], Labels={}),
            HostConfig=dict(Mounts=[dict(Type="bind", Source=str(data), Target="/data")],
                NetworkMode="research-net", GroupAdd=[991]), Mounts=[mount],
            State=dict(Running=True, Status="running"),
            NetworkSettings=dict(Networks={"research-net": dict(Aliases=["platform"])}))
        containers = {old["Id"]: old}
        cfg = dict(schema="seampact-deploy/1", id="fixture-owner", endpoint="unix://" + str(socket),
                   container="platform", version="0.15.9", current_image="platform:current",
                   code_dir=str(root / "code"), data_mount=mount, tools=True, images=[])
        config_path = root / "deployment.json"
        config_path.write_text(json.dumps(cfg), encoding="utf-8")
        options = {name: None for name in ("repo", "code_dir", "extras", "base_image", "pip_index_url", "pip_extra_index_url",
            "torch_version", "torch_index_url", "dependency_lock", "docker_cli_image", "port", "bind", "data_dir",
            "harness_dir", "network", "container", "tools")}
        options.update(action="configure", ready_timeout=2, local=False, refresh_dependencies=False,
                       reuse_data=False, adopt_compose=False, gpu_observe=GPU)
        args = SimpleNamespace(**options)
        events = []

        def api(method, path, body=None, **kwargs):
            if method == "GET" and path == "/version":
                return {"ApiVersion": "1.54"}
            if method == "GET" and path == "/containers/json":
                return [info for info in containers.values() if info["State"]["Running"]]
            parts = urlsplit(path)
            if method == "POST" and parts.path == "/containers/create":
                identity = "b" * 64
                info = dict(Id=identity, Name="/" + parse_qs(parts.query)["name"][0],
                    Image=image_id, Config={key: copy.deepcopy(value) for key, value in body.items()
                                          if key not in {"HostConfig", "NetworkingConfig"}},
                    HostConfig=copy.deepcopy(body["HostConfig"]), Mounts=[copy.deepcopy(mount)],
                    State=dict(Running=False, Status="created"), NetworkSettings=copy.deepcopy(old["NetworkSettings"]))
                containers[identity] = info
                events.append("create")
                return {"Id": identity}
            identity = parts.path.split("/")[2]
            if method == "DELETE":
                events.append("remove-old")
                del containers[identity]
            elif parts.path.endswith("/stop"):
                events.append("stop-old")
                containers[identity]["State"].update(Running=False, Status="exited")
            elif parts.path.endswith("/rename"):
                containers[identity]["Name"] = "/" + parse_qs(parts.query)["name"][0]
            elif parts.path.endswith("/start"):
                events.append("start-new")
                containers[identity]["State"].update(Running=True, Status="running")
            else:
                self.fail("Unexpected Docker API operation: " + method + " " + path)

        def docker(*command, **kwargs):
            stdout = ""
            if command[:2] == ("container", "inspect"):
                info = next((item for item in containers.values() if command[2] in (item["Id"], item["Name"].lstrip("/"))), None)
                return SimpleNamespace(returncode=0 if info else 1, stdout=json.dumps([info]) if info else "", stderr="")
            if command[:2] == ("image", "inspect"):
                stdout = json.dumps([dict(Id=image_id, Config=dict(Env=[], Labels={"org.opencontainers.image.version": "0.15.9"}))])
            elif command[0] == "run":
                events.append("gpu-probe-before")
                return SimpleNamespace(returncode=1 if fail_probe else 0, stdout=GPU + "\n", stderr="")
            elif command[0] == "exec":
                if command[2] == "nvidia-smi":
                    events.append("gpu-probe-after")
                    stdout = GPU + "\n"
                else:
                    stdout = "29.5.3"
            elif command[0] == "cp":
                self.assertFalse(old["State"]["Running"])
                events.append("backup")
                shutil.copyfile(database, Path(command[2]) / "platform.sqlite3")
            else:
                self.fail("Unexpected Docker CLI operation: " + str(command))
            return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

        def fail(message):
            raise RuntimeError(message)

        def health(container, path):
            if path == "/health":
                return dict(status="ok", version="0.15.9")
            return dict(tasks=[], analyses=[], runtime_groups=[], deployments=[], scheduler={}, resources={})

        local_os = SimpleNamespace(**{name: getattr(os, name) for name in dir(os)})
        local_os.fchmod = lambda *args: None
        local_os.umask = lambda *args: 0
        opened = []

        def tracked_open(*args, **kwargs):
            stream = open(*args, **kwargs)
            opened.append(stream)
            return stream

        scope = dict(ast=ast, copy=copy, datetime=datetime, json=json, os=local_os, Path=Path, re=re,
            shutil=SimpleNamespace(which=lambda _: "docker"), sys=sys, time=time, uuid=uuid, quote=quote,
            ROOT=root, CONFIG_PATH=config_path, DOCKER_ENV={}, ARGS=args,
            fcntl=SimpleNamespace(LOCK_EX=1, LOCK_NB=2, flock=lambda *args: None),
            stat=SimpleNamespace(S_ISSOCK=lambda mode: True), open=tracked_open)
        exec(compile(ast.Module(body=FUNCTIONS, type_ignores=[]), str(SOURCE), "exec"), scope)
        scope.update(api=api, docker=docker, fail=fail, container_http=health,
            container_preflight=lambda _: dict(http_status=200, body=dict(operation="update", status="READY", items=[])),
            cleanup=lambda: None)
        error = None
        try:
            scope["main"]()
        except RuntimeError as exc:
            error = exc
        finally:
            for stream in opened:
                stream.close()
        self.assertEqual(database.read_bytes(), b"same-image state fixture")
        return events, containers, json.loads(config_path.read_text(encoding="utf-8")), error

    def test_same_image_configure_preserves_state_and_finishes_only_after_gpu_probe(self):
        with tempfile.TemporaryDirectory() as directory:
            events, containers, cfg, error = self.exercise(directory)
            self.assertIsNone(error)
            self.assertEqual(events, ["gpu-probe-before", "create", "stop-old", "backup", "start-new", "gpu-probe-after", "remove-old"])
            self.assertNotIn("pending", cfg)
            self.assertEqual(cfg["version"], "0.15.9")
            self.assertEqual(cfg["current_image"], "sha256:" + "1" * 64)
            self.assertEqual(cfg["images"], [])
            self.assertEqual(cfg["gpu_observe"], GPU)
            candidate = next(iter(containers.values()))
            self.assertEqual(candidate["Image"], cfg["current_image"])
            self.assertIn("KEPT=value", candidate["Config"]["Env"])
            self.assertEqual(candidate["HostConfig"]["DeviceRequests"][0]["Capabilities"], [["gpu", "utility"]])
            self.assertTrue((Path(cfg["latest_backup"]) / "data/platform.sqlite3").is_file())

    def test_failed_nvidia_probe_keeps_running_platform_and_no_pending_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            events, containers, cfg, error = self.exercise(directory, fail_probe=True)
            self.assertRegex(str(error), "could not be verified")
            self.assertEqual(events, ["gpu-probe-before"])
            self.assertTrue(next(iter(containers.values()))["State"]["Running"])
            self.assertNotIn("pending", cfg)


if __name__ == "__main__":
    unittest.main()
