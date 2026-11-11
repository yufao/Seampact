"""Read-only identity checks for explicitly declared Hugging Face cache files.

This does not infer a source loader, authorize execution, or establish model
quality. Callers must independently prove how the original loader obtains these
values and run this check in the execution environment.
"""

import hashlib
import os
import re
import sys
from pathlib import Path, PurePosixPath


_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_REPO_PART = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,95}\Z")


def validate_hub_cache_spec(spec):
    """Validate and normalize a fixed-hub-cache/1 declaration, or raise ValueError."""
    if not isinstance(spec, dict) or set(spec) != {"schema", "repo_id", "revision", "cache_dir", "ref", "required_files"}:
        raise ValueError("fixed Hub cache declaration has invalid fields")
    if spec["schema"] != "fixed-hub-cache/1":
        raise ValueError("unsupported Hub cache declaration schema")
    repo = spec["repo_id"]
    if not isinstance(repo, str) or len(repo) > 193:
        raise ValueError("invalid Hub repository ID")
    parts = repo.split("/")
    if (len(parts) != 2 or repo.endswith(".git") or
            any(not _REPO_PART.fullmatch(p) or p.endswith((".", "-")) or ".." in p or "--" in p for p in parts)):
        raise ValueError("Hub repository ID must be namespace/name")
    revision = spec["revision"]
    if not isinstance(revision, str) or not _COMMIT.fullmatch(revision):
        raise ValueError("revision must be a full lowercase commit")
    if spec["ref"] not in ("main", revision):
        raise ValueError("ref must be main or the fixed revision")
    cache_dir = spec["cache_dir"]
    if not isinstance(cache_dir, str) or not cache_dir or "\x00" in cache_dir or not Path(cache_dir).is_absolute():
        raise ValueError("cache_dir must be an absolute path in the checking environment")
    files = spec["required_files"]
    if not isinstance(files, list) or not files:
        raise ValueError("at least one declared file is required")
    normalized, seen = [], set()
    for item in files:
        if not isinstance(item, dict) or set(item) not in ({"path", "sha256"}, {"path", "sha256", "bytes"}):
            raise ValueError("invalid required file declaration")
        name, sha = item["path"], item["sha256"]
        if (not isinstance(name, str) or not name or "\\" in name or "\x00" in name or ":" in name
                or name.startswith("/") or any(part in ("", ".", "..") for part in name.split("/"))
                or PurePosixPath(name).is_absolute()):
            raise ValueError("required file path must be a safe relative POSIX path")
        if name in seen or not isinstance(sha, str) or not _SHA256.fullmatch(sha):
            raise ValueError("duplicate path or invalid SHA-256")
        seen.add(name)
        row = {"path": name, "sha256": sha}
        if "bytes" in item:
            size = item["bytes"]
            if not isinstance(size, int) or isinstance(size, bool) or size < 0:
                raise ValueError("file bytes must be a nonnegative integer")
            row["bytes"] = size
        normalized.append(row)
    return dict(schema="fixed-hub-cache/1", repo_id=repo, revision=revision,
                cache_dir=cache_dir, ref=spec["ref"], required_files=normalized)


def _offline_state():
    env = os.environ.get("HF_HUB_OFFLINE") == "1"
    hub = sys.modules.get("huggingface_hub.constants")
    transformers = sys.modules.get("transformers.utils.hub")
    hub_value = getattr(hub, "HF_HUB_OFFLINE", None) if hub is not None else None
    transformers_value = getattr(transformers, "_is_offline_mode", None) if transformers is not None else None
    active = env and (hub is None or hub_value is True) and (transformers is None or transformers_value is True)
    return dict(environment_is_one=env, hub_constants_loaded=hub is not None,
                hub_offline=hub_value, transformers_hub_loaded=transformers is not None,
                transformers_offline=transformers_value, active=active)


def _contained(path, root):
    return path == root or root in path.parents


def _link(path):
    # Windows junctions are not always reported by is_symlink().
    return path.is_symlink() or getattr(os.path, "isjunction", lambda _: False)(path)


def _safe_file(snapshot, blobs, name):
    """Constrain every link hop to this snapshot or this repository's blobs."""
    current = snapshot
    remaining = list(PurePosixPath(name).parts)
    links = 0
    while remaining:
        current = current / remaining.pop(0)
        if _link(current):
            links += 1
            if links > 40:
                raise ValueError("too many cache symlink hops")
            target = Path(os.path.abspath(os.path.join(current.parent, os.readlink(current))))
            root = next((allowed for allowed in (snapshot, blobs) if _contained(target, allowed)), None)
            if root is None:
                raise ValueError("symlink escapes the selected snapshot and repository blobs")
            remaining = list(target.relative_to(root).parts) + remaining
            current = root
    if not current.is_file():
        raise ValueError("declared path is not a regular file")
    return current


def check_hub_cache(spec, require_offline=True):
    """Verify declared cached file identities without network access or mutation."""
    value = validate_hub_cache_spec(spec)
    if not isinstance(require_offline, bool):
        raise ValueError("require_offline must be boolean")
    offline = _offline_state()
    result = dict(status="MISSING", resolved_path=None, missing_files=[], reason=None,
                  repo_id=value["repo_id"], revision=value["revision"], ref=value["ref"],
                  offline=offline, offline_required=require_offline,
                  scope="declared file identity at observation only; no atomic freeze, source, quality, or lifecycle authorization")
    if require_offline and not offline["active"]:
        result["reason"] = "HF offline mode is not active in this process"
        return result
    cache = Path(value["cache_dir"])
    repo = cache / ("models--" + value["repo_id"].replace("/", "--"))
    snapshots = repo / "snapshots"
    snapshot = snapshots / value["revision"]
    blobs = repo / "blobs"
    try:
        for directory in (cache, repo, snapshots, snapshot):
            if _link(directory) or not directory.is_dir():
                raise ValueError("cache, repository, and snapshot directories must be present and not symlinks")
        if _link(blobs) or (blobs.exists() and not blobs.is_dir()):
            raise ValueError("repository blobs directory is not a regular directory")
        if value["ref"] == "main":
            refs = repo / "refs"
            ref = refs / "main"
            if _link(refs) or not refs.is_dir() or _link(ref) or not ref.is_file():
                raise ValueError("main ref is missing or not a regular file")
            if ref.read_text(encoding="ascii").strip() != value["revision"]:
                raise ValueError("main ref does not match the fixed revision")
        snapshot_root = snapshot.resolve(strict=True)
        blobs_root = blobs.resolve(strict=False)
        result["resolved_path"] = str(snapshot_root)
        for item in value["required_files"]:
            try:
                path = _safe_file(snapshot_root, blobs_root, item["path"])
                size = path.stat().st_size
                if "bytes" in item and size != item["bytes"]:
                    raise ValueError("size mismatch")
                digest = hashlib.sha256()
                total = 0
                with path.open("rb") as stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(block)
                        total += len(block)
                if "bytes" in item and total != item["bytes"]:
                    raise ValueError("size changed while reading")
                if digest.hexdigest() != item["sha256"]:
                    raise ValueError("SHA-256 mismatch")
            except (OSError, ValueError, RuntimeError) as error:
                result["missing_files"].append(dict(path=item["path"], reason=str(error)))
        if result["missing_files"]:
            result["reason"] = "one or more declared files could not be verified"
        else:
            result.update(status="IDENTITY_VERIFIED", reason=None)
    except (OSError, ValueError, UnicodeError, RuntimeError) as error:
        result["reason"] = str(error)
    return result
