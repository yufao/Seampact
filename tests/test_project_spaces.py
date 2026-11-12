import os
from pathlib import Path

import pytest

from seampact.project_spaces import ProjectSpaces
from seampact.store import Store


@pytest.fixture
def spaces(tmp_path):
    store = Store(tmp_path / "state.sqlite")
    yield ProjectSpaces(tmp_path / "home", store)
    store.close()


def test_sessions_leases_and_cleanup_preserve_data(spaces):
    project = spaces.default()["id"]
    other = spaces.create({"name": "Other"})["id"]
    first = spaces.ensure_session(project, "conversation")
    assert first == spaces.ensure_session(project, "conversation")
    with pytest.raises(ValueError):
        spaces.ensure_session(other, "conversation")
    spaces.write_file(project, None, "data", "input.txt", "input")
    spaces.write_file(project, "conversation", "workspace", "result.txt", "result")
    lease = spaces.begin_task(project, "conversation", "task")
    assert spaces.begin_task(project, "conversation", "task") == lease
    with pytest.raises(ValueError):
        spaces.begin_task(project, "conversation", "second-task")
    with pytest.raises(ValueError):
        spaces.cleanup(project, {"target": "workspace"})
    with pytest.raises(ValueError):
        spaces.put_file(project, None, "data", "input.txt", b"replacement")
    with pytest.raises(ValueError):
        spaces.delete_file(project, None, "data", "input.txt")
    spaces.finish_task("task")
    spaces.cleanup(project, {"target": "workspace", "conversation_id": "conversation"})
    assert spaces.read_file(project, None, "data", "input.txt")["content"] == "input"
    assert spaces.list_files(project, "conversation", "workspace")["entries"] == []
    assert spaces.delete_file(project, None, "data", "input.txt")["deleted"]
    spaces.write_file(project, None, "data", "input.txt", "input")
    spaces.archive(project)
    assert spaces.read_file(project, None, "data", "input.txt")["content"] == "input"
    assert spaces.ensure_session(project, "conversation") == first
    with pytest.raises(ValueError):
        spaces.ensure_session(project, "new-conversation")
    with pytest.raises(ValueError):
        spaces.begin_task(project, "conversation", "new-task")


def test_restart_preserves_uncertain_lease(tmp_path):
    store = Store(tmp_path / "state.sqlite")
    original = ProjectSpaces(tmp_path / "home", store)
    original.default()
    original.begin_task("default", "conv", "task")
    restarted = ProjectSpaces(tmp_path / "home", store)
    assert restarted.task_scope("task")["status"] == "UNKNOWN"
    with pytest.raises(ValueError):
        restarted.cleanup("default", {"target": "scratch"})
    store.close()


@pytest.mark.parametrize("path", ["../outside", "/absolute", "C:\\outside", "a/../b", "a:stream", "NUL"])
def test_paths_are_confined(spaces, path):
    spaces.default()
    spaces.ensure_session("default", "conv")
    with pytest.raises(ValueError):
        spaces.write_file("default", "conv", "workspace", path, "x")


def test_links_rejected_and_bounded_read(spaces, tmp_path):
    spaces.default()
    spaces.ensure_session("default", "conv")
    spaces.write_file("default", "conv", "workspace", "file.txt", "abcdef")
    assert spaces.read_file("default", "conv", "workspace", "file.txt", 2, 2)["content"] == "cd"
    root = spaces.file_path("default", "conv", "workspace", "file.txt").parent
    outside = tmp_path / "outside.txt"
    outside.write_text("outside")
    try:
        os.link(outside, root / "hardlink.txt")
    except OSError:
        pytest.skip("hard links unavailable")
    with pytest.raises(ValueError):
        spaces.write_file("default", "conv", "workspace", "hardlink.txt", "changed")
    assert outside.read_text() == "outside"


def test_unresolved_queries_do_not_load_lease_history(spaces, monkeypatch):
    spaces.default()
    other = spaces.create({"name": "其他项目"})["id"]
    for i in range(30):
        spaces.store.save("project_lease", f"old-{i}", dict(
            task_id=f"old-{i}", project_id="default", conversation_id=f"old-{i}", status="FINISHED"))
    original_list = spaces.store.list

    def guarded_list(kind):
        assert kind != "project_lease", "lease history must be filtered in the database"
        return original_list(kind)

    monkeypatch.setattr(spaces.store, "list", guarded_list)
    spaces.begin_task(other, "busy", "active")
    assert spaces.task_scopes("default", unresolved_only=True) == []
    assert len(spaces.task_scopes("default")) == 30
    spaces.require_idle("default")
    spaces.put_file("default", None, "data", "input.txt", b"input")
    spaces.cleanup("default", {"target": "workspace"})
    with pytest.raises(ValueError):
        spaces.require_idle(other)
    plan = spaces.store.db.execute(
        "EXPLAIN QUERY PLAN SELECT 1 FROM documents WHERE kind='project_lease' "
        "AND json_extract(data, '$.status') IN ('ACTIVE', 'UNKNOWN') "
        "AND json_extract(data, '$.project_id')=? LIMIT 1", ("default",)).fetchall()
    assert any("project_lease_unresolved" in str(row) for row in plan)


def test_utf8_pagination_and_binary_rejection(spaces):
    spaces.default()
    spaces.put_file("default", None, "data", "text.txt", "中🙂文".encode("utf-8"))
    offset, pieces = 0, []
    while True:
        page = spaces.read_file("default", None, "data", "text.txt", offset, 1)
        pieces.append(page["content"])
        assert page["next_offset"] > offset
        offset = page["next_offset"]
        if page["eof"]:
            break
    assert "".join(pieces) == "中🙂文"
    assert offset == len("中🙂文".encode("utf-8"))
    spaces.put_file("default", None, "data", "binary.xlsx", b"PK\x03\x04\x00\xff")
    with pytest.raises(ValueError, match="UTF-8"):
        spaces.read_file("default", None, "data", "binary.xlsx", 0, 1)


def test_archive_and_scoped_workspace_cleanup(spaces):
    spaces.default()
    spaces.ensure_session("default", "idle")
    spaces.write_file("default", "idle", "workspace", "result.txt", "x")
    spaces.begin_task("default", "busy", "task")
    with pytest.raises(ValueError):
        spaces.archive("default")
    with pytest.raises(ValueError):
        spaces.cleanup("default", {"target": "workspace", "conversation_id": "busy"})
    with pytest.raises(ValueError):
        spaces.cleanup("default", {"target": "scratch", "conversation_id": "idle"})
    spaces.cleanup("default", {"target": "workspace", "conversation_id": "idle"})
    assert not spaces.list_files("default", "idle", "workspace")["entries"]
    spaces.finish_task("task")
    assert spaces.archive("default")["archived"]


def test_task_mounts_keep_sessions_separate_and_uncertain_scratch(spaces):
    spaces.default()
    spaces.begin_task("default", "first", "first-task")
    spaces.begin_task("default", "second", "second-task")
    first = spaces.container_mounts("first-task")
    second = spaces.container_mounts("second-task")
    assert [(item["target"], item["read_only"]) for item in first] == [
        ("/data", True), ("/workspace", False), ("/scratch", False)]
    assert first[0] == second[0]
    assert first[1]["source"] != second[1]["source"]
    assert first[2]["source"] != second[2]["source"]
    temporary = Path(first[2]["source"]) / "pending.txt"
    temporary.write_text("pending")
    spaces.finish_task("first-task", drained=False)
    assert temporary.exists()
    with pytest.raises(ValueError, match="活动任务租约"):
        spaces.container_mounts("first-task")
    spaces.finish_task("first-task", drained=True)
    assert not temporary.exists()
    assert spaces.container_mounts("second-task") == second
