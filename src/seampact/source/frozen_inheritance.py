"""Finite, source-only resolution of ordinary local single inheritance.

This identifies Python's original class and method definitions. It does not
grant lifecycle, extraction, or independent-execution permissions.
"""

import ast
import importlib.util
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from seampact.source.execution_context import explicit_import_roots
from seampact.source.abc_kernel import check_abc_source
from seampact.source.inspection import digest, native_ast
from seampact.source.verification import source_unit


class InheritanceIssue(ValueError):
    pass


class _ImportConflict(InheritanceIssue):
    pass


@dataclass
class ClassSource:
    path: str
    node: ast.ClassDef
    external_endpoint: str = ''


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
        self.package_modules = {}
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
            # The containing source checkout need not itself be a Python name.
            # Only an original __init__.py chain identifies this package root;
            # do not trim arbitrary path prefixes to invent import aliases.
            parent = PurePosixPath(path).parent
            root = parent
            while (root / "__init__.py").as_posix() in inspector.files:
                root = root.parent
            if root != parent:
                relative = list(PurePosixPath(path).relative_to(root).with_suffix("").parts)
                if relative[-1] == "__init__":
                    relative.pop()
                if relative and all(part.isidentifier() for part in relative):
                    self.package_modules[path] = ".".join(relative)

    def tree(self, path):
        if path not in self.trees:
            self.trees[path] = native_ast(source_unit(self.inspector, path, "file"))
        return self.trees[path]

    def _module(self, path):
        return self.package_modules.get(path) or next((name for name, item in self.modules.items()
            if item == path and not name.startswith("src.")), "")

    def _module_path(self, module, path, before_line=None):
        parent = PurePosixPath(path).parent
        package = parent
        while (package / "__init__.py").as_posix() in self.inspector.files:
            package = package.parent
        roots = [item["path"] for item in explicit_import_roots(self.inspector, path, before_line)]
        roots += [parent.as_posix(), package.as_posix(), ".", "src"]
        choices = list(dict.fromkeys((PurePosixPath(root) / module.replace(".", "/")).as_posix() + suffix
            for root in roots for suffix in (".py", "/__init__.py")))
        found = [candidate for candidate in choices if candidate in self.inspector.files]
        if len(found) > 1:
            raise _ImportConflict("inherited module has multiple frozen import candidates: " + module)
        return found[0] if found else None

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

    def _qualified(self, qualified, seen, context_path, before_line=None):
        # Prefer a complete module prefix, including package re-exports.
        for index in range(len(qualified.split(".")) - 1, 0, -1):
            parts = qualified.split(".")
            path = self._module_path(".".join(parts[:index]), context_path, before_line)
            if path is not None:
                return self._name(path, ".".join(parts[index:]), seen)
        raise InheritanceIssue("inherited class is external or outside frozen source: " + qualified)

    def _literal_exports(self, path, node):
        """Names exported by a frozen literal-only module, without importing it.

        Executable setup, re-exports and dynamic __all__ remain ambiguous. A
        constants module can therefore be excluded as a writer of a class name
        without granting any dependency, lifecycle or execution permission.
        """
        target = self._module_path(self._import_name(path, node), path, node.lineno)
        if target is None:
            raise InheritanceIssue("wildcard import source is outside frozen source")
        values = {}
        for statement in self.tree(target).body:
            if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant) and isinstance(statement.value.value, str):
                continue
            if isinstance(statement, ast.Pass):
                continue
            if not isinstance(statement, ast.Assign) or any(not isinstance(t, ast.Name) for t in statement.targets):
                raise InheritanceIssue("wildcard import has a non-literal export namespace")
            try:
                value = ast.literal_eval(statement.value)
            except (ValueError, TypeError, SyntaxError, RecursionError):
                raise InheritanceIssue("wildcard import has a non-literal export namespace") from None
            for name in statement.targets:
                values[name.id] = value
        if "__all__" not in values:
            return {name for name in values if not name.startswith("_")}
        exported = values["__all__"]
        if (not isinstance(exported, (list, tuple)) or any(not isinstance(name, str) for name in exported)
                or any(name not in values for name in exported)):
            raise InheritanceIssue("wildcard import has an unresolved __all__")
        return set(exported)

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
                        if first in self._literal_exports(path, node):
                            raise InheritanceIssue("wildcard import makes inherited class binding ambiguous")
                        continue
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
            return self._qualified(".".join([qualified, *tail]), seen, path, node.lineno)
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
                from seampact.source.type_references import TypeReferences
                reference = TypeReferences(self.inspector, self.tree).resolve(current.path, base, base)
                if reference == 'abc.ABC':
                    checked = check_abc_source(self.inspector, chain)
                    if checked['status'] != 'CHECKED':
                        raise InheritanceIssue('original abc.ABC prefix or target kernel is unproved: ' +
                                               checked.get('reason', 'kernel conditions unknown'))
                    current.external_endpoint = 'abc.ABC'
                    current = None
                else:
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
        for path in sorted(p for p in self.inspector.files if p.endswith(".py")):
            try:
                tree = self.tree(path)
            except (SyntaxError, ValueError, KeyError, OSError) as error:
                raise InheritanceIssue("frozen source is unavailable for the inherited class rewrite check") from error
            for node in tree.body:
                for value in ast.walk(node):
                    target = None
                    if isinstance(value, ast.Attribute) and isinstance(value.ctx, (ast.Store, ast.Del)):
                        target = value.value
                    elif isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id in {"setattr", "delattr"} and value.args:
                        target = value.args[0]
                    if isinstance(target, (ast.Name, ast.Attribute)):
                        try:
                            cls = self._name(path, ast.unparse(target))
                        except _ImportConflict:
                            raise
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

    def resolve(self, path, symbol, wrapper_check=None):
        parts = symbol.split(".")
        if len(parts) != 2:
            raise InheritanceIssue("inherited entry must name Class.method")
        try:
            chain = self.chain(path, parts[0])
            method = self.method(chain, parts[1])
            if method is None:
                raise InheritanceIssue("native inherited method is absent: " + symbol)
            # Only a caller with an independent finite wrapper proof may inspect
            # the original method body. Ordinary binding resolution stays strict.
            if method.node.decorator_list and not (wrapper_check and wrapper_check(method)):
                raise InheritanceIssue("inherited native entry descriptor/decorator is not an ordinary instance method")
            return chain, method
        except InheritanceIssue as error:
            # Report the separately checked finite graph at the real refusal
            # site. It remains diagnostic until target protocols/effects are
            # consumed, and does not replace this resolver's strict boundary.
            from seampact.source.class_factories import finite_class_graph
            error.facts = finite_class_graph(self.inspector, path, parts[0])
            error.facts['strict_rejection'] = dict(status='UNKNOWN', reason=str(error), path=path, symbol=symbol)
            raise

    def facts(self, chain, method):
        def location(path, node):
            return dict(path=path, symbol=node.name, line=node.lineno, end_line=node.end_lineno,
                        file_sha256=self.inspector.files[path]["sha256"],
                        ast_sha256=digest(ast.dump(node, include_attributes=False).encode()))
        return dict(rule="frozen-single-inheritance/1", status="SOURCE_RESOLVED",
                    owner=location(chain[0].path, chain[0].node),
                    class_chain=[location(item.path, item.node) for item in chain],
                    definition=dict(location(method.path, method.node), symbol=method.symbol),
                    external_endpoint=chain[-1].external_endpoint or None,
                    scope="original receiver and effective method definition; no lifecycle or split permission")
