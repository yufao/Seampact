"""Run explicit SeamPact containers and record scoped, credential-free evidence.

Images must already be built. This driver never builds, pulls, prunes, accesses
other projects, or calls a model provider. Startup login tokens remain in memory.
"""
import argparse
import http.cookiejar
import json
import secrets
import subprocess
import time
import tomllib
from pathlib import Path
EXPECTED_VERSION = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
from urllib.error import HTTPError
from urllib.request import Request, build_opener, HTTPCookieProcessor, urlopen


def verify(compose, env_file, output, docker_host=None, environment="unspecified"):
    docker = ["docker"] + (["--host", docker_host] if docker_host else [])
    base = docker + ["compose", "--env-file", str(Path(env_file).resolve()),
                     "--file", str(Path(compose).resolve())]
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    expected_project = None
    checks = {}
    receipts = []
    report = dict(schema="docker-verification/1", status="FAIL", checks=checks,
                  cases=[], containers=receipts, production_admitted=False,
                  remote_server="NOT_RUN", live_model_quality="NOT_RUN",
                  environment=environment,
                  candidate_origin="manually_selected_mechanism_probe")

    def command(arguments, timeout=180, log_file=None):
        result = subprocess.run(arguments, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=timeout)
        if log_file:
            log_file.write_text(result.stdout + result.stderr, encoding="utf-8")
        if result.returncode:
            # Probe output contains no provider credentials or startup token.
            # Control output is deliberately never copied to a report or log.
            raise RuntimeError("container command failed: " + arguments[-1])
        return result.stdout

    def inspect(name):
        info = json.loads(command(docker + ["inspect", name]))[0]
        labels = info["Config"].get("Labels") or {}
        if not expected_project or labels.get("com.docker.compose.project") != expected_project:
            raise RuntimeError("refusing to operate a foreign container")
        return info

    def receipt(info, service):
        inputs = [m for m in info["Mounts"] if m["Destination"] == "/input"]
        reports = [m for m in info["Mounts"] if m["Destination"] == "/reports"]
        return dict(service=service, image_id=info["Image"],
                    user=info["Config"]["User"],
                    network_mode=info["HostConfig"]["NetworkMode"],
                    read_only=info["HostConfig"]["ReadonlyRootfs"],
                    cap_drop=info["HostConfig"]["CapDrop"],
                    input_read_only=len(inputs) == 1 and not inputs[0]["RW"],
                    evidence_read_only=len(reports) == 1 and not reports[0]["RW"],
                    exit_code=info["State"]["ExitCode"])

    def login(port, container, previous_token=None):
        token = None
        for _ in range(60):
            # Reading only this owned container; raw logs are never persisted.
            logs = command(docker + ["logs", container])
            import re
            tokens = re.findall(r"\?access=([A-Za-z0-9_-]+)", logs)
            if tokens and tokens[-1] != previous_token:
                token = tokens[-1]
                try:
                    with urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as response:
                        if response.status == 200:
                            break
                except OSError:
                    pass
            time.sleep(.2)
        if not token or token == previous_token:
            raise RuntimeError("control did not provide a startup session")
        opener = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
        with opener.open(f"http://127.0.0.1:{port}/?access=" + token, timeout=5) as response:
            if "access=" in response.geturl():
                raise RuntimeError("login token was not removed from browser URL")
        with opener.open(f"http://127.0.0.1:{port}/api/state", timeout=5) as response:
            if response.status != 200:
                raise RuntimeError("startup session was not established")
        return opener, token

    try:
        config = json.loads(command(base + ["--profile", "checks", "--profile", "probes",
                                           "--profile", "sdk-workflow", "config", "--format", "json"]))
        if config["name"] not in {"seampact", "seampact-demo"}:
            raise ValueError("independent seampact Compose project required")
        expected_project = config["name"]
        services = config["services"]
        def host_path(value):
            value = value.replace("\\", "/").rstrip("/")
            for prefix in ("/run/desktop/mnt/host/", "/host_mnt/"):
                if value.startswith(prefix):
                    remainder = value[len(prefix):]
                    if len(remainder) > 1 and remainder[1] == "/":
                        value = remainder[0] + ":" + remainder[1:]
            return value.casefold() if len(value) > 1 and value[1] == ":" else value

        # Refuse to recreate a same-named project from another workspace.
        # Ownership is checked before any mutation, including disposable jobs.
        previous = command(docker + ["ps", "--all", "--quiet", "--filter",
                                     "label=com.docker.compose.project=" + expected_project]).split()
        expected_mounts = {v["target"]: host_path(v["source"])
                           for v in services["control"]["volumes"] if v["type"] == "bind"}
        for container_id in previous:
            old = inspect(container_id)
            labels = old["Config"].get("Labels") or {}
            if host_path(labels.get("com.docker.compose.project.config_files", "")) != host_path(str(Path(compose).resolve())):
                raise RuntimeError("seampact project already belongs to another Compose workspace")
            if labels.get("com.docker.compose.service") == "control":
                mounted = {m["Destination"]: host_path(m["Source"]) for m in old["Mounts"] if m["Type"] == "bind"}
                if any(mounted.get(target) != source for target, source in expected_mounts.items()):
                    raise RuntimeError("existing control belongs to another data workspace")
                if old["State"]["Running"]:
                    public_port = int(services["control"]["ports"][0]["published"])
                    existing_client, _ = login(public_port, container_id)
                    with existing_client.open(f"http://127.0.0.1:{public_port}/api/state", timeout=5) as response:
                        existing_state = json.load(response)
                    if any(job["status"] in {"RUNNING", "CANCELLING"} for job in existing_state["analyses"]) or any(
                            demo["status"] == "RUNNING" for demo in existing_state["demos"]) or existing_state["runtime"]["used"]:
                        raise RuntimeError("refusing maintenance while control has active or unresolved work")
        for service in ("control", "core-check", "probe-mmct", "probe-crag", "probe-crag-sdk"):
            if service not in services:
                raise ValueError("missing deployment service")
            command(docker + ["image", "inspect", services[service]["image"]])
        report["images_available"] = "PASS"  # Availability does not prove a fresh build.
        for service, check in (("core-check", "core_checks"), ("probe-crag", "crag_fragment"),
                               ("probe-mmct", "mmct_sdk"), ("probe-crag-sdk", "crag_sdk")):
            name = "seampact-verify-" + service + "-" + secrets.token_hex(4)
            try:
                command(base + ["run", "--pull", "never", "--no-deps", "--name", name, service], timeout=240,
                        log_file=output / (service + ".log"))
                info = inspect(name)
                item = receipt(info, service)
                receipts.append(item)
                if item["exit_code"] != 0 or item["network_mode"] != "none" or not item["read_only"]:
                    raise RuntimeError("probe isolation or exit status failed")
                checks[check] = "PASS"
            finally:
                # Names were freshly generated by this driver; verify ownership
                # before removing just this disposable check container.
                try:
                    inspect(name)
                except (RuntimeError, ValueError):
                    pass
                else:
                    if inspect(name)["State"]["Running"]:
                        command(docker + ["stop", "--time", "10", name])
                    command(docker + ["rm", name])
        checks["network_isolation"] = "PASS"
        control = services["control"]
        port = int(control["ports"][0]["published"])
        if control["ports"][0].get("host_ip") != "127.0.0.1":
            raise ValueError("control must publish only host loopback")
        command(base + ["up", "--detach", "--no-build", "--no-deps", "--pull", "never", "control"])
        container = command(base + ["ps", "--quiet", "control"]).strip()
        info = inspect(container)
        item = receipt(info, "control")
        receipts.append(item)
        if not item["read_only"] or not item["input_read_only"] or not item["evidence_read_only"] or item["user"].split(":")[0] == "0":
            raise RuntimeError("control isolation failed")
        checks["control_image"] = checks["read_only_sources"] = "PASS"
        opener, token = login(port, container)
        root = f"http://127.0.0.1:{port}"

        def request(path, body=None, client=None):
            data = json.dumps(body).encode() if body is not None else None
            req = Request(root + path, data=data,
                          headers={"Content-Type": "application/json"} if data else {})
            with (client or opener).open(req, timeout=5) as response:
                return json.load(response)

        health = request("/health")
        if not health.get("session_required"):
            raise RuntimeError("container session protection missing")
        try:
            with urlopen(root + "/api/state", timeout=5):
                raise RuntimeError("unauthenticated private state was exposed")
        except HTTPError as error:
            if error.code != 403:
                raise
        before = request("/api/state")
        if len(before["sources"]) != 4 or before["version"] != EXPECTED_VERSION:
            raise RuntimeError("unexpected control version or source catalog")
        checks["health"] = "PASS"
        frozen = request("/api/freeze", {"source_id": before["sources"][0]["id"],
                                        "observation": "Docker persistence check; no model inference"})
        demonstration = request("/api/demo", {})
        for _ in range(60):
            state = request("/api/state")
            completed = next((d for d in state["demos"] if d["id"] == demonstration["id"]), None)
            if completed and completed["status"] != "RUNNING":
                break
            time.sleep(.1)
        if not completed or completed["status"] != "PASS":
            raise RuntimeError("controlled local mechanism check failed")
        command(base + ["restart", "control"])
        old_client = opener
        opener, token = login(port, container, previous_token=token)
        try:
            request("/api/state", client=old_client)
            raise RuntimeError("old startup session survived restart")
        except HTTPError as error:
            if error.code != 403:
                raise
        after = request("/api/state")
        if not any(r["id"] == frozen["id"] for r in after["requests"]) or not any(
                d["id"] == demonstration["id"] and d["status"] == "PASS" for d in after["demos"]):
            raise RuntimeError("state did not survive control restart")
        checks["persistence"] = "PASS"
        report.update(status="PASS", version=after["version"], sources=4,
                      mechanism_evidence=after.get("evidence", {}).get("summary"),
                      session_rotation="PASS", controlled_demo="PASS",
                      remote_server="PASS" if environment == "remote-linux" else "NOT_RUN",
                      scope=environment + " containers; original algorithms with controlled endpoints")
    except Exception as error:
        report.update(error=type(error).__name__ + ": " + str(error))
    (output / "docker-result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("compose")
    parser.add_argument("env_file")
    parser.add_argument("output")
    parser.add_argument("--docker-host")
    parser.add_argument("--environment", choices=("unspecified", "local-docker-desktop", "remote-linux"), default="unspecified")
    args = parser.parse_args()
    result = verify(args.compose, args.env_file, args.output, args.docker_host, args.environment)
    print(json.dumps(dict(status=result["status"], checks=result["checks"], error=result.get("error"))))
    raise SystemExit(0 if result["status"] == "PASS" else 1)
