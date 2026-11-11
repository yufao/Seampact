"""Internal normal-managed birth facts for the first structure-only domain.

The fixed default CPython/Core baseline is part of the existing trusted runtime
model. This module binds its normal builder and start occurrence to an actual
backend handle and registered generation. It neither audits all startup history
nor grants source import, invocation, lifecycle or semantic-export permission.
"""
import ast
import copy
import hashlib
import json
import re
import sys
import tempfile
import weakref
from pathlib import Path, PurePosixPath

from seampact.source.call_value_conditions import digest
from seampact.source.value_conditioned_admission import verify_structure_plan


SCHEMA = 'normal-managed-value-conditioned-birth/1'
_GETFRAME = sys._getframe
_PENDING = weakref.WeakKeyDictionary()
_BIRTHS = weakref.WeakKeyDictionary()
_PERMISSIONS = dict(semantic_export=False, invoke=False, dispatch=False, process_start=False, import_prefix=False)
_SHA = re.compile(r'sha256:[0-9a-f]{64}')
_STABLE = ('id', 'group_id', 'session', 'generation', 'profile', 'contract_digest',
           'binding_fingerprint', 'image', 'image_id', 'backend', 'origin')


class _PendingBirth:
    __slots__ = ('__weakref__',)


def _frame(owner, method):
    from seampact.hosting.deployment import Deployments
    from seampact.source.value_conditioned_process import _original_function
    if type(owner) is not Deployments or sys._getframe is not _GETFRAME:
        raise ValueError('the exact normal deployment owner and frame are required')
    if method in owner.__dict__:
        raise ValueError('normal deployment method has an instance shadow')
    function = Deployments.__dict__[method]
    _original_function(function)
    frame = _GETFRAME(2)
    for _ in range(8):
        if frame is None:
            break
        if frame.f_code is function.__code__ and frame.f_locals.get('self') is owner:
            return frame
        frame = frame.f_back
    raise ValueError('normal deployment occurrence is absent; posthoc birth claims are rejected')


def _stage(frame, before):
    tree = ast.parse(Path(frame.f_code.co_filename).read_bytes())
    lines = [node.lineno for node in ast.walk(tree) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)
             and node.func.value.id == 'backends' and node.func.attr == 'start']
    if not lines or (before and frame.f_lineno >= min(lines)) or (not before and frame.f_lineno <= min(lines)):
        raise ValueError('birth evidence is outside the normal pre/post backend start stage')


def _record(owner, record):
    if (type(record) is not dict or owner.instances.get(record.get('id')) is not record
            or record.get('session') != owner.session or record.get('origin') != 'PLATFORM_STARTED'
            or record.get('backend') != 'docker' or type(record.get('generation')) is not int
            or record['generation'] < 1 or record.get('stale')):
        raise ValueError('the actual current managed instance record is required')
    return {key: copy.deepcopy(record.get(key)) for key in _STABLE}


def _mounts(config):
    targets = []
    for mount in config.get('mounts', []):
        target = mount.get('target') if type(mount) is dict else None
        if (type(target) is not str or str(PurePosixPath(target)) != target or
                '..' in PurePosixPath(target).parts or not target.startswith('/') or
                not any(target == root or target.startswith(root + '/') for root in ('/model', '/models'))):
            raise ValueError('mount overlays an unproved startup, Core, source or resolver domain')
        if type(mount.get('source')) is not str or not mount['source'] or type(mount.get('read_only', True)) is not bool:
            raise ValueError('original model-directory mount is malformed')
        targets.append(target)
    return targets


def _artifact(owner, record, artifact, config, reference):
    from seampact.hosting import backends
    from seampact.hosting.deployment import GROUP_PROFILE, binding_fingerprint
    from seampact.hosting.image_build import worker_dockerfile
    from seampact.source.value_conditioned_process import _original_function
    from seampact.target_runtime import core_identity
    from seampact.units import NativeBindings, UnitCatalog
    _original_function(backends.start)
    _original_function(backends.build)
    _original_function(worker_dockerfile)
    if (record.get('profile') != GROUP_PROFILE['id'] or config.get('kind') != 'docker'
            or config.get('pass_env') or any(config.get(key) for key in ('entrypoint', 'command', 'environment'))
            or record.get('gpu_devices') != []):
        raise ValueError('normal default group worker startup conditions remain UNKNOWN')
    targets = _mounts(config)
    catalog = owner.platform.catalog
    if type(catalog) is not UnitCatalog:
        raise ValueError('the actual normal catalog is required')
    group = owner.platform.store.get('runtime_group', record['group_id'])
    if type(group) is not dict or len(group.get('units', [])) != 1 or len(group.get('entries', [])) != 1:
        raise ValueError('birth scope requires one actual structural source root')
    contract = group.get('contract') or {}
    if contract.get('digest') != hashlib.sha256(json.dumps(
            {key: value for key, value in contract.items() if key != 'digest'},
            sort_keys=True, ensure_ascii=False).encode()).hexdigest():
        raise ValueError('the actual frozen group contract content changed')
    uid = group['units'][0]
    pair = catalog.structural.get(uid)
    unit = owner.platform.store.get('unit', uid)
    if (not pair or type(pair[0]) is not NativeBindings or not unit
            or unit.get('binding') != 'SOURCE_VALUE_CONDITIONED_NATIVE_ENTRY' or unit.get('status') != 'STRUCTURE_ONLY'):
        raise ValueError('an actual independently checked structural binding is required')
    binding, entry = pair
    structure = unit.get('structure_admission')
    if (not verify_structure_plan(binding.inspector, entry, binding.candidate, structure)
            or group['entries'] != [entry['id']] or unit.get('runtime_group') != group['id']):
        raise ValueError('current source, H or original root mapping changed')
    if artifact != owner.platform.store.get('deployment', group['id']):
        raise ValueError('the current normal artifact record changed')
    folder = Path(artifact['folder'])
    spec = json.loads((folder / 'worker.json').read_text(encoding='utf-8'))
    environment = artifact.get('environment') or {}
    if (environment.get('python') != '3.11' or environment.get('base_image') != 'python:3.11-slim'
            or any(environment.get(key) for key in ('requirements', 'packages', 'project', 'pip_extra_index_url'))):
        raise ValueError('third-party or source-installed startup baseline is outside the first domain')
    if (spec.get('profile') != GROUP_PROFILE['id'] or spec.get('candidate') != binding.candidate
            or spec.get('request') != binding.inspector.request or spec.get('platform_core') != core_identity('worker')
            or spec.get('group', {}).get('units') != [uid] or spec.get('group', {}).get('entries') != [entry['id']]
            or spec.get('contract_digest') != group.get('contract', {}).get('digest')
            or binding_fingerprint(spec) != artifact.get('binding_fingerprint')
            or record.get('binding_fingerprint') != artifact.get('binding_fingerprint')
            or record.get('contract_digest') != spec.get('contract_digest')):
        raise ValueError('actual artifact source, Core, contract or fingerprint differs')
    # Replay the original finite builder, not a caller-supplied "default" flag.
    check_python = ("import sys; from seampact.hosting.environments import check_python_version; "
                    "assert '.'.join(map(str,sys.version_info[:2])) == " + repr(environment['python']) + "; "
                    "r=check_python_version('.'.join(map(str,sys.version_info[:3])), " +
                    repr(environment.get('python_requirements', [])) +
                    "); assert r['status'] == 'SUPPORTED', r['missing']")
    with tempfile.TemporaryDirectory() as temporary:
        generated = Path(temporary)
        reuse = worker_dockerfile(generated, binding.inspector, environment, check_python)
        if ((generated / 'Dockerfile').read_bytes() != (folder / 'Dockerfile').read_bytes()
                or reuse != json.loads((folder / 'build-reuse.json').read_text(encoding='utf-8'))
                or reuse != artifact.get('build_reuse')):
            raise ValueError('artifact does not use the exact normal default builder recipe')
    build = artifact.get('build') or {}
    image = build.get('image_id')
    if (build.get('status') != 'BUILT' or build.get('builder_driver') != 'docker'
            or not _SHA.fullmatch(image or '') or record.get('image_id') != image
            or build.get('requested_base_image') != environment['base_image']
            or not _SHA.fullmatch(build.get('resolved_base_id') or '')
            or reference not in (image, backends.content_image_reference(image)) or record.get('image') != reference):
        raise ValueError('normal built image and content-bound start reference are unproved')
    commands = [step.get('argv') for step in build.get('steps', []) if step.get('returncode') == 0]
    normal_build = [argv for argv in commands if type(argv) is list and argv[:3] == ['docker', 'buildx', 'build']
                    and '--target' not in argv and argv[-1] == str(folder)
                    and 'BASE_IMAGE=' + backends.content_image_reference(build['resolved_base_id']) in argv]
    if len(normal_build) != 1:
        raise ValueError('normal builder command receipt is absent')
    return dict(spec_digest=digest(spec), structure_digest=structure['plan_digest'], unit_id=uid,
                source_version=structure['source_version'], platform_core=spec['platform_core'],
                baseline=dict(id='normal-default-core-cpython/1', python='3.11', base_image='python:3.11-slim',
                              no_source_install=True, model_mount_targets=targets),
                artifact_support=digest(dict(spec=spec, environment=environment, build=build, reuse=reuse)))


def before_start(owner, record, artifact, config, reference):
    """Register the actual normal pre-start occurrence, never a posthoc claim."""
    frame = _frame(owner, '_start_unprotected')
    _stage(frame, True)
    if any(frame.f_locals.get(key) is not value for key, value in
           (('record', record), ('artifact', artifact), ('config', config))) or frame.f_locals.get('reference') != reference:
        raise ValueError('birth arguments are not the actual normal start locals')
    if frame.f_locals.get('credentials') or record.get('state') != 'UNDEPLOYED' or record.get('handle') or record.get('apply'):
        raise ValueError('source credentials, prior backend work or a warm record is unproved')
    stable = _record(owner, record)
    support = _artifact(owner, record, artifact, config, reference)
    if record['id'] in _BIRTHS.setdefault(owner, {}):
        raise ValueError('this normal birth occurrence was already registered')
    token = _PendingBirth()
    _PENDING[token] = dict(owner=weakref.ref(owner), record=record, stable=stable, support=support,
                           config=copy.deepcopy(config), artifact=copy.deepcopy(artifact), reference=reference)
    _BIRTHS[owner][record['id']] = dict(state='PENDING', token=weakref.ref(token), stable=stable, record=record)
    return token


def _command(result, pending):
    from seampact.hosting import backends
    record, config = pending['record'], pending['config']
    name = 'seampact-' + record['name']
    if result.get('status') != 'APPLIED' or result.get('handle') != dict(kind='docker', container=name):
        raise ValueError('the actual normal backend did not apply the expected container')
    steps = result.get('steps', [])
    if len(steps) != 1 or steps[0].get('returncode') != 0:
        raise ValueError('one successful original docker-run receipt is required')
    argv = steps[0].get('argv')
    prefix = ['docker', 'run', '-d', '--pull=never', '--name', name, '--label', 'seampact.group=' + record['group_id'],
              '--label', 'seampact.instance=' + record['id']]
    if type(argv) is not list or argv[:len(prefix)] != prefix:
        raise ValueError('backend start command is not the original managed command')
    rest = argv[len(prefix):]
    if config.get('docker_network'):
        expected = ['--network', config['docker_network']]
        advertise = 'http://' + name + ':8890'
    else:
        if len(rest) < 2 or rest[0] != '-p':
            raise ValueError('normal published worker port is absent')
        host = config.get('publish_host', '127.0.0.1')
        match = re.fullmatch(re.escape(host) + r':([0-9]+):8890', rest[1])
        if not match or not 1 <= int(match[1]) <= 65535:
            raise ValueError('normal worker port mapping differs')
        expected = rest[:2]
        advertise = 'http://' + config.get('advertise_host', host) + ':' + match[1]
    expected += backends.gpu_runtime_arguments([])
    for mount in config.get('mounts', []):
        expected += ['-v', mount['source'] + ':' + mount['target'] + (':ro' if mount.get('read_only', True) else '')]
    expected += [pending['reference']] + backends.worker_args(record, config['coordinator_url'], advertise)
    if rest != expected:
        raise ValueError('custom environment, entrypoint, mount or worker command was applied')
    container_id = steps[0].get('stdout_tail', '').strip()
    if not re.fullmatch(r'[0-9a-f]{64}', container_id):
        raise ValueError('actual docker container identity is absent')
    return dict(kind='docker', container=name, container_id=container_id), digest(argv), advertise


def confirm_start(owner, token, result):
    frame = _frame(owner, '_start_unprotected')
    _stage(frame, False)
    pending = _PENDING.get(token) if type(token) is _PendingBirth else None
    if pending is None or pending['owner']() is not owner or frame.f_locals.get('result') is not result:
        raise ValueError('matching prospective normal backend occurrence is absent')
    record = pending['record']
    if frame.f_locals.get('record') is not record or _record(owner, record) != pending['stable']:
        raise ValueError('normal instance or generation changed during startup')
    if _artifact(owner, record, pending['artifact'], pending['config'], pending['reference']) != pending['support']:
        raise ValueError('normal artifact support changed during startup')
    handle, command_digest, endpoint = _command(result, pending)
    cell = dict(state='BACKEND_CONFIRMED', stable=pending['stable'], support=pending['support'],
                handle=handle, command_digest=command_digest, endpoint=endpoint, record=record)
    _BIRTHS[owner][record['id']] = cell
    _PENDING.pop(token, None)
    return _public(cell)


def bind_registration(owner, record, health):
    frame = _frame(owner, 'attach')
    if frame.f_locals.get('record') is not record or frame.f_locals.get('health') is not health:
        raise ValueError('actual normal registration locals are required')
    cell = _BIRTHS.get(owner, {}).get(record.get('id'))
    if (not cell or cell['state'] not in ('BACKEND_CONFIRMED', 'REGISTERED') or _record(owner, record) != cell['stable']
            or cell['record'] is not record or record.get('handle', {}).get('container') != cell['handle']['container']
            or record.get('handle', {}).get('kind') != 'docker' or frame.f_locals.get('endpoint') != cell['endpoint']
            or record.get('state') != 'READY' or type(record.get('process_nonce')) is not str or not record['process_nonce']
            or record.get('units') != [] or health.get('units') != []
            or record.get('guarded_units') != [cell['support']['unit_id']]
            or health.get('guarded_units') != [cell['support']['unit_id']]
            or any(health.get(key) != value for key, value in
                   (('session', record['session']), ('instance_id', record['id']), ('generation', record['generation']),
                    ('process_nonce', record['process_nonce']), ('binding_fingerprint', record['binding_fingerprint'])))):
        raise ValueError('normal confirmed backend and registered process are not the same birth')
    if cell.get('process_nonce') not in (None, record['process_nonce']):
        raise ValueError('registered process was replaced')
    cell.update(state='REGISTERED', process_nonce=record['process_nonce'])
    return _public(cell)


def _public(cell):
    stable = cell['stable']
    return dict(schema=SCHEMA, status='MANAGED_BIRTH_CHECKED' if cell['state'] == 'REGISTERED' else cell['state'],
                session=stable['session'], instance_id=stable['id'], generation=stable['generation'],
                process_nonce=cell.get('process_nonce'), image_id=stable['image_id'],
                binding_fingerprint=stable['binding_fingerprint'], contract_digest=stable['contract_digest'],
                handle=copy.deepcopy(cell.get('handle')), support=copy.deepcopy(cell.get('support')),
                command_digest=cell.get('command_digest'), permissions=dict(_PERMISSIONS),
                requires=['ACTUAL_WORKER_SOURCE_TARGETS', 'CLOSED_ROOTS_AND_PRIMITIVE_PORTS',
                          'SAME_CALL_NO_EXTERNAL_IO_OR_ESCAPE', 'SAME_LOCK_THROUGH_USE', 'CURRENT_RESOURCE_AND_PERMIT'])


def read_birth(owner, identity):
    """Pure same-session query of internal birth facts; never consume caller flags."""
    from seampact.hosting.deployment import Deployments
    answer = dict(schema=SCHEMA, status='UNKNOWN', permissions=dict(_PERMISSIONS), reason=None)
    if type(owner) is not Deployments or type(identity) is not dict:
        return dict(answer, reason='normal deployment owner and exact process identity are required')
    cell = _BIRTHS.get(owner, {}).get(identity.get('instance_id'))
    if not cell:
        return dict(answer, reason='prospective normal managed birth is absent')
    record = cell['record']
    try:
        if (owner.instances.get(record['id']) is not record or record.get('stale') or record.get('state') in ('STOPPED', 'FAILED', 'UNKNOWN')
                or _record(owner, record) != cell['stable']
                or any(identity.get(key) != value for key, value in
                       (('session', record['session']), ('instance_id', record['id']), ('generation', record['generation'])))):
            return dict(answer, reason='normal managed process, session or generation changed')
        if cell['state'] != 'REGISTERED':
            return dict(answer, status='PENDING', reason='normal backend application or process registration is not confirmed')
        if cell.get('process_nonce') != record.get('process_nonce') or identity.get('process_nonce') != cell.get('process_nonce'):
            return dict(answer, reason='normal registered process nonce changed')
    except (ValueError, KeyError, TypeError):
        return dict(answer, reason='normal managed instance record changed')
    return _public(cell)
