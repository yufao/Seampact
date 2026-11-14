"""Project identity determines mounts, call scope and persistent index ownership."""
import copy
from pathlib import Path
from types import SimpleNamespace

import pytest

from seampact.capability_contract import (
    IR_SCHEMA, PACKAGE_SCHEMA, ContractError, check_bindings, package_digest, project_binding_path,
)
from seampact.capability_catalog import CapabilityCatalog
from seampact.hosting.capability_hosting import (
    check_project_scope, group_identity, instance_contract, project_scope, resource_backend,
)
from seampact.project_applications import belongs_to_project
from seampact.project_spaces import ProjectSpaces
from seampact.runtime import ProtocolError, _task_context
from seampact.store import Store


def manifest():
    return dict(schema=PACKAGE_SCHEMA, name="retrieval", source=dict(id="source", version="v1"),
        generator=dict(template="python-adapter/1"), environment=dict(python="3.11"),
        implementation=dict(module="adapter", symbol="Retriever"),
        instance=dict(concurrency=4, replicable=True, cancel="none"),
        resources=[dict(id=identity, kind="data", required=True, description=identity)
                   for identity in ("corpus", "index")],
        capabilities=[dict(schema=IR_SCHEMA, id="search", name="Search", purpose="Search project files",
            input=dict(type="object"), output=dict(kind="json"), state=dict(scope="instance"),
            resources=["corpus", "index"])], files=[dict(path="adapter.py", sha256="0" * 64)])


def bindings(description, project="default"):
    return dict(corpus=dict(form="project", project_id=project, area="data", read_only=True),
                index=dict(form="project", project_id=project, area="indexes", read_only=False,
                           namespace=package_digest(description)[:24]))


@pytest.fixture
def platform(tmp_path):
    store = Store(tmp_path / "state.sqlite")
    projects = ProjectSpaces(tmp_path / "home", store)
    projects.default()
    yield SimpleNamespace(store=store, projects=projects)
    store.close()


def test_project_bindings_reject_paths_cross_project_and_foreign_index_identity():
    description = manifest()
    selected = bindings(description)
    assert check_bindings(description, selected) == []
    assert project_binding_path(selected["index"]).endswith("/indexes/" + package_digest(description)[:24])
    changes = [lambda b: b["corpus"].update(path="/host/private"),
               lambda b: b["corpus"].update(project_id="../other"),
               lambda b: b["corpus"].update(read_only=False),
               lambda b: b["index"].update(namespace="f" * 24),
               lambda b: b["index"].update(project_id="other")]
    for change in changes:
        invalid = copy.deepcopy(selected)
        change(invalid)
        with pytest.raises(ContractError):
            check_bindings(description, invalid)
    contract = instance_contract("group", description, package_digest(description), selected)
    assert contract["concurrency"] == 1
    assert contract["replicas"]["replicable"] is False


def test_derived_mounts_exclude_platform_parent_and_persist_index(platform, monkeypatch):
    description = manifest()
    selected = bindings(description)
    namespace = selected["index"]["namespace"]
    index = platform.projects.project_mount("default", "indexes", namespace)
    (index / "snapshot.json").write_text("cached", encoding="utf-8")
    exists = Path.exists
    monkeypatch.setattr(Path, "exists", lambda path: False if str(path) == "/.dockerenv" else exists(path))
    config = dict(kind="docker", mounts=[dict(source="/host/platform", target=str(platform.projects.home),
                                              read_only=False)])
    mounted = resource_backend(platform, dict(bindings=selected), config)
    assert mounted["mounts"] == [
        dict(source=str(platform.projects.home / "projects" / "default" / "data"),
             target="/work/projects/default/data", read_only=True),
        dict(source=str(index), target="/work/projects/default/indexes/" + namespace, read_only=False)]
    platform.projects.begin_task("default", "conv", "task")
    platform.projects.finish_task("task")
    platform.projects.cleanup("default", dict(target="workspace"))
    assert (index / "snapshot.json").read_text(encoding="utf-8") == "cached"
    assert len(config["mounts"]) == 1  # the backend's mapping itself is not mutated


def test_project_guard_checks_active_task_or_explicit_diagnostic_scope(platform):
    selected = bindings(manifest())
    group = dict(bindings=selected)
    with pytest.raises(ProtocolError, match="明确的项目范围"):
        check_project_scope(platform, group)
    with project_scope("default"):
        check_project_scope(platform, group)
    other = platform.projects.create(dict(name="Other"))["id"]
    with project_scope(other), pytest.raises(ProtocolError, match="不同项目"):
        check_project_scope(platform, group)
    platform.projects.begin_task("default", "conv", "task")
    token = _task_context.set("task")
    try:
        check_project_scope(platform, group)
        with pytest.raises(ProtocolError, match="不同项目"):
            check_project_scope(platform, dict(bindings=bindings(manifest(), other)))
        platform.projects.finish_task("task")
        with pytest.raises(ProtocolError, match="活动任务租约"):
            check_project_scope(platform, group)
    finally:
        _task_context.reset(token)


def test_installation_isolated_by_project_and_bind_cannot_transfer_owner(platform, tmp_path, monkeypatch):
    description = manifest()
    current = [description]
    monkeypatch.setattr("seampact.capability_catalog.load_package", lambda folder: current[0])
    folder = tmp_path / "source-package"
    folder.mkdir()
    (folder / "content").write_text("fixture", encoding="utf-8")
    catalog = CapabilityCatalog(platform.projects.home, platform.store)
    first = catalog.install(folder, bindings(description, "first"))
    second = catalog.install(folder, bindings(description, "second"))
    assert first["group_id"] != second["group_id"]
    assert first["capabilities"] != second["capabilities"]
    assert catalog.unit(first["capabilities"][0])["status"] == "READY"
    assert catalog.unit(second["capabilities"][0])["status"] == "READY"

    revised = copy.deepcopy(description)
    revised["source"]["version"] = "v2"
    current[0] = revised
    updated = catalog.install(folder, bindings(revised, "first"))
    assert updated["group_id"] == first["group_id"]
    assert catalog.unit(second["capabilities"][0])["status"] == "READY"
    assert catalog._group(second["group_id"])["bindings"] == bindings(description, "second")
    with pytest.raises(ValueError, match="其他项目"):
        catalog.bind(first["group_id"], bindings(revised, "second"))
    unbound = catalog.bind(first["group_id"], {"corpus": None, "index": None})
    assert catalog._group(first["group_id"])["project_owner"] == "first"
    assert belongs_to_project(catalog, catalog.unit(unbound["capabilities"][0]), "first")
    assert not belongs_to_project(catalog, catalog.unit(unbound["capabilities"][0]), "second")
    with pytest.raises(ValueError, match="其他项目"):
        catalog.bind(first["group_id"], bindings(revised, "second"))


def test_existing_project_group_keeps_identity_on_regeneration(platform, tmp_path, monkeypatch):
    description = manifest()
    monkeypatch.setattr("seampact.capability_catalog.load_package", lambda folder: description)
    folder = tmp_path / "source-package"
    folder.mkdir()
    (folder / "content").write_text("fixture", encoding="utf-8")
    catalog = CapabilityCatalog(platform.projects.home, platform.store)
    old_id = group_identity(description)
    old = catalog._register(description, package_digest(description), old_id, bindings(description, "first"))
    # Simulate an installation predating the explicit owner field.
    old_group = catalog._group(old_id)
    old_group.pop("project_owner")
    platform.store.save("runtime_group", old_id, old_group)
    assert catalog.install(folder, bindings(description, "first"))["group_id"] == old_id
    new = catalog.install(folder, bindings(description, "second"))
    assert new["group_id"] != old_id
    assert catalog.unit(old["capabilities"][0])["status"] == "READY"
    assert catalog.group_for_install(description, {}) != old_id
