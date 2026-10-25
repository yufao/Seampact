"""One-case recovery: remove only a recorded, never-started initial container.

Run on the user's Linux deployment host. Keeps state, backups and images. Does
not handle interrupted updates of an existing old container or start anything.
"""
import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def fail(message):
    raise SystemExit(message)


def validate(record, info, root):
    pending = record.get("pending") or {}
    if record.get("schema") != "seampact-deploy/1" or not pending or "old_id" not in pending or pending.get("old_id") is not None:
        fail("Not a recoverable initial-install record; existing-platform updates are excluded.")
    if pending.get("candidate_id") != info.get("Id") or info.get("Name", "").lstrip("/") != record.get("container"):
        fail("The inspected container does not match the saved deployment identity.")
    state = info.get("State") or {}
    if state.get("Status") != "created" or state.get("Running") or state.get("Pid", 0) != 0:
        fail("The container must still be in Created state with no running process.")
    if not str(state.get("StartedAt", "")).startswith("0001-") or "setgroups" not in state.get("Error", ""):
        fail("This recovery is limited to the recorded setgroups failure before first startup.")
    mounts = [m for m in info.get("Mounts", []) if m.get("Destination") == "/data"]
    data = Path(record.get("data_dir") or "").expanduser().resolve()
    if len(mounts) != 1 or mounts[0].get("Type") != "bind" or not mounts[0].get("RW"):
        fail("Expected the original writable bind mount at /data.")
    if Path(mounts[0]["Source"]).resolve() != data or not (data/"platform.sqlite3").is_file():
        fail("Original data identity or database is unavailable.")
    saved = record.get("latest_backup")
    if not saved:
        fail("No completed state backup is recorded.")
    backup = Path(saved).resolve()
    if not backup.is_relative_to(root/"backups") or not (backup/"data/platform.sqlite3").is_file():
        fail("The completed deployment backup is unavailable inside this workspace.")
    return backup


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="Remove only a recorded never-started initial-install container after its setgroups failure")
    parser.add_argument("--root", default="~/seampact-workspace")
    args = parser.parse_args()
    if sys.platform != "linux":
        fail("Run this recovery on the Linux deployment host.")
    root = Path(args.root).expanduser().resolve()
    import fcntl
    lock = (root/"deployment.lock").open("a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fail("Another deployment command is running; nothing was changed.")
    path = root/"deployment.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    endpoint = record.get("endpoint", "")
    if not endpoint.startswith("unix://"):
        fail("A saved local Unix Docker endpoint is required.")
    pending = record.get("pending") or {}
    identity = pending.get("candidate_id")
    if not identity:
        fail("No pending container was recorded; nothing was removed.")
    command = ["docker", "--host", endpoint]
    # Match deploy.sh's selected daemon, regardless of shell context variables.
    env = dict(os.environ)
    for key in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"):
        env.pop(key, None)
    inspected = subprocess.run(command+["container", "inspect", identity], capture_output=True, text=True, env=env)
    if inspected.returncode:
        fail("Cannot inspect the recorded pending container; nothing was changed.")
    info = json.loads(inspected.stdout)[0]
    backup = validate(record, info, root)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    copy = backup/("deployment-before-group-fix-"+stamp+".json")
    shutil.copy2(path, copy)
    # No -f and no -v: Docker refuses a running container and retains its volumes.
    subprocess.run(command+["container", "rm", identity], check=True, env=env)
    current = json.loads(path.read_text(encoding="utf-8"))
    if current != record:
        fail("Deployment record changed meanwhile; the saved record was not overwritten.")
    current.pop("pending")
    temporary = root/"deployment-group-fix.tmp"
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(current, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    temporary.replace(path)
    print("Removed only the recorded never-started container. State, backup, images and deployment identity were retained.")
    print("Now run the corrected deploy.sh install --reuse-data with the original data directory.")


if __name__ == "__main__":
    main()
