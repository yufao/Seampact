"""Frozen, paginated source evidence. Source imports are never executed."""

import ast
import hashlib
import json
from pathlib import Path, PurePosixPath

EXCLUDED = {".git", ".venv", "node_modules", "__pycache__", "var", ".env"}
TEXT_SUFFIXES = {".py", ".ipynb", ".md", ".toml", ".yaml", ".yml", ".co", ".json", ".txt",
                 ".sh", ".bash", ".cfg", ".ini", ".csv", ".tsv"}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def safe_path(root, relative):
    p = PurePosixPath(relative.replace("\\", "/"))
    if p.is_absolute() or not p.parts or ".." in p.parts or ":" in relative:
        raise ValueError("relative source path required")
    if any(part in EXCLUDED or part.startswith(".env") or "credential" in part.lower()
           or part.lower().endswith((".pem", ".key")) for part in p.parts):
        raise ValueError("configuration secrets and excluded directories are not source evidence")
    root = Path(root).resolve()
    result = root.joinpath(*p.parts).resolve()
    if not result.is_relative_to(root):
        raise ValueError("path or symlink leaves the registered root")
    return result


def units(path, data):
    text = data.decode("utf-8-sig")
    if path.endswith(".ipynb"):
        book = json.loads(text)
        return [(f"cell:{i}", "".join(cell.get("source", [])))
                for i, cell in enumerate(book.get("cells", []))
                if cell.get("cell_type") == "code"]
    return [("file", text)]


def symbols(path, data):
    result, unknown = [], []
    for unit, source in units(path, data):
        if not path.endswith((".py", ".ipynb")):
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError as e:
            unknown.append(dict(unit=unit, kind="PARSE_UNSUPPORTED", line=e.lineno))
            continue

        def walk(parent, prefix=""):
            for node in ast.iter_child_nodes(parent):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    name = prefix + node.name
                    if not isinstance(node, ast.ClassDef):
                        # Nested function scopes are distinguished, not mistaken for exports.
                        result.append(dict(symbol=name, unit=unit, line=node.lineno,
                            end_line=node.end_lineno, asynchronous=isinstance(node, ast.AsyncFunctionDef),
                            decorators=[ast.unparse(d) for d in node.decorator_list],
                            generator=any(isinstance(n, (ast.Yield, ast.YieldFrom)) for n in ast.walk(node)),
                            calls=sorted({ast.unparse(n.func) for n in ast.walk(node) if isinstance(n, ast.Call)})))
                    walk(node, name + ".")
                else:
                    walk(node, prefix)
        walk(tree)
    return dict(symbols=result, unknown=unknown, closure="UNKNOWN")


class Inspector:
    def __init__(self, root, snapshots, request):
        self.root = Path(root).resolve()
        self.snapshots = Path(snapshots)
        self.request = request
        self.files = {f["path"]: f for f in request["files"]}

    @classmethod
    def freeze(cls, root, snapshots, identity, paths, entry, observation):
        if not entry or not observation:
            raise ValueError("entry and pre-analysis observation contract required")
        snapshots = Path(snapshots)
        snapshots.mkdir(parents=True, exist_ok=True)
        records = []
        for relative in dict.fromkeys(paths):
            path = safe_path(root, relative)
            if path.suffix not in TEXT_SUFFIXES or not path.is_file():
                raise ValueError(f"not a supported source artifact: {relative}")
            data = path.read_bytes()
            sha = digest(data)
            destination = snapshots / sha
            if not destination.exists():
                with destination.open("xb") as out:
                    out.write(data)
            records.append(dict(path=relative.replace("\\", "/"), sha256=sha, bytes=len(data)))
        request = dict(schema="integration-request/1", id=identity, entry=entry,
                       observation=observation, files=records, dependency_closure="UNKNOWN",
                       resources=[], missing=["native run", "dependency closure", "owner/effect summaries"])
        request["source_version"] = digest(json.dumps(records, sort_keys=True).encode())
        return cls(root, snapshots, request)

    def list_source(self, offset=0, count=100):
        if type(offset) is not int or offset < 0 or type(count) is not int or count < 1:
            raise ValueError("positive page size and nonnegative offset required")
        values = list(self.files.values())
        return dict(files=values[offset:offset+count], total=len(values),
                    next_offset=offset+count if offset+count < len(values) else None,
                    dependency_closure="UNKNOWN")

    def data(self, path):
        record = self.files[path]
        data = (self.snapshots / record["sha256"]).read_bytes()
        if digest(data) != record["sha256"]:
            raise ValueError("frozen source digest changed")
        return data

    def read_file(self, path, unit="file", start=1, count=200):
        if type(start) is not int or start < 1 or type(count) is not int or count < 1:
            raise ValueError("positive line start and page size required")
        available = dict(units(path, self.data(path)))
        if unit not in available:
            return dict(path=path, units=list(available), requested_unit=unit)
        lines = available[unit].splitlines()
        return dict(path=path, unit=unit, sha256=self.files[path]["sha256"], start=start,
                    lines=lines[start-1:start-1+count], total_lines=len(lines),
                    next_line=start+count if start-1+count < len(lines) else None)

    def inspect_symbols(self, path):
        return dict(path=path, sha256=self.files[path]["sha256"], **symbols(path, self.data(path)))

    def add_source(self, path):
        """Explicit dependency extension makes a new version; never silently mutates a freeze."""
        data = safe_path(self.root, path).read_bytes()
        if Path(path).suffix not in TEXT_SUFFIXES:
            raise ValueError("unsupported source type")
        sha = digest(data)
        dest = self.snapshots / sha
        if not dest.exists():
            with dest.open("xb") as out:
                out.write(data)
        record = dict(path=path.replace("\\", "/"), sha256=sha, bytes=len(data))
        if record["path"] in self.files:
            raise ValueError("already frozen; use the existing immutable artifact")
        self.request["files"].append(record)
        self.files[record["path"]] = record
        self.request["source_version"] = digest(json.dumps(self.request["files"], sort_keys=True).encode())
        return record
