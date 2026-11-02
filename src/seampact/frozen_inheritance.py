"""Finite, source-only resolution of ordinary local single inheritance.

This identifies Python's original class and method definitions. It does not
grant lifecycle, extraction, or independent-execution permissions.
"""

import ast
import importlib.util
from dataclasses import dataclass
from pathlib import Path

from .inspection import digest, native_ast
from .verification import source_unit


class InheritanceIssue(ValueError):
    pass


@dataclass
class ClassSource:
    path: str
    node: ast.ClassDef


@dataclass
class MethodSource:
    path: str
    owner: ast.ClassDef
    node: ast.AST

    @property
    def symbol(self):
        return self.owner.name + "." + self.node.name


class FrozenInheritance:
    """Resolve explicit names/imports only; never import the inspected source."""

    def __init__(self, inspector):
        self.inspector = inspector
        self.modules, self.trees = {}, {}
        for path in inspector.files:
            if not path.endswith(".py"):
                continue
            parts = list(Path(path).with_suffix("").parts)
            if parts[-1] == "__init__":
                parts.pop()
            if parts and all(part.isidentifier() for part in parts):
                self.modules[".".join(parts)] = path
                if parts[0] == "src" and len(parts) > 1:
                    self.modules[".".join(parts[1:])] = path

    def tree(self, path):
        if path not in self.trees:
            self.trees[path] = native_ast(source_unit(self.inspector, path, "file"))
        return self.trees[path]

    def _module(self, path):
        return next((name for name, item in self.modules.items() if item == path and not name.startswith("src.")), "")

    def _import_name(self, path, node):
        module = node.module or ""
        if node.level:
            current = self._module(path)
            package = current if Path(path).name == "__init__.py" else current.rpartition(".")[0]
            try:
                module = importlib.util.resolve_name("." * node.level + module, package)
            except (ValueError, ImportError) as error:
                raise InheritanceIssue("relative inherited class import is unresolved") from error
        return module

    def _qualified(self, qualified, seen):
        # Prefer a complete module prefix, including package re-exports.
        for index in range(len(qualified.split(".")) - 1, 0, -1):
            parts = qualified.split(".")
            path = self.modules.get(".".join(parts[:index]))
            if path is not None:
                return self._name(path, ".".join(parts[index:]), seen)
        raise InheritanceIssue("inherited class is external or outside frozen source: " + qualified)

    def _name(self, path, name, seen=()):
        key = (path, name)
        if key in seen:
            raise InheritanceIssue("cyclic inherited class name resolution")
        seen = (*seen, key)
        first, *tail = name.split(".")
        bindings = []
        for node in self.tree(path).body:
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == first:
                bindings.append((node, None))
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    local = alias.asname or (alias.name if isinstance(node, ast.ImportFrom) else alias.name.split(".")[0])
                    if alias.name == "*":
                        raise InheritanceIssue("wildcard import makes inherited class binding ambiguous")
                    if local == first:
                        qualified = ((self._import_name(path, node) + "." + alias.name).strip(".")
                                     if isinstance(node, ast.ImportFrom) else alias.name if alias.asname else local)
                        bindings.append((node, qualified))
            else:
                if any(isinstance(value, ast.Name) and isinstance(value.ctx, (ast.Store, ast.Del)) and value.id == first
                       for value in ast.walk(node)):
                    raise InheritanceIssue("inherited class binding is reassigned or conditional: " + first)
        if len(bindings) != 1:
            raise InheritanceIssue("inherited class binding is absent or ambiguous: " + first)
        node, qualified = bindings[0]
        if qualified is not None:
            return self._qualified(".".join([qualified, *tail]), seen)
        if tail or not isinstance(node, ast.ClassDef):
            raise InheritanceIssue("inherited base must identify an original top-level class")
        return ClassSource(path, node)

    def chain(self, path, symbol):
        current, chain, seen = self._name(path, symbol), [], set()
        while current is not None:
            key = (current.path, current.node.name)
            if key in seen:
                raise InheritanceIssue("cyclic native inheritance")
            seen.add(key)
            chain.append(current)
            node = current.node
            if node.decorator_list or node.keywords or len(node.bases) > 1:
                raise InheritanceIssue("native inheritance requires undecorated single classes without metaclass keywords")
            protocol_names = {"__new__", "__getattribute__", "__getattr__", "__setattr__", "__init_subclass__", "__class_getitem__"}
            for member in node.body:
                if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if member.name in protocol_names:
                        raise InheritanceIssue("native inherited class has custom allocation or attribute/class dispatch")
                elif isinstance(member, (ast.Assign, ast.AnnAssign)):
                    targets = member.targets if isinstance(member, ast.Assign) else [member.target]
                    if any(isinstance(value, ast.Name) and value.id in protocol_names for target in targets for value in ast.walk(target)):
                        raise InheritanceIssue("native inherited class assigns a custom class protocol")
                elif not isinstance(member, (ast.Assign, ast.AnnAssign, ast.Pass, ast.Expr)):
                    raise InheritanceIssue("native inherited class body has conditional or dynamic bindings")
            base = node.bases[0] if node.bases else None
            if base is None:
                current = None
            elif isinstance(base, ast.Name) and base.id == "object":
                # A shadowed object would be a different original base.
                if any((isinstance(value, ast.Name) and isinstance(value.ctx, (ast.Store, ast.Del)) and value.id == "object")
                       or (isinstance(value, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and value.name == "object")
                       or (isinstance(value, ast.alias) and (value.asname or value.name.split(".")[0]) == "object")
                       for value in ast.walk(self.tree(current.path))):
                    raise InheritanceIssue("native object base is shadowed")
                current = None
            elif isinstance(base, (ast.Name, ast.Attribute)):
                current = self._name(current.path, ast.unparse(base))
            else:
                raise InheritanceIssue("dynamic native inheritance is unsupported")
        self._check_rewrites(chain)
        method_names = {member.name for item in chain for member in item.node.body
                        if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))}
        for item in chain:
            for member in item.node.body:
                if not isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                args = member.args.posonlyargs + member.args.args
                receiver = args[0].arg if args else None
                for value in ast.walk(member):
                    if (isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id == "super"):
                        shadowed = any((isinstance(marker, ast.Name) and isinstance(marker.ctx, (ast.Store, ast.Del)) and marker.id == "super")
                            or (isinstance(marker, ast.arg) and marker.arg == "super")
                            or (isinstance(marker, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and marker.name == "super")
                            or (isinstance(marker, ast.alias) and (marker.asname or marker.name.split(".")[0]) == "super")
                            for marker in ast.walk(self.tree(item.path)))
                        if shadowed:
                            raise InheritanceIssue("inherited super dispatch is shadowed in frozen source")
                    if (isinstance(value, ast.Attribute) and isinstance(value.ctx, (ast.Store, ast.Del))
                            and isinstance(value.value, ast.Name) and value.value.id == receiver
                            and value.attr in method_names):
                        raise InheritanceIssue("native inherited receiver rewrites an original method")
                    if (isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name)
                            and value.value.id == receiver and value.attr in {"__dict__", "__class__"}):
                        raise InheritanceIssue("native inherited receiver exposes dynamic class/attribute storage")
                    if (isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
                            and value.func.id in {"setattr", "delattr"} and value.args
                            and isinstance(value.args[0], ast.Name) and value.args[0].id == receiver):
                        raise InheritanceIssue("native inherited receiver has dynamic attribute mutation")
        return chain

    def _check_rewrites(self, chain):
        # Source mutations of a class through an explicit local/imported name
        # are rejected, even when an eventual runtime patch would look callable.
        keys = {(item.path, item.node.name) for item in chain}
        for path in sorted(set(self.modules.values())):
            for node in self.tree(path).body:
                for value in ast.walk(node):
                    target = None
                    if isinstance(value, ast.Attribute) and isinstance(value.ctx, (ast.Store, ast.Del)):
                        target = value.value
                    elif isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id in {"setattr", "delattr"} and value.args:
                        target = value.args[0]
                    if isinstance(target, (ast.Name, ast.Attribute)):
                        try:
                            cls = self._name(path, ast.unparse(target))
                        except InheritanceIssue:
                            continue
                        if (cls.path, cls.node.name) in keys:
                            raise InheritanceIssue("native inherited class is monkey-patched in frozen source")

    @staticmethod
    def method(chain, name):
        for item in chain:
            found = []
            for member in item.node.body:
                if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and member.name == name:
                    found.append(member)
                elif isinstance(member, (ast.Assign, ast.AnnAssign)):
                    targets = member.targets if isinstance(member, ast.Assign) else [member.target]
                    if any(isinstance(value, ast.Name) and value.id == name for target in targets for value in ast.walk(target)):
                        found.append(member)
            if found:
                if len(found) != 1 or not isinstance(found[0], (ast.FunctionDef, ast.AsyncFunctionDef)):
                    raise InheritanceIssue("inherited method is shadowed or has an unknown descriptor: " + name)
                return MethodSource(item.path, item.node, found[0])
        return None

    def resolve(self, path, symbol):
        parts = symbol.split(".")
        if len(parts) != 2:
            raise InheritanceIssue("inherited entry must name Class.method")
        chain = self.chain(path, parts[0])
        method = self.method(chain, parts[1])
        if method is None:
            raise InheritanceIssue("native inherited method is absent: " + symbol)
        if method.node.decorator_list:
            raise InheritanceIssue("inherited native entry descriptor/decorator is not an ordinary instance method")
        return chain, method

    def facts(self, chain, method):
        def location(path, node):
            return dict(path=path, symbol=node.name, line=node.lineno, end_line=node.end_lineno,
                        file_sha256=self.inspector.files[path]["sha256"],
                        ast_sha256=digest(ast.dump(node, include_attributes=False).encode()))
        return dict(rule="frozen-single-inheritance/1", status="SOURCE_RESOLVED",
                    owner=location(chain[0].path, chain[0].node),
                    class_chain=[location(item.path, item.node) for item in chain],
                    definition=dict(location(method.path, method.node), symbol=method.symbol),
                    scope="original receiver and effective method definition; no lifecycle or split permission")
