"""Validate explicitly declared research-data uses and related splits.

The manifest records declarations; it does not verify licences, discover all
related examples, establish that adapters read only declared files, or grant
Unit permissions.
"""

import hashlib
import json
import re
from pathlib import Path


PURPOSES = frozenset({"train", "validation", "test"})
SCHEMA = "research-data-manifest/1"


def _text(value, field):
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{field} must be a nonempty, trimmed string")
    return value


def _strings(value, field, *, nonempty=False, distinct=True):
    if not isinstance(value, list) or (nonempty and not value):
        raise ValueError(f"{field} must be {'a nonempty' if nonempty else 'an'} array")
    items = [_text(item, field) for item in value]
    if distinct and len(set(items)) != len(items):
        raise ValueError(f"{field} must not contain duplicate values")
    return items


def _digest(value, field):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError(f"{field} must be a lowercase hexadecimal sha256 digest")
    return value


def task_digest(task):
    """Hash the canonical UTF-8 JSON task excluding its two identity fields."""
    if not isinstance(task, dict):
        raise ValueError("research data task must be an object")
    value = {k: v for k, v in task.items() if k not in {"id", "data_member_id"}}
    try:
        encoded = json.dumps(value, sort_keys=True, ensure_ascii=False,
                             separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as error:
        raise ValueError("research data task must be canonical JSON") from error
    return hashlib.sha256(encoded).hexdigest()


def validate_manifest(manifest, purpose, member_ids):
    """Validate one purpose against a complete, explicitly related manifest.

    ``derived_from`` contains stable member IDs or external stable identifiers.
    Matching identifiers connect records even when the origin itself is not a
    member. ``group_ids`` are global within this manifest, so namespacing them
    by dataset or another stable authority is the manifest author's duty.
    """
    if not isinstance(manifest, dict) or manifest.get("schema") != SCHEMA:
        raise ValueError("research data manifest requires schema research-data-manifest/1")
    if not isinstance(purpose, str) or purpose not in PURPOSES:
        raise ValueError("research data purpose must be train, validation or test")
    selected = _strings(member_ids, "member_ids", nonempty=True, distinct=False)
    rows = manifest.get("members")
    if not isinstance(rows, list) or not rows:
        raise ValueError("research data manifest requires nonempty members")

    members, parent, rank, owners = {}, {}, {}, {}

    def find(node):
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(left, right):
        left, right = find(left), find(right)
        if left == right:
            return
        if rank[left] < rank[right]:
            left, right = right, left
        parent[right] = left
        if rank[left] == rank[right]:
            rank[left] += 1

    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("manifest members must be objects")
        identity = _text(row.get("id"), "member.id")
        if identity in members:
            raise ValueError("manifest member id must be unique")
        upstream = row.get("upstream")
        if not isinstance(upstream, dict):
            raise ValueError("member.upstream must be an object")
        dataset, revision, example = (_text(upstream.get(k), "upstream." + k)
                                      for k in ("dataset", "revision", "example_id"))
        groups = _strings(row.get("group_ids"), "member.group_ids", nonempty=True)
        lineage = _strings(row.get("derived_from"), "member.derived_from")
        allowed = _strings(row.get("allowed_use"), "member.allowed_use", nonempty=True)
        if set(allowed) - PURPOSES:
            raise ValueError("member.allowed_use contains an unknown purpose")
        split = row.get("split")
        if not isinstance(split, str) or split not in PURPOSES:
            raise ValueError("member.split must be train, validation or test")
        if split not in allowed:
            raise ValueError("member split must be permitted by allowed_use")
        _digest(row.get("expected_task_sha256"), "member.expected_task_sha256")
        files = row.get("files", [])
        if not isinstance(files, list):
            raise ValueError("member.files must be an array")
        file_paths = set()
        for item in files:
            if not isinstance(item, dict):
                raise ValueError("member.files entries must be objects")
            file_path = _text(item.get("path"), "member.files.path")
            if file_path in file_paths:
                raise ValueError("member.files paths must be unique")
            file_paths.add(file_path)
            _digest(item.get("sha256"), "member.files.sha256")
        members[identity] = row
        parent[identity], rank[identity] = identity, 0
        # Separate namespaces prevent an unrelated group name from colliding
        # with an upstream or lineage identifier.
        # Revision is retained as provenance but cannot make the same upstream
        # example independent across two different revisions.
        keys = [("upstream", dataset, example), ("lineage", identity)]
        keys += [("group", group) for group in groups]
        keys += [("lineage", origin) for origin in lineage]
        for key in keys:
            if key in owners:
                union(identity, owners[key])
            else:
                owners[key] = identity

    split_by_group = {}
    for identity, row in members.items():
        root = find(identity)
        split_by_group.setdefault(root, set()).add(row["split"])
    if any(len(splits) != 1 for splits in split_by_group.values()):
        raise ValueError("related research data members cross split boundaries")

    for identity in selected:
        if identity not in members:
            raise ValueError("selected research data member is missing from manifest")
        row = members[identity]
        if row["split"] != purpose or purpose not in row["allowed_use"]:
            raise ValueError("selected research data member is not allowed for purpose")

    return dict(schema=SCHEMA, purpose=purpose, member_ids=selected,
                member_count=len(members), selected_count=len(selected),
                selected_distinct_count=len(set(selected)),
                declared_group_count=len(split_by_group),
                scope="declared provenance only; licence, completeness and task binding not independently verified")


def validate_tasks(manifest, purpose, tasks, *, base_path=None):
    """Validate task bindings and the bytes of each selected declared file.

    Relative file paths resolve against ``base_path`` (the manifest directory
    when called by ``load_manifest``). This does not constrain adapter reads.
    """
    if not isinstance(tasks, list) or not tasks or any(not isinstance(t, dict) for t in tasks):
        raise ValueError("research data tasks must be a nonempty array of objects")
    identities = [_text(task.get("data_member_id"), "task.data_member_id") for task in tasks]
    result = validate_manifest(manifest, purpose, identities)
    members = {row["id"]: row for row in manifest["members"]}
    root = Path(base_path) if base_path is not None else Path.cwd()
    checked_files = {}
    for task, identity in zip(tasks, identities):
        row = members[identity]
        if task_digest(task) != row["expected_task_sha256"]:
            raise ValueError("research data task sha256 mismatch")
        for entry in row.get("files", []):
            path = Path(entry["path"])
            if not path.is_absolute():
                path = root / path
            path = path.resolve()
            if path not in checked_files:
                value = hashlib.sha256()
                with path.open("rb") as stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b""):
                        value.update(block)
                checked_files[path] = value.hexdigest()
            if checked_files[path] != entry["sha256"]:
                raise ValueError("research data declared file sha256 mismatch")
    result["declared_files_checked"] = len(checked_files)
    result["declared_files"] = [dict(path=str(path), sha256=digest)
                                for path, digest in sorted(checked_files.items(), key=lambda item: str(item[0]))]
    result["scope"] = ("task JSON and declared file bytes checked; licence, provenance completeness "
                       "and exclusive adapter inputs not independently verified")
    return result


def load_manifest(path, expected_sha256, purpose, tasks):
    """Read and validate the exact manifest bytes identified by SHA-256."""
    _digest(expected_sha256, "manifest sha256")
    manifest_path = Path(path).resolve()
    data = manifest_path.read_bytes()
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected_sha256:
        raise ValueError("research data manifest sha256 mismatch")
    try:
        manifest = json.loads(data.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("research data manifest must be UTF-8 JSON") from error
    result = validate_tasks(manifest, purpose, tasks, base_path=manifest_path.parent)
    result["manifest_sha256"] = actual
    return result
