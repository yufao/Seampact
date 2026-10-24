"""Repository addresses become complete, read-only analysis source scopes."""

import os
import re
import shutil
import stat
import subprocess
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from .inspection import inventory_repository


def inside_platform_checkout(home, path):
    """A weight path inside a platform clone is not a durable resource location."""
    if not isinstance(path, str) or not path.strip():
        return False
    try:
        resolved = Path(path).expanduser().resolve(strict=False)
        base = (Path(home).resolve() / "repositories").resolve()
    except OSError:
        return False
    return resolved == base or base in resolved.parents


def managed_checkout(home, root):
    """Return a platform clone at home/repositories/<id>, never a user directory."""
    if not isinstance(root, str) or not root.strip():
        return None
    try:
        path = Path(root).expanduser().resolve(strict=False)
        base = (Path(home).resolve() / "repositories").resolve()
    except OSError:
        return None
    if path == base:
        return None
    try:
        relative = path.relative_to(base)
    except ValueError:
        return None
    if len(relative.parts) != 1 or relative.parts[0] in {".", ".."}:
        return None
    return path


def remove_checkout(path):
    """Delete one platform clone. Git marks some metadata read-only on Windows."""
    path = Path(path)
    if not path.exists():
        return False

    def onerror(function, failed, exc_info):
        try:
            os.chmod(failed, stat.S_IWRITE)
            function(failed)
        except OSError:
            raise exc_info[1]

    shutil.rmtree(path, onerror=onerror)
    return True


class RepositoryIntake:
    def __init__(self, home):
        self.home = Path(home).resolve()

    @staticmethod
    def _git(arguments, required=True):
        try:
            result = subprocess.run(["git", *arguments], stdin=subprocess.DEVNULL,
                capture_output=True, text=True,
                env=dict(os.environ, GIT_TERMINAL_PROMPT="0"))
        except OSError as error:
            if not required:
                return None
            raise ValueError("Git is unavailable") from error
        if result.returncode:
            if not required:
                return None
            raise ValueError("Git repository operation failed")
        return result.stdout.strip()

    def prepare(self, address, revision=None, entry=None, resources=None):
        """Accept a local directory or Git URL without importing its source code.

        resources is an optional list of resource paths or metadata objects.
        An omitted entry produces static candidates, rather than a selected
        execution entry or a decomposition answer.
        """
        if not isinstance(address, str) or not address.strip():
            raise ValueError("provide a repository URL or local directory")
        address = address.strip()
        if revision is not None and (not isinstance(revision, str) or not revision.strip()):
            raise ValueError("revision must be a nonempty Git reference")
        identity = uuid.uuid4().hex
        local = Path(address).expanduser()
        remote = "://" in address or (not re.match(r"^[A-Za-z]:[\\/]", address)
            and bool(re.match(r"^(?:[^/\\\s@]+@)?[^/\\\s:]+:.+", address)))
        if local.is_dir():
            root = local.resolve()
            head = self._git(["-C", str(root), "rev-parse", "--verify", "HEAD"], required=False)
            if revision:
                requested = self._git(["-C", str(root), "rev-parse", "--verify", "--end-of-options",
                                       revision.strip() + "^{commit}"])
                if requested != head:
                    raise ValueError("local directory HEAD does not match the requested revision")
        elif remote:
            root = self.home / "repositories" / identity
            root.parent.mkdir(parents=True, exist_ok=True)
            # Native artifact manifests hash the original bytes. Windows Git
            # must not rewrite their JSON files to CRLF during checkout.
            try:
                self._git(["clone", "-c", "core.autocrlf=false", "--", address, str(root)])
                if revision:
                    commit = self._git(["-C", str(root), "rev-parse", "--verify", "--end-of-options",
                                       revision.strip() + "^{commit}"])
                    self._git(["-C", str(root), "checkout", "--detach", commit])
                head = self._git(["-C", str(root), "rev-parse", "--verify", "HEAD"])
                # Even a branch URL is represented by the actual checked-out commit.
                self._git(["-C", str(root), "checkout", "--detach", head])
            except Exception:
                try:
                    remove_checkout(root)
                except OSError:
                    pass
                raise
        else:
            raise ValueError("local source directory does not exist")

        structure = inventory_repository(root, excluded_roots=[self.home])
        if resources is not None:
            if not isinstance(resources, list):
                raise ValueError("resources must be a list of paths or metadata objects")
            known = {item["path"] for item in structure["resources"]}
            for item in resources:
                record = dict(item) if isinstance(item, dict) else dict(path=item)
                if not isinstance(record.get("path"), str) or not record["path"]:
                    raise ValueError("each resource requires a path")
                if record["path"] not in known:
                    record.setdefault("kind", "declared")
                    record.setdefault("bytes", None)
                    structure["resources"].append(record)
                    known.add(record["path"])
        candidates = structure["entry_candidates"]
        inferred = dict(status="STATIC_CANDIDATES", candidates=candidates,
                        selected=None, execution="NOT_RUN")
        name = root.name if not remote else Path(urlsplit(address).path.rstrip("/")).name.removesuffix(".git")
        return dict(id=identity, name=name or identity, address=address, root=str(root),
                    revision=head, entry=entry if entry else inferred,
                    files=structure["files"], inventory=structure["inventory"],
                    resources=structure["resources"])
