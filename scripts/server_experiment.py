"""Run a Linux user experiment with its dedicated model endpoints."""
import json
import os
from pathlib import Path
import shlex
import sqlite3
import subprocess
import sys
import time
from urllib.request import Request, urlopen


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


def wait_for_idle(url=None):
    url = url or os.environ.get("SEAMPACT_EXPERIMENT_PLATFORM_URL")
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
                return execution
        except (OSError, ValueError, KeyError):
            if container and inspect("container", container)[1] in {"exited", "dead", "missing"}:
                return
        if not waiting:
            print("Waiting for the platform's in-flight work to settle.", flush=True)
            waiting = True
        time.sleep(5)


def release_workers(output, database, url):
    """End-of-run fallback for containers identified by this pilot's immutable task and worker IDs."""
    output = Path(output)
    if not output.is_dir():
        return
    tasks, sessions = set(), set()
    for path in output.glob("*-start.json"):
        pilot = json.loads(path.read_bytes())
        tasks.update(pilot["task_ids"])
        sessions.add(pilot["workload"]["session"])
    if not tasks:
        return
    ready = wait_for_idle(url)
    with sqlite3.connect(Path(database).resolve().as_uri() + "?mode=ro", uri=True) as db:
        rows = db.execute("SELECT data FROM documents WHERE kind='runtime_instance' AND "
            "json_extract(data,'$.run_id') IN (" + ",".join("?" for _ in tasks) + ")", list(tasks))
        owned = {record["id"]: record for record in (json.loads(row[0]) for row in rows)}
    if not owned:
        return
    listing = subprocess.check_output([*docker(), "ps", "-aq", "--no-trunc", "--filter", "label=seampact.instance"], text=True)
    for container in listing.splitlines():
        inspected = subprocess.run([*docker(), "inspect", "--format",
            '{{index .Config.Labels "seampact.instance"}} {{index .Config.Labels "seampact.group"}} {{.State.Status}}',
            container], capture_output=True, text=True)
        if inspected.returncode and "no such" in inspected.stderr.lower():
            continue
        inspected.check_returncode()
        instance, group, status = inspected.stdout.strip().split()
        row = owned.get(instance)
        if not row or row["group_id"] != group:
            continue
        handle = row.get("handle") or {}
        if handle.get("container_id") and handle["container_id"] != container:
            continue
        if status in {"created", "exited", "dead"}:
            subprocess.run([*docker(), "rm", "--", container], check=True)
        elif ready and ready.get("session") in sessions:
            req = Request(url.rstrip("/") + "/api/deployments/drain-stop",
                data=json.dumps(dict(group_id=group, instance_id=instance)).encode(),
                headers={"Content-Type": "application/json"})
            with urlopen(req, timeout=180) as response:
                json.load(response)
            _, after, _ = inspect("container", container)
            if after in {"created", "exited", "dead"}:
                subprocess.run([*docker(), "rm", "--", container], check=True)
            elif after != "missing":
                raise RuntimeError(f"Worker {instance} still holds its container ({after})")
        elif ready is None and handle.get("container_id") == container and os.environ.get("SEAMPACT_EXPERIMENT_PLATFORM_CONTAINER"):
            switch("container", container, "stop")
            if inspect("container", container)[1] != "missing":
                subprocess.run([*docker(), "rm", "--", container], check=True)
        else:
            print("Keeping a live worker outside the settled scheduler session:", instance, flush=True)


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
    if len(argv) == 4 and argv[0] == "release-workers":
        return release_workers(*argv[1:])
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
