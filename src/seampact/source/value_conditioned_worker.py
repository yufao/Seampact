"""In-process original target checks; no complete worker or call permission.

The prospective capture hooks are for the normal controlled import engine,
after its independently admitted prefix and reservation.  They never execute
the prefix.  Until a separate process/owner no-writer proof is implemented,
even a successful target check retains an UNKNOWN complete worker proof.
"""
import ast
import builtins
import copy
import hashlib
import sys
import threading
import types

from seampact.source.call_value_conditions import builtin_state, literal_default
from seampact.source.import_only import _metadata
from seampact.source.inspection import native_ast
from seampact.source.verification import source_unit
from seampact.source.value_conditioned_admission import verify_structure_plan
from seampact.source.value_conditioned_calls import verify_call_source
from seampact.source.sdk_code_identity import code_shape


SCHEMA = 'source-value-conditioned-worker-targets/1'
_PERMISSIONS = dict(semantic_export=False, invoke=False, dispatch=False, process_start=False, import_prefix=False)
_GETTRACE, _GETPROFILE = sys.gettrace, sys.getprofile
_PENDING, _OCCURRENCES = {}, {}


class _ProspectiveCapture:
    __slots__ = ()


def _native(binding):
    from seampact.units import NativeBindings, _native_context_lock, _IMPORT_ONLY_LOADER_KERNEL
    if type(binding) is not NativeBindings:
        raise ValueError('actual original native binding is required')
    attributes = object.__getattribute__(binding, '__dict__')
    if (type(attributes) is not dict or any(type(key) is not str for key in attributes) or
            any(name in attributes for name in (*_IMPORT_ONLY_LOADER_KERNEL['protected'], '_verify_import_only_target'))):
        raise ValueError('original binding has unknown instance method hooks')
    for class_, meta, mro, expected in _IMPORT_ONLY_LOADER_KERNEL['classes']:
        if type(class_) is not meta:
            raise ValueError('original loader class protocol changed')
        actual_mro = type.__dict__['__mro__'].__get__(class_, type)
        values = type.__dict__['__dict__'].__get__(class_, type)
        if (len(actual_mro) != len(mro) or any(a is not b for a, b in zip(actual_mro, mro)) or
                any(type(key) is not str for key in values) or len(values) != len(expected) or
                any(key not in values or values[key] is not value for key, value in expected)):
            raise ValueError('original loader class or method binding changed')
    lock = attributes.get('lock')
    if (type(lock) is not type(_native_context_lock) or not _native_context_lock._is_owned() or not lock._is_owned()):
        raise ValueError('actual original binding and both source read/use locks are required')
    if (type(binding.detached) is not bool or binding.detached or type(binding.patches) is not dict or
            type(binding.recipes) is not dict or binding.patches or binding.recipes or
            type(binding.import_only_entries) is not dict or type(binding.module_paths) is not dict or
            any(type(key) is not str for key in binding.import_only_entries) or
            any(type(key) is not str for key in binding.module_paths) or
            any(type(value) is not tuple or len(value) != 2 or type(value[0]) is not str or type(value[1]) is not bool
                for value in binding.module_paths.values())):
        raise ValueError('original binding is detached or has unsupported source transformations')
    if (sys.gettrace is not _GETTRACE or sys.getprofile is not _GETPROFILE or
            _GETTRACE() is not None or _GETPROFILE() is not None or not builtin_state()):
        raise ValueError('unknown tracing, profiling or builtin operation is active')


def _identity(identity):
    identity = _metadata(identity)
    if (type(identity) is not dict or set(identity) != {'session', 'instance_id', 'generation', 'process_nonce'} or
            any(type(identity.get(key)) is not str or not identity[key] for key in ('session', 'instance_id', 'process_nonce')) or
            type(identity.get('generation')) is not int or identity['generation'] < 1):
        raise ValueError('worker occurrence identity is incomplete')
    return identity


def _names(binding, prefix):
    result = {}
    for path in prefix['modules_order']:
        names = [name for name, item in binding.module_paths.items() if item[0] == path and not name.startswith('src.')]
        if len(names) != 1:
            raise ValueError('original source module name is absent or ambiguous')
        result[path] = names[0]
    if len(set(result.values())) != len(result):
        raise ValueError('original source module names are not unique')
    return result


def begin_capture(binding, entry, unit, structure, instance_identity):
    """Record the cold point before a separately admitted original prefix.

    Called only by the normal import engine, with both locks held and before
    entering its source context.  This function never imports original source.
    """
    from seampact.units import _check_import_only_loader
    _native(binding)
    _check_import_only_loader(binding)
    identity = _identity(instance_identity)
    entry, structure = _metadata(entry), _metadata(structure)
    if (type(unit) is not dict or any(type(key) is not str for key in unit) or
            type(unit.get('id')) is not str or not unit['id'] or
            not verify_structure_plan(binding.inspector, entry, binding.candidate, structure)):
        raise ValueError('original frozen structure or unit is unavailable')
    names = _names(binding, structure['prefix_plan'])
    if (type(sys.modules) is not dict or any(type(key) is not str for key in sys.modules) or
            type(binding.modules) is not dict or any(type(key) is not str for key in binding.modules) or
            any(name in binding.modules or name in sys.modules for name in names.values()) or
            unit['id'] in binding.import_only_entries):
        raise ValueError('source prefix is already loaded or has an old receipt')
    token = _ProspectiveCapture()
    _PENDING[token] = dict(binding=binding, entry=copy.deepcopy(entry), unit_id=unit['id'],
        structure=copy.deepcopy(structure), identity=identity, names=names, thread_id=threading.get_ident())
    return token


def abort_capture(token):
    """Discard an unfinished prospective occurrence without source actions."""
    return _PENDING.pop(token, None) is not None


def forget_capture(token):
    """Forget only in-process evidence; never retire a source owner."""
    return _OCCURRENCES.pop(token, None) is not None


def _module_namespace(module):
    if type(module) is not types.ModuleType:
        raise ValueError('original loaded source module is not exact')
    namespace = types.ModuleType.__dict__['__dict__'].__get__(module, types.ModuleType)
    if type(namespace) is not dict or any(type(key) is not str for key in namespace):
        raise ValueError('source module namespace has an unknown key protocol')
    return namespace


def _same_literal(actual, expected):
    if type(actual) is not type(expected) or type(expected) not in (type(None), str, bool, int, float):
        return False
    return float.hex(actual) == float.hex(expected) if type(expected) is float else actual == expected


def _check_original_definition(function, node, module_name):
    """Compare original defaults and annotations, not just captured snapshots."""
    if (type(function.__name__) is not str or function.__name__ != node.name or
            type(function.__qualname__) is not str or function.__qualname__ != node.name or
            type(function.__module__) is not str or function.__module__ != module_name or function.__closure__ is not None or
            type(function.__dict__) is not dict or function.__dict__):
        raise ValueError('original function metadata or closure changed')
    positional = tuple(literal_default(value) for value in node.args.defaults)
    actual = function.__defaults__ or ()
    if type(actual) is not tuple or len(actual) != len(positional) or any(
            not _same_literal(a, b) for a, b in zip(actual, positional)):
        raise ValueError('original function positional default differs from source')
    expected_kw = {arg.arg: literal_default(value) for arg, value in zip(node.args.kwonlyargs, node.args.kw_defaults)
                   if value is not None}
    actual_kw = function.__kwdefaults__ or {}
    if (type(actual_kw) is not dict or any(type(key) is not str for key in actual_kw) or set(actual_kw) != set(expected_kw) or
            any(not _same_literal(actual_kw[name], value) for name, value in expected_kw.items())):
        raise ValueError('original function keyword default differs from source')
    arguments = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs,
                 *([node.args.vararg] if node.args.vararg else []),
                 *([node.args.kwarg] if node.args.kwarg else [])]
    annotations = {arg.arg: arg.annotation for arg in arguments if arg.annotation is not None}
    if node.returns is not None:
        annotations['return'] = node.returns
    actual_annotations = function.__annotations__
    if (type(actual_annotations) is not dict or any(type(key) is not str for key in actual_annotations)
            or set(actual_annotations) != set(annotations)):
        raise ValueError('original function annotations changed')
    for name, annotation in annotations.items():
        expected = getattr(builtins, annotation.id) if isinstance(annotation, ast.Name) else None
        if actual_annotations[name] is not expected:
            raise ValueError('original function annotation binding changed')


def _check_objects(binding, entry, receipt, structure, names, modules):
    from seampact.source.activation_scope import ActivationScope
    from seampact.units import (_FrozenLoader, _verify_native_callable_snapshot, _plain_callable_defaults,
                                _IMPORT_ONLY_LOADER_KERNEL)
    if (type(receipt) is not dict or any(type(key) is not str for key in receipt) or
            binding.import_only_entries.get(receipt.get('unit_id')) is not receipt or
            type(receipt.get('helpers')) is not dict or type(sys.modules) is not dict or
            any(type(key) is not str for key in sys.modules)):
        raise ValueError('original registered in-process import receipt is absent')
    if (any(key not in builtins.__dict__ or builtins.__dict__[key] is not value
            for key, value in _IMPORT_ONLY_LOADER_KERNEL['builtins']) or
            len(builtins.__dict__) != len(_IMPORT_ONLY_LOADER_KERNEL['builtins'])):
        raise ValueError('original full builtin namespace changed')
    binding._verify_import_only_target(entry, receipt)
    helpers = receipt['helpers']
    if any(type(key) is not tuple or len(key) != 2 or any(type(part) is not str for part in key) for key in helpers):
        raise ValueError('original helper receipt keys have an unknown protocol')
    resolver = ActivationScope(binding.inspector, entry, binding.candidate)
    module_names = {path: name for path, name in names.items()}
    bookkeeping = {'__name__', '__doc__', '__package__', '__loader__', '__spec__', '__file__', '__cached__',
                   '__builtins__', '__path__', '__annotations__'}
    for path, name in module_names.items():
        module = modules.get(path)
        if module is None or sys.modules.get(name) is not module:
            raise ValueError('original source module identity changed')
        namespace = _module_namespace(module)
        if path == entry['path'] and receipt.get('namespace') is not namespace:
            raise ValueError('original entry receipt does not belong to its source module')
        loader = namespace.get('__loader__')
        spec = namespace.get('__spec__')
        if (namespace.get('__builtins__') is not builtins.__dict__ or type(loader) is not _FrozenLoader or
                loader.binding is not binding or type(loader.path) is not str or loader.path != path or
                type(spec) is not __import__('importlib').machinery.ModuleSpec or spec.loader is not loader or
                type(spec.name) is not str or spec.name != name or type(namespace.get('__name__')) is not str or
                namespace['__name__'] != name):
            raise ValueError('original controlled loader, module identity or builtin association changed')
        if '__annotations__' in namespace and (type(namespace['__annotations__']) is not dict or namespace['__annotations__']):
            raise ValueError('unexpected mutable module annotations')
        source = source_unit(binding.inspector, path)
        tree = native_ast(source)
        filename = str(binding.inspector.root / path)
        compiled = compile(source, filename, 'exec', dont_inherit=True)
        code = {(item.co_name, item.co_firstlineno): item for item in compiled.co_consts if type(item) is types.CodeType}
        declarations = set()
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                item = helpers.get((path, node.name))
                expected = code.get((node.name, node.lineno))
                if (type(item) is not dict or any(type(key) is not str for key in item) or item.get('namespace') is not namespace or
                        item.get('source_sha256') != binding.inspector.files[path]['sha256'] or
                        item.get('definition_ast_sha256') != hashlib.sha256(ast.dump(node, include_attributes=False).encode()).hexdigest() or
                        type(item.get('function')) is not types.FunctionType or expected is None):
                    raise ValueError('original imported helper source or capture is missing')
                function = item['function']
                if path == entry['path'] and node.name == entry['symbol'] and function is not receipt.get('function'):
                    raise ValueError('original entry and imported helper receipts identify different callables')
                current = namespace.get(node.name)
                if current is not function:
                    if function is not receipt.get('function') or current is not receipt.get('approved_proxy') or current is None:
                        raise ValueError('original helper binding changed')
                    _verify_native_callable_snapshot(current, receipt['approved_proxy_snapshot'])
                if (function.__globals__ is not namespace or function.__builtins__ is not builtins.__dict__ or
                        code_shape(function.__code__) != code_shape(expected)):
                    raise ValueError('original function code/global association differs from frozen source')
                _check_original_definition(function, node, name)
                _verify_native_callable_snapshot(function, item['snapshot'])
                if _plain_callable_defaults(function) != item['defaults']:
                    raise ValueError('original function defaults changed')
                declarations.add(node.name)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    local = alias.asname or (alias.name.split('.')[0] if isinstance(node, ast.Import) else alias.name)
                    if isinstance(node, ast.Import):
                        target = resolver._resolve(path, alias.name if alias.asname else alias.name.split('.')[0])
                        expected = modules.get(target)
                    else:
                        target = resolver._resolve(path, node.module or '', node.level)
                        parent = modules.get(target)
                        if parent is None:
                            raise ValueError('original source import parent is absent')
                        expected = _module_namespace(parent).get(alias.name)
                    if expected is None or namespace.get(local) is not expected:
                        raise ValueError('original source import alias changed')
                    declarations.add(local)
            elif isinstance(node, ast.Assign):
                expected = ast.literal_eval(node.value)
                for target in node.targets:
                    if (not isinstance(target, ast.Name) or target.id not in namespace or
                            not _same_literal(namespace[target.id], expected)):
                        raise ValueError('original immutable global value changed')
                    declarations.add(target.id)
            elif isinstance(node, ast.Pass) or (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
                                               and type(node.value.value) is str):
                continue
            else:
                raise ValueError('unknown original definition prefix binding')
        for child_path, child in modules.items():
            child_name = module_names[child_path]
            prefix = name + '.'
            if child_name.startswith(prefix) and '.' not in child_name[len(prefix):]:
                local = child_name[len(prefix):]
                if namespace.get(local) is child:
                    declarations.add(local)
        if set(namespace) - declarations - bookkeeping:
            raise ValueError('original source namespace has an unexplained binding')


def finish_capture(token, receipt):
    """Seal the receipt produced by the normal engine in the same locked import."""
    pending = _PENDING.pop(token, None)
    if pending is None:
        raise ValueError('prospective original import occurrence is absent')
    binding, entry, structure = pending['binding'], pending['entry'], pending['structure']
    _native(binding)
    if (pending['thread_id'] != threading.get_ident() or
            not verify_structure_plan(binding.inspector, entry, binding.candidate, structure) or
            type(receipt) is not dict or any(type(key) is not str for key in receipt) or
            binding.import_only_entries.get(pending['unit_id']) is not receipt or
            receipt.get('instance_identity') != pending['identity'] or
            receipt.get('plan_digest') != structure['prefix_plan']['plan_digest']):
        raise ValueError('actual original import receipt does not match the prospective occurrence')
    # This internal identity is attached by the normal engine, not serialized.
    receipt['unit_id'] = pending['unit_id']
    modules = {path: sys.modules.get(name) for path, name in pending['names'].items()}
    _check_objects(binding, entry, receipt, structure, pending['names'], modules)
    _OCCURRENCES[token] = dict(**pending, receipt=receipt, modules=modules)
    return token


def check_worker_targets(binding, entry, unit, structure, source_proof, arguments, instance_identity):
    """Recheck live objects; report no-writer and full worker proof UNKNOWN."""
    result = dict(schema=SCHEMA, status='UNKNOWN', target_status='UNKNOWN', reason=None,
        worker_proof='UNKNOWN', no_writer='UNKNOWN', permissions=dict(_PERMISSIONS),
        residual=['CURRENT_PROCESS_AND_OWNER_NO_BYPASS_WRITER', 'MATCHED_ACTUAL_WORKER_GENERATION',
                  'TRUSTED_CAPTURE_ENGINE_AND_UNINTERRUPTED_LOCK',
                  'CURRENT_COMPLETE_RESERVATION_AND_ATOMIC_PERMIT', 'SAME_LOCK_THROUGH_ACTUAL_USE'])
    try:
        _native(binding)
        identity = _identity(instance_identity)
        entry, structure, source_proof = _metadata(entry), _metadata(structure), _metadata(source_proof)
        if type(unit) is not dict or any(type(key) is not str for key in unit) or type(unit.get('id')) is not str:
            raise ValueError('original unit identity is missing')
        receipt = binding.import_only_entries.get(unit['id'])
        matches = [row for row in _OCCURRENCES.values() if row['binding'] is binding and row['receipt'] is receipt
                   and row['unit_id'] == unit['id'] and row['identity'] == identity]
        if len(matches) != 1:
            raise ValueError('normal prospective original import occurrence or instance identity is absent')
        occurrence = matches[0]
        if (entry != occurrence['entry'] or structure != occurrence['structure'] or
                not verify_structure_plan(binding.inspector, entry, binding.candidate, structure) or
                not verify_call_source(binding.inspector, entry, binding.candidate, structure, arguments, source_proof)):
            raise ValueError('same source, original structure or actual call proof changed')
        if (receipt.get('plan_digest') != structure['prefix_plan']['plan_digest'] or
                receipt.get('instance_identity') != identity):
            raise ValueError('original prefix plan or worker identity changed')
        if (type(binding.modules) is not dict or any(type(key) is not str for key in binding.modules) or
                _names(binding, structure['prefix_plan']) != occurrence['names'] or
                any(binding.modules.get(name) is not occurrence['modules'][path]
                    for path, name in occurrence['names'].items())):
            raise ValueError('original binding module owner table changed')
        _check_objects(binding, entry, receipt, structure, occurrence['names'], occurrence['modules'])
        result.update(target_status='WORKER_TARGETS_CHECKED', reason='actual object targets checked; no-writer and full worker proof remain UNKNOWN',
                      source_version=structure['source_version'], structure_digest=structure['plan_digest'],
                      payload_digest=source_proof['payload_digest'], modules_checked=sorted(occurrence['modules']))
    except (ValueError, KeyError, TypeError, SyntaxError, OSError, AttributeError, RuntimeError) as error:
        result['reason'] = str(error)
    return result
