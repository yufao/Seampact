"""Container-only startup: immutable source input and private writable state.

Keys are read from private bind-mounted files immediately before exec. Compose
does not contain key values. Probe containers cannot reach external networks.
"""
import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from urllib.request import urlopen


def release_sources(input_root=Path("/input")):
    root = input_root.resolve()
    manifest = json.loads((root / "release-manifest.json").read_text(encoding="utf-8"))
    sources = json.loads((root / "sources.json").read_text(encoding="utf-8"))
    hashes = manifest["artifact_hashes"]
    if hashlib.sha256((root / "sources.json").read_bytes()).hexdigest() != hashes["sources.json"]:
        raise ValueError("source registry digest mismatch")
    for identity, source in sources.items():
        relative = Path(source["root"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("portable source registry required")
        source_root = (root / relative).resolve()
        if not source_root.is_relative_to(root) or source_root == root:
            raise ValueError("source root leaves mounted release")
        for filename in source["files"]:
            target = (source_root / filename).resolve()
            if not target.is_relative_to(source_root):
                raise ValueError("source artifact leaves source root")
            key = target.relative_to(root).as_posix()
            if hashlib.sha256(target.read_bytes()).hexdigest() != hashes[key]:
                raise ValueError("source artifact digest mismatch: " + identity + "/" + filename)
    return sources


def load_secrets(directory=Path("/run/seampact-secrets")):
    for provider in ("deepseek", "glm"):
        path = directory / (provider + "_api_key")
        if path.exists():
            if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
                raise ValueError("API key must be a regular private file")
            # Reject a credentials directory or file readable by other users.
            if directory.stat().st_mode & 0o077 or path.stat().st_mode & 0o077:
                raise ValueError("API secret directory requires 0700 and key files 0600")
            value = path.read_text(encoding="utf-8").strip()
            if value:
                if any(character.isspace() for character in value):
                    raise ValueError("API key must be a single token")
                os.environ[provider.upper() + "_API_KEY"] = value


def exec_python(*arguments):
    os.execv(sys.executable, [sys.executable, "-B", *arguments])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("serve", "health", "check", "probe-mmct", "probe-crag", "probe-crag-sdk", "eval-crag-api"))
    args = parser.parse_args()
    os.umask(0o077)
    if args.command == "health":
        with urlopen("http://127.0.0.1:8874/health", timeout=3) as response:
            if response.status != 200:
                raise SystemExit(1)
        return
    if args.command == "check":
        subprocess.run([sys.executable, "-B", "-m", "unittest", "discover", "-s", "/app/tests", "-v"], check=True)
        subprocess.run([sys.executable, "-B", "-m", "unittest", "discover", "-s", "/app/deploy", "-p", "test_*.py", "-v"], check=True)
        exec_python("-m", "seampact", "demo", "--home", "/reports/core-check")
    sources = release_sources()
    if args.command == "eval-crag-api":
        def artifact(name):
            value = os.environ.get(name, "")
            if not value or Path(value).name != value or "/" in value or "\\" in value:
                raise ValueError("explicit API artifact filename required")
            return str(Path("/api-input") / value)
        exec_python("/app/tools/eval_crag_package.py", artifact("SEAMPACT_API_PACKAGE"),
                    artifact("SEAMPACT_API_RECORD"), "/reports/crag-api")
    if args.command == "serve":
        load_secrets()
        exec_python("-m", "seampact", "serve", "--home", "/data/platform",
                    "--sources", "/input/sources.json", "--host", "0.0.0.0",
                    "--port", "8874", "--public-port", os.environ.get("SEAMPACT_PORT", "8875"),
                    "--evidence", "/reports", "--artifacts", "/data/artifacts")
    identity, tool, output = {
        "probe-mmct": ("mmct-image", "probe_mmct.py", "mmct"),
        "probe-crag": ("crag-text", "probe_crag.py", "crag"),
        "probe-crag-sdk": ("crag-text", "probe_crag_sdk.py", "crag-sdk"),
    }[args.command]
    exec_python("/app/tools/" + tool, str(Path("/input") / sources[identity]["root"]), "/reports/" + output)


if __name__ == "__main__":
    main()
