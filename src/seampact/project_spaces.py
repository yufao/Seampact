"""Managed project files and conservative task leases."""

import json
import os
import re
import shutil
import stat
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path, PureWindowsPath

from .store import now

_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_RESERVED = re.compile(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?\Z", re.I)
_MAX_FILE = 16 * 1024 * 1024
_MAX_READ = 65536
_MAX_ENTRIES = 200


def _identity(value):
    if not isinstance(value, str) or not _ID.fullmatch(value) or _RESERVED.fullmatch(value):
        raise ValueError("标识无效，只能使用字母、数字、下划线和连字符")
    return value


def _parts(value):
    if not isinstance(value, str) or "\x00" in value:
        raise ValueError("路径无效")
    if not value:
        return []
    if value.startswith(("/", "\\")) or PureWindowsPath(value).drive:
        raise ValueError("不允许使用绝对路径")
    parts = value.replace("\\", "/").split("/")
    if any(not p or p in (".", "..") or any(c in p for c in ':*?"<>|') or
           any(ord(c) < 32 for c in p) or p[-1] in ". " or
           _RESERVED.fullmatch(p) for p in parts):
        raise ValueError("路径包含无效的名称")
    return parts


def _checked(path, *, directory=None):
    st = path.lstat()
    if stat.S_ISLNK(st.st_mode) or getattr(st, "st_file_attributes", 0) & 0x400:
        raise ValueError("不允许使用符号链接或重解析点")
    if stat.S_ISREG(st.st_mode):
        if st.st_nlink != 1:
            raise ValueError("不允许使用硬链接文件")
        if directory is True:
            raise ValueError("需要目录")
    elif stat.S_ISDIR(st.st_mode):
        if directory is False:
            raise ValueError("需要普通文件")
    else:
        raise ValueError("不允许使用特殊文件")
    return st


class ProjectSpaces:
    def __init__(self, home, store):
        self.home = Path(home).absolute()
        self.store = store
        self.lock = store.lock
        self.home.mkdir(parents=True, exist_ok=True)
        _checked(self.home, directory=True)
        self.root = self.home / "projects"
        self.root.mkdir(exist_ok=True)
        _checked(self.root, directory=True)
        # A restarted process cannot assert that outstanding calls have drained.
        with self._locked():
            self.store.db.execute("""CREATE INDEX IF NOT EXISTS project_lease_project
                ON documents(json_extract(data, '$.project_id')) WHERE kind='project_lease'""")
            self.store.db.execute("""CREATE INDEX IF NOT EXISTS project_lease_unresolved
                ON documents(json_extract(data, '$.project_id'),
                             json_extract(data, '$.conversation_id'))
                WHERE kind='project_lease'
                  AND json_extract(data, '$.status') IN ('ACTIVE', 'UNKNOWN')""")
            self.store.db.execute("""CREATE INDEX IF NOT EXISTS project_session_project
                ON documents(json_extract(data, '$.project_id')) WHERE kind='project_session'""")
            self.store.db.execute("""UPDATE documents SET data=json_set(data, '$.status', 'UNKNOWN')
                WHERE kind='project_lease'
                  AND json_extract(data, '$.status') IN ('ACTIVE', 'UNKNOWN')
                  AND json_extract(data, '$.status')='ACTIVE'""")

    @contextmanager
    def _locked(self):
        # Serialize across independent Store connections as well as threads.
        with self.store.transaction():
            if not self.store.db.in_transaction:
                self.store.db.execute("BEGIN IMMEDIATE")
            yield

    def list(self):
        return self.store.list("project")

    def create(self, body):
        name = body.get("name") if isinstance(body, dict) else None
        if not isinstance(name, str) or not name.strip() or len(name) > 200:
            raise ValueError("请输入项目名称，长度不超过 200 个字符")
        project = dict(id=uuid.uuid4().hex, name=name.strip(), archived=False, created_at=now())
        with self._locked():
            self.store.create("project", project["id"], project)
            self._directory(project["id"], ["data"], create=True)
        return project

    def default(self):
        with self._locked():
            project = self.store.get("project", "default")
            if project is None:
                project = dict(id="default", name="默认项目", archived=False, created_at=now())
                self.store.create("project", "default", project)
            self._directory("default", ["data"], create=True)
            return project

    def get(self, project_id):
        project = self.store.get("project", _identity(project_id))
        if project is None:
            raise ValueError("项目不存在")
        return project

    def _available(self, project_id):
        project = self.get(project_id)
        if project.get("archived"):
            raise ValueError("项目已归档，无法开展新工作")
        return project

    def archive(self, project_id):
        with self._locked():
            project = self.get(project_id)
            self.require_idle(project_id)
            project["archived"] = True
            self.store.save("project", project_id, project)
            return project

    def session(self, conversation_id):
        return self.store.get("project_session", _identity(conversation_id))

    def task_scope(self, task_id):
        return self.store.get("project_lease", _identity(task_id))

    def project_mount(self, project_id, area, namespace=None):
        """Resolve a persistent project resource without accepting a host path."""
        project_id = _identity(project_id)
        if area not in {"data", "indexes"}:
            raise ValueError("能力资料区域必须为 data 或 indexes")
        if area == "data" and namespace is not None:
            raise ValueError("项目资料不接受索引命名空间")
        if area == "indexes" and (not isinstance(namespace, str)
                                  or not re.fullmatch(r"[0-9a-f]{24}", namespace)):
            raise ValueError("索引命名空间必须由能力包身份生成")
        with self._locked():
            self._available(project_id)
            return self._directory(project_id, [area, *([namespace] if namespace else [])], create=True)

    def container_mounts(self, task_id):
        """Mount only an active task's project input, session output and scratch."""
        with self._locked():
            lease = self.task_scope(task_id)
            if not lease or lease.get("status") != "ACTIVE":
                raise ValueError("隔离执行需要有效的活动任务租约")
            project_id, conversation_id = lease["project_id"], lease["conversation_id"]
            self._available(project_id)
            session = self.session(conversation_id)
            if not session or session["project_id"] != project_id:
                raise ValueError("任务会话与项目归属不一致")
            paths = [(self._directory(project_id, ["data"]), "/data", True),
                     (self._directory(project_id, ["sessions", conversation_id, "workspace"]), "/workspace", False),
                     (self._directory(project_id, ["tasks", task_id, "scratch"]), "/scratch", False)]
            return [dict(source=str(path), target=target, read_only=read_only)
                    for path, target, read_only in paths]

    def task_scopes(self, project_id, unresolved_only=False):
        self.get(project_id)
        unresolved = " AND json_extract(data, '$.status') IN ('ACTIVE', 'UNKNOWN')" if unresolved_only else ""
        with self.lock:
            rows = self.store.db.execute(
                "SELECT data FROM documents WHERE kind='project_lease' "
                "AND json_extract(data, '$.project_id')=?" + unresolved,
                (project_id,)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def _has_unresolved(self, project_id, conversation_id=None):
        sql = ("SELECT 1 FROM documents WHERE kind='project_lease' "
               "AND json_extract(data, '$.status') IN ('ACTIVE', 'UNKNOWN') "
               "AND json_extract(data, '$.project_id')=?")
        args = [project_id]
        if conversation_id is not None:
            sql += " AND json_extract(data, '$.conversation_id')=?"
            args.append(conversation_id)
        return self.store.db.execute(sql + " LIMIT 1", args).fetchone() is not None

    def require_idle(self, project_id, conversation_id=None):
        project_id = _identity(project_id)
        if conversation_id is not None:
            _identity(conversation_id)
        with self.lock:
            self.get(project_id)
            if self._has_unresolved(project_id, conversation_id):
                raise ValueError("项目或会话中有运行中或状态未确认的任务")

    def _ensure_session(self, project_id, conversation_id):
        project = self.get(project_id)
        conversation_id = _identity(conversation_id)
        record = self.session(conversation_id)
        if record and record["project_id"] != project_id:
            raise ValueError("此会话属于其他项目")
        if project.get("archived"):
            if record:
                return record
            raise ValueError("项目已归档，无法创建会话")
        if not record:
            record = dict(id=conversation_id, conversation_id=conversation_id, project_id=project_id)
            self.store.create("project_session", conversation_id, record)
        self._directory(project_id, ["sessions", conversation_id, "workspace"], create=True)
        return record

    def ensure_session(self, project_id, conversation_id):
        with self._locked():
            return self._ensure_session(_identity(project_id), conversation_id)

    def begin_task(self, project_id, conversation_id, task_id):
        project_id, conversation_id, task_id = map(_identity, (project_id, conversation_id, task_id))
        with self._locked():
            self._available(project_id)
            old = self.store.get("project_lease", task_id)
            if old:
                if (old["project_id"] == project_id and old["conversation_id"] == conversation_id
                        and old["status"] == "ACTIVE"):
                    return old
                raise ValueError("此任务标识已被使用")
            if self._has_unresolved(project_id, conversation_id):
                raise ValueError("此会话有运行中或状态未确认的任务")
            self._ensure_session(project_id, conversation_id)
            lease = dict(task_id=task_id, project_id=project_id, conversation_id=conversation_id, status="ACTIVE")
            self._directory(project_id, ["tasks", task_id, "scratch"], create=True)
            self.store.create("project_lease", task_id, lease)
            return lease

    def finish_task(self, task_id, drained=True):
        task_id = _identity(task_id)
        with self._locked():
            lease = self.store.get("project_lease", task_id)
            if not lease:
                raise ValueError("未找到任务租约")
            if lease["status"] == "FINISHED":
                return lease
            if not drained:
                lease["status"] = "UNKNOWN"
            else:
                self._clear_directory(lease["project_id"], ["tasks", task_id, "scratch"])
                lease["status"] = "FINISHED"
            self.store.save("project_lease", task_id, lease)
            return lease

    def cleanup(self, project_id, body):
        project_id = _identity(project_id)
        if not isinstance(body, dict) or body.get("target") not in ("scratch", "workspace"):
            raise ValueError("清理范围必须为 scratch 或 workspace")
        target, conversation_id = body["target"], body.get("conversation_id")
        if conversation_id is not None:
            _identity(conversation_id)
        with self._locked():
            self.get(project_id)
            if self._has_unresolved(project_id, conversation_id if target == "workspace" else None):
                raise ValueError("项目中有运行中或状态未确认的任务，无法清理")
            removed = 0
            if target == "scratch":
                for lease in self.task_scopes(project_id):
                    if conversation_id is None or lease["conversation_id"] == conversation_id:
                        removed += self._clear_directory(project_id, ["tasks", lease["task_id"], "scratch"])
            elif conversation_id is not None:
                record = self.session(conversation_id)
                if not record or record["project_id"] != project_id:
                    raise ValueError("项目会话不存在")
                removed += self._clear_directory(project_id, ["sessions", conversation_id, "workspace"])
            else:
                rows = self.store.db.execute(
                    "SELECT data FROM documents WHERE kind='project_session' "
                    "AND json_extract(data, '$.project_id')=?", (project_id,)).fetchall()
                for row in rows:
                    record = json.loads(row[0])
                    removed += self._clear_directory(project_id, ["sessions", record["id"], "workspace"])
            return dict(project_id=project_id, target=target, conversation_id=conversation_id, removed=removed)

    def _directory(self, project_id, parts, create=False):
        path = self.root
        _checked(path, directory=True)
        for part in [project_id, *parts]:
            path = path / part
            if create:
                path.mkdir(exist_ok=True)
            _checked(path, directory=True)
        return path

    def _clear_directory(self, project_id, parts):
        try:
            directory = self._directory(project_id, parts)
        except FileNotFoundError:
            return 0
        resolved = directory.resolve(strict=True)
        root = self.root.resolve(strict=True)
        if resolved == root or not resolved.is_relative_to(root):
            raise ValueError("清理位置超出平台管理的项目目录")
        # Inspect the entire tree before deleting anything; do not follow links.
        for root, dirs, files in os.walk(directory, topdown=True, followlinks=False):
            for name in dirs:
                _checked(Path(root) / name, directory=True)
            for name in files:
                _checked(Path(root) / name, directory=False)
        children = list(directory.iterdir())
        for child in children:
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
        return len(children)

    def _base(self, project_id, conversation_id, area, create=False):
        self.get(project_id)
        if area == "data":
            return self._directory(project_id, ["data"], create=create)
        if area != "workspace":
            raise ValueError("文件区域必须为 data 或 workspace")
        if conversation_id is None:
            raise ValueError("访问会话工作区需要指定会话")
        record = self.session(conversation_id)
        if not record or record["project_id"] != project_id:
            raise ValueError("项目会话不存在")
        return self._directory(project_id, ["sessions", conversation_id, "workspace"], create=create)

    def _resolve(self, project_id, conversation_id, area, path, *, create_parents=False, allow_root=False):
        parts = _parts(path)
        if not parts and not allow_root:
            raise ValueError("请指定文件路径")
        current = self._base(_identity(project_id), conversation_id, area, create=create_parents)
        for part in parts[:-1]:
            current = current / part
            if create_parents:
                current.mkdir(exist_ok=True)
            _checked(current, directory=True)
        if parts:
            current = current / parts[-1]
        if current.exists() or current.is_symlink():
            _checked(current)
        return current

    def list_files(self, project_id, cid=None, area="data", path=""):
        directory = self._resolve(project_id, cid, area, path, allow_root=True)
        _checked(directory, directory=True)
        entries = []
        with os.scandir(directory) as scan:
            for item in scan:
                if len(entries) == _MAX_ENTRIES:
                    return dict(area=area, path=path, entries=entries, truncated=True)
                st = _checked(Path(item.path))
                name = item.name
                _parts(name)
                entry = dict(name=name, path="/".join([*_parts(path), name]),
                             type="directory" if stat.S_ISDIR(st.st_mode) else "file")
                if entry["type"] == "file":
                    entry["size"] = st.st_size
                entries.append(entry)
        return dict(area=area, path=path, entries=entries, truncated=False)

    def file_path(self, project_id, cid, area, path):
        result = self._resolve(project_id, cid, area, path)
        _checked(result, directory=False)
        return result

    def read_file(self, project_id, cid, area, path, offset=0, limit=_MAX_READ):
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= _MAX_READ:
            raise ValueError("读取范围无效")
        file = self.file_path(project_id, cid, area, path)
        with file.open("rb") as stream:
            stream.seek(offset)
            # Retain enough lookahead to finish a UTF-8 character and detect
            # common binary files even for a very small requested page.
            raw = stream.read(max(limit + 4, 4096))
        if b"\x00" in raw:
            raise ValueError("文件不是 UTF-8 文本，请使用适合该文件类型的工具")
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            if exc.reason != "unexpected end of data" or exc.end != len(raw):
                raise ValueError("文件不是 UTF-8 文本，请使用适合该文件类型的工具") from exc
        chunk = raw[:limit]
        try:
            content = chunk.decode("utf-8")
        except UnicodeDecodeError as exc:
            if exc.reason != "unexpected end of data" or exc.end != len(chunk):
                raise ValueError("文件不是 UTF-8 文本或读取位置不在字符边界") from exc
            for end in range(len(chunk) + 1, min(len(raw), len(chunk) + 3) + 1):
                try:
                    content = raw[:end].decode("utf-8")
                    chunk = raw[:end]
                    break
                except UnicodeDecodeError as tail:
                    if tail.reason != "unexpected end of data" or tail.end != end:
                        raise ValueError("文件不是 UTF-8 文本，请使用适合该文件类型的工具") from tail
            else:
                raise ValueError("文件不是完整的 UTF-8 文本")
        return dict(area=area, path=path, content=content,
                    offset=offset, next_offset=offset + len(chunk), eof=len(chunk) == len(raw))

    def write_file(self, project_id, cid, area, path, content):
        if not isinstance(content, str):
            raise ValueError("文件内容必须是文本")
        return self.put_file(project_id, cid, area, path, content.encode("utf-8"))

    def delete_file(self, project_id, cid, area, path):
        with self._locked():
            self._available(project_id)
            self.require_idle(project_id, None if area == "data" else cid)
            file = self.file_path(project_id, cid, area, path)
            file.unlink()
            return dict(area=area, path=path, deleted=True)

    def put_file(self, project_id, cid, area, path, data):
        if not isinstance(data, bytes) or len(data) > _MAX_FILE:
            raise ValueError("文件无效或超过 16 MiB")
        with self._locked():
            self._available(project_id)
            if area == "data" and self._has_unresolved(project_id):
                raise ValueError("项目中有运行中或状态未确认的任务，无法修改共享文件")
            file = self._resolve(project_id, cid, area, path, create_parents=True)
            if file.exists() or file.is_symlink():
                _checked(file, directory=False)
            fd, temp_name = tempfile.mkstemp(prefix=".upload-", dir=file.parent)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(data)
                if file.exists() or file.is_symlink():
                    _checked(file, directory=False)
                os.replace(temp_name, file)
            finally:
                if os.path.exists(temp_name):
                    os.unlink(temp_name)
            return dict(area=area, path=path, size=len(data))
