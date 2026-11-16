"""Run a Linux user experiment with its dedicated model endpoints."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
from urllib.request import urlopen


MODELS = "seampact-experiment-models.service"


def docker():
    return ["docker", "--host", "unix://" + os.environ["XDG_RUNTIME_DIR"] + "/docker.sock"]


def inspect(kind, name):
    """Return stable target, lifecycle state and identity of this activation."""
    if kind == "unit":
        result = subprocess.check_output(["systemctl", "--user", "show", "-p", "LoadState",
            "-p", "ActiveState", "-p", "InvocationID", "--", name], text=True)
        info = dict(line.split("=", 1) for line in result.splitlines())
        return name, info["ActiveState"] if info["LoadState"] == "loaded" else "missing", info.get("InvocationID", "")
    result = subprocess.run([*docker(), "container", "inspect", "--format",
        "{{.Id}} {{.State.Status}} {{.State.StartedAt}}", "--", name], text=True, capture_output=True)
    if result.returncode and "no such" in result.stderr.lower():
        return name, "missing", ""
    result.check_returncode()
    return result.stdout.strip().split()


def switch(kind, name, action):
    prefix = ["systemctl", "--user"] if kind == "unit" else docker()
    subprocess.run([*prefix, action, "--", name], check=True)


def save(path, record):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(record) + "\n")
    temporary.replace(path)


def wait_for_idle():
    url = os.environ.get("SEAMPACT_EXPERIMENT_PLATFORM_URL")
    if not url:
        return
    container = os.environ.get("SEAMPACT_EXPERIMENT_PLATFORM_CONTAINER")
    waiting = False
    while True:
        try:
            with urlopen(url.rstrip("/") + "/api/state", timeout=10) as response:
                state = json.load(response)
            active = {"RUNNING", "CANCELLING", "QUEUED", "STARTING"}
            jobs = any(item.get("status") in active for key in ("tasks", "analyses", "generations")
                       for item in state[key])
            execution = state["scheduler"]["execution"]
            if not jobs and not any(execution[key] for key in ("active_tasks", "queued_calls", "running_calls",
                                                               "elastic_actions_in_flight")):
                return
        except (OSError, ValueError, KeyError):
            if container and inspect("container", container)[1] in {"exited", "dead", "missing"}:
                return
        if not waiting:
            print("Waiting for the platform's in-flight work to settle.", flush=True)
            waiting = True
        time.sleep(5)


def release(path):
    if not path.is_file():
        return
    record = json.loads(path.read_text())
    if record["ready"] and record["owned"]:
        wait_for_idle()
    failures = []
    for entry in reversed(record["owned"].copy()):
        kind, name, stamp = entry
        try:
            _, status, current = inspect(kind, name)
            if status not in {"missing", "inactive", "failed", "exited", "dead", "created"}:
                if stamp and stamp != current:
                    print("Keeping a later activation:", name, flush=True)
                else:
                    switch(kind, name, "stop")
                    print("Released", kind, name, flush=True)
            record["owned"].remove(entry)
            save(path, record)
        except (OSError, subprocess.CalledProcessError) as error:
            failures.append(f"{name}: {error}")
    if failures:
        raise RuntimeError("\n".join(failures))
    path.unlink()


def acquire(path):
    release(path)  # A failed stop keeps only its outstanding entries.
    record = {"ready": False, "owned": []}
    for kind, setting in (("unit", "SEAMPACT_MODEL_UNITS"), ("container", "SEAMPACT_MODEL_CONTAINERS")):
        for name in shlex.split(os.environ.get(setting, "")):
            target, state, _ = inspect(kind, name)
            if state in {"active", "activating", "reloading", "running", "restarting"}:
                print("Using an already running", kind, name, flush=True)
                continue
            if state not in {"inactive", "failed", "exited", "created"}:
                raise RuntimeError(f"{kind} {name} cannot start from {state}")
            entry = [kind, target, ""]
            record["owned"].append(entry)
            save(path, record)  # ExecStopPost can recover even if startup is interrupted.
            switch(kind, target, "start")
            entry[2] = inspect(kind, target)[2]
            save(path, record)
    deadline = time.monotonic() + 300
    for socket in shlex.split(os.environ.get("SEAMPACT_MODEL_SOCKETS", "")):
        while subprocess.run(["curl", "--fail", "--silent", "--noproxy", "*", "--max-time", "3",
                              "--unix-socket", socket, "-o", "/dev/null", "http://localhost/health"]).returncode:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Model did not become ready at {socket}")
            time.sleep(2)
    record["ready"] = True
    save(path, record)


def main(argv):
    if len(argv) >= 3 and argv[0] == "run":
        args = ["systemd-run", "--user", "--unit=" + argv[1], "--property=Type=exec",
                "--property=Requires=" + MODELS, "--property=After=" + MODELS, "--property=UMask=0077",
                "--working-directory=" + os.getcwd(),
                "--setenv=DOCKER_HOST=unix://" + os.environ["XDG_RUNTIME_DIR"] + "/docker.sock", "--", *argv[2:]]
        os.execvp(args[0], args)
    if argv in (["acquire"], ["release"]):
        path = Path(os.environ["STATE_DIRECTORY"]) / "owned.json"
        (acquire if argv == ["acquire"] else release)(path)
        return
    raise SystemExit("Usage: server_experiment.py run UNIT COMMAND [ARG ...]")


if __name__ == "__main__":
    main(sys.argv[1:])
