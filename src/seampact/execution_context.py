"""Finite source-backed process directories and explicit Python import roots."""

import ast
import json
import re
import shlex
from pathlib import Path, PurePosixPath

from .inspection import units


class ContextIssue(ValueError):
    def __init__(self, code, field, message, evidence=None):
        super().__init__(message)
        self.code, self.field_paths = code, [field]
        self.structured_issues = [dict(code=code, issue_code=code, field_paths=[field], reason=message,
                                       evidence=evidence or [])]


def relative_directory(value):
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise ValueError("working directory must be a repository-relative POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("working directory cannot leave the frozen repository")
    return path.as_posix()


def process_context(inspector, entry):
    """A local README invocation or Docker source-copy mapping proves cwd.

    Container paths are mapped back to repository source directories; a platform
    path supplied by a candidate is never accepted as an execution directory.
    """
    spec = entry.get("cwd")
    if spec is None:
        return dict(cwd=".", rule="repository-root-default/1", evidence=[], import_paths=[])
    def fail(message):
        raise ContextIssue("execution_context_unproven", "cwd", message)
    if not isinstance(spec, dict):
        fail("CLI cwd requires a relative path and frozen source evidence")
    try:
        directory = relative_directory(spec.get("path"))
    except ValueError as error:
        fail(str(error))
    target = (inspector.root / directory).resolve()
    if not target.is_relative_to(inspector.root.resolve()) or not target.is_dir():
        fail("CLI cwd is outside or absent from the frozen source tree")
    evidence = spec.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        fail("CLI cwd requires frozen source evidence")
    checked, snippets = [], []
    for item in evidence:
        if not isinstance(item, dict) or item.get("path") not in inspector.files:
            fail("CLI cwd evidence is outside the frozen source scope")
        lines = dict(units(item["path"], inspector.data(item["path"])))["file"].splitlines()
        start, end = item.get("start"), item.get("end")
        if type(start) is not int or type(end) is not int or not 1 <= start <= end <= len(lines):
            fail("CLI cwd evidence range is invalid")
        checked.append(dict(path=item["path"], start=start, end=end))
        snippets.append((item, lines[start - 1:end]))
    for item, snippet in snippets:
        parent = PurePosixPath(item["path"]).parent
        # Documentation commands are parsed, never executed. The containing
        # README defines the starting repository context for these local examples.
        if PurePosixPath(item["path"]).suffix.lower() in {".md", ".rst", ".txt"}:
            current = parent
            for line in snippet:
                try:
                    words = shlex.split(line.strip().removeprefix("$ "))
                except ValueError:
                    continue
                if len(words) == 2 and words[0] == "cd":
                    try:
                        current = PurePosixPath(relative_directory((current / words[1]).as_posix()))
                    except ValueError:
                        break
                if len(words) >= 2 and words[0] in {"python", "python3"}:
                    if (current / words[1]).as_posix() == entry["path"] and current.as_posix() == directory:
                        return dict(cwd=directory, rule="documented-script-directory/1", evidence=checked, import_paths=[])
        if PurePosixPath(item["path"]).name == "Dockerfile":
            all_lines = dict(units(item["path"], inspector.data(item["path"])))["file"].splitlines()
            relevant = [number for number, line in enumerate(all_lines, 1)
                        if re.match(r"^\s*(ADD|COPY|WORKDIR|ENTRYPOINT)\s", line, re.I)]
            if any(not item["start"] <= number <= item["end"] for number in relevant):
                continue
            copies, working = [], None
            for line in snippet:
                words = line.strip().split()
                if len(words) == 3 and words[0].upper() in {"ADD", "COPY"}:
                    copies.append((words[1], words[2]))
                if len(words) == 2 and words[0].upper() == "WORKDIR":
                    working = words[1]
                if line.strip().upper().startswith("ENTRYPOINT ") and working:
                    try:
                        argv = json.loads(line.strip()[len("ENTRYPOINT "):])
                    except (ValueError, TypeError):
                        continue
                    if not isinstance(argv, list) or len(argv) < 2 or argv[0] not in {"python", "python3"}:
                        continue
                    matching = [(source, destination) for source, destination in copies if destination == working]
                    if len(matching) != 1:
                        continue
                    for source, destination in matching:
                        try:
                            mapped = relative_directory((parent / source).as_posix())
                        except ValueError:
                            continue
                        if mapped == directory and (PurePosixPath(mapped) / argv[1]).as_posix() == entry["path"]:
                            return dict(cwd=directory, rule="docker-source-directory/1", evidence=checked, import_paths=[])
    fail("CLI cwd evidence does not establish this original script's directory")


def explicit_import_roots(inspector, path, before_line=None):
    """Only top-level sys.path.insert(0, dirname(abspath(__file__)))
    and str(Path(__file__).resolve().parent), before imports, are supported.
    No source expression is evaluated.
    """
    tree = ast.parse(dict(units(path, inspector.data(path)))["file"])
    imports, roots, tainted = {}, [], set()
    for statement in tree.body:
        if before_line is not None and statement.lineno >= before_line:
            break
        if isinstance(statement, ast.Import):
            for name in statement.names:
                if (name.asname or name.name) not in tainted:
                    imports[name.asname or name.name] = name.name
        elif isinstance(statement, ast.ImportFrom):
            for name in statement.names:
                if (name.asname or name.name) not in tainted:
                    imports[name.asname or name.name] = (statement.module or "") + "." + name.name
        elif isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call):
            call = statement.value
            normalized = ast.unparse(call)
            for alias, original in imports.items():
                normalized = re.sub(r"\b" + re.escape(alias) + r"\b", original, normalized)
            if normalized.startswith("sys.path."):
                # Any unresolved mutation ends the previously known ordering.
                previous = roots
                roots = []
            else:
                previous = roots
            if any(isinstance(node, ast.Name) and node.id not in imports and node.id not in {"str", "__file__"}
                   for node in ast.walk(call)):
                continue
            if "str" not in tainted and "__file__" not in tainted and normalized in {"sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))",
                              "sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))"}:
                roots = previous
                roots.append(dict(path=PurePosixPath(path).parent.as_posix(), start=statement.lineno,
                                  end=statement.end_lineno))
        else:
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                imports.pop(statement.name, None)
                tainted.add(statement.name)
            # A rebound import is no longer proof of the original operation.
            for node in ast.walk(statement):
                if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
                    imports.pop(node.id, None)
                    tainted.add(node.id)
                if isinstance(node, ast.Attribute) and isinstance(node.ctx, (ast.Store, ast.Del)):
                    if ast.unparse(node).startswith("sys.path"):
                        roots = []
                    base = node
                    while isinstance(base, ast.Attribute):
                        base = base.value
                    if isinstance(base, ast.Name):
                        imports.pop(base.id, None)
                        tainted.add(base.id)
    return roots
