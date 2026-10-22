"""Frozen, paginated source evidence. Source imports are never executed."""

import ast
import codecs
import configparser
import hashlib
import io
import json
import os
import re
import sys
import tokenize
from pathlib import Path, PurePosixPath

try:
    import tomllib
except ModuleNotFoundError:
    try:
        import tomli as tomllib
    except ModuleNotFoundError:
        tomllib = None


SUPPORTED_PYTHON_VERSIONS = ((3, 10), (3, 11), (3, 12), (3, 13))


def python_runtime_issue(version=None):
    version = tuple(sys.version_info[:2] if version is None else version)
    if version not in SUPPORTED_PYTHON_VERSIONS:
        return ("finite native profile covers Python 3.10–3.13; current interpreter is " +
                ".".join(map(str, version)))
    return None


def native_syntax_issues(tree):
    """New interpreters do not grant new scope or binding rules implicitly."""
    result = []
    for node in ast.walk(tree):
        if type(node).__name__ == "TypeAlias" or getattr(node, "type_params", None):
            result.append(dict(kind="NATIVE_SYNTAX_UNSUPPORTED", line=getattr(node, "lineno", None),
                node=type(node).__name__, reason="type aliases and type-parameter scopes need a finite native binding rule"))
    return result


def native_ast(source, filename="<unknown>"):
    issue = python_runtime_issue()
    if issue:
        raise ValueError(issue)
    tree = ast.parse(source, filename=filename)
    issues = native_syntax_issues(tree)
    if issues:
        issue = issues[0]
        raise ValueError("native syntax unsupported at line " + str(issue["line"]) + ": " + issue["reason"])
    return tree


EXCLUDED = {".git", ".venv", "venv", "node_modules", "__pycache__", ".env", ".local-private",
            ".cache", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", ".aws", ".ssh"}
SOURCE_SUFFIXES = {".py", ".pyi", ".ipynb", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx",
                   ".java", ".kt", ".kts", ".go", ".rs", ".c", ".cc", ".cpp", ".h", ".hpp",
                   ".cs", ".rb", ".php", ".lua", ".r", ".jl", ".sh", ".bash", ".zsh",
                   ".ps1", ".bat", ".cmd", ".sql", ".co", ".html", ".css", ".scss", ".vue"}
CONFIG_SUFFIXES = {".toml", ".yaml", ".yml", ".json", ".cfg", ".ini", ".xml", ".conf", ".properties"}
TEXT_SUFFIXES = SOURCE_SUFFIXES | CONFIG_SUFFIXES | {".md", ".rst", ".txt", ".csv", ".tsv", ".jsonl", ".log"}
MODEL_SUFFIXES = {".safetensors", ".pt", ".pth", ".ckpt", ".onnx", ".pb", ".h5", ".hdf5", ".gguf"}
DATA_SUFFIXES = {".npy", ".npz", ".pkl", ".pickle", ".parquet", ".arrow", ".feather", ".avro",
                 ".db", ".sqlite", ".sqlite3", ".dat"}
BINARY_SUFFIXES = MODEL_SUFFIXES | DATA_SUFFIXES | {
    ".bin", ".zip", ".tar", ".gz", ".7z", ".bz2", ".xz", ".rar", ".png", ".jpg", ".jpeg",
    ".gif", ".bmp", ".tif", ".tiff", ".webp", ".ico", ".pdf", ".docx", ".xlsx", ".pptx",
    ".mp3", ".wav", ".flac", ".aac", ".ogg", ".mp4", ".avi", ".mov", ".mkv", ".webm",
    ".so", ".dll", ".exe", ".class", ".pyc", ".o", ".a", ".wasm", ".ttf", ".woff", ".woff2"}


def excluded(parts):
    return any(part.lower() in EXCLUDED or part.lower().startswith(".env")
        or "credential" in part.lower() or part.lower().endswith((".pem", ".key"))
        or part.lower() in {"id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"} for part in parts)


def decode_text(path, data):
    """Decode source without evaluating it; preserve its bytes in the snapshot."""
    if data.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        encoding = "utf-32"
    elif data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        encoding = "utf-16"
    else:
        if b"\x00" in data:
            raise ValueError("binary resource is not readable source")
        encoding = "utf-8-sig"
        if str(path).lower().endswith((".py", ".pyi")):
            try:
                encoding, _ = tokenize.detect_encoding(io.BytesIO(data).readline)
            except SyntaxError:
                # An invalid coding cookie is still source text for inspection.
                pass
    try:
        text = data.decode(encoding)
    except UnicodeDecodeError:
        if Path(path).suffix.lower() not in TEXT_SUFFIXES:
            raise
        try:
            text, encoding = data.decode("gb18030"), "gb18030"
        except UnicodeDecodeError:
            text, encoding = data.decode("cp1252"), "cp1252"
    if re.search(r"[\x00-\x08\x0b\x0e-\x1f]", text):
        raise ValueError("binary resource is not readable source")
    return text, encoding


def _authentication_field(name):
    value = re.sub(r"[^a-z0-9]", "", name.lower()) if isinstance(name, str) else ""
    return value in {"password", "passwd", "authorization", "clientsecret", "secretkey", "hf_token",
                     "hftoken", "bearertoken", "refreshtoken"} or value.endswith(("apikey", "accesstoken", "authtoken"))


def _placeholder(value):
    value = value.strip().lower()
    compact = re.sub(r"[\s_\-<>\[\]{}.]", "", value)
    if not value or compact in {"empty", "none", "null", "dummy", "test", "testing", "example", "placeholder",
                   "changeme", "notrequired", "notneeded", "unused", "apikey", "yourkey", "yourapikey",
                   "yourtoken", "yourpassword", "yourapikeyhere", "yourkeyhere", "replaceme", "replacewithyourapikey"}:
        return True
    return bool(re.fullmatch(r"(?:sk[-_])?(?:x{3,}|\*{3,}|\.\.\.)", value))


def withheld_source(path, data):
    """Finite literal-credential guard; it is not a general secret-flow proof."""
    text, _ = decode_text(path, data)
    positions = []
    def record(unit, line, field, value):
        if isinstance(value, str) and _authentication_field(field) and not _placeholder(value):
            positions.append(dict(path=path, unit=unit, line=line, status="WITHHELD",
                                  reason="explicit authentication field contains a non-placeholder string literal"))
    def inspect(unit, source, python=False):
        if python:
            try:
                tree = ast.parse(source)
            except SyntaxError:
                tree = None
            if tree:
                def literal(field, node):
                    if isinstance(node, ast.Constant):
                        record(unit, node.lineno, field, node.value)
                for node in ast.walk(tree):
                    if isinstance(node, (ast.Assign, ast.AnnAssign)):
                        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                        for target in targets:
                            if isinstance(target, (ast.Name, ast.Attribute)):
                                literal(target.id if isinstance(target, ast.Name) else target.attr, node.value)
                            elif isinstance(target, ast.Subscript) and isinstance(target.slice, ast.Constant):
                                literal(target.slice.value, node.value)
                    elif isinstance(node, ast.keyword):
                        literal(node.arg, node.value)
                    elif isinstance(node, ast.Dict):
                        for key, value in zip(node.keys, node.values):
                            if isinstance(key, ast.Constant):
                                literal(key.value, value)
                    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                        args = [*node.args.posonlyargs, *node.args.args]
                        for arg, value in zip(args[len(args) - len(node.args.defaults):], node.args.defaults):
                            literal(arg.arg, value)
                        for arg, value in zip(node.args.kwonlyargs, node.args.kw_defaults):
                            literal(arg.arg, value)
        # Covers explicit quoted fields in non-Python files and literal comments.
        pattern = r'''(?<![\w])(?P<field>[A-Za-z_][\w-]*)['"]?\s*[:=]\s*(?:"(?P<double>(?:\\.|[^"\\\r\n])*)"|'(?P<single>(?:\\.|[^'\\\r\n])*)')'''
        dynamic_tail = re.compile(r"\s*(?:\+|%|\.(?:format|join)\s*\()")
        for match in re.finditer(pattern, source):
            if not _authentication_field(match["field"]):
                continue
            value = match["double"] if match["double"] is not None else match["single"]
            if _placeholder(value) or dynamic_tail.match(source, match.end()):
                continue
            record(unit, source.count("\n", 0, match.start()) + 1, match["field"],
                   value)
    inspect("file", text, str(path).lower().endswith((".py", ".pyi")))
    if str(path).lower().endswith(".ipynb"):
        try:
            book = json.loads(text)
            for index, cell in enumerate(book.get("cells", [])):
                source = cell.get("source", [])
                source = "".join(source) if isinstance(source, list) else source
                if isinstance(source, str):
                    inspect(f"cell:{index}", source, cell.get("cell_type") == "code")
        except (ValueError, TypeError, AttributeError):
            pass
    return sorted({(item["unit"], item["line"]): item for item in positions}.values(),
                  key=lambda item: (item["unit"], item["line"]))


class SourceWithheld(ValueError):
    def __init__(self, path, positions):
        super().__init__("source text withheld: explicit authentication literal must be removed from analysis scope")
        self.report = dict(path=path, status="WITHHELD", positions=positions,
                           reason="explicit authentication literal; original file and historical snapshot remain unchanged")


def file_kind(path):
    suffix = Path(path).suffix.lower()
    if suffix in MODEL_SUFFIXES:
        return "model"
    if suffix in DATA_SUFFIXES:
        return "data"
    if suffix in BINARY_SUFFIXES:
        return "binary"
    if suffix in SOURCE_SUFFIXES or Path(path).name in {"Dockerfile", "Makefile", "CMakeLists.txt"}:
        return "source"
    if suffix in CONFIG_SUFFIXES:
        return "config"
    return "text"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def safe_path(root, relative):
    p = PurePosixPath(relative.replace("\\", "/"))
    if p.is_absolute() or not p.parts or ".." in p.parts or ":" in relative:
        raise ValueError("relative source path required")
    if excluded(p.parts):
        raise ValueError("configuration secrets and excluded directories are not source evidence")
    root = Path(root).resolve()
    result = root.joinpath(*p.parts).resolve()
    if not result.is_relative_to(root):
        raise ValueError("path or symlink leaves the registered root")
    return result


def units(path, data):
    text, _ = decode_text(path, data)
    if path.lower().endswith(".ipynb"):
        book = json.loads(text)
        return [(f"cell:{i}", "".join(cell.get("source", [])))
                for i, cell in enumerate(book.get("cells", []))
                if cell.get("cell_type") == "code"]
    return [("file", text)]


def symbols(path, data):
    positions = withheld_source(path, data)
    if positions:
        return dict(symbols=[], unknown=positions, closure="UNKNOWN", status="WITHHELD")
    result, unknown = [], []
    for unit, source in units(path, data):
        if not path.lower().endswith((".py", ".ipynb")):
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError as e:
            unknown.append(dict(unit=unit, kind="PARSE_UNSUPPORTED", line=e.lineno,
                                python=".".join(map(str, sys.version_info[:2]))))
            continue
        unknown.extend(dict(unit=unit, **issue) for issue in native_syntax_issues(tree))

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


def source_facts(path, data):
    """Static imports and entry anchors are evidence candidates, not bindings."""
    positions = withheld_source(path, data)
    if positions:
        return dict(imports=[], entry_candidates=[], unknown=positions,
                    dependency_closure="UNKNOWN", status="WITHHELD")
    imports, entries, unknown = [], [], []
    suffix = Path(path).suffix.lower()
    try:
        available = units(path, data)
    except (ValueError, TypeError, AttributeError, SyntaxError, UnicodeError):
        return dict(imports=[], entry_candidates=[],
            unknown=[dict(path=path, kind="SOURCE_STRUCTURE_PARSE_UNSUPPORTED")], dependency_closure="UNKNOWN")
    for unit, source in available:
        if suffix in {".py", ".pyi", ".ipynb"}:
            try:
                tree = ast.parse(source)
            except SyntaxError as error:
                unknown.append(dict(path=path, unit=unit, kind="PARSE_UNSUPPORTED", line=error.lineno,
                                    python=".".join(map(str, sys.version_info[:2]))))
                continue
            unknown.extend(dict(path=path, unit=unit, **issue) for issue in native_syntax_issues(tree))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for name in node.names:
                        imports.append(dict(path=path, unit=unit, line=node.lineno, module=name.name,
                                            names=[], level=0, kind="import"))
                elif isinstance(node, ast.ImportFrom):
                    imports.append(dict(path=path, unit=unit, line=node.lineno, module=node.module,
                        names=[name.name for name in node.names], level=node.level, kind="from"))
                elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "__import__":
                    unknown.append(dict(path=path, unit=unit, line=node.lineno, kind="DYNAMIC_IMPORT"))
            for node in tree.body:
                if isinstance(node, ast.If) and isinstance(node.test, ast.Compare):
                    expression = node.test
                    parts = [expression.left, *expression.comparators]
                    if (len(parts) == 2 and len(expression.ops) == 1 and isinstance(expression.ops[0], ast.Eq)
                        and any(isinstance(item, ast.Name) and item.id == "__name__" for item in parts)
                        and any(isinstance(item, ast.Constant) and item.value == "__main__" for item in parts)):
                        entries.append(dict(path=path, unit=unit, line=node.lineno, kind="python_main_guard", symbol="__main__"))
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "main":
                    entries.append(dict(path=path, unit=unit, line=node.lineno, kind="main_callable", symbol=node.name))
                elif isinstance(node, ast.Expr):
                    call = node.value.value if isinstance(node.value, ast.Await) else node.value
                    if isinstance(call, ast.Call):
                        entries.append(dict(path=path, unit=unit, line=node.lineno, kind="top_level_call", symbol=ast.unparse(call.func)))
            if Path(path).name == "__main__.py":
                entries.append(dict(path=path, unit=unit, line=1, kind="python_module_entry", symbol="__main__"))
        else:
            patterns = []
            if suffix in {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".vue"}:
                patterns = [r"\b(?:import|export)\s+(?:[^;\n]*?\s+from\s+)?['\"]([^'\"]+)['\"]",
                            r"\brequire\s*\(\s*['\"]([^'\"]+)['\"]\s*\)"]
            elif suffix in {".c", ".cc", ".cpp", ".h", ".hpp"}:
                patterns = [r"^\s*#\s*include\s*[<\"]([^>\"]+)[>\"]"]
            elif suffix in {".java", ".kt", ".kts", ".cs"}:
                patterns = [r"^\s*(?:import|using)\s+(?:static\s+)?([\w.*]+)"]
            elif suffix in {".sh", ".bash", ".zsh"}:
                patterns = [r"^\s*(?:source|\.)\s+['\"]?([^\s'\";]+)"]
            for pattern in patterns:
                for match in re.finditer(pattern, source, re.MULTILINE):
                    imports.append(dict(path=path, unit=unit, line=source.count("\n", 0, match.start()) + 1,
                                        module=match.group(1), names=[], level=0, kind="textual_static_import"))
            if suffix in SOURCE_SUFFIXES and not patterns:
                unknown.append(dict(path=path, unit=unit, kind="STATIC_IMPORT_PARSER_UNSUPPORTED"))
            if suffix in {".sh", ".bash", ".zsh", ".ps1", ".bat", ".cmd"}:
                entries.append(dict(path=path, unit=unit, line=1, kind="script", symbol=None))
            try:
                if Path(path).name == "pyproject.toml":
                    if tomllib is None:
                        unknown.append(dict(path=path, unit=unit, kind="TOML_PARSER_MISSING",
                            reason="Python 3.10 TOML inspection requires tomli in the target environment"))
                        continue
                    project = tomllib.loads(source)
                    tables = [project.get("project", {}).get("scripts", {}),
                              project.get("project", {}).get("gui-scripts", {}),
                              project.get("tool", {}).get("poetry", {}).get("scripts", {})]
                    for table in tables:
                        for name, command in table.items():
                            entries.append(dict(path=path, unit=unit, kind="declared_script", name=name, symbol=command))
                elif Path(path).name == "package.json":
                    package = json.loads(source)
                    for name, command in package.get("scripts", {}).items():
                        entries.append(dict(path=path, unit=unit, kind="declared_script", name=name, symbol=command))
                    if package.get("bin"):
                        entries.append(dict(path=path, unit=unit, kind="declared_binary_entry", symbol=package["bin"]))
                elif Path(path).name == "setup.cfg":
                    config = configparser.ConfigParser(interpolation=None)
                    config.read_string(source)
                    if config.has_section("options.entry_points"):
                        for group, commands in config.items("options.entry_points"):
                            entries.extend(dict(path=path, unit=unit, kind="declared_script", group=group, symbol=command.strip())
                                           for command in commands.splitlines() if command.strip())
            except (ValueError, TypeError, AttributeError, configparser.Error):
                unknown.append(dict(path=path, unit=unit, kind="ENTRY_DECLARATION_PARSE_UNSUPPORTED"))
            if Path(path).name == "Dockerfile":
                for line, command in enumerate(source.splitlines(), 1):
                    if re.match(r"\s*(?:ENTRYPOINT|CMD)\s", command, re.IGNORECASE):
                        entries.append(dict(path=path, unit=unit, line=line, kind="container_entry", symbol=command.strip()))
    return dict(imports=imports, entry_candidates=entries, unknown=unknown, dependency_closure="UNKNOWN")


def inventory_repository(root, excluded_roots=()):
    """Inventory every non-excluded path; only decodable text enters files."""
    root = Path(root).resolve()
    excluded_roots = {Path(path).resolve() for path in excluded_roots if Path(path).resolve() != root}
    inventory, files, resources, entries = [], [], [], []
    def unreadable(error):
        raise ValueError("repository directory is unreadable") from error
    for directory, children, names in os.walk(root, onerror=unreadable):
        base = Path(directory)
        children[:] = sorted(name for name in children
            if not excluded((name,)) and (base / name).resolve() not in excluded_roots)
        for name in list(children):
            path = base / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                children.remove(name)
                inventory.append(dict(path=relative, kind="symlink", readable=False, bytes=path.lstat().st_size))
            else:
                inventory.append(dict(path=relative, kind="directory", readable=False, bytes=None))
        for name in sorted(names):
            if excluded((name,)):
                continue
            path = base / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink() or not path.resolve().is_relative_to(root) or not path.is_file():
                inventory.append(dict(path=relative, kind="symlink" if path.is_symlink() else "special",
                                      readable=False, bytes=path.lstat().st_size))
                continue
            record = dict(path=relative, kind=file_kind(relative), bytes=path.stat().st_size, readable=False)
            if path.suffix.lower() not in BINARY_SUFFIXES:
                data = path.read_bytes()
                try:
                    _, encoding = decode_text(relative, data)
                except (UnicodeError, ValueError, SyntaxError):
                    record["kind"] = "binary"
                else:
                    positions = withheld_source(relative, data)
                    if positions:
                        record.update(status="WITHHELD", positions=positions,
                                      reason="explicit authentication literal; source text excluded")
                    else:
                        record.update(readable=True, encoding=encoding)
                        files.append(relative)
                        try:
                            entries.extend(source_facts(relative, data)["entry_candidates"])
                        except (ValueError, TypeError, AttributeError):
                            record["structure"] = "PARSE_UNSUPPORTED"
            inventory.append(record)
            if not record["readable"]:
                resources.append({key: record[key] for key in ("path", "kind", "bytes", "status", "positions", "reason")
                                  if key in record})
    return dict(files=files, inventory=inventory, resources=resources, entry_candidates=entries)


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
        records, withheld = [], []
        for relative in dict.fromkeys(paths):
            path = safe_path(root, relative)
            if path.suffix.lower() in BINARY_SUFFIXES or not path.is_file():
                raise ValueError(f"not a supported source artifact: {relative}")
            data = path.read_bytes()
            decode_text(relative, data)
            positions = withheld_source(relative, data)
            if positions:
                withheld.append(dict(path=relative.replace("\\", "/"), status="WITHHELD", positions=positions,
                                     reason="explicit authentication literal; source text excluded"))
                continue
            sha = digest(data)
            destination = snapshots / sha
            if not destination.exists():
                with destination.open("xb") as out:
                    out.write(data)
            records.append(dict(path=relative.replace("\\", "/"), sha256=sha, bytes=len(data)))
        request = dict(schema="integration-request/1", id=identity, entry=entry,
                       observation=observation, files=records, dependency_closure="UNKNOWN",
                       resources=[], withheld_sources=withheld,
                       missing=["native run", "dependency closure", "owner/effect summaries"])
        versioned = dict(files=records, withheld_sources=withheld) if withheld else records
        request["source_version"] = digest(json.dumps(versioned, sort_keys=True).encode())
        return cls(root, snapshots, request)

    @classmethod
    def freeze_repository(cls, source, snapshots, identity, observation):
        """Freeze the entire text scope produced by RepositoryIntake.prepare."""
        inspector = cls.freeze(source["root"], snapshots, identity, source["files"], source["entry"], observation)
        inspector.request.update(source_id=source["id"], source_name=source["name"],
            source_address=source["address"], source_revision=source.get("revision"),
            inventory=source["inventory"], resources=source.get("resources", []),
            entry_profile=source.get("entry_profile", {}), configuration=source.get("configuration", {}),
            user_resources=source.get("user_resources", []))
        withheld = {item["path"]: item for item in source["inventory"] if item.get("status") == "WITHHELD"}
        withheld.update({item["path"]: item for item in inspector.request["withheld_sources"]})
        if withheld:
            inspector.request["withheld_sources"] = list(withheld.values())
            inspector.request["inventory"] = [{**item, **withheld[item["path"]], "readable": False}
                if item["path"] in withheld else item for item in source["inventory"]]
            if isinstance(inspector.request["entry"], dict) and isinstance(inspector.request["entry"].get("candidates"), list):
                inspector.request["entry"] = dict(inspector.request["entry"], candidates=[item for item in
                    inspector.request["entry"]["candidates"] if item.get("path") not in withheld])
            inspector.request["source_version"] = digest(json.dumps(dict(files=inspector.request["files"],
                withheld_sources=inspector.request["withheld_sources"]), sort_keys=True).encode())
        return inspector

    def list_source(self, offset=0, count=100):
        if type(offset) is not int or offset < 0 or type(count) is not int or count < 1:
            raise ValueError("positive page size and nonnegative offset required")
        values = list(self.files.values())
        return dict(files=values[offset:offset+count], total=len(values),
                    next_offset=offset+count if offset+count < len(values) else None,
                    dependency_closure="UNKNOWN")

    def data(self, path):
        blocked = next((item for item in self.request.get("withheld_sources", []) if item["path"] == path), None)
        if blocked:
            raise SourceWithheld(path, blocked["positions"])
        record = self.files[path]
        data = (self.snapshots / record["sha256"]).read_bytes()
        if digest(data) != record["sha256"]:
            raise ValueError("frozen source digest changed")
        positions = withheld_source(path, data)
        if positions:
            raise SourceWithheld(path, positions)
        return data

    def read_file(self, path, unit="file", start=1, count=200):
        if type(start) is not int or start < 1 or type(count) is not int or count < 1:
            raise ValueError("positive line start and page size required")
        try:
            available = dict(units(path, self.data(path)))
        except SourceWithheld as error:
            return error.report
        if unit not in available:
            return dict(path=path, units=list(available), requested_unit=unit)
        lines = available[unit].splitlines()
        return dict(path=path, unit=unit, sha256=self.files[path]["sha256"], start=start,
                    lines=lines[start-1:start-1+count], total_lines=len(lines),
                    next_line=start+count if start-1+count < len(lines) else None)

    def inspect_symbols(self, path):
        if getattr(self, "target_runtime", None):
            return self.target_runtime.call("inspect_symbols", path=path)
        try:
            return dict(path=path, sha256=self.files[path]["sha256"], **symbols(path, self.data(path)))
        except SourceWithheld as error:
            return error.report

    def inspect_construction(self, path, unit="file"):
        """Original statement anchors and def-use names; no candidate code is evaluated."""
        if getattr(self, "target_runtime", None):
            return self.target_runtime.call("inspect_construction", path=path, unit=unit)
        try:
            available = dict(units(path, self.data(path)))
        except SourceWithheld as error:
            return error.report
        if unit not in available:
            raise ValueError("source unit is not frozen")
        tree = native_ast(available[unit])
        scopes = []
        for node in tree.body:
            functions = [(node.name, node)] if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) else (
                [(node.name + "." + member.name, member) for member in node.body
                 if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))] if isinstance(node, ast.ClassDef) else [])
            for symbol, function in functions:
                statements = []
                for statement in function.body:
                    calls = [dict(target=ast.unparse(call.func), line=call.lineno, column=call.col_offset,
                                  end_line=call.end_lineno, end_column=call.end_col_offset)
                             for call in ast.walk(statement) if isinstance(call, ast.Call)]
                    statements.append(dict(kind=type(statement).__name__, line=statement.lineno,
                        end_line=statement.end_lineno,
                        reads=sorted({name.id for name in ast.walk(statement) if isinstance(name, ast.Name) and isinstance(name.ctx, ast.Load)}),
                        writes=sorted({name.id for name in ast.walk(statement) if isinstance(name, ast.Name) and isinstance(name.ctx, ast.Store)}),
                        calls=calls))
                scopes.append(dict(symbol=symbol, asynchronous=isinstance(function, ast.AsyncFunctionDef),
                    parameters=[argument.arg for argument in [*function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs]],
                    statements=statements))
        return dict(path=path, unit=unit, sha256=self.files[path]["sha256"], scopes=scopes,
                    effects="UNKNOWN", alias_closure="UNKNOWN", execution="NOT_RUN")

    def list_structure(self, path="", recursive=True):
        """Return directory and file categories, without resource contents."""
        if not isinstance(path, str) or type(recursive) is not bool:
            raise ValueError("structure path and recursive flag required")
        prefix = path.replace("\\", "/").strip("/")
        if prefix:
            safe_path(self.root, prefix)
        inventory = self.request.get("inventory")
        if inventory is None:
            inventory = [dict(path=item["path"], kind=file_kind(item["path"]),
                              bytes=item["bytes"], readable=True) for item in self.files.values()]
        selected = []
        for item in inventory:
            relative = item["path"]
            if prefix and relative != prefix and not relative.startswith(prefix + "/"):
                continue
            remainder = relative[len(prefix):].lstrip("/") if prefix else relative
            if recursive or "/" not in remainder:
                selected.append(item)
        return dict(path=prefix, inventory=selected, total=len(selected), dependency_closure="UNKNOWN")

    def inspect_imports(self, path=None, unit=None):
        """Query static imports from frozen text; dynamic closure stays unknown."""
        if getattr(self, "target_runtime", None):
            return self.target_runtime.call("inspect_imports", path=path, unit=unit)
        paths = [path] if path is not None else list(self.files)
        imports, unknown = [], []
        for relative in paths:
            try:
                facts = source_facts(relative, self.data(relative))
            except SourceWithheld as error:
                unknown.extend(error.report["positions"])
                continue
            imports.extend(item for item in facts["imports"] if unit is None or item.get("unit") == unit)
            unknown.extend(item for item in facts["unknown"] if unit is None or item.get("unit") == unit)
        return dict(paths=paths, imports=imports, unknown=unknown, dependency_closure="UNKNOWN")

    def list_resources(self, path="", offset=0, count=100):
        if not isinstance(path, str) or type(offset) is not int or offset < 0 or type(count) is not int or count < 1:
            raise ValueError("resource path, nonnegative offset and positive page size required")
        prefix = path.replace("\\", "/")
        records = [resource for resource in self.request.get("resources", [])
                   if resource.get("path", "").replace("\\", "/").startswith(prefix)]
        return dict(resources=records[offset:offset + count], total=len(records),
                    next_offset=offset + count if offset + count < len(records) else None,
                    loading="NOT_RUN", compatibility="UNKNOWN")

    def entry_candidates(self, path=""):
        """Expose source entry anchors without selecting an execution host."""
        if getattr(self, "target_runtime", None):
            return self.target_runtime.call("entry_candidates", path=path)
        if not isinstance(path, str):
            raise ValueError("entry path prefix required")
        candidates, unknown = [], []
        for relative in self.files:
            if not relative.startswith(path):
                continue
            try:
                facts = source_facts(relative, self.data(relative))
            except SourceWithheld as error:
                unknown.extend(error.report["positions"])
                continue
            candidates.extend(facts["entry_candidates"])
            unknown.extend(facts["unknown"])
        return dict(path=path, candidates=candidates, unknown=unknown, selected=None, execution="NOT_RUN")

    def add_source(self, path):
        """Explicit dependency extension makes a new version; never silently mutates a freeze."""
        if path.replace("\\", "/") in self.files:
            raise ValueError("already frozen; use the existing immutable artifact")
        if Path(path).suffix.lower() in BINARY_SUFFIXES:
            raise ValueError("unsupported source type")
        data = safe_path(self.root, path).read_bytes()
        _, encoding = decode_text(path, data)
        positions = withheld_source(path, data)
        if positions:
            item = dict(path=path.replace("\\", "/"), status="WITHHELD", positions=positions,
                        reason="explicit authentication literal; source text excluded")
            previous = self.request.setdefault("withheld_sources", [])
            if item not in previous:
                previous.append(item)
            if "inventory" in self.request:
                self.request["inventory"] = [record for record in self.request["inventory"] if record["path"] != item["path"]]
                self.request["inventory"].append(dict(item, kind=file_kind(path), readable=False))
            self.request["source_version"] = digest(json.dumps(dict(files=self.request["files"],
                withheld_sources=previous), sort_keys=True).encode())
            return item
        sha = digest(data)
        dest = self.snapshots / sha
        if not dest.exists():
            with dest.open("xb") as out:
                out.write(data)
        record = dict(path=path.replace("\\", "/"), sha256=sha, bytes=len(data))
        self.request["files"].append(record)
        self.files[record["path"]] = record
        if "inventory" in self.request:
            self.request["inventory"].append(dict(path=record["path"], kind=file_kind(path),
                bytes=len(data), readable=True, encoding=encoding))
        self.request["source_version"] = digest(json.dumps(self.request["files"], sort_keys=True).encode())
        return record
