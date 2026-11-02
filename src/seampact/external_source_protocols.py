"""Narrow source checks for documented external call-preserving protocols.

These facts support dependency reachability only. They neither remove native
decorators nor establish effects, lifecycle, or extraction eligibility.
"""

import ast
import importlib.util

from .inspection import units


TORCH_NO_GRAD = "https://docs.pytorch.org/docs/stable/generated/torch.no_grad"
SPACY_LOAD = "https://spacy.io/api/top-level#spacy.load"
SPACY_CALL = "https://spacy.io/api/language#call"


class ExternalSourceProtocols:
    def __init__(self, inspector):
        self.inspector = inspector
        self.trees, self.cache = {}, {}

    def tree(self, path):
        if path not in self.trees:
            self.trees[path] = ast.parse(dict(units(path, self.inspector.data(path)))["file"])
        return self.trees[path]

    def unmodified(self, qualified):
        """Reject explicit source rebinding, mutation, and module escapes.

        Names and immutable aliases come from source imports, including aliases
        in other frozen files. No package is imported to perform this check.
        """
        if qualified in self.cache:
            return self.cache[qualified]
        module, _, attribute = qualified.partition(".")
        valid = True
        for path in sorted(self.inspector.files):
            if not path.endswith(".py"):
                continue
            try:
                tree = self.tree(path)
            except (SyntaxError, ValueError, KeyError, OSError):
                valid = False
                break
            aliases = {}
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        aliases.setdefault(alias.asname or alias.name.split(".")[0], set()).add(
                            alias.name if alias.asname else alias.name.split(".")[0])
                elif isinstance(node, ast.ImportFrom) and not node.level:
                    for alias in node.names:
                        aliases.setdefault(alias.asname or alias.name, set()).add((node.module or "") + "." + alias.name)

            def names(node):
                if isinstance(node, ast.Name):
                    return aliases.get(node.id, set())
                if isinstance(node, ast.Attribute):
                    return {name + "." + node.attr for name in names(node.value)}
                return set()

            # Resolve plain aliases to a fixed point within the finite source.
            for _ in range(len(aliases) + 1):
                changed = False
                for node in ast.walk(tree):
                    if isinstance(node, (ast.Assign, ast.AnnAssign)):
                        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                        for target in targets:
                            if isinstance(target, ast.Name):
                                previous = set(aliases.get(target.id, set()))
                                aliases.setdefault(target.id, set()).update(names(node.value))
                                changed |= previous != aliases[target.id]
                if not changed:
                    break
            for node in ast.walk(tree):
                mutated = set()
                if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                elif isinstance(node, ast.Delete):
                    targets = node.targets
                else:
                    targets = []
                for target in targets:
                    if isinstance(target, ast.Attribute):
                        mutated.update(names(target))
                    elif isinstance(target, ast.Name) and qualified in names(target):
                        mutated.add(qualified)
                    elif isinstance(target, ast.Subscript):
                        containers = names(target.value)
                        if module + ".__dict__" in containers:
                            if not isinstance(target.slice, ast.Constant) or target.slice.value == attribute:
                                mutated.add(qualified)
                        if "sys.modules" in containers and isinstance(target.slice, ast.Constant) and target.slice.value == module:
                            mutated.add(qualified)
                if isinstance(node, ast.Call):
                    if isinstance(node.func, ast.Name) and node.func.id in {"setattr", "delattr"} and len(node.args) >= 2:
                        roots = names(node.args[0])
                        if isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str):
                            mutated.update(name + "." + node.args[1].value for name in roots)
                        elif module in roots:
                            mutated.add(qualified)
                    # Passing the module or the protocol object to unknown code
                    # could mutate it; retain an unsupported binding instead.
                    for argument in [*node.args, *(item.value for item in node.keywords)]:
                        if names(argument) & {module, qualified}:
                            mutated.add(qualified)
                if any(name == qualified or name.startswith(qualified + ".") for name in mutated):
                    valid = False
                    break
            if not valid:
                break
        self.cache[qualified] = valid
        return valid

    def lazy_guard(self, path, function, name):
        """Recognize one module None binding and one guarded factory write."""
        tree = self.tree(path)
        initial = [node for node in tree.body if isinstance(node, ast.Assign)
                   and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) and node.targets[0].id == name
                   and isinstance(node.value, ast.Constant) and node.value.value is None]
        statements = [node for node in function.body if not isinstance(node, ast.Global)
                      and not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
                               and isinstance(node.value.value, str))]
        if len(initial) != 1 or not statements or not isinstance(statements[0], ast.If):
            return None
        guard = statements[0]
        test = guard.test
        matches = (isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not)
                   and isinstance(test.operand, ast.Name) and test.operand.id == name)
        matches |= (isinstance(test, ast.Compare) and isinstance(test.left, ast.Name) and test.left.id == name
                    and len(test.ops) == 1 and isinstance(test.ops[0], ast.Is)
                    and isinstance(test.comparators[0], ast.Constant) and test.comparators[0].value is None)
        if not matches or guard.orelse or len(guard.body) != 1 or not isinstance(guard.body[0], ast.Assign):
            return None
        assignment = guard.body[0]
        if (len(assignment.targets) != 1 or not isinstance(assignment.targets[0], ast.Name)
                or assignment.targets[0].id != name or not isinstance(assignment.value, ast.Call)):
            return None
        writes = [node for node in ast.walk(tree) if isinstance(node, ast.Name) and node.id == name
                  and isinstance(node.ctx, (ast.Store, ast.Del))]
        if len(writes) != 2:
            return None
        parents = {id(child): parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
        guard_name = getattr(test, "operand", getattr(test, "left", None))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id == name and isinstance(node.ctx, ast.Load):
                parent = parents.get(id(node))
                if ((node.lineno, node.col_offset) == (getattr(guard_name, "lineno", None), getattr(guard_name, "col_offset", None))
                        or isinstance(parent, ast.Call) and parent.func is node):
                    continue
                # Aliasing, returning, configuring, or exposing the singleton
                # would need another protocol for its possible mutations.
                return None
        for source in sorted(self.inspector.files):
            if not source.endswith(".py"):
                continue
            source_tree = self.tree(source)
            module_name = path[:-3].replace("/", ".").removesuffix(".__init__")
            module_names = {module_name, module_name.removeprefix("src.")}
            current = source[:-3].replace("/", ".").removesuffix(".__init__")
            package = current if source.endswith("/__init__.py") else current.rpartition(".")[0]
            aliases = {}
            for node in ast.walk(source_tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        aliases[alias.asname or alias.name.split(".")[0]] = alias.name if alias.asname else alias.name.split(".")[0]
                elif isinstance(node, ast.ImportFrom):
                    imported = node.module or ""
                    if node.level:
                        try:
                            imported = importlib.util.resolve_name("." * node.level + imported, package)
                        except (ValueError, ImportError):
                            continue
                    for alias in node.names:
                        aliases[alias.asname or alias.name] = (imported + "." + alias.name).strip(".")

            def qualified(node):
                if isinstance(node, ast.Name):
                    return aliases.get(node.id, "")
                if isinstance(node, ast.Attribute):
                    parent = qualified(node.value)
                    return parent + "." + node.attr if parent else ""
                return ""

            for _ in range(len(aliases) + 1):
                for node in ast.walk(source_tree):
                    if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                        value = qualified(node.value)
                        if value:
                            aliases[node.targets[0].id] = value
            for node in ast.walk(source_tree):
                if (isinstance(node, ast.Attribute) and node.attr == name and isinstance(node.ctx, (ast.Store, ast.Del))
                        and qualified(node.value) in module_names):
                    return None
                if (isinstance(node, ast.Subscript) and isinstance(node.ctx, (ast.Store, ast.Del))
                        and qualified(node.value) in {module + ".__dict__" for module in module_names}
                        and (not isinstance(node.slice, ast.Constant) or node.slice.value == name)):
                    return None
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    if source == path and node.func.id in {"globals", "locals", "exec", "eval"}:
                        return None
                    if node.func.id in {"setattr", "delattr"} and len(node.args) >= 2:
                        if (qualified(node.args[0]) in module_names
                                and (not isinstance(node.args[1], ast.Constant) or node.args[1].value == name)):
                            return None
        return initial[0], guard, assignment.value
