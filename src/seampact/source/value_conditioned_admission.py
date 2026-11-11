"""A no-permission structural plan for a small value-conditioned domain.

This only describes a frozen original entry and its definition prefix.  It
does not load the source, prove its body, start a process, or admit any call.
"""
import ast
import hashlib
import re
from pathlib import Path

from seampact.source.call_value_conditions import digest, literal_default
from seampact.source.import_only import _metadata, compile_import_only_plan
from seampact.source.inspection import Inspector, native_ast
from seampact.source.verification import check_candidate, source_unit


SCHEMA = 'source-value-conditioned-structure/1'
_TYPES = {'str': 'string', 'int': 'integer', 'float': 'number', 'bool': 'boolean'}
_EMPTY_RESOURCES = {'requirements': [], 'bindings': {}}
_PERMISSIONS = dict(semantic_export=False, invoke=False, dispatch=False,
                    process_start=False, import_prefix=False)
_STRUCTURE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _definition_prefix(inspector, paths, entry_path, symbol):
    """Require a simple source-only prefix, including dormant definitions."""
    for path in paths:
        tree = native_ast(source_unit(inspector, path))
        for statement in tree.body:
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if statement.decorator_list:
                    raise ValueError('definition decorator is outside the pure prefix domain')
                args = statement.args
                for default in [*args.defaults, *(d for d in args.kw_defaults if d is not None)]:
                    literal_default(default)
                annotations = [a.annotation for a in [*args.posonlyargs, *args.args, *args.kwonlyargs,
                    *([args.vararg] if args.vararg else []), *([args.kwarg] if args.kwarg else [])]]
                annotations.append(statement.returns)
                if any(a is not None and not (isinstance(a, ast.Name) and a.id in _TYPES)
                       and not (isinstance(a, ast.Constant) and a.value is None) for a in annotations):
                    raise ValueError('definition annotation is outside the pure builtin prefix')
            elif isinstance(statement, (ast.Import, ast.ImportFrom)):
                if isinstance(statement, ast.ImportFrom) and statement.module == '__future__':
                    raise ValueError('postponed annotation evaluation is outside the first domain')
            elif isinstance(statement, (ast.Assign, ast.AnnAssign)):
                targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
                if not all(isinstance(target, ast.Name) for target in targets):
                    raise ValueError('module assignment target is not a plain name')
                if isinstance(statement, ast.AnnAssign):
                    raise ValueError('module annotation is outside the pure prefix domain')
                literal_default(statement.value)
            elif isinstance(statement, ast.Pass) or (isinstance(statement, ast.Expr)
                    and isinstance(statement.value, ast.Constant) and type(statement.value.value) is str):
                continue
            else:
                raise ValueError('module statement is outside the pure definition prefix')
        # Rebinding an annotation name or entry name anywhere in the frozen
        # original module is conservatively excluded.  The later worker check
        # still has to establish the actual builtin and source object identity.
        protected = set(_TYPES) | {'__builtins__', '__loader__', '__spec__'}
        if path == entry_path:
            protected.add(symbol)
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)) and node.id in protected:
                raise ValueError('builtin annotation or entry binding can be rebound')
            if isinstance(node, ast.arg) and node.arg in protected:
                raise ValueError('builtin annotation or entry name is shadowed')
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name in protected:
                if path != entry_path or node.name != symbol or node not in tree.body:
                    raise ValueError('builtin annotation or entry name is redefined')
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    local = alias.asname or (alias.name.split('.')[0] if isinstance(node, ast.Import) else alias.name)
                    if alias.name == '*' or local in protected:
                        raise ValueError('import can change a protected source binding')


def _interface(node):
    if node.decorator_list or node.args.vararg or node.args.kwarg:
        raise ValueError('decorated or variadic entries are outside the first domain')
    if node.args.posonlyargs:
        raise ValueError('positional-only transport is outside the first domain')
    params = [*node.args.args, *node.args.kwonlyargs]
    defaults = ([None] * (len(node.args.args) - len(node.args.defaults)) + list(node.args.defaults)
                + list(node.args.kw_defaults))
    properties, required, originals = {}, [], {}
    for parameter, default in zip(params, defaults):
        annotation = parameter.annotation
        if not isinstance(annotation, ast.Name) or annotation.id not in _TYPES:
            raise ValueError('each argument must have an original builtin primitive annotation')
        properties[parameter.arg] = {'type': _TYPES[annotation.id]}
        if default is None:
            required.append(parameter.arg)
        else:
            originals[parameter.arg] = literal_default(default)
    result = node.returns
    if isinstance(result, ast.Name) and result.id in _TYPES:
        declared = {'type': _TYPES[result.id]}
    elif isinstance(result, ast.Constant) and result.value is None:
        declared = {'type': 'null'}
    else:
        raise ValueError('return must have an original builtin primitive annotation')
    return dict(type='object', properties=properties, required=required, additionalProperties=False), declared, originals


def compile_structure_plan(inspector, entry, candidate):
    """Recompute structural evidence, retaining the original body as UNKNOWN."""
    body = dict(schema=SCHEMA, status='UNKNOWN', body_obligation='UNKNOWN',
                result_obligation='REQUIRES_SAME_CALL_COMPATIBILITY', permissions=dict(_PERMISSIONS),
                worker_predicates=['ACTUAL_ORIGINAL_CALLABLE_AND_DEFAULTS', 'ORIGINAL_BUILTINS_AND_MODULES',
                    'ORIGINAL_PREFIX_OCCURRENCE', 'MATCHED_WORKER_GENERATION', 'LOCKED_READ_USE_AND_NO_WRITER'],
                unresolved=['COMPLETE_SAME_CALL_SOURCE_CONTROL_EFFECTS_AND_EXITS',
                    'SAME_CALL_RESULT_COMPATIBILITY', 'SAME_CALL_RESOURCE_AND_TASK_ADMISSION',
                    'ACTUAL_WORKER_TARGETS_AND_NO_WRITER', 'FULL_DOMAIN_BODY_AND_DEPENDENCY_CLOSURE'],
                invalidation=['SOURCE_OR_CANDIDATE', 'ENTRY_OR_BOUNDARY', 'CHECKER_OR_SOURCE_READER',
                    'INTERFACE_OR_PREFIX', 'CONFIGURATION_OR_RESOURCES'],
                reason=None)
    try:
        if hashlib.sha256(Path(__file__).read_bytes()).hexdigest() != _STRUCTURE_SHA256:
            raise ValueError('loaded structure checker source changed')
        if type(inspector) is not Inspector or getattr(inspector, 'target_runtime', None) is not None:
            raise ValueError('the source inspector must be the local original snapshot reader')
        entry, candidate = _metadata(entry), _metadata(candidate)
        if type(entry) is not dict or type(candidate) is not dict:
            raise ValueError('entry and candidate must be frozen JSON objects')
        if (candidate.get('entries') != [entry] or candidate.get('dependencies', []) != [] or
                len(candidate.get('boundaries', [])) != 1):
            raise ValueError('first domain requires one entry, one boundary and no declared dependencies')
        if any(candidate.get(k) for k in ('configuration', 'resources', 'services', 'sdk', 'native_wiring')):
            raise ValueError('candidate configuration, resources, SDK or service injection is unsupported')
        if (inspector.request.get('withheld_sources') or
                any(inspector.request.get(k) for k in ('configuration', 'dependency_bindings', 'user_resources', 'resources'))):
            raise ValueError('withheld source or injected configuration/resources are outside the first domain')
        if set(entry) - {'id', 'kind', 'path', 'symbol', 'unit', 'boundary_ids'}:
            raise ValueError('entry has an unsupported construction, configuration or binding field')
        if (entry.get('kind') != 'function' or entry.get('unit', 'file') != 'file' or
                type(entry.get('id')) is not str or not re.fullmatch(r'[A-Za-z0-9_-]+', entry['id']) or
                type(entry.get('symbol')) is not str or not entry['symbol'].isidentifier() or entry['symbol'] in _TYPES or
                type(entry.get('path')) is not str or not entry['path'].endswith('.py') or
                entry['path'] not in inspector.files):
            raise ValueError('entry must identify one top-level original Python function')
        boundary = candidate['boundaries'][0]
        if (type(boundary) is not dict or set(boundary) - {'id', 'mode', 'path', 'symbol', 'unit'} or
                type(boundary.get('id')) is not str or not re.fullmatch(r'[A-Za-z0-9_-]+', boundary['id']) or
                boundary.get('mode') != 'W' or boundary.get('path') != entry['path'] or
                boundary.get('symbol') != entry['symbol'] or boundary.get('unit', 'file') != 'file' or
                entry.get('boundary_ids') != [boundary.get('id')]):
            raise ValueError('entry requires its one whole original W boundary, without native wiring')
        from seampact.typed_contract import CHECKER_ID, installation_evidence
        installation = installation_evidence(inspector, candidate)
        report = check_candidate(inspector, candidate)
        rows = report.get('boundaries', [])
        if (report.get('source_version') != inspector.request.get('source_version') or report.get('admitted') is not False or
                len(rows) != 1 or rows[0].get('id') != boundary.get('id') or rows[0].get('mode') != 'W' or
                rows[0].get('buildable') is not True or rows[0].get('runtime_admitted') is not False or
                rows[0].get('obligations', {}).get('source_anchor', {}).get('status') != 'PASS' or
                rows[0].get('obligations', {}).get('binding_rule', {}).get('status') != 'PASS'):
            raise ValueError('normal whole W source and binding obligations were not checked')
        tree = native_ast(source_unit(inspector, entry['path']))
        matches = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == entry['symbol']]
        if len(matches) != 1 or type(matches[0]) is not ast.FunctionDef:
            raise ValueError('original top-level synchronous function is absent or ambiguous')
        node = matches[0]
        if any(isinstance(n, (ast.Yield, ast.YieldFrom, ast.Await, ast.AsyncFor, ast.AsyncWith)) for n in ast.walk(node)):
            raise ValueError('suspension or lazy source result is outside the first domain')
        interface, result, defaults = _interface(node)
        prefix = compile_import_only_plan(inspector, entry, candidate, _EMPTY_RESOURCES)
        if (prefix.get('status') != 'CHECKED' or prefix.get('activation_prefix', {}).get('status') != 'CHECKED' or
                prefix.get('runtime_protocol_requirements') or prefix.get('stdlib_requirements') or prefix.get('top_calls') or
                type(prefix.get('modules_order')) is not list or entry['path'] not in prefix['modules_order'] or
                len(prefix['modules_order']) != len(set(prefix['modules_order']))):
            raise ValueError('complete original pure import/definition prefix is UNKNOWN')
        _definition_prefix(inspector, prefix['modules_order'], entry['path'], entry['symbol'])
        body.update(status='STRUCTURE_CHECKED', source_version=inspector.request['source_version'],
            entry_digest=digest(entry), candidate_digest=digest(candidate),
            checker_identity=dict(core=CHECKER_ID, structure_sha256=_STRUCTURE_SHA256),
            installation_evidence=installation,
            source_anchor=dict(path=entry['path'], symbol=entry['symbol'], source_sha256=inspector.files[entry['path']]['sha256'],
                               definition_ast_sha256=hashlib.sha256(ast.dump(node, include_attributes=False).encode()).hexdigest()),
            interface=interface, declared_result_schema=result, original_defaults=defaults,
            prefix_plan=prefix, whole_w=rows[0], reason=None)
    except (ValueError, KeyError, TypeError, SyntaxError, OSError, AttributeError) as error:
        body['reason'] = str(error)
    return dict(body, plan_digest=digest(body))


def verify_structure_plan(inspector, entry, candidate, plan):
    """Recompute rather than consume serialized metadata as evidence."""
    try:
        supplied = _metadata(plan)
        return (type(supplied) is dict and supplied.get('status') == 'STRUCTURE_CHECKED'
                and supplied == compile_structure_plan(inspector, entry, candidate))
    except (ValueError, KeyError, TypeError, SyntaxError, OSError):
        return False
