"""Create a new, private Docker workspace; never overwrite a prior deployment."""
import argparse
import json
import os
import sys
from pathlib import Path

# Explicit trusted sibling path also works with the documented Python -I.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from container_entry import release_sources


def prepare(release, workspace, port=8875):
    release = Path(release).resolve()
    workspace = Path(workspace).resolve()
    if not 1024 <= port <= 65535:
        raise ValueError("choose an unprivileged TCP port")
    if workspace.is_relative_to(release) or release.is_relative_to(workspace):
        raise ValueError("writable workspace must be outside immutable release")
    sources = release_sources(release)
    values = {
        "SEAMPACT_RELEASE_ROOT": release.as_posix(),
        "SEAMPACT_DATA_DIR": (workspace / "state").as_posix(),
        "SEAMPACT_REPORT_DIR": (workspace / "reports").as_posix(),
        "SEAMPACT_SECRET_DIR": (workspace / "secrets").as_posix(),
        "SEAMPACT_UID": str(getattr(os, "getuid", lambda: 10001)()),
        "SEAMPACT_GID": str(getattr(os, "getgid", lambda: 10001)()),
        "SEAMPACT_PORT": str(port),
    }
    if any("'" in value or "\n" in value or "\r" in value for value in values.values()):
        raise ValueError("workspace paths cannot contain single quotes or line breaks")
    # The known parent is required; this cannot recursively create unexpected
    # server directories. mkdir(exist_ok=False) protects prior state.
    os.umask(0o077)
    workspace.mkdir(mode=0o700, exist_ok=False)
    for directory in ("state", "reports", "secrets"):
        (workspace / directory).mkdir(mode=0o700)
    env_file = workspace / "compose.env"
    env_file.write_text("".join(name + "='" + value + "'\n" for name, value in values.items()), encoding="utf-8")
    return dict(workspace=str(workspace), compose_environment=str(env_file),
                sources=len(sources), port=port, api_keys="NOT_CONFIGURED")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("release_root")
    parser.add_argument("workspace")
    parser.add_argument("--port", type=int, default=8875)
    args = parser.parse_args()
    print(json.dumps(prepare(args.release_root, args.workspace, args.port), ensure_ascii=False))
