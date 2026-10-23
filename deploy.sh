#!/usr/bin/env bash
# Standalone Linux entry point: this file can be uploaded before cloning Seampact.
set -euo pipefail
command -v python3 >/dev/null 2>&1 || { echo 'Python 3 is required on the deployment host.' >&2; exit 1; }
exec python3 - "$0" "$@" <<'SEAMPACT_DEPLOY_PY'
"""Small, private deployment record; Docker retains all platform state mounts."""
import argparse
import copy
import datetime
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import socket
import stat
import subprocess
import sys
import time
from urllib.parse import quote
import uuid


SCRIPT = Path(sys.argv.pop(1)).resolve()
PARSER = argparse.ArgumentParser(
    prog="bash deploy.sh",
    description="Install/update the Linux Docker platform, preserving /data and other mounts.",
    epilog="Examples: bash deploy.sh install --repo <GIT_URL>; bash deploy.sh update; "
           "bash deploy.sh update --container OLD_NAME --adopt-compose")
PARSER.add_argument("action", choices=("install", "update", "status", "cleanup"))
PARSER.add_argument("--root", default="~/seampact-workspace", help="private workspace (default: ~/seampact-workspace)")
PARSER.add_argument("--repo", help="Git URL, needed only when the code clone does not exist")
PARSER.add_argument("--code-dir", help="existing Git clone; default: <root>/Seampact")
PARSER.add_argument("--container", help="existing container to adopt, or fresh container name")
PARSER.add_argument("--adopt-compose", action="store_true", help="explicitly transfer a Compose container to this script")
PARSER.add_argument("--local", action="store_true", help="use the current clean checkout without git pull")
PARSER.add_argument("--port", type=int, help="fresh deployment: host port (default: 8880)")
PARSER.add_argument("--bind", help="fresh deployment: published address (default: 127.0.0.1)")
PARSER.add_argument("--data-dir", help="fresh deployment: persistent /data directory")
PARSER.add_argument("--reuse-data", action="store_true", help="install only: explicitly reuse and back up an existing --data-dir after the old container was removed")
PARSER.add_argument("--harness-dir", help="fresh deployment: persistent /harnesses directory")
PARSER.add_argument("--network", help="fresh deployment: shared bridge network (default: seampact-runtime-<deployment-id>)")
TOOLS = PARSER.add_mutually_exclusive_group()
TOOLS.add_argument("--with-tools", dest="tools", action="store_true", help="enable Docker CLI/buildx and selected local socket")
TOOLS.add_argument("--without-tools", dest="tools", action="store_false", help="fresh deployment: omit Docker CLI/socket")
PARSER.set_defaults(tools=None)
PARSER.add_argument("--extras", help="package extras to build, e.g. rl; persisted, default empty")
PARSER.add_argument("--base-image", help="platform Python 3.11 base image; persisted")
PARSER.add_argument("--docker-cli-image", help="official Docker CLI image; persisted, default docker:29-cli")
PARSER.add_argument("--ready-timeout", type=int, default=120, help="startup wait in seconds (default: 120)")
ARGS = PARSER.parse_args()
if sys.platform != "linux":
    PARSER.exit(1, "Deployment commands require a Linux host; --help is available on other systems.\n")
import fcntl
ROOT = Path(ARGS.root).expanduser().resolve()
CONFIG_PATH = ROOT / "deployment.json"
DOCKER_ENV = dict(os.environ)
for _key in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"):
    DOCKER_ENV.pop(_key, None)


def fail(message):
    raise RuntimeError(message)


def private_error(operation, stdout="", stderr=""):
    path = ROOT / "last-error.txt"
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(operation + "\n\nstdout:\n" + (stdout or "") + "\n\nstderr:\n" + (stderr or ""))
    return str(path)


def run(argv, *, cwd=None, visible=False, check=True, docker=False):
    result = subprocess.run(argv, cwd=cwd, env=DOCKER_ENV if docker else None,
                            text=True, stdout=None if visible else subprocess.PIPE,
                            stderr=None if visible else subprocess.PIPE)
    if check and result.returncode:
        # Do not print command arguments or captured output: they may contain credentials.
        operation = Path(argv[0]).name + " " + (argv[3] if docker else argv[1] if len(argv) > 1 else "")
        path = private_error(operation, result.stdout, result.stderr)
        fail("Command failed: " + operation + ". Private captured details: " + path)
    return result


def private_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_name(path.name + ".tmp")
    fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    temp.replace(path)


def save():
    private_json(CONFIG_PATH, CONFIG)


class UnixHTTP(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(str(SOCKET_PATH))


def api(method, path, body=None, allowed=(200, 201, 204), versioned=True):
    conn = UnixHTTP("localhost", timeout=150)
    prefix = "/v" + API_VERSION if versioned else ""
    payload = None if body is None else json.dumps(body).encode()
    try:
        conn.request(method, prefix + path, payload, {"Content-Type": "application/json"})
        response = conn.getresponse()
        content = response.read()
        if response.status not in allowed:
            operation = "Docker Engine " + method + " " + path.split("?")[0] + " (HTTP " + str(response.status) + ")"
            error_path = private_error(operation, stderr=content.decode("utf-8", errors="replace"))
            fail(operation + ". Private response details: " + error_path)
        return json.loads(content) if content else None
    finally:
        conn.close()


def docker(*args, **kwargs):
    return run(["docker", "--host", ENDPOINT, *args], docker=True, **kwargs)


def inspect_container(name, required=True):
    result = docker("container", "inspect", name, check=False)
    if result.returncode:
        if required:
            fail("Container does not exist: " + name + ". Use status and inspect the saved deployment record.")
        return None
    return json.loads(result.stdout)[0]


def image_info(image):
    return json.loads(docker("image", "inspect", image).stdout)[0]


def endpoint():
    if CONFIG.get("endpoint"):
        return CONFIG["endpoint"]
    context = os.environ.get("DOCKER_CONTEXT")
    if not context and os.environ.get("DOCKER_HOST"):
        return os.environ["DOCKER_HOST"]
    if not context:
        context = run(["docker", "context", "show"]).stdout.strip()
    return run(["docker", "context", "inspect", context, "--format", "{{.Endpoints.docker.Host}}"] ).stdout.strip()


def prepare_code():
    code = Path(ARGS.code_dir or CONFIG.get("code_dir") or ROOT / "Seampact").expanduser().resolve()
    if child_of(ROOT, code):
        fail("--root must be outside the Git checkout so deployment settings/backups remain private.")
    if not (code / ".git").exists():
        if code.exists() and any(code.iterdir()):
            fail("Code directory is nonempty and is not a Git clone; select --code-dir explicitly.")
        if not ARGS.repo:
            fail("The code clone is missing. Supply --repo <GIT_URL> once.")
        code.parent.mkdir(parents=True, exist_ok=True)
        print("Cloning the platform source (Git output kept private).", flush=True)
        run(["git", "clone", "--", ARGS.repo, str(code)])
    if run(["git", "status", "--porcelain", "--untracked-files=normal"], cwd=code).stdout:
        fail("The code checkout has local changes. Preserve/commit them before deployment; no reset or stash is performed.")
    CONFIG["code_dir"] = str(code)
    save()
    if os.environ.get("SEAMPACT_DEPLOY_REEXEC") != "1":
        pulled = ARGS.action == "update" and not ARGS.local
        if pulled:
            print("Pulling the current branch with --ff-only (Git output kept private).", flush=True)
            run(["git", "pull", "--ff-only"], cwd=code)
        latest = code / "deploy.sh"
        if not latest.is_file():
            fail("The updated checkout has no deploy.sh. Keep the old container running and check the selected branch.")
        if pulled or latest.resolve() != SCRIPT:
            env = dict(os.environ, SEAMPACT_DEPLOY_REEXEC="1")
            os.execvpe("bash", ["bash", str(latest), *sys.argv[1:]], env)
    return code


def mount_signature(row):
    return {key: row.get(key) for key in ("Type", "Name", "Source", "Destination", "RW", "Propagation")}


def child_of(path, parent):
    return path == parent or parent in path.parents


def check_state_path(path):
    path = Path(path).resolve()
    if child_of(ROOT, path):
        fail("--root (including deployment.json/backups) must be outside the platform data directory.")
    code = Path(ARGS.code_dir or CONFIG.get("code_dir") or ROOT / "Seampact").expanduser().resolve()
    if child_of(code, path):
        fail("The state directory cannot contain the Git code directory. Select separate persistent state/code paths.")


def check_unmounted_state(data):
    for row in api("GET", "/containers/json"):
        for mount in row.get("Mounts", []):
            source = mount.get("Source")
            if source and mount.get("RW", True) and (child_of(data, Path(source).resolve()) or child_of(Path(source).resolve(), data)):
                fail("Another running container can write the selected state directory. Stop that writer before continuing.")


def install_data_path():
    data = Path(ARGS.data_dir or ROOT / "data").expanduser().resolve()
    check_state_path(data)
    if ARGS.reuse_data:
        if not ARGS.data_dir or not data.is_dir() or not (data / "platform.sqlite3").is_file():
            fail("--reuse-data requires an explicit existing --data-dir containing platform.sqlite3; no empty replacement is created.")
    elif data.exists() and (not data.is_dir() or any(data.iterdir())):
        fail("Fresh install requires an empty data directory. If the old container is gone, use install --reuse-data --data-dir PATH; otherwise use update --container NAME.")
    check_unmounted_state(data)
    return data


def command_option(command, option, default=None):
    if option in command:
        index = command.index(option)
        return command[index + 1] if index + 1 < len(command) else None
    return next((item.split("=", 1)[1] for item in command if item.startswith(option + "=")), default)


def validate_old(old):
    config, host = old["Config"], old["HostConfig"]
    command = config.get("Cmd") or []
    if config.get("Entrypoint") != ["python", "-B", "-m", "seampact"] or not command or command[0] != "serve" or command_option(command, "--home") != "/data":
        fail("Adoption supports the standard python -B -m seampact entrypoint and serve --home /data. "
             "A custom launcher/home needs its original deployment command so the actual state is preserved.")
    legacy = command_option(command, "--legacy-config")
    if legacy and not any(row["Type"] in ("bind", "volume") and
                          (legacy == row["Destination"] or legacy.startswith(row["Destination"].rstrip("/") + "/"))
                          for row in old.get("Mounts", [])):
        fail("--legacy-config points outside persistent mounts. Preserve that private configuration before adoption.")
    labels = config.get("Labels") or {}
    if any(key.startswith("com.docker.compose.") for key in labels) and not ARGS.adopt_compose:
        fail("This container is managed by Compose. Continue with its original Compose command, or use --adopt-compose "
             "once to explicitly transfer future platform updates to deploy.sh (same mounts/network/options).")
    if host.get("AutoRemove"):
        fail("The old container uses --rm and would disappear when stopped. Recreate it without --rm before adoption.")
    if host.get("VolumesFrom") or host.get("Links") or any(str(host.get(key, "")).startswith("container:")
            for key in ("NetworkMode", "PidMode", "IpcMode")):
        fail("This container shares volumes/namespaces or links another container. Keep its original deployment command; "
             "these dependencies cannot be safely transferred automatically.")
    if host.get("NetworkMode") == "none":
        fail("A platform with network=none cannot complete the HTTP readiness check; use the original deployment command.")
    mounts = [row for row in old.get("Mounts", []) if row["Destination"] == "/data"]
    if len(mounts) != 1 or mounts[0]["Type"] not in ("bind", "volume") or not mounts[0].get("RW"):
        fail("Adoption requires one writable bind/volume mounted exactly at /data. Preserve the old container, "
             "copy its entire state out, and explicitly mount that state before adoption.")
    data = mounts[0]
    if any(row["Destination"].startswith("/data/") for row in old.get("Mounts", [])):
        fail("Nested mounts under /data need a separate backup procedure. Keep the original deployment command; no container was stopped.")
    check_state_path(data["Source"])
    if data["Type"] == "bind" and not Path(data["Source"]).is_dir():
        fail("The /data bind source must be an accessible directory on this host.")
    if CONFIG.get("data_mount") and mount_signature(data) != CONFIG["data_mount"]:
        fail("The /data mount differs from the saved deployment. Inspect deployment.json privately; no container was stopped.")
    # An image with VOLUME directives may have anonymous volumes absent from Binds/Mounts.
    # The resolved volume names below prevent replacement with new empty volumes.
    for row in old.get("Mounts", []):
        if row["Type"] not in ("bind", "volume", "tmpfs"):
            fail("Unsupported mount type " + row["Type"] + "; use the original deployment command.")
    for row in (old.get("NetworkSettings", {}).get("Networks") or {}).values():
        ipam = row.get("IPAMConfig") or {}
        if ipam.get("IPv4Address") or ipam.get("IPv6Address"):
            fail("The old container has a fixed network IP. Keep its original deployment command to handle address transfer explicitly.")
    for row in api("GET", "/containers/json"):
        if row["Id"] == old["Id"]:
            continue
        for other in row.get("Mounts", []):
            overlap = other.get("Source") and (child_of(Path(data["Source"]).resolve(), Path(other["Source"]).resolve()) or
                                                child_of(Path(other["Source"]).resolve(), Path(data["Source"]).resolve()))
            if other.get("RW", True) and (overlap or (data.get("Name") and other.get("Name") == data["Name"])):
                fail("Another running container uses the platform state mount. Stop that writer explicitly before updating.")
    return data


def inspect_environment(rows):
    return dict(item.split("=", 1) for item in rows or [] if "=" in item)


def replacement_spec(old, new_image):
    spec = copy.deepcopy(old["Config"])
    host = copy.deepcopy(old["HostConfig"])
    host["Mounts"] = host.get("Mounts") or []
    for port, bindings in (host.get("PortBindings") or {}).items():
        if any(not row.get("HostPort") for row in bindings or []):
            resolved = (old.get("NetworkSettings", {}).get("Ports") or {}).get(port)
            if not resolved:
                fail("A dynamically published port is unresolved on the stopped old container; use its original deployment command.")
            host["PortBindings"][port] = copy.deepcopy(resolved)
    old_image = image_info(old["Image"])["Config"]
    new_image_config = image_info(new_image)["Config"]
    old_defaults = inspect_environment(old_image.get("Env"))
    new_defaults = inspect_environment(new_image_config.get("Env"))
    overrides = {key: value for key, value in inspect_environment(spec.get("Env")).items()
                 if key not in old_defaults or old_defaults[key] != value}
    for key in ("SEAMPACT_REVISION", "SEAMPACT_EXTRAS"):
        overrides.pop(key, None)
    spec["Env"] = [key + "=" + value for key, value in dict(new_defaults, **overrides).items()]
    spec["Image"] = new_image
    old_labels = old_image.get("Labels") or {}
    labels = {**(new_image_config.get("Labels") or {}),
              **{key: value for key, value in (spec.get("Labels") or {}).items()
                 if key not in old_labels or old_labels[key] != value}}
    for key in ("org.opencontainers.image.version", "org.opencontainers.image.revision"):
        if key in (new_image_config.get("Labels") or {}):
            labels[key] = new_image_config["Labels"][key]
    spec["Labels"] = {key: value for key, value in labels.items()
                      if not (ARGS.adopt_compose and key.startswith("com.docker.compose."))}
    spec["Labels"]["io.seampact.deployment"] = CONFIG["id"]
    present = {row["Target"] for row in host.get("Mounts") or []}
    # Bind strings contain ':' separators only on this Linux/local-daemon entry point.
    present.update(row.split(":")[1] for row in host.get("Binds") or [] if ":" in row)
    present.update((host.get("Tmpfs") or {}).keys())
    for row in old.get("Mounts", []):
        if row["Destination"] in present:
            continue
        if row["Type"] == "tmpfs":
            fail("An unresolved tmpfs mount cannot be preserved; keep the original deployment command.")
        added = dict(Type=row["Type"], Source=row.get("Name") if row["Type"] == "volume" else row["Source"],
                     Target=row["Destination"], ReadOnly=not row["RW"])
        if row["Type"] == "bind" and row.get("Propagation"):
            added["BindOptions"] = {"Propagation": row["Propagation"]}
        host.setdefault("Mounts", []).append(added)
    endpoints = {}
    for name, row in (old.get("NetworkSettings", {}).get("Networks") or {}).items():
        endpoints[name] = {key: copy.deepcopy(row[key]) for key in ("IPAMConfig", "Links", "Aliases", "DriverOpts", "GwPriority", "MacAddress")
                           if row.get(key) is not None}
    spec["HostConfig"] = host
    if endpoints:
        spec["NetworkingConfig"] = {"EndpointsConfig": endpoints}
    return spec


def add_tools(spec):
    host = spec["HostConfig"]
    destinations = {row["Target"] for row in host.get("Mounts") or []}
    destinations.update(row.split(":")[1] for row in host.get("Binds") or [] if ":" in row)
    if "/var/run/docker.sock" in destinations:
        # Existing mounts are intentionally preserved; require them to refer to the selected daemon.
        matches = [row for row in (OLD or {}).get("Mounts", []) if row["Destination"] == "/var/run/docker.sock"]
        if matches and Path(matches[0]["Source"]).resolve() != SOCKET_PATH.resolve():
            fail("The old Docker socket mount targets another daemon. Keep its original deployment command or correct the mount explicitly.")
    else:
        host.setdefault("Mounts", []).append(dict(Type="bind", Source=str(SOCKET_PATH), Target="/var/run/docker.sock"))
    env = inspect_environment(spec.get("Env"))
    env.update(DOCKER_HOST="unix:///var/run/docker.sock", DOCKER_CONFIG="/data/docker-client")
    spec["Env"] = [key + "=" + value for key, value in env.items()]
    groups = host.get("GroupAdd") or []
    gid = str(SOCKET_PATH.stat().st_gid)
    if gid not in groups:
        groups.append(gid)
    host["GroupAdd"] = groups


def fresh_spec(image):
    data = install_data_path()
    harness = Path(ARGS.harness_dir or ROOT / "harnesses").expanduser().resolve()
    for directory in (data, harness):
        directory.mkdir(parents=True, exist_ok=True)
    port, bind = ARGS.port or 8880, ARGS.bind or "127.0.0.1"
    network = ARGS.network or "seampact-runtime-" + CONFIG["id"][:8]
    if not 1 <= port <= 65535:
        fail("--port must be between 1 and 65535.")
    result = docker("network", "inspect", network, check=False)
    if result.returncode:
        docker("network", "create", "--driver", "bridge", network)
    elif json.loads(result.stdout)[0].get("Driver") != "bridge":
        fail("Fresh deployment requires a user-defined bridge network.")
    rootless = any("rootless" in item for item in api("GET", "/info").get("SecurityOptions", []))
    user = "0:0" if rootless else str(os.getuid()) + ":" + str(os.getgid())
    defaults = image_info(image)["Config"]
    spec = {key: copy.deepcopy(defaults[key]) for key in ("Env", "Entrypoint", "WorkingDir", "ExposedPorts") if key in defaults}
    spec.update(Image=image, User=user, Labels={"io.seampact.deployment": CONFIG["id"]},
                Cmd=["serve", "--home", "/data", "--host", "0.0.0.0", "--port", "8880", "--public-port", str(port)],
                HostConfig=dict(Init=True, RestartPolicy={"Name": "unless-stopped"}, NetworkMode=network,
                                PortBindings={"8880/tcp": [{"HostIp": bind, "HostPort": str(port)}]},
                                Mounts=[dict(Type="bind", Source=str(data), Target="/data"),
                                        dict(Type="bind", Source=str(harness), Target="/harnesses")]),
                NetworkingConfig={"EndpointsConfig": {network: {"Aliases": [CONFIG["container"]]}}})
    CONFIG.update(data_dir=str(data), harness_dir=str(harness), runtime_network=network, port=port, bind=bind)
    return spec


def container_http(container, path):
    # Use the container's configured listener, so host-network and custom ports survive adoption.
    command = (OLD or {}).get("Config", {}).get("Cmd") or []
    port = command_option(command, "--port", "8880")
    code = "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:" + str(int(port)) + path + "',timeout=5).read().decode())"
    result = docker("exec", container, "python", "-c", code, check=False)
    if result.returncode:
        return None
    try:
        return json.loads(result.stdout)
    except ValueError:
        return None


def check_idle():
    if not OLD or not OLD["State"].get("Running"):
        return
    state = container_http(OLD["Id"], "/api/state")
    if not isinstance(state, dict):
        fail("Cannot read the running platform's /api/state. Stop its work and container explicitly, then rerun update.")
    if any(not isinstance(state.get(key), list) for key in ("tasks", "analyses", "runtime_groups", "deployments")) or not isinstance(state.get("scheduler"), dict):
        fail("The running platform has an unrecognized state schema. Stop its work and container explicitly before adoption.")
    busy = any(row.get("status") in ("RUNNING", "CANCELLING") for key in ("tasks", "analyses") for row in state.get(key, []))
    scheduler = state.get("scheduler") or {}
    busy = busy or bool(scheduler.get("active") or scheduler.get("waiting") or scheduler.get("used"))
    busy = busy or any(row.get("status") in ("QUEUED", "RUNNING") for row in (scheduler.get("research") or {}).get("runs", []))
    busy = busy or any((row.get("target_runtime") or {}).get("status") == "PREPARING" for row in state.get("harnesses", []))
    busy = busy or any((row.get(key) or {}).get("status") == "RUNNING" for row in state.get("deployments", []) for key in ("build", "publish"))
    busy = busy or bool((state.get("runtime_controller") or {}).get("locks"))
    held = any(instance.get("state") in ("UNDEPLOYED", "STARTING", "READY", "DRAINING", "UNKNOWN") or
               (instance.get("state") == "FAILED" and (instance.get("handle") or instance.get("endpoint")) and not instance.get("released"))
               for group in state.get("runtime_groups", []) for instance in group.get("instances", []))
    if busy or held:
        fail("The platform has active/queued work or retained runtime instances. Stop work and release workers in the page first; "
             "the script does not stop/delete workers or carry sessions across a platform restart.")


def cleanup():
    successful = [row for row in CONFIG.get("images", []) if row.get("status") == "ready"]
    keep = {tag for row in successful[-2:] for tag in row["tags"]}
    keep.update(filter(None, (CONFIG.get("current_image"), CONFIG.get("previous_image"))))
    used = {row["ImageID"] for row in api("GET", "/containers/json?all=1")}
    removed = 0
    for row in CONFIG.get("images", []):
        for tag in row["tags"]:
            if tag in keep:
                continue
            result = docker("image", "inspect", tag, check=False)
            if result.returncode:
                continue
            image = json.loads(result.stdout)[0]
            if image["Id"] in used or (image["Config"].get("Labels") or {}).get("io.seampact.deployment") != CONFIG["id"]:
                continue
            if docker("image", "rm", tag, check=False).returncode == 0:
                removed += 1
    print("Cleanup removed " + str(removed) + " older owned image tags; current/previous images and all used images are retained.")


def show_status():
    old = inspect_container(CONFIG.get("container", "seampact-platform"), required=False)
    print("Deployment record: " + str(CONFIG_PATH))
    print("Code: " + CONFIG.get("code_dir", "not configured"))
    print("Container: " + CONFIG.get("container", "not configured") + "; state: " + (old["State"]["Status"] if old else "missing"))
    print("Last ready version: " + CONFIG.get("version", "unknown"))
    print("Runtime network: " + str(CONFIG.get("runtime_network") or "preserved host/multiple networks; inspect locally"))
    print("Tools image: " + ("enabled" if CONFIG.get("tools") else "disabled"))
    print("Latest state backup: " + CONFIG.get("latest_backup", "none"))
    if CONFIG.get("pending"):
        print("Interrupted replacement; retained containers: " + CONFIG["pending"]["old_name"] + ", " + CONFIG["pending"]["candidate_name"])
    if old and old["State"].get("Running"):
        health = container_http(old["Id"], "/health")
        print("HTTP health: " + (str(health.get("status")) + "; version: " + str(health.get("version")) if isinstance(health, dict) else "unavailable"))


def main():
    global CONFIG, ENDPOINT, SOCKET_PATH, API_VERSION, OLD
    os.umask(0o077)
    if ARGS.ready_timeout <= 0:
        fail("--ready-timeout must be a positive number of seconds.")
    if ARGS.port is not None and not 1 <= ARGS.port <= 65535:
        fail("--port must be between 1 and 65535.")
    if ARGS.reuse_data and ARGS.action != "install":
        fail("--reuse-data is only for install after the old container has been removed. Existing containers use update.")
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = open(ROOT / "deployment.lock", "a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fail("Another deployment command is already running in this workspace.")
    CONFIG = json.loads(CONFIG_PATH.read_text(encoding="utf-8")) if CONFIG_PATH.exists() else {}
    if ARGS.action in ("status", "cleanup") and not CONFIG:
        fail("No deployment.json exists at --root. Select the workspace used for installation/adoption.")
    if CONFIG and CONFIG.get("schema") != "seampact-deploy/1":
        fail("Unsupported deployment record schema.")
    if not shutil.which("docker"):
        fail("Docker CLI is required on the deployment host.")
    ENDPOINT = endpoint()
    if not ENDPOINT.startswith("unix://"):
        fail("This entry point requires a local Linux Docker Unix socket; SSH/TCP contexts are not supported.")
    SOCKET_PATH = Path(ENDPOINT[7:])
    if not SOCKET_PATH.exists() or not stat.S_ISSOCK(SOCKET_PATH.stat().st_mode):
        fail("The selected Docker Unix socket is unavailable. Start/select the intended daemon first.")
    API_VERSION = api("GET", "/version", versioned=False)["ApiVersion"]
    OLD = None
    if ARGS.action == "status":
        OLD = inspect_container(CONFIG["container"], required=False)
        show_status()
        return
    if ARGS.action == "cleanup":
        cleanup()
        return
    if CONFIG.get("pending"):
        fail("A prior replacement is incomplete. Run status, inspect retained containers and the backup, then resolve it manually. "
             "No old container is automatically restarted against possibly migrated data.")
    if ARGS.action == "install" and CONFIG.get("current_image") and not ARGS.reuse_data:
        fail("This workspace is already deployed. Use update.")
    if CONFIG.get("container") and ARGS.container and CONFIG["container"] != ARGS.container:
        fail("--container differs from the saved deployment. Use the original workspace/container pair.")
    CONFIG.update(schema="seampact-deploy/1", id=CONFIG.get("id") or str(uuid.uuid4()), endpoint=ENDPOINT,
                  container=ARGS.container or CONFIG.get("container") or "seampact-platform")
    planned_code = Path(ARGS.code_dir or CONFIG.get("code_dir") or ROOT / "Seampact").expanduser().resolve()
    if child_of(ROOT, planned_code):
        fail("--root must be outside the Git checkout so deployment settings/backups remain private.")
    OLD = inspect_container(CONFIG["container"], required=ARGS.action == "update")
    if ARGS.action == "install" and OLD:
        fail("The requested container already exists. Adopt it with update --container NAME.")
    if not OLD:
        install_data_path()
    if OLD:
        if any(value is not None for value in (ARGS.port, ARGS.bind, ARGS.data_dir, ARGS.harness_dir, ARGS.network)):
            fail("Port/path/network flags are fresh-install options. Updates retain the existing container settings exactly.")
        if ARGS.tools is False:
            fail("--without-tools is a fresh-install option. Existing tools/socket mounts are preserved on update.")
        data = validate_old(OLD)
        CONFIG["data_mount"] = mount_signature(data)
        CONFIG["data_dir"] = data.get("Source") if data["Type"] == "bind" else None
        networks = OLD.get("NetworkSettings", {}).get("Networks") or {}
        CONFIG["runtime_network"] = next(iter(networks)) if len(networks) == 1 and OLD["HostConfig"].get("NetworkMode") not in ("host", "bridge", "default") else None
        image_env = inspect_environment(image_info(OLD["Image"])["Config"].get("Env"))
        CONFIG.setdefault("extras", image_env.get("SEAMPACT_EXTRAS", ""))
        has_tools = docker("exec", OLD["Id"], "docker", "--version", check=False).returncode == 0 if OLD["State"].get("Running") else any(row["Destination"] == "/var/run/docker.sock" for row in OLD.get("Mounts", []))
        CONFIG.setdefault("tools", has_tools)
    CONFIG["tools"] = ARGS.tools if ARGS.tools is not None else CONFIG.get("tools", True)
    CONFIG["extras"] = ARGS.extras if ARGS.extras is not None else CONFIG.get("extras", "")
    CONFIG["base_image"] = ARGS.base_image or CONFIG.get("base_image") or "python:3.11-slim"
    CONFIG["docker_cli_image"] = ARGS.docker_cli_image or CONFIG.get("docker_cli_image") or "docker:29-cli"
    save()
    code = prepare_code()
    text = (code / "pyproject.toml").read_text(encoding="utf-8")
    version = re.search(r'^version\s*=\s*"([^"\n]+)"', text, re.M).group(1)
    revision = run(["git", "rev-parse", "HEAD"], cwd=code).stdout.strip()
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    tag = version + "-" + revision[:12] + "-" + stamp + "-" + uuid.uuid4().hex[:4]
    core = "seampact-core-" + CONFIG["id"][:8] + ":" + tag
    runtime = "seampact-tools-" + CONFIG["id"][:8] + ":" + tag if CONFIG["tools"] else core
    build = dict(tags=list(dict.fromkeys((core, runtime))), runtime=runtime, version=version, status="building")
    CONFIG.setdefault("images", []).append(build)
    save()
    print("Building platform " + version + " before stopping the old container.", flush=True)
    docker("build", "--label", "io.seampact.deployment=" + CONFIG["id"], "--build-arg", "BASE_IMAGE=" + CONFIG["base_image"],
           "--build-arg", "SEAMPACT_VERSION=" + version, "--build-arg", "SEAMPACT_REVISION=" + revision,
           "--build-arg", "SEAMPACT_EXTRAS=" + CONFIG["extras"], "-t", core, "-f", str(code / "deploy/Dockerfile"), str(code), visible=True)
    if CONFIG["tools"]:
        docker("build", "--label", "io.seampact.deployment=" + CONFIG["id"], "--build-arg", "PLATFORM_IMAGE=" + core,
               "--build-arg", "DOCKER_CLI_IMAGE=" + CONFIG["docker_cli_image"], "-t", runtime,
               "-f", str(code / "deploy/Dockerfile.tools"), str(code), visible=True)
    spec = replacement_spec(OLD, runtime) if OLD else fresh_spec(runtime)
    if CONFIG["tools"] and (not OLD or ARGS.tools is True):
        add_tools(spec)
    check_idle()
    candidate = CONFIG["container"] + "-next-" + uuid.uuid4().hex[:8]
    # Creation validates Docker options while the old platform is still running.
    created = api("POST", "/containers/create?name=" + quote(candidate, safe=""), spec)
    candidate_id = created["Id"]
    backup_dir = (ROOT / "backups" / (stamp + "-" + uuid.uuid4().hex[:4])).resolve()
    try:
        if OLD:
            candidate_info = inspect_container(candidate_id)
            before = sorted((mount_signature(row) for row in OLD["Mounts"] if row["Type"] in ("bind", "volume")), key=lambda row: row["Destination"])
            after = sorted((mount_signature(row) for row in candidate_info["Mounts"] if row["Type"] in ("bind", "volume")), key=lambda row: row["Destination"])
            if ARGS.tools is True and not any(row["Destination"] == "/var/run/docker.sock" for row in before):
                after = [row for row in after if row["Destination"] != "/var/run/docker.sock"]
            if before != after:
                fail("The candidate's persistent mounts differ from the original. No old container was stopped; use the original deployment command.")
        backup_dir.mkdir(parents=True, mode=0o700)
        private_json(backup_dir / "container.json", OLD or spec)
        if OLD:
            # Remove only this newly-created, never-started candidate if the final idle check fails.
            check_idle()
        elif ARGS.reuse_data:
            install_data_path()
            print("Backing up the existing state directory before the new platform starts.", flush=True)
            (backup_dir / "data").mkdir(mode=0o700)
            docker("cp", candidate_id + ":/data/.", str(backup_dir / "data"))
            CONFIG["latest_backup"] = str(backup_dir)
            save()
    except Exception:
        api("DELETE", "/containers/" + candidate_id + "?v=0")
        raise
    if OLD:
        retained = CONFIG["container"] + "-previous-" + stamp
        CONFIG["pending"] = dict(old_id=OLD["Id"], old_name=CONFIG["container"], candidate_id=candidate_id, candidate_name=candidate)
        save()
        print("Stopping the platform and copying its entire /data into a private backup.", flush=True)
        api("POST", "/containers/" + OLD["Id"] + "/stop?t=60", allowed=(204, 304))
        (backup_dir / "data").mkdir(mode=0o700)
        # Docker copies the source modes but gives the backup to the invoking host user.
        # Original mount ownership stays untouched; original UID/GID remain in container.json.
        docker("cp", OLD["Id"] + ":/data/.", str(backup_dir / "data"))
        CONFIG["latest_backup"] = str(backup_dir)
        save()
        api("POST", "/containers/" + OLD["Id"] + "/rename?name=" + quote(retained, safe=""))
        CONFIG["pending"]["old_name"] = retained
        save()
    else:
        CONFIG["pending"] = dict(old_id=None, old_name="none", candidate_id=candidate_id, candidate_name=candidate)
        save()
    api("POST", "/containers/" + candidate_id + "/rename?name=" + quote(CONFIG["container"], safe=""))
    CONFIG["pending"]["candidate_name"] = CONFIG["container"]
    save()
    api("POST", "/containers/" + candidate_id + "/start")
    deadline = time.monotonic() + ARGS.ready_timeout
    health = None
    while time.monotonic() < deadline:
        current = inspect_container(candidate_id)
        if not current["State"].get("Running"):
            break
        health = container_http(candidate_id, "/health")
        if isinstance(health, dict) and health.get("status") == "ok" and health.get("version") == version:
            break
        time.sleep(2)
    else:
        health = None
    if not isinstance(health, dict) or health.get("status") != "ok" or health.get("version") != version:
        # A failed version may already have changed the shared database; never auto-rollback.
        api("POST", "/containers/" + candidate_id + "/stop?t=30", allowed=(204, 304))
        fail("The new platform did not report the expected /health version. It is stopped; the old container, old image, "
             "state mounts and backup remain. Inspect logs locally. Restore the stopped backup before any version rollback.")
    if CONFIG["tools"]:
        if docker("exec", candidate_id, "docker", "version", "--format", "{{.Server.Version}}", check=False).returncode or docker("exec", candidate_id, "docker", "buildx", "version", check=False).returncode:
            api("POST", "/containers/" + candidate_id + "/stop?t=30", allowed=(204, 304))
            fail("The platform starts, but its Docker socket/client/buildx check failed. The new container is stopped; "
                 "the old container and data backup remain for manual diagnosis.")
    if OLD:
        api("DELETE", "/containers/" + OLD["Id"] + "?v=0")
    ready = inspect_container(candidate_id)
    data = next(row for row in ready["Mounts"] if row["Destination"] == "/data")
    CONFIG.update(data_mount=mount_signature(data), version=version, current_image=runtime,
                  previous_image=OLD["Image"] if OLD else None)
    CONFIG.pop("pending", None)
    build["status"] = "ready"
    save()
    cleanup()
    print("Platform " + version + " is ready. Existing state, source/weight mounts and worker images are preserved.")
    print("After an update, restart/register workers in the page; regenerate runtime packages when their protocol changes.")
    show_status()


try:
    main()
except (RuntimeError, OSError, ValueError, KeyError, StopIteration) as error:
    # OSError paths are useful locally; captured inspect/env/response bodies are never emitted.
    print("Deployment stopped: " + str(error), file=sys.stderr)
    sys.exit(1)
SEAMPACT_DEPLOY_PY
