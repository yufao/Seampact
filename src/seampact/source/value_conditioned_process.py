"""Prospective normal-worker scope for the first value-conditioned domain.

CPython and the frozen Core retain their existing primitive semantics. This
module checks actual source capabilities, routes and locks; it does not try to
prove absence of native threads or hostile VM memory writes. The trusted
managed baseline is established by a normal coordinator birth query, not by
an arbitrary promise about earlier startup history.
No returned record or token grants source execution, import or process start.
"""
import copy
import signal
import sys
import types
import weakref
from functools import lru_cache
from pathlib import Path

from seampact.source.value_conditioned_admission import compile_structure_plan, verify_structure_plan
from seampact.source.sdk_code_identity import code_shape


SCHEMA = 'source-value-conditioned-process-scope/1'
BIRTH_ROUTE = '/api/runtime/birth'
_PERMISSIONS = dict(semantic_export=False, invoke=False, dispatch=False, process_start=False, import_prefix=False)
_GETFRAME, _GETTRACE, _GETPROFILE = sys._getframe, sys.gettrace, sys.getprofile
_GETSIGNAL, _VALID_SIGNALS = signal.getsignal, signal.valid_signals
_SCOPES = weakref.WeakKeyDictionary()


class _WorkerScope:
    __slots__ = ('__weakref__',)


def _namespace(obj):
    values = object.__getattribute__(obj, '__dict__')
    if type(values) is not dict or any(type(key) is not str for key in values):
        raise ValueError('actual worker object has an unknown namespace protocol')
    return values


@lru_cache(maxsize=32)
def _compiled_core(data, path, optimize):
    """Cache only immutable source compilation; actual bindings stay uncached."""
    return compile(data, path, 'exec', dont_inherit=True, optimize=optimize)


def _original_function(function):
    """Check loaded code against its actual Core source, not its name or hash alone."""
    if type(function) is not types.FunctionType:
        raise ValueError('normal Core function object is required')
    actual_shape = code_shape(function.__code__)
    module = sys.modules.get(function.__module__)
    if type(module) is not types.ModuleType or function.__globals__ is not module.__dict__:
        raise ValueError('normal Core function module association changed')
    path = Path(module.__file__).resolve()
    package = Path(__file__).resolve().parents[1]
    if not path.is_relative_to(package) or function.__code__.co_filename != str(path):
        raise ValueError('normal Core function source is outside the actual package')
    root = _compiled_core(path.read_bytes(), str(path), sys.flags.optimize)
    pending, matches = [root], []
    while pending:
        code = pending.pop()
        if (code.co_name == function.__code__.co_name and
                code.co_firstlineno == function.__code__.co_firstlineno):
            matches.append(code)
        pending.extend(item for item in code.co_consts if type(item) is types.CodeType)
    if len(matches) != 1 or code_shape(matches[0]) != actual_shape:
        raise ValueError('loaded Core function differs from the current original code: ' + function.__qualname__)


def _methods(obj, class_, names):
    if type(obj) is not class_:
        raise ValueError('the exact normal Core object is required')
    values = _namespace(obj)
    for name in names:
        if name in values:
            raise ValueError('normal Core method has an instance shadow: ' + name)
        function = class_.__dict__.get(name)
        _original_function(function)


def _hooks():
    if (sys._getframe is not _GETFRAME or sys.gettrace is not _GETTRACE or sys.getprofile is not _GETPROFILE
            or signal.getsignal is not _GETSIGNAL or signal.valid_signals is not _VALID_SIGNALS):
        raise ValueError('actual tracing, frame or signal inspection primitive changed')
    if _GETTRACE() is not None or _GETPROFILE() is not None:
        raise ValueError('an unproved trace or profile callback is active')
    for number in _VALID_SIGNALS():
        try:
            handler = _GETSIGNAL(number)
        except (ValueError, OSError):
            continue
        if (handler is not signal.SIG_DFL and handler is not signal.SIG_IGN and
                not (type(handler) is int and handler in (0, 1)) and handler is not signal.default_int_handler):
            raise ValueError('an unproved signal callback is registered')
    monitoring = getattr(sys, 'monitoring', None)
    if monitoring is not None and any(monitoring.get_tool_id(tool) is not None for tool in range(6)):
        raise ValueError('an unproved interpreter monitoring callback is registered')
    # Current callback checks supplement the finite normal managed baseline;
    # they are not a claim to reconstruct arbitrary earlier process history.
    return dict(current_hooks='CURRENT_HOOKS_CHECKED', startup_status='UNKNOWN',
                startup_residual=['NORMAL_MANAGED_BASELINE_BIRTH_QUERY'])


def _routing(worker):
    from seampact.worker import make_worker_server
    _original_function(make_worker_server)
    server = _namespace(worker).get('server')
    if server is None:
        return 'NORMAL_BOOTSTRAP_NO_SERVER'
    from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
    class_ = type(server)
    if (type(class_) is not type or class_.__bases__ != (ThreadingHTTPServer,) or
            class_.__qualname__ != 'make_worker_server.<locals>.Server'):
        raise ValueError('the actual server is outside the normal worker route')
    _methods(server, class_, ('process_request_thread',))
    handler = server.RequestHandlerClass
    if (type(handler) is not type or handler.__bases__ != (BaseHTTPRequestHandler,) or
            handler.__qualname__ != 'make_worker_server.<locals>.Handler'):
        raise ValueError('the actual request handler is outside the normal worker route')
    for name in ('log_message', 'send', 'do_GET', 'do_POST'):
        function = handler.__dict__.get(name)
        _original_function(function)
        cells = dict(zip(function.__code__.co_freevars, function.__closure__ or ()))
        if 'worker' in cells and cells['worker'].cell_contents is not worker:
            raise ValueError('normal request route belongs to another worker')
    process = class_.__dict__['process_request_thread']
    cells = dict(zip(process.__code__.co_freevars, process.__closure__ or ()))
    if cells.get('worker') is None or cells['worker'].cell_contents is not worker:
        raise ValueError('normal server thread route belongs to another worker')
    return 'NORMAL_CORE_ROUTES_CHECKED'


def _loader(binding, context_basis=None):
    """Check the base loader or its one normal, locked source-context projection."""
    from seampact.units import (_check_import_only_loader, _IMPORT_ONLY_LOADER_KERNEL, _FrozenFinder,
                                _native_context_lock, _import_only_function_metadata,
                                _verify_native_callable_snapshot, _UNSET_NATIVE_ATTRIBUTE,
                                _NATIVE_MRO, _NATIVE_TYPE, _NATIVE_CLASS_DICT)
    if (type(sys.meta_path) is not list or type(sys.path) is not list or type(sys.modules) is not dict
            or any(type(key) is not str for key in sys.modules)
            or any(type(value) is not str for value in sys.path)):
        raise ValueError('the actual import protocol has unknown container members')
    if context_basis is None or tuple(sys.meta_path) == context_basis['base_meta']:
        _check_import_only_loader(binding)
        return
    kernel = _IMPORT_ONLY_LOADER_KERNEL
    if (not _native_context_lock._is_owned() or not binding.lock._is_owned()
            or type(sys.meta_path) is not list or len(sys.meta_path) != len(context_basis['base_meta']) + 1
            or type(sys.meta_path[0]) is not _FrozenFinder or sys.meta_path[0].binding is not binding
            or any(actual is not expected for actual, expected in zip(sys.meta_path[1:], context_basis['base_meta']))
            or type(sys.path) is not list
            or sys.path != [str(binding.inspector.root), str(binding.inspector.root / 'src'), *context_basis['base_path']]
            or Path.cwd() != binding.inspector.root):
        raise ValueError('the actual loader is not the normal locked source-context projection')
    _original_function(type(binding).__dict__['_context'].__wrapped__)
    for class_, meta, mro, expected in kernel['classes']:
        values = _NATIVE_CLASS_DICT.__get__(class_, _NATIVE_TYPE)
        actual_mro = _NATIVE_MRO.__get__(class_, _NATIVE_TYPE)
        if (type(class_) is not meta or len(actual_mro) != len(mro)
                or any(a is not b for a, b in zip(actual_mro, mro)) or len(values) != len(expected)
                or any(type(key) is not str for key in values)
                or any(key not in values or values[key] is not value for key, value in expected)):
            raise ValueError('the actual controlled loader class protocol changed')
    if any(name in _namespace(binding) for name in kernel['protected']):
        raise ValueError('the actual controlled loader has an instance shadow')
    for module, name, namespace, references in kernel['modules']:
        if (type(module) is not types.ModuleType or module.__dict__ is not namespace
                or any(type(key) is not str for key in namespace)
                or any(key not in namespace or namespace[key] is not value for key, value in references)
                or sys.modules.get(name) is not module):
            raise ValueError('the actual controlled loader module protocol changed')
    import builtins
    if (len(builtins.__dict__) != len(kernel['builtins']) or
            any(key not in builtins.__dict__ or builtins.__dict__[key] is not value for key, value in kernel['builtins'])):
        raise ValueError('the actual controlled loader builtin protocol changed')
    for row in kernel['functions']:
        function, namespace = row['function'], row['globals']
        if (any(type(key) is not str for key in namespace) or function.__builtins__ is not row['builtins']
                or any(namespace.get(key, _UNSET_NATIVE_ATTRIBUTE) is not value for key, value in row['references'])):
            raise ValueError('the actual controlled loader function globals changed')
        metadata = _import_only_function_metadata(function)
        if (len(metadata) != len(row['metadata']) or
                any(key not in metadata or metadata[key] is not value for key, value in row['metadata'])):
            raise ValueError('the actual controlled loader function metadata changed')
        _verify_native_callable_snapshot(function, row['snapshot'])


def _actual(worker, unit_id, *, cold=False, context_basis=None):
    from seampact.worker import Worker, RemoteRuntime, GROUP_PROFILE, REMOTE_PROFILE
    from seampact.units import NativeBindings, UnitCatalog
    from seampact.runtime import OwnerRuntime
    from seampact.store import Store
    from seampact.target_runtime import core_identity
    _methods(worker, Worker, ('__init__', 'identity', '_identify', 'invoke', 'check_call', '_check_guarded_worker', 'import_only',
                              'prewarm', 'drain', 'shutdown', 'health'))
    attributes = _namespace(worker)
    catalog, runtime = attributes.get('catalog'), attributes.get('runtime')
    _methods(catalog, UnitCatalog, ('install', 'invoke', '_invoke_bound', 'check_call_conditions',
                                   'require_call_conditions', 'prepare_import_only', '_verify_operation'))
    _methods(runtime, RemoteRuntime, ('__init__', 'identity', 'call', 'submit', 'acall', 'wrap'))
    if 'context' in _namespace(runtime) or RemoteRuntime.context is not OwnerRuntime.context:
        raise ValueError('the actual owner runtime context chain changed')
    _original_function(RemoteRuntime.context)
    store = attributes.get('store')
    _methods(store, Store, ('get', 'list', 'save'))
    if catalog.store is not store or catalog.runtime is not runtime or catalog.remote_router is not None:
        raise ValueError('normal worker catalog, store or runtime ownership changed')
    spec = attributes.get('spec')
    if type(spec) is not dict or spec.get('schema') != 'native-worker/1':
        raise ValueError('actual normal worker specification is missing')
    core = core_identity('worker')
    if (spec.get('platform_core') != core or worker.profile not in (GROUP_PROFILE['id'], REMOTE_PROFILE['id'])
            or worker.profile != spec.get('profile', REMOTE_PROFILE['id'])):
        raise ValueError('actual worker profile or frozen Core identity changed')
    identity = Worker.identity(worker)
    if (type(identity.get('generation')) is not int or identity['generation'] < 1
            or any(type(identity.get(key)) is not str or not identity[key]
                   for key in ('session', 'instance_id', 'process_nonce'))
            or runtime.identity() != identity or catalog._worker_call_identity != identity):
        raise ValueError('current normal worker process and generation differ')
    units = store.list('unit')
    if (len(units) != 1 or units[0].get('id') != unit_id or worker.units != set()
            or _namespace(worker).get('guarded_units') != {unit_id}):
        raise ValueError('the actual worker has an additional or missing source root')
    unit = units[0]
    if unit.get('binding') != 'SOURCE_VALUE_CONDITIONED_NATIVE_ENTRY' or unit.get('status') != 'STRUCTURE_ONLY':
        raise ValueError('the original root is not the independently guarded structural entry')
    mappings = []
    for name in ('bindings', 'deferred', 'structural'):
        table = _namespace(catalog).get(name, {})
        if type(table) is not dict or any(key != unit_id for key in table):
            raise ValueError('the actual catalog has an unproved source capability root')
        if unit_id in table:
            if name == 'bindings':
                mappings.append((table[unit_id], catalog.entry_specs.get(unit_id)))
            else:
                mappings.append(table[unit_id])
    if len(mappings) != 1 or type(mappings[0]) not in (tuple, list) or len(mappings[0]) != 2:
        raise ValueError('one normal source binding and original entry are required')
    binding, entry = mappings[0]
    if type(binding) is not NativeBindings or binding.runtime is not runtime or binding.detached:
        raise ValueError('the actual original source binding differs')
    _loader(binding, context_basis)
    _methods(binding, NativeBindings, ('invoke', '_run', '_invoke', '_load', 'prepare', '_namespace'))
    candidate = binding.candidate
    structure = compile_structure_plan(binding.inspector, entry, candidate)
    if structure.get('status') != 'STRUCTURE_CHECKED' or not verify_structure_plan(binding.inspector, entry, candidate, structure):
        raise ValueError('the actual source, primitive interface or complete pure prefix is unproved')
    if (candidate != spec.get('candidate') or binding.inspector.request != spec.get('request')
            or unit.get('source_version') != structure['source_version']
            or unit.get('entry_id') != entry['id'] or unit.get('path') != entry['path']
            or unit.get('symbol') != entry['symbol'] or unit.get('kind') != 'function'
            or unit.get('boundary_ids') != entry['boundary_ids']
            or unit.get('parameters') != structure['interface'] or unit.get('input_schema') != structure['interface']
            or unit.get('result_schema') != structure['declared_result_schema']
            or unit.get('native_input_schemas') or unit.get('construction') or unit.get('inheritance_binding')):
        raise ValueError('actual source roots or primitive input/result ports changed')
    if (set(binding.approved_entries) - {unit_id} or
            any(value != entry for value in binding.approved_entries.values()) or
            set(catalog.entry_specs) - {unit_id}):
        raise ValueError('an additional original source entry is exposed')
    group = store.get('runtime_group', unit.get('runtime_group'))
    if (type(group) is not dict or group.get('units') != [unit_id] or group.get('entries') != [entry['id']]
            or group.get('source_version') != unit['source_version'] or worker.group.get('id') != group.get('id')
            or worker.group is not spec.get('group') or runtime.group != group.get('id')
            or runtime.coordinator != worker.coordinator
            or spec.get('group', {}).get('units') != [unit_id]
            or spec.get('group', {}).get('entries') != [entry['id']]):
        raise ValueError('actual original group root set changed')
    roots = (group.get('contract') or {}).get('root_entries', [])
    if roots and roots != [entry['id']]:
        raise ValueError('the actual group has an additional contract root')
    if binding.patches or binding.recipes or binding.notebooks or binding.programs or binding.owners:
        raise ValueError('an unproved transformation, construction or source object root is present')
    names = {name for name, item in binding.module_paths.items()
             if item[0] in structure['prefix_plan']['modules_order'] and not name.startswith('src.')}
    if cold and (binding.modules or binding.entries or binding.import_only_entries or binding.active_calls
                 or any(name in sys.modules for name in names)):
        raise ValueError('source state is warm or already exposed before prospective worker scope')
    hooks = _hooks()
    return dict(binding=binding, entry=copy.deepcopy(entry), unit=unit, structure=structure, identity=identity,
                root_status='CLOSED_SOURCE_ROOTS_CHECKED', port_status='PRIMITIVE_PORTS_CHECKED',
                core_status='ACTUAL_CORE_CHAIN_CHECKED', route_status=_routing(worker), **hooks)


def _report(actual):
    return dict(schema=SCHEMA, status='UNKNOWN', process_status='UNKNOWN', no_writer='UNKNOWN',
                root_status=actual['root_status'], port_status=actual['port_status'],
                core_status=actual['core_status'], route_status=actual['route_status'],
                current_hooks=actual['current_hooks'], startup_status=actual['startup_status'],
                residual=list(actual['startup_residual']) + ['PROSPECTIVE_NORMAL_WORKER_SCOPE',
                    'SAME_LOCK_THROUGH_ACTUAL_USE', 'CURRENT_RESOURCE_AND_PERMIT'],
                permissions=dict(_PERMISSIONS), reason='actual capabilities checked; startup provenance remains unproved')


def inspect_worker_scope(worker, unit_id):
    """Read actual normal objects; a post-construction inspection creates no token."""
    try:
        return _report(_actual(worker, unit_id, cold=True))
    except (ValueError, KeyError, TypeError, AttributeError, OSError, RuntimeError) as error:
        return dict(schema=SCHEMA, status='UNKNOWN', process_status='UNKNOWN', no_writer='UNKNOWN',
                    root_status='UNKNOWN', port_status='UNKNOWN', core_status='UNKNOWN',
                    permissions=dict(_PERMISSIONS), residual=['ACTUAL_NORMAL_WORKER_SCOPE'], reason=str(error))


def begin_worker_scope(worker, unit_id):
    """Called prospectively by the actual Worker.__init__, before any source prefix.

    No approval flag is accepted. The token keeps weak object identities, not a
    serializable permission. A normal constructor call frame is required, and
    normal managed process birth is consumed separately after registration.
    """
    from seampact.worker import Worker
    if sys._getframe is not _GETFRAME:
        raise ValueError('the actual normal worker bootstrap frame cannot be checked')
    frame = _GETFRAME(1)
    if (frame.f_code is not Worker.__init__.__code__ or frame.f_locals.get('self') is not worker
            or frame.f_globals is not sys.modules[Worker.__module__].__dict__):
        raise ValueError('prospective scope must originate inside the normal Worker constructor')
    actual = _actual(worker, unit_id, cold=True)
    if 'source/value_conditioned_process.py' not in worker.spec['platform_core'].get('files', {}):
        raise ValueError('scope checker is not bound to the normal worker bootstrap Core')
    from seampact.units import _native_context_lock
    token = _WorkerScope()
    _SCOPES[token] = dict(worker=weakref.ref(worker), catalog=weakref.ref(worker.catalog),
                          binding=weakref.ref(actual['binding']), entry=actual['entry'],
                          unit_id=unit_id, structure_digest=actual['structure']['plan_digest'],
                          identity=actual['identity'], context_lock=_native_context_lock,
                          binding_lock=actual['binding'].lock,
                          base_meta=tuple(sys.meta_path), base_path=tuple(sys.path), birth=None)
    return token


def bind_worker_birth(token):
    """Query the actual coordinator internally; never accept a caller receipt.

    Registration precedes this query. A pending reply permits no source step
    and can be retried by the normal worker before its original import prefix.
    """
    result = dict(schema=SCHEMA, status='UNKNOWN', birth_status='UNKNOWN', permissions=dict(_PERMISSIONS), reason=None)
    try:
        from seampact.hosting.deployment import json_request
        if type(token) is not _WorkerScope or token not in _SCOPES:
            raise ValueError('normal prospective in-process worker token is absent')
        record = _SCOPES[token]
        worker = record['worker']()
        if worker is None or record['catalog']() is not worker.catalog:
            raise ValueError('normal managed worker scope owner changed')
        actual = _actual(worker, record['unit_id'], cold=record['birth'] is None)
        if actual['identity'] != record['identity'] or actual['binding'] is not record['binding']():
            raise ValueError('actual managed worker identity changed before birth query')
        _original_function(json_request)
        from seampact.source.import_only import _metadata
        receipt = _metadata(json_request(worker.coordinator + BIRTH_ROUTE, actual['identity']))
        if type(receipt) is not dict:
            raise ValueError('normal coordinator birth reply is not exact metadata')
        if receipt.get('status') == 'PENDING':
            return dict(result, birth_status='PENDING', reason='normal managed registration is not yet bound')
        _match_birth(receipt, worker, actual)
        record['birth'] = receipt
        return dict(result, status='MANAGED_BIRTH_BOUND', birth_status='MANAGED_BIRTH_CHECKED', reason=None)
    except (ValueError, KeyError, TypeError, AttributeError, OSError, RuntimeError) as error:
        result['reason'] = str(error)
    return result


def _match_birth(receipt, worker, actual):
    from seampact.hosting.deployment import binding_fingerprint
    from seampact.source.call_value_conditions import digest
    if (type(receipt) is not dict or receipt.get('schema') != 'normal-managed-value-conditioned-birth/1'
            or receipt.get('status') != 'MANAGED_BIRTH_CHECKED'
            or any(receipt.get(key) != value for key, value in actual['identity'].items())
            or receipt.get('binding_fingerprint') != binding_fingerprint(worker.spec)
            or receipt.get('contract_digest') != worker.spec.get('contract_digest')):
        raise ValueError('normal coordinator birth does not match this actual worker and specification')
    support = receipt.get('support') or {}
    baseline = support.get('baseline') or {}
    handle = receipt.get('handle') or {}
    if (support.get('spec_digest') != digest(worker.spec)
            or support.get('unit_id') != actual['unit']['id']
            or support.get('structure_digest') != actual['structure']['plan_digest']
            or support.get('source_version') != actual['structure']['source_version']
            or support.get('platform_core') != worker.spec.get('platform_core')
            or baseline.get('id') != 'normal-default-core-cpython/1'
            or baseline.get('python') != '3.11' or baseline.get('base_image') != 'python:3.11-slim'
            or handle.get('kind') != 'docker' or type(handle.get('container_id')) is not str
            or len(handle['container_id']) != 64
            or type(receipt.get('command_digest')) is not str
            or not receipt.get('permissions') or any(value is not False for value in receipt['permissions'].values())):
        raise ValueError('normal managed baseline, source support or actual backend handle is unproved')


def forget_scope(token):
    """Drop evidence only; never retire an original owner or release a resource."""
    return _SCOPES.pop(token, None) is not None if type(token) is _WorkerScope else False


def verify_scope(token, binding, unit, structure, identity):
    """Recheck with both locks held in the original read/use interval.

    Both the base state and its exact normal source-context projection are
    checked without removing a FrozenFinder or rewriting sys.path. Keep both
    locks continuously held through the independent target check and use.
    """
    result = dict(schema=SCHEMA, status='UNKNOWN', process_status='UNKNOWN', no_writer='UNKNOWN',
                  permissions=dict(_PERMISSIONS), residual=['ACTUAL_NORMAL_WORKER_SCOPE'], reason=None)
    try:
        from seampact.units import _native_context_lock
        if type(token) is not _WorkerScope or token not in _SCOPES:
            raise ValueError('normal prospective in-process worker token is absent')
        record = _SCOPES[token]
        worker = record['worker']()
        if worker is None or record['binding']() is not binding or record['catalog']() is not worker.catalog:
            raise ValueError('normal worker scope owner is gone or replaced')
        if (_native_context_lock is not record['context_lock'] or binding.lock is not record['binding_lock']
                or not _native_context_lock._is_owned() or not binding.lock._is_owned()):
            raise ValueError('the same original binding and source-context locks are required')
        actual = _actual(worker, record['unit_id'], context_basis=record)
        if (actual['binding'] is not binding or actual['identity'] != record['identity'] or identity != record['identity']
                or actual['entry'] != record['entry'] or actual['unit'] != unit
                or actual['structure'] != structure or structure['plan_digest'] != record['structure_digest']):
            raise ValueError('actual worker generation, root, primitive ports or frozen structure changed')
        result = _report(actual)
        result['residual'].remove('PROSPECTIVE_NORMAL_WORKER_SCOPE')
        result['scope_status'] = 'NORMAL_PROSPECTIVE_SCOPE_CHECKED'
        if record['birth'] is not None:
            _match_birth(record['birth'], worker, actual)
            result.update(status='PROCESS_SCOPE_CHECKED', process_status='PROCESS_SCOPE_CHECKED',
                          no_writer='CHECKED', startup_status='TRUSTED_MANAGED_BASELINE_BOUND',
                          reason=None, birth_status='MANAGED_BIRTH_CHECKED',
                          residual=['SAME_CALL_NO_EXTERNAL_IO_OR_ESCAPE', 'SAME_LOCK_THROUGH_ACTUAL_USE',
                                    'CURRENT_RESOURCE_AND_PERMIT'])
    except (ValueError, KeyError, TypeError, AttributeError, OSError, RuntimeError) as error:
        result['reason'] = str(error)
    return result
