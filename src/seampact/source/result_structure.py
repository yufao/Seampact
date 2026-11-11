"""Finite successful-return structure proofs over the existing frozen tracer.

This proves syntax and original source-call forwarding, never SDK return types,
termination, effects, or permission. Unknown children remain untyped. No source
module is imported or executed, and mutable owner state is not a first-call
invariant.
"""

import ast
import copy
import hashlib
import math

from seampact.source.dependency_closure import _Closure, _Class, _External, _Instance
from seampact.source.frozen_inheritance import FrozenInheritance, InheritanceIssue


def _unknown(reason):
    return dict(status="UNKNOWN", reason=reason)


def _join(proofs):
    if not proofs:
        return _unknown("no_proved_successful_return")
    if any(p["status"] != "CHECKED" for p in proofs):
        return _unknown("a_normal_return_structure_is_unknown")
    kinds = {p["kind"] for p in proofs}
    if len(kinds) != 1:
        return _unknown("normal_return_structures_differ")
    first = proofs[0]
    if first["kind"] in {"tuple", "list"}:
        if len({len(p["items"]) for p in proofs}) != 1:
            return _unknown("normal_return_container_lengths_differ")
        return dict(status="CHECKED", kind=first["kind"],
                    items=[_join([p["items"][i] for p in proofs]) for i in range(len(first["items"]))])
    return dict(status="CHECKED", kind=first["kind"])


def _schema(proof):
    if proof["status"] != "CHECKED":
        return {}
    kind = proof["kind"]
    if kind in {"tuple", "list"}:
        return {"type": "array", "x-python-container": kind,
                "prefixItems": [_schema(p) for p in proof["items"]],
                "minItems": len(proof["items"]), "maxItems": len(proof["items"])}
    return {"type": kind}


def _unknown_paths(proof, path="$"):
    if proof["status"] != "CHECKED":
        return [path]
    return [p for i, child in enumerate(proof.get("items", [])) for p in _unknown_paths(child, f"{path}[{i}]")]


def _own_nodes(node):
    """Do not mistake a nested generator definition for its caller."""
    for child in ast.iter_child_nodes(node):
        yield child
        if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            yield from _own_nodes(child)


class _ResultObserver:
    def reset(self, checker):
        self.checker, self.frames, self.active = checker, [], False
        self.returns, self.state_evidence, self.class_cache = [], [], {}
        self.stable_fields = set()

    def cite(self, path, node, kind):
        return dict(path=path, unit="file", start=node.lineno,
                    end=getattr(node, "end_lineno", node.lineno), kind=kind,
                    source_sha256=self.checker.inspector.files[path]["sha256"],
                    ast_sha256=hashlib.sha256(ast.dump(node, include_attributes=False).encode()).hexdigest())

    def class_facts(self, cls):
        key = (cls.path, cls.name)
        if key in self.class_cache:
            return self.class_cache[key]
        facts = dict(status="UNKNOWN", unstable=set(), escaped=set(), evidence=[])
        self.class_cache[key] = facts
        try:
            chain = FrozenInheritance(self.checker.inspector).chain(cls.path, cls.name)
        except InheritanceIssue:
            return facts
        safe = True
        for source in chain:
            facts["evidence"].append(self.cite(source.path, source.node, "receiver_state_class"))
            for method in source.node.body:
                if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if isinstance(method, (ast.Assign, ast.AnnAssign)) and method.value is not None and not isinstance(method.value, ast.Constant):
                        safe = False  # An unproved data descriptor can intercept instance fields.
                    continue
                if method.decorator_list or method.name in {"__new__", "__getattr__", "__getattribute__", "__setattr__", "__delattr__"}:
                    safe = False
                parameters = method.args.posonlyargs + method.args.args
                if not parameters:
                    continue
                receiver = parameters[0].arg
                parents = {id(child): parent for parent in ast.walk(method) for child in ast.iter_child_nodes(parent)}
                captured = {n.name for n in ast.walk(method) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and n is not method and any(isinstance(v, ast.Name) and isinstance(v.ctx, ast.Load)
                        and v.id == receiver for v in ast.walk(n))}
                for value in ast.walk(method):
                    parent = parents.get(id(value))
                    if (isinstance(value, ast.Name) and isinstance(value.ctx, ast.Load)
                            and value.id == source.node.name):
                        safe = False  # A peer can expose or reflectively rewrite its class.
                    if isinstance(value, ast.Name) and isinstance(value.ctx, ast.Load) and value.id == receiver:
                        # A bare receiver alias, return, argument, closure value,
                        # or dynamic storage operation can expose its fields.
                        if not (isinstance(parent, ast.Attribute) and parent.value is value):
                            safe = False
                    if isinstance(value, ast.Name) and isinstance(value.ctx, ast.Load) and value.id in captured:
                        if not (isinstance(parent, ast.Call) and parent.func is value):
                            safe = False
                    if not (isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name)
                            and value.value.id == receiver):
                        continue
                    if value.attr.startswith("__") and value.attr.endswith("__"):
                        safe = False
                    if method.name != "__init__" and isinstance(value.ctx, (ast.Store, ast.Del)):
                        facts["unstable"].add(value.attr)
                    if not isinstance(value.ctx, ast.Load):
                        continue
                    if isinstance(parent, ast.Attribute) and parent.value is value:
                        if parent.attr.startswith("__") and parent.attr.endswith("__"):
                            safe = False
                        continue
                    if isinstance(parent, ast.Call) and parent.func is value:
                        continue  # Original source method dispatch; its own body is covered.
                    builtin_test = (isinstance(parent, ast.Call) and isinstance(parent.func, ast.Name)
                        and parent.func.id == "isinstance" and len(parent.args) == 2 and parent.args[0] is value
                        and not parent.keywords and "isinstance" not in self.checker.module(source.path)
                        and not any((isinstance(v, ast.Name) and isinstance(v.ctx, (ast.Store, ast.Del))
                                     and v.id == "isinstance") or isinstance(v, ast.arg) and v.arg == "isinstance"
                                    or isinstance(v, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and v.name == "isinstance"
                                    or isinstance(v, ast.alias) and (v.asname or v.name.split(".")[0]) == "isinstance"
                                    or isinstance(v, (ast.Global, ast.Nonlocal)) and "isinstance" in v.names
                                    for v in ast.walk(method)))
                    if not builtin_test:
                        facts["escaped"].add(value.attr)
        facts["status"] = "CHECKED" if safe else "UNKNOWN"
        return facts

    def prepare_entry(self, receiver):
        # Open all ordinary retained values before the public call. Preserve a
        # source instance reference only when every original peer method keeps
        # that field private and never rewrites it. Aliases share the decision.
        seen = set()
        def widen(value):
            if not isinstance(value, _Instance) or value.local_class is None or id(value) in seen:
                return
            seen.add(id(value))
            facts = self.class_facts(value.local_class)
            if facts["status"] != "CHECKED":
                value.type_known = False
            escaped = [value.attributes.get(name) for name in facts["escaped"]]
            preserved, opaque, opened = [], [], []
            for name, field in list(value.attributes.items()):
                retain = (facts["status"] == "CHECKED" and name not in facts["unstable"]
                    and isinstance(field, _Instance) and field.type_known
                    and not any(field is item for item in escaped))
                if retain:
                    if field.local_class is not None:
                        self.stable_fields.add((value.allocation, name))
                        preserved.append(name)
                        widen(field)
                    else:
                        # Keep the tracer's existing SDK-opaque marker, not an
                        # SDK type/value/shape claim. Replacing it by a missing
                        # local callable would invent a new source-code gap.
                        opaque.append(name)
                else:
                    opened.append(name)
                    value.attributes[name] = None
            self.state_evidence.append(dict(path=value.local_class.path, symbol=value.local_class.name,
                status=facts["status"], preserved_source_instance_fields=sorted(preserved),
                preserved_opaque_fields=sorted(opaque),
                widened_fields=sorted(opened), evidence=facts["evidence"]))
        widen(receiver)
        self.active = True

    def conditional_truth(self, node, env, path, truth):
        if not self.active or truth is None:
            return truth
        # Literal-only tests do not depend on retained or module state. Other
        # value tests are explored both ways, even if the fresh constructor
        # happened to give the tracer a literal value.
        if not any(isinstance(n, (ast.Name, ast.Attribute, ast.Call, ast.Subscript)) for n in ast.walk(node)):
            return truth
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "isinstance"
                and "isinstance" not in env and len(node.args) == 2 and not node.keywords):
            value = node.args[0]
            if isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name):
                owner = env.get(value.value.id)
                field = owner.attributes.get(value.attr) if isinstance(owner, _Instance) else None
                if (isinstance(field, _Instance) and field.local_class is not None and field.type_known
                        and (owner.allocation, value.attr) in self.stable_fields
                        and self.class_facts(field.local_class)["status"] == "CHECKED"):
                    return truth
        return None

    def begin(self, target):
        frame = dict(target=target, returns=[], calls={}, barriers=[])
        own = list(_own_nodes(target.node))
        if isinstance(target.node, ast.AsyncFunctionDef) or any(isinstance(n, (ast.Yield, ast.YieldFrom)) for n in own):
            frame["barriers"].append("async_or_generator_result_is_not_a_body_return")
        if target.node.decorator_list:
            frame["barriers"].append("decorated_return_protocol_is_not_proved")
        self.frames.append(frame)
        return frame

    def annotation_safe(self, node, env):
        if node is None or isinstance(node, ast.Constant):
            return True
        if isinstance(node, (ast.Tuple, ast.List)):
            return all(self.annotation_safe(n, env) for n in node.elts)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            return self.annotation_safe(node.left, env) and self.annotation_safe(node.right, env)
        value = node.value if isinstance(node, ast.Subscript) else node
        parts = []
        while isinstance(value, ast.Attribute):
            parts.insert(0, value.attr)
            value = value.value
        if not isinstance(value, ast.Name):
            return False
        bound = env.get(value.id)
        name = bound.name if isinstance(bound, _External) else value.id if value.id not in env else ""
        name = ".".join([name] + parts)
        primitive = name in {"str", "int", "float", "bool", "bytes", "list", "tuple", "dict", "set", "frozenset", "object", "type"}
        # Existing official typing syntax has no source-defined subscription
        # dispatch. A source Box[T], arbitrary SDK subscription or call does.
        ordinary = primitive or name.startswith("typing.")
        return ordinary and (not isinstance(node, ast.Subscript) or self.annotation_safe(node.slice, env))

    def definition(self, node, path, env):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args.posonlyargs + node.args.args + node.args.kwonlyargs
            args += [a for a in (node.args.vararg, node.args.kwarg) if a is not None]
            annotations = [node.returns] + [a.annotation for a in args]
            defaults = node.args.defaults + [n for n in node.args.kw_defaults if n is not None]
            def literal_default(default):
                if isinstance(default, ast.Name):
                    return True  # A bare value lookup invokes no Python protocol.
                try:
                    ast.literal_eval(default)
                    return True
                except (ValueError, TypeError, SyntaxError, RecursionError):
                    return False
            safe_defaults = all(literal_default(default) for default in defaults)
            if not safe_defaults or not all(self.annotation_safe(n, env) for n in annotations):
                self.checker.negative_gap(path, node, "result proof lacks finite definition-time annotation/default coverage")
        elif isinstance(node, ast.AnnAssign) and not self.annotation_safe(node.annotation, env):
            self.checker.negative_gap(path, node, "result proof lacks finite annotation subscription coverage")
        elif isinstance(node, ast.ClassDef):
            # The original class loop validates annotations/defaults with its
            # actual sequential class environment, not a guessed module view.
            # Other base/metaclass initialization remains outside this proof.
            if (node.decorator_list or node.keywords or any(not (isinstance(base, ast.Name)
                    and base.id == "object" and "object" not in env) for base in node.bases)):
                self.checker.negative_gap(path, node, "result proof lacks finite class-definition protocol coverage")
            if any(not isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Assign, ast.AnnAssign, ast.Pass))
                   and not (isinstance(member, ast.Expr) and isinstance(member.value, ast.Constant))
                   for member in node.body):
                self.checker.negative_gap(path, node, "result proof lacks finite class-body continuation coverage")

    def statement(self, node, path, env):
        self.definition(node, path, env)
        if self.frames and isinstance(node, (ast.With, ast.AsyncWith, ast.Try, ast.While, ast.AsyncFor)):
            # Context suppression, finally replacement, and untraced while-else
            # continuation can change the set of normal returns.
            self.frames[-1]["barriers"].append("return_control_protocol_unsupported:" + type(node).__name__)

    def call_result(self, node, proof):
        if self.frames:
            self.frames[-1]["calls"].setdefault(id(node), []).append(proof or _unknown("source_call_result_unproved"))

    def expression(self, node, frame):
        if node is None:
            return dict(status="CHECKED", kind="null")
        if isinstance(node, ast.Constant):
            kinds = {type(None): "null", bool: "boolean", int: "integer", float: "number", str: "string"}
            kind = kinds.get(type(node.value))
            if kind and (kind != "number" or math.isfinite(node.value)):
                return dict(status="CHECKED", kind=kind)
        if isinstance(node, (ast.Tuple, ast.List)):
            if any(isinstance(item, ast.Starred) for item in node.elts):
                return _unknown("starred_return_container_length_unproved")
            return dict(status="CHECKED", kind="tuple" if isinstance(node, ast.Tuple) else "list",
                        items=[self.expression(item, frame) for item in node.elts])
        if isinstance(node, ast.Call):
            return _join(frame["calls"].get(id(node), []))
        return _unknown("return_expression_type_unproved:" + type(node).__name__)

    def return_value(self, node, path):
        if self.frames:
            frame = self.frames[-1]
            frame["returns"].append(self.expression(node.value, frame))
            self.returns.append(self.cite(path, node, "return"))

    def finish(self, frame, terminated):
        if not terminated:
            frame["returns"].append(dict(status="CHECKED", kind="null"))
        proof = _join(frame["returns"]) if not frame["barriers"] else _unknown(frame["barriers"][0])
        target = frame["target"]
        frame["annotation"] = dict(expression=ast.unparse(target.node.returns) if target.node.returns else None,
                                  evidence=[self.cite(target.path, target.node, "result_annotation")])
        proof = copy.deepcopy(proof)
        proof["annotation"] = frame["annotation"]
        proof["annotation_node"] = target.node.returns
        proof["annotation_path"] = target.path
        return proof

    def discard(self, frame):
        if self.frames and self.frames[-1] is frame:
            self.frames.pop()


def _annotation_container(observer, proof):
    node, path = proof.get("annotation_node"), proof.get("annotation_path")
    if isinstance(node, ast.Subscript):
        node = node.value
    module = observer.checker.module(path) if path else {}
    name = ast.unparse(node) if node is not None else ""
    if isinstance(node, ast.Name):
        bound = module.get(node.id)
        name = bound.name if isinstance(bound, _External) else node.id if node.id not in module else ""
    return {"list": "list", "typing.List": "list", "tuple": "tuple", "typing.Tuple": "tuple",
            "str": "string", "int": "integer", "float": "number", "bool": "boolean"}.get(name)


def derive_result_structure(inspector, entry, candidate=None):
    """Report a partial source-proved output shape, with all unknowns explicit.

    CHECKED means a common reported structure covers the finite normal-return
    paths. It does not assert successful execution, all leaf types, or owner
    lifecycle properties. The original annotation remains separate evidence.
    """
    observer = _ResultObserver()
    checker = _Closure(inspector, candidate or dict(entries=[entry], dependencies=[]), result_observer=observer)
    trace = checker.entry(entry)
    proof = checker.result_output.get("proof", _unknown("entry_return_not_proved"))
    closed = (trace["status"] == "CHECKED" and not checker.negative_scope_gaps
              and not entry.get("binding_program") and checker.steps <= 16000)
    structure = {k: v for k, v in proof.items() if not k.startswith("annotation")}
    if not closed:
        structure = _unknown("finite_source_return_coverage_incomplete")
    declared, proved = _annotation_container(observer, proof), structure.get("kind")
    conflict = "UNKNOWN"
    if structure["status"] == "CHECKED" and declared:
        conflict = "COMPATIBLE_ROOT" if declared == proved or declared == "number" and proved == "integer" else "CONFLICT"
    evidence = observer.returns + [dict(site, source_sha256=inspector.files[site["path"]]["sha256"])
                                   for site in checker.callable_sites]
    # No object identity, host observation or temporary snapshot path enters
    # the proof. Stable frozen coordinates/bytes form the contract evidence.
    evidence = sorted({(e["path"], e["start"], e["end"], e.get("kind", "callable"), e["ast_sha256"]): e
                       for e in evidence}.values(), key=lambda e: (e["path"], e["start"], e["end"], e.get("kind", "callable")))
    unknown = _unknown_paths(structure)
    return dict(schema="source-result-structure/1", entry_id=entry["id"], status=structure["status"],
        successful_return_only=True, structure=structure,
        result_schema=_schema(structure) if structure["status"] == "CHECKED" else None,
        fully_typed=structure["status"] == "CHECKED" and not unknown, unknown_paths=unknown,
        annotation=proof.get("annotation", dict(expression=None, evidence=[])),
        annotation_conflict=dict(status=conflict, declared_container=declared, proved_container=proved),
        trace_status=trace["status"], negative_scope_status="UNKNOWN" if checker.negative_scope_gaps else "CHECKED",
        negative_scope_gaps=checker.negative_scope_gaps, unresolved_targets=trace["unresolved_targets"],
        source_evidence=evidence, owner_state=sorted(observer.state_evidence, key=lambda e: (e["path"], e["symbol"])),
        conditions=["successful_normal_returns_only", "frozen_entry_configuration",
                    "native_owner_not_mutated_outside_declared_calls", "retained_mutable_values_widened",
                    "finite_source_call_and_protocol_coverage"])
