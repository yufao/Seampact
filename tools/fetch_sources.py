"""Fetch small, commit-pinned public inputs without cloning upstream repositories.

Only coordinates and hashes are versioned. Snapshots and the generated local
workspace manifest belong in ignored local directories, alongside private state.
No model API, Python package installation, or native Harness execution occurs.
"""
import argparse
import hashlib
import json
import os
import re
import tempfile
import tomllib
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen


CORE = Path(__file__).resolve().parents[1]
CODE_DIRECTORIES = ("src", "tests", "tools", "docs", "deploy", "config")
CODE_SUFFIXES = {".py", ".js", ".html", ".css", ".md", ".txt", ".yaml", ".yml", ".toml"}
CODE_ROOT_FILES = ("README.md", "pyproject.toml", ".env.example", ".gitignore", ".dockerignore")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def relative_path(value):
    path = PurePosixPath(value)
    if (not value or path.is_absolute() or ".." in path.parts or ":" in value
            or "\\" in value or str(path) != value):
        raise ValueError("source specification contains an unsafe artifact path")
    return path


def destination(root, relative):
    path = relative_path(relative)
    candidate = root.joinpath(*path.parts)
    # Resolve directories as well as files: a planted symlink cannot redirect
    # either a reuse check or an atomic replacement outside this workspace.
    if not candidate.resolve().is_relative_to(root.resolve()):
        raise ValueError("source artifact leaves its selected root")
    if candidate.is_symlink():
        raise ValueError("source artifact must not be a symlink")
    return candidate


def atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".seampact-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_spec(path):
    spec = json.loads(path.read_text(encoding="utf-8"))
    if spec.get("schema") != "pinned-source-spec/1":
        raise ValueError("unsupported source specification")
    identities = set()
    for source in spec["sources"]:
        identity = source["id"]
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", identity) or identity in identities:
            raise ValueError("invalid or duplicate source identity")
        identities.add(identity)
        upstream = urlparse(source["source_url"])
        repository = upstream.path.strip("/").split("/")
        if (upstream.scheme != "https" or upstream.netloc != "github.com"
                or upstream.query or upstream.fragment or len(repository) != 2
                or any(not re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in repository)
                or not re.fullmatch(r"[0-9a-f]{40}", source["revision"])):
            raise ValueError("source requires a fixed public GitHub commit")
        paths = set()
        for artifact in source["artifacts"]:
            relative_path(artifact["path"])
            if artifact["path"] in paths:
                raise ValueError("duplicate source artifact")
            paths.add(artifact["path"])
            if (not re.fullmatch(r"[0-9a-f]{64}", artifact["sha256"])
                    or not isinstance(artifact["bytes"], int) or artifact["bytes"] < 0):
                raise ValueError("source artifact requires a byte count and SHA-256")
        if not set(source["files"]).issubset(paths):
            raise ValueError("registered source files are absent from the specification")
    return spec


def matches(data, artifact):
    return len(data) == artifact["bytes"] and digest(data) == artifact["sha256"]


def existing_bytes(path, artifact):
    if not path.is_file():
        return None
    data = path.read_bytes()
    return data if matches(data, artifact) else None


def fetch_bytes(source, artifact):
    repository = urlparse(source["source_url"]).path.strip("/")
    relative = "/".join(quote(part, safe="") for part in relative_path(artifact["path"]).parts)
    url = "https://raw.githubusercontent.com/" + repository + "/" + source["revision"] + "/" + relative
    request = Request(url, headers={"User-Agent": "SeamPact-pinned-source-fetch/1"})
    with urlopen(request, timeout=60) as response:
        data = response.read(artifact["bytes"] + 1)
    if not matches(data, artifact):
        raise ValueError("downloaded source digest mismatch: " + source["id"] + "/" + artifact["path"])
    return data


def code_hashes(workspace):
    hashes = {}
    for directory in CODE_DIRECTORIES:
        root = workspace / directory
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(workspace).as_posix()
            if "__pycache__" in path.parts or not path.is_file():
                continue
            if path.suffix not in CODE_SUFFIXES and path.name != "Dockerfile" and relative != "config/source-spec.json":
                continue
            if path.is_symlink() or not path.resolve().is_relative_to(workspace):
                raise ValueError("workspace build contains an unsafe code path")
            hashes[relative] = digest(path.read_bytes())
    for name in CODE_ROOT_FILES:
        path = destination(workspace, name)
        if path.is_file():
            hashes[name] = digest(path.read_bytes())
    return hashes


def prepare(spec_path, workspace, identities=None, reuse_release=None, offline=False, check=False):
    spec_path, workspace = Path(spec_path).resolve(), Path(workspace).resolve()
    spec = read_spec(spec_path)
    wanted = set(identities or (source["id"] for source in spec["sources"]))
    known = {source["id"] for source in spec["sources"]}
    if not wanted or not wanted.issubset(known):
        raise ValueError("unknown source identity")
    sources = [source for source in spec["sources"] if source["id"] in wanted]
    reuse_root = Path(reuse_release).resolve() if reuse_release else None
    counts = {"already_valid": 0, "reused": 0, "downloaded": 0}
    hashes = code_hashes(workspace)
    registry, notices = {}, []
    for source in sources:
        identity = source["id"]
        for artifact in source["artifacts"]:
            relative = "sources/" + identity + "/" + artifact["path"]
            target = destination(workspace, relative)
            data = existing_bytes(target, artifact)
            if data is not None:
                counts["already_valid"] += 1
            else:
                if check:
                    raise ValueError("missing or changed local source: " + identity + "/" + artifact["path"])
                if reuse_root:
                    data = existing_bytes(destination(reuse_root, relative), artifact)
                if data is not None:
                    counts["reused"] += 1
                elif offline:
                    raise ValueError("offline input unavailable: " + identity + "/" + artifact["path"])
                else:
                    data = fetch_bytes(source, artifact)
                    counts["downloaded"] += 1
                atomic_write(target, data)
            hashes[relative] = artifact["sha256"]
        registry[identity] = {key: source[key] for key in ("name", "files", "entry", "revision", "scope")}
        registry[identity]["root"] = "sources/" + identity
        notices.append(dict(id=identity, source_url=source["source_url"], revision=source["revision"],
                            license_status=source["license_status"],
                            license_files=[artifact["path"] for artifact in source["artifacts"] if artifact["kind"] == "license"]))
    registry_bytes, notice_bytes = encoded(registry), encoded(notices)
    hashes["sources.json"] = digest(registry_bytes)
    hashes["SOURCE-NOTICES.json"] = digest(notice_bytes)
    version = tomllib.loads((workspace / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    manifest = dict(schema="workspace-build/1", version=version, artifact_hashes=dict(sorted(hashes.items())),
                    source_spec_sha256=digest(spec_path.read_bytes()), sources=len(sources),
                    identity_scope="selected pinned upstream inputs and allowlisted local code at manifest generation",
                    release_verification="NOT_A_RELEASE_ATTESTATION", runtime_dependencies=[],
                    optional_sdk_environments=["docs/requirements-sdk-mmct.txt", "docs/requirements-sdk-crag.txt"])
    if check:
        if (destination(workspace, "sources.json").read_bytes() != registry_bytes
                or destination(workspace, "SOURCE-NOTICES.json").read_bytes() != notice_bytes):
            raise ValueError("generated source registry or notices differ from the specification")
        existing_manifest = json.loads(destination(workspace, "release-manifest.json").read_text(encoding="utf-8"))
        if existing_manifest != manifest:
            raise ValueError("workspace manifest is stale; regenerate after code or source changes")
    else:
        atomic_write(destination(workspace, "sources.json"), registry_bytes)
        atomic_write(destination(workspace, "SOURCE-NOTICES.json"), notice_bytes)
        atomic_write(destination(workspace, "release-manifest.json"), encoded(manifest))
    return dict(status="PASS", operation="CHECK" if check else "PREPARE", sources=len(sources),
                artifacts=sum(len(source["artifacts"]) for source in sources), **counts,
                version=version, manifest_schema=manifest["schema"], model_api_calls=0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, default=CORE / "config" / "source-spec.json")
    parser.add_argument("--workspace", type=Path, default=CORE)
    parser.add_argument("--source", action="append", help="select an ID; repeat to select several, default all four")
    parser.add_argument("--reuse-release", type=Path, help="reuse bytes from an extracted release containing sources/")
    parser.add_argument("--offline", action="store_true", help="do not download missing artifacts")
    parser.add_argument("--check", action="store_true", help="check local sources and manifest without writing or downloading")
    args = parser.parse_args()
    try:
        result = prepare(args.spec, args.workspace, args.source, args.reuse_release, args.offline, args.check)
    except (OSError, ValueError, KeyError) as error:
        # Path-bearing OS messages can contain a user's home. The public report
        # records the error class; ordinary validation messages use relative IDs.
        message = str(error) if isinstance(error, ValueError) else "local I/O or upstream request failed"
        print(json.dumps(dict(status="FAIL", error=type(error).__name__, detail=message), ensure_ascii=False))
        raise SystemExit(1)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
