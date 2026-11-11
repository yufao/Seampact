"""Narrow source checks for documented external call-preserving protocols.

These facts support dependency reachability only. They neither remove native
decorators nor establish effects, lifecycle, or extraction eligibility.
"""

import ast
import importlib.util

from seampact.source.inspection import units
from seampact.source.type_references import TypeReferences


TORCH_NO_GRAD = "https://docs.pytorch.org/docs/stable/generated/torch.no_grad"
SPACY_LOAD = "https://spacy.io/api/top-level#spacy.load"
SPACY_CALL = "https://spacy.io/api/language#call"


class ExternalSourceProtocols:
    def __init__(self, inspector):
        self.inspector = inspector
        self.trees, self.cache = {}, {}
        self.type_references = TypeReferences(inspector, self.tree)
        self.type_reference_facts = []
        self.activation_scope = None
        self._scope_used = False
        self._scope_checker = None

    def bind_activation(self, entry, candidate=None, *, checker=None):
        """Bind one independently collected upper bound before source replay.

        An incomplete collector falls back to the full frozen source domain.
        Even a complete source bound carries an independent original-worker
        obligation; it does not turn source non-mutation into runtime permission.
        """
        from seampact.source.activation_scope import ActivationScope
        self.activation_scope = ActivationScope(self.inspector, entry, candidate)
        self._scope_used, self._scope_checker = False, checker
        self.cache.clear()
        self.type_references.bind_activation(self.activation_scope, require_scope=self._require_scope)
        return self.activation_scope.requirements()

    def _require_scope(self):
        """Emit a target premise only when a failed full scan is narrowed."""
        from seampact.source.activation_scope import RULE, GAP
        from seampact.source.call_value_conditions import digest
        if self._scope_used or self.activation_scope is None or not self.activation_scope.closed:
            return
        self._scope_used = True
        self.type_references._scope_used = True
        requirement = self.activation_scope.requirements()
        checker = self._scope_checker
        if checker is not None:
            path = self.activation_scope.entry.get('path')
            if path in self.inspector.files and path.endswith('.py'):
                try:
                    tree = self.tree(path)
                except (SyntaxError, ValueError, KeyError, OSError):
                    return requirement
                if not tree.body:
                    return requirement
                node = tree.body[0]
                checker.protocol(RULE, path, node, target_requirements=requirement,
                    operation='independent conservative original source activation upper bound')
                if self.activation_scope.closed:
                    context = checker.call_context
                    verified = getattr(context, 'runtime_verified', ()) if context is not None else ()
                    if (type(verified) is not tuple or any(type(value) is not str for value in verified)
                            or digest(requirement) not in verified):
                        checker.negative_gap(path, node, GAP)
        return requirement

    def source_paths(self):
        return (self.activation_scope.paths() if self._scope_used else
                tuple(sorted(path for path in self.inspector.files if path.endswith('.py'))))

    def activation_diagnostic(self):
        if self.activation_scope is None:
            return None
        return dict(self.activation_scope.requirements(), source_domain_restricted=self._scope_used)

    def type_reference(self, path, operation, operand, qualified=None):
        fact = self.type_references.check(path, operation, operand, qualified)
        if fact not in self.type_reference_facts:
            self.type_reference_facts.append(fact)
        return fact

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
        paths = tuple(sorted(path for path in self.inspector.files if path.endswith('.py')))
        valid = self._unmodified(qualified, paths)
        if (not valid and self.activation_scope is not None and self.activation_scope.closed
                and self.activation_scope.paths() != paths):
            valid = self._unmodified(qualified, self.activation_scope.paths())
            if valid:
                self._require_scope()
        self.cache[qualified] = valid
        return valid

    def _unmodified(self, qualified, paths):
        module, _, attribute = qualified.partition(".")
        local_bindings = {}
        parts = qualified.split('.')
        for end in range(1, len(parts)):
            for suffix in ('.py', '/__init__.py'):
                source = '/'.join(parts[:end]) + suffix
                if source in self.inspector.files:
                    local_bindings[source] = (parts[end], '.'.join(parts[:end + 1]))
        valid = True
        for path in paths:
            if not path.endswith(".py"):
                continue
            try:
                tree = self.tree(path)
            except (SyntaxError, ValueError, KeyError, OSError):
                valid = False
                break
            aliases = {}
            if path in local_bindings:
                local, origin = local_bindings[path]
                aliases[local] = {origin}
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

            def type_names(node):
                direct = names(node)
                if direct:
                    return direct
                return set().union(*(type_names(child) for child in ast.iter_child_nodes(node)))

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
                if isinstance(node, ast.ExceptHandler) and node.type is not None:
                    for target in type_names(node.type):
                        if target == qualified or target.startswith(qualified + '.'):
                            self.type_reference(path, node, node.type, target)
                            # A source-position fact does not close the SDK
                            # object's runtime identity for this bool guard.
                            mutated.add(qualified)
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
                            if len(node.args) == 2 and node.args[1] is argument and qualified in names(argument):
                                # SOURCE_CHECKED describes one source operand,
                                # not runtime identity or boolean permission.
                                self.type_reference(path, node, argument, qualified)
                            mutated.add(qualified)
                if any(name == qualified or name.startswith(qualified + ".") for name in mutated):
                    valid = False
                    break
            if not valid:
                break
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
        for source in self.source_paths():
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
