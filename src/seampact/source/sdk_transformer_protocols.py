"""Finite original transformer/native object protocols, never tensor values.

Profiles are conditional facts from normal fixed resource declarations and
exact metadata bytes. Descriptors are source-interpreter values: they never
import an SDK, load a model, act as runtime objects, or confer cold permission.
The original native identity, constructor/callback/IO effects and resource
conditions must still be independently checked by the normal worker.
"""
import ast
import hashlib
import json
from dataclasses import dataclass, replace
from pathlib import Path

from seampact.source.call_value_conditions import digest
from seampact.source.import_only import _metadata
from seampact.source import sdk_bounded_values as bounded


RULE = 'source-finite-transformer-sdk-protocol/1'
GAP = 'original transformer/native protocol requires independent target, asset and effect verification'
UNHANDLED = object()
_TYPE, _STR, _DICT = type, str, dict


class SDKProtocolUnknown(ValueError):
    pass


class _NotRuntime:
    def __bool__(self):
        raise SDKProtocolUnknown('an SDK descriptor is not an actual truth value')

    def __iter__(self):
        raise SDKProtocolUnknown('an SDK descriptor is not an actual iterable')


@dataclass(frozen=True)
class FixedTransformerProfile:
    _json: str

    def facts(self):
        return json.loads(self._json)


@dataclass(frozen=True, eq=False)
class SDKValue(_NotRuntime):
    kind: str
    origin: tuple
    profile: object = None
    dimensions: tuple = ()
    dtype: object = None
    fields: tuple = ()


@dataclass(frozen=True, eq=False)
class SDKMethod(_NotRuntime):
    receiver: SDKValue
    name: str


def is_sdk_value(value):
    return _TYPE(value) in (SDKValue, SDKMethod)


def _json_bytes(value):
    if _TYPE(value) is not bytes or len(value) > 1048576:
        raise SDKProtocolUnknown('original transformer metadata is not finite native bytes')

    def pairs(values):
        result = {}
        for key, item in values:
            if _TYPE(key) is not _STR or key in result:
                raise SDKProtocolUnknown('original transformer metadata has duplicate/unknown keys')
            result[key] = item
        return result

    def constant(value):
        raise SDKProtocolUnknown('original transformer metadata contains a non-finite number')

    return _metadata(json.loads(value.decode('utf-8'), object_pairs_hook=pairs, parse_constant=constant))


def compile_fixed_transformer_profile(resource, files):
    """Recheck exact bytes against the normal fixed declaration, not a flag."""
    resource = _metadata(resource)
    if (_TYPE(resource) is not _DICT or resource.get('kind') != 'weight' or
            resource.get('configurable') is not False or _TYPE(files) is not _DICT or
            any(_TYPE(key) is not _STR for key in files)):
        raise SDKProtocolUnknown('transformer profile requires an original fixed weight declaration')
    cache = resource.get('placement', {}).get('hub_cache', {})
    basis = resource.get('execution_basis', {})
    if (cache.get('schema') != 'fixed-hub-cache/1' or
            any(_TYPE(cache.get(key)) is not _STR or not cache[key] for key in ('repo_id', 'revision', 'cache_dir')) or
            _TYPE(basis.get('source_version')) is not _STR or _TYPE(resource.get('definition_digest')) is not _STR):
        raise SDKProtocolUnknown('original fixed Hub placement/source definition is incomplete')
    wanted = ('config.json', 'tokenizer_config.json', 'special_tokens_map.json')
    records, values = [], {}
    for name in wanted:
        matches = [row for row in cache.get('required_files', []) if row.get('path') == name]
        if len(matches) != 1 or name not in files:
            raise SDKProtocolUnknown('complete original transformer metadata bytes are absent')
        record, raw = matches[0], files[name]
        if (_TYPE(raw) is not bytes or _TYPE(record.get('bytes')) is not int or len(raw) != record['bytes'] or
                hashlib.sha256(raw).hexdigest() != record.get('sha256')):
            raise SDKProtocolUnknown('original transformer metadata byte identity changed')
        values[name] = _json_bytes(raw)
        records.append(dict(path=name, bytes=len(raw), sha256=record['sha256']))
    config, tokenizer, special = (values[name] for name in wanted)
    if (config.get('model_type') != 't5' or config.get('architectures') != ['T5ForConditionalGeneration'] or
            tokenizer.get('tokenizer_class') not in ('T5Tokenizer', 'T5TokenizerFast') or
            config.get('auto_map') or tokenizer.get('auto_map')):
        raise SDKProtocolUnknown('original local model/tokenizer class selection is outside the finite T5 path')
    eos = tokenizer.get('eos_token')
    added = tokenizer.get('added_tokens_decoder')
    if (_TYPE(eos) is not _STR or not eos or len(eos) > 65536 or special.get('eos_token') != eos or
            _TYPE(added) is not _DICT or len(added) > 1024):
        raise SDKProtocolUnknown('original special-token conversion path is not completely fixed')
    for index, token in added.items():
        if (_TYPE(index) is not _STR or not index.isdecimal() or _TYPE(token) is not _DICT or
                set(token) != {'content', 'lstrip', 'normalized', 'rstrip', 'single_word', 'special'} or
                _TYPE(token.get('content')) is not _STR or len(token['content']) > 65536 or
                any(_TYPE(token.get(key)) is not bool for key in ('lstrip', 'normalized', 'rstrip', 'single_word', 'special'))):
            raise SDKProtocolUnknown('an original AddedToken constructor input is outside the finite ordinary domain')
    if not any(token['content'] == eos for token in added.values()):
        raise SDKProtocolUnknown('original EOS AddedToken association is absent')
    if (config.get('torch_dtype') != 'float32' or _TYPE(config.get('vocab_size')) is not int or config['vocab_size'] <= 0 or
            config.get('is_encoder_decoder') is not True or tokenizer.get('truncation_side', 'right') != 'right' or
            tokenizer.get('padding_side', 'right') != 'right' or config.get('quantization_config') is not None):
        raise SDKProtocolUnknown('original dtype/model/tokenizer settings are outside the finite unmodified path')
    body = dict(schema='fixed-transformer-source-profile/1', source_version=basis['source_version'],
        resource_id=resource['id'], origin_id=resource.get('origin_id'), definition_digest=resource['definition_digest'],
        placement=cache, metadata_files=records, selected_binding_count=0,
        tokenizer_class='T5TokenizerFast', model_class='T5ForConditionalGeneration', eos_text=eos,
        eos_storage='original AddedToken from complete added_tokens_decoder', added_token_count=len(added),
        model_input_names=['input_ids', 'attention_mask'], vocab_size=config['vocab_size'], original_dtype='float32',
        return_dict=config.get('return_dict', True), torchscript=config.get('torchscript', False),
        original_kwargs='AutoTokenizer use_fast omitted=True; trust_remote_code omitted; model device_map=auto; torch_dtype omitted',
        default_requirements=['original AutoTokenizer local registry/availability and omitted defaults',
            'original PretrainedConfig return_dict=True and torchscript=False defaults when omitted',
            'original unmodified T5Fast model_input_names and padding/truncation defaults',
            'original complete AddedToken construction and native str(content) protocol',
            'original factory IO/caches/native hooks/dtype/device/normal and exceptional effects'],
        execution_permission=False, lifecycle_permissions=[])
    body['digest'] = digest(body)
    return FixedTransformerProfile(json.dumps(body, sort_keys=True, ensure_ascii=False, allow_nan=False))


def _profile(checker, checkpoint, kwargs):
    profiles = getattr(checker.call_context, 'sdk_profiles', ()) if checker.call_context is not None else ()
    if _TYPE(profiles) is not tuple:
        return None
    matches = []
    for profile in profiles:
        if _TYPE(profile) is not FixedTransformerProfile:
            continue
        fact = profile.facts()
        if (fact['source_version'] == checker.inspector.request.get('source_version') and
                fact['placement']['repo_id'] == checkpoint and kwargs.get('cache_dir') == fact['placement']['cache_dir']):
            matches.append(profile)
    return matches[0] if len(matches) == 1 else None


def profiles_from_fixed_resources(resources, entry_id):
    """Read small metadata from normal fixed resources; never load SDK/weights.

    These facts do not prove the original first lookup, class state or factory
    effects. The normal resource check and actual-target verifier stay separate.
    """
    if resources is None:
        return ()
    from seampact.configuration.dependencies import DependencyBindings
    from seampact.configuration.hub_cache import validate_hub_cache_spec, _safe_file
    if _TYPE(resources) is not DependencyBindings:
        raise SDKProtocolUnknown('transformer metadata requires the original normal resource binding')
    profiles = []
    for raw in resources.rows.values():
        resource = _metadata(raw)
        if (resource.get('kind') != 'weight' or resource.get('configurable') is not False or
                entry_id not in resource.get('entry_checks', {}) or
                resource['entry_checks'][entry_id].get('applicability') == 'SOURCE_PROVED_NOT_USED' or
                'hub_cache' not in resource.get('placement', {})):
            continue
        spec = validate_hub_cache_spec(resource['placement']['hub_cache'])
        names = {'config.json', 'tokenizer_config.json', 'special_tokens_map.json'}
        if not names <= {row['path'] for row in spec['required_files']}:
            continue
        repo = Path(spec['cache_dir']) / ('models--' + spec['repo_id'].replace('/', '--'))
        snapshot, blobs = repo / 'snapshots' / spec['revision'], repo / 'blobs'
        if not snapshot.is_dir() or snapshot.is_symlink() or blobs.is_symlink():
            raise SDKProtocolUnknown('normal fixed transformer metadata roots changed')
        files = {}
        for name in sorted(names):
            path = _safe_file(snapshot, blobs, name)
            with path.open('rb') as stream:
                files[name] = stream.read(1048577)
        profiles.append(compile_fixed_transformer_profile(resource, files))
    return tuple(profiles)


def source_owner_escape(value, active=None, depth=0):
    """Check actual finite argument carriers; no name-based owner exemption."""
    from seampact.source.dependency_closure import _LiteralNone
    if depth > 32:
        return 'UNKNOWN'
    kind = _TYPE(value)
    if kind is _LiteralNone or kind in (str, int, float, bool) or bounded.is_bounded(value):
        return 'NO_SOURCE_OWNER_ARGUMENT'
    if kind is SDKValue:
        return 'NO_SOURCE_OWNER_ARGUMENT'
    if kind not in (list, tuple, dict):
        return 'UNKNOWN'  # Includes raw None, opaque objects and callbacks.
    if kind is dict and any(_TYPE(key) is not _STR for key in value):
        return 'UNKNOWN'
    active = set() if active is None else active
    if id(value) in active:
        return 'UNKNOWN'
    active.add(id(value))
    try:
        items = value.values() if kind is dict else value
        return ('NO_SOURCE_OWNER_ARGUMENT' if all(source_owner_escape(item, active, depth + 1) == 'NO_SOURCE_OWNER_ARGUMENT'
                                                 for item in items) else 'UNKNOWN')
    finally:
        active.remove(id(value))


def _input(value):
    from seampact.source.dependency_closure import _LiteralNone
    kind = _TYPE(value)
    if value is None:
        return {'unknown_source_value': True}
    if kind is _LiteralNone:
        return None
    if kind in (str, int, float, bool):
        return _metadata(value)
    if bounded.is_bounded(value):
        return dict(domain=kind.__name__, fields={name: _input(getattr(value, name)) for name in value.__dataclass_fields__})
    if kind is SDKValue:
        return dict(sdk_kind=value.kind, origin=list(value.origin), dimensions=[_input(item) for item in value.dimensions],
                    dtype=value.dtype, profile_digest=None if value.profile is None else value.profile.facts()['digest'])
    if kind is tuple or kind is list:
        return [_input(item) for item in value]
    if kind is dict and all(_TYPE(key) is _STR for key in value):
        return {key: _input(item) for key, item in value.items()}
    return {'unknown_source_protocol': kind.__name__}


def _origin(checker, path, node):
    return (checker.inspector.request.get('source_version'), path, node.lineno, node.col_offset,
            hashlib.sha256(ast.dump(node, include_attributes=False).encode()).hexdigest())


def _require(checker, operation, args, kwargs, path, node, profile=None, *, result=None):
    compiling = checker.call_context is None or checker.call_context.compiling
    no_owner = source_owner_escape([args, kwargs])
    input_checked = no_owner == 'NO_SOURCE_OWNER_ARGUMENT' and (profile is not None or not operation.startswith('transformers.'))
    requirement = dict(schema=RULE, operation=operation, source_version=checker.inspector.request.get('source_version'),
        source_call=dict(path=path, source_sha256=checker.inspector.files[path]['sha256'], line=node.lineno,
                         col=node.col_offset, ast_sha256=_origin(checker, path, node)[-1]),
        inputs=_input([args, kwargs]), input_status='REQUIRES_ACTUAL_CALL' if compiling else 'CHECKED' if input_checked else 'UNKNOWN',
        profile=None if profile is None else profile.facts(),
        source_owner_argument=no_owner,
        result_structure=result, normal_result_only=True,
        original_effects=['original import/definition and factory class selection', 'original IO/cache/native allocation/device and hook effects',
                          'all original callback/normal/exception paths; no tensor or score evaluated'],
        target_status='REQUIRES_NORMAL_WORKER_CHECK', execution_permission=False, lifecycle_permissions=[])
    checker.protocol(RULE, path, node, target_requirements=requirement)
    verified = getattr(checker.call_context, 'runtime_verified', ()) if checker.call_context is not None else ()
    if not compiling and (not input_checked or _TYPE(verified) is not tuple or any(_TYPE(item) is not _STR for item in verified) or digest(requirement) not in verified):
        checker.negative_gap(path, node, GAP)
    return requirement


def summarize_factory(checker, target, args, kwargs, path, node):
    if target not in ('transformers.AutoTokenizer.from_pretrained', 'transformers.AutoModelForSeq2SeqLM.from_pretrained', 'torch.nn.Softmax'):
        return UNHANDLED
    compiling = checker.call_context is None or checker.call_context.compiling
    if not compiling and source_owner_escape([args, kwargs]) != 'NO_SOURCE_OWNER_ARGUMENT':
        checker.negative_gap(path, node, 'original SDK factory has an unproved argument/source-owner escape protocol')
        return None
    if target == 'torch.nn.Softmax':
        if args or kwargs != {'dim': -1}:
            checker.negative_gap(path, node, 'original finite Softmax constructor binding is UNKNOWN')
            return None
        _require(checker, target, args, kwargs, path, node, result='original torch.nn.Softmax; no source owner argument')
        return SDKValue('nn-softmax', _origin(checker, path, node))
    if (len(args) != 1 or _TYPE(args[0]) is not _STR or
            any(_TYPE(name) is not _STR for name in kwargs) or
            set(kwargs) - ({'cache_dir'} if target.endswith('AutoTokenizer.from_pretrained') else {'cache_dir', 'device_map'}) or
            target.endswith('AutoModelForSeq2SeqLM.from_pretrained') and kwargs.get('device_map') != 'auto'):
        checker.negative_gap(path, node, 'original finite transformer factory arguments/defaults are UNKNOWN')
        return None
    profile = _profile(checker, args[0], kwargs)
    _require(checker, target, args, kwargs, path, node, profile,
             result='opaque original factory result' if profile is None else 'original local finite T5 class result')
    if profile is None:
        # A parameterized class capability is not an observed/ready object.
        # Every actual getter/call retains matching original class/profile
        # premises, so an absent runtime profile still fails target checking.
        kind = 'tokenizer' if target.endswith('AutoTokenizer.from_pretrained') else 'model'
        return SDKValue(kind, _origin(checker, path, node))
    kind = 'tokenizer' if target.endswith('AutoTokenizer.from_pretrained') else 'model'
    return SDKValue(kind, _origin(checker, path, node), profile)


def sdk_attribute(checker, parent, name, path, node, *, calling=False):
    if _TYPE(parent) is not SDKValue:
        return UNHANDLED
    profile = parent.profile
    if parent.kind == 'tokenizer' and name == 'eos_token' and not calling:
        _require(checker, 'transformers.SpecialTokensMixin.eos_token', [parent], {}, path, node, profile,
                 result='original non-None AddedToken converted by original native str(content)')
        return (profile.facts()['eos_text'] if profile is not None else
                SDKValue('sdk-string', parent.origin, fields=(('source', 'conditional original non-None EOS string'),)))
    if parent.kind == 'model' and name == 'device' and not calling:
        _require(checker, 'transformers.PreTrainedModel.device', [parent], {}, path, node, profile,
                 result='original torch.device; device identity/value not guessed')
        return SDKValue('device', parent.origin, profile)
    supported = {'model': {'eval'}, 'tokenizer': set(), 'nn-softmax': set(), 'batch-encoding': {'items'},
                 'tensor': {'to', 'size', 'squeeze', 'cpu', 'numpy', 'max', 'item'}, 'ndarray': {'tolist'},
                 'sdk-string': {'join'}}
    if calling and name in supported.get(parent.kind, set()):
        return SDKMethod(parent, name)
    if parent.kind == 'model-output' and name == 'logits' and not calling:
        return dict(parent.fields).get('logits')
    checker.negative_gap(path, node, 'original SDK object attribute/descriptor protocol remains UNKNOWN')
    return None


def sdk_call(checker, function, args, kwargs, path, scope, node):
    if _TYPE(function) is SDKMethod and function.receiver.kind == 'sdk-string' and function.name == 'join':
        if len(args) != 1 or kwargs or _TYPE(args[0]) not in (list, tuple) or any(
                _TYPE(item) not in (str, bounded.BoundedString) and not (
                    _TYPE(item) is SDKValue and item.kind == 'sdk-string') for item in args[0]):
            checker.negative_gap(path, node, 'original SDK string join operands remain UNKNOWN')
            return None
        _require(checker, 'builtins.str.join', [function.receiver, args[0]], {}, path, node,
                 function.receiver.profile, result='original successful exact string; no text or length invented')
        return SDKValue('sdk-string', _origin(checker, path, node), function.receiver.profile)
    if _TYPE(function) is SDKValue and function.kind == 'tokenizer':
        texts = args[0] if len(args) == 1 else None
        if _TYPE(texts) is list and all(_TYPE(item) in (str, bounded.BoundedString) or
                _TYPE(item) is SDKValue and item.kind == 'sdk-string' for item in texts):
            count = len(texts)
        elif _TYPE(texts) is bounded.BoundedList and _TYPE(texts.element) in (str, bounded.BoundedString):
            count = bounded.length(texts)
        else:
            count = None
        maximum = kwargs.get('max_length')
        if (count is None or _TYPE(maximum) is not int or maximum <= 0 or
                set(kwargs) != {'max_length', 'truncation', 'padding', 'return_tensors'} or
                kwargs.get('truncation') is not True or kwargs.get('padding') is not True or
                kwargs.get('return_tensors') != 'pt'):
            checker.negative_gap(path, node, 'original finite tokenizer batch/argument domain remains UNKNOWN')
            return None
        _require(checker, 'transformers.PreTrainedTokenizerBase.__call__', [function, texts], kwargs,
                 path, node, function.profile,
                 result='conditional original T5Fast ordered input_ids/attention_mask int64 tensors; no token values invented')
        dimensions = (count, bounded.BoundedInteger(0, maximum))
        fields = tuple((name, SDKValue('tensor', _origin(checker, path, node), function.profile,
                                     dimensions=dimensions, dtype='int64'))
                       for name in ('input_ids', 'attention_mask'))
        return SDKValue('batch-encoding', _origin(checker, path, node), function.profile, fields=fields)
    if _TYPE(function) is SDKMethod and function.receiver.kind == 'batch-encoding' and function.name == 'items':
        if args or kwargs:
            return None
        from seampact.source.call_value_conditions import FiniteIteration
        _require(checker, 'transformers.BatchEncoding.items', [function.receiver], {}, path, node,
                 function.receiver.profile, result='same original mapping order and tensor references')
        return FiniteIteration(function.receiver.fields, id(node))
    if _TYPE(function) is SDKMethod and function.receiver.kind == 'tensor':
        receiver, name = function.receiver, function.name
        if name == 'size' and len(args) == 1 and _TYPE(args[0]) is int and not kwargs:
            index = args[0]
            if not -len(receiver.dimensions) <= index < len(receiver.dimensions):
                checker.negative_gap(path, node, 'original tensor size index is outside proved rank')
                return None
            _require(checker, 'torch.Tensor.size', [receiver, index], {}, path, node,
                     receiver.profile, result='same original tensor dimension interval')
            return receiver.dimensions[index]
        if ((name == 'to' and len(args) == 1 and _TYPE(args[0]) is SDKValue and args[0].kind == 'device') or
                name in ('cpu', 'numpy') and not args) and not kwargs:
            _require(checker, 'torch.Tensor.' + name, [receiver, *args], {}, path, node,
                     receiver.profile, result='same shape/dtype; original transfer/alias/exception effects retained')
            return replace(receiver, kind='ndarray' if name == 'numpy' else 'tensor', origin=_origin(checker, path, node))
        if name == 'squeeze' and len(args) == 1 and _TYPE(args[0]) is int and not kwargs:
            index = args[0]
            if -len(receiver.dimensions) <= index < len(receiver.dimensions):
                index %= len(receiver.dimensions)
                dimension = receiver.dimensions[index]
                if _TYPE(dimension) is int or _TYPE(dimension) is bounded.BoundedInteger and (
                        dimension.lower == dimension.upper or dimension.upper < 1 or dimension.lower > 1):
                    is_one = dimension == 1 if _TYPE(dimension) is int else dimension.lower == dimension.upper == 1
                    shape = receiver.dimensions[:index] + receiver.dimensions[index + 1:] if is_one else receiver.dimensions
                    _require(checker, 'torch.Tensor.squeeze', [receiver, args[0]], {}, path, node,
                             receiver.profile, result='original dimension removal only when exactly one; no tensor values invented')
                    return replace(receiver, dimensions=shape, origin=_origin(checker, path, node))
    if _TYPE(function) is SDKMethod and function.receiver.kind == 'model' and function.name == 'eval':
        if args or kwargs:
            return None
        _require(checker, 'torch.nn.Module.eval', [function.receiver], {}, path, node, function.receiver.profile,
                 result='same original model; original recursive training-state mutation retained')
        return function.receiver
    if is_sdk_value(function):
        checker.negative_gap(path, node, 'original SDK callable/result protocol remains UNKNOWN')
        return None
    return UNHANDLED


def sdk_subscript(checker, parent, index, path, node):
    if _TYPE(parent) is not SDKValue:
        return UNHANDLED
    if parent.kind == 'batch-encoding' and _TYPE(index) is str and index in dict(parent.fields):
        _require(checker, 'transformers.BatchEncoding.__getitem__', [parent, index], {}, path, node,
                 parent.profile, result='same original mapping tensor reference')
        return dict(parent.fields)[index]
    checker.negative_gap(path, node, 'original SDK indexed-result structure remains UNKNOWN')
    return None


def sdk_iteration(checker, value, node):
    return None


def verify_transformer_targets(requirements, context=None, *, profiles=()):
    """Compare normal re-read profiles, retaining all unclosed SDK premises.

    The caller builds ``profiles`` again from the current normal fixed
    resource declarations and actual metadata bytes. Matching those bytes is
    an asset fact, never proof of a model's internal state or cold permission.
    """
    rows = []
    if _TYPE(requirements) is list and _TYPE(profiles) is tuple:
        actual = {profile.facts()['digest']: profile.facts() for profile in profiles if _TYPE(profile) is FixedTransformerProfile}
        for value in requirements:
            row = dict(status='UNKNOWN', allowed=False, resource_metadata='UNKNOWN')
            try:
                requirement = _metadata(value)
                if (requirement.get('schema') != RULE or requirement.get('execution_permission') is not False or
                        requirement.get('target_status') != 'REQUIRES_NORMAL_WORKER_CHECK'):
                    raise SDKProtocolUnknown('original transformer source requirement is malformed')
                source = requirement.get('profile')
                if (_TYPE(source) is _DICT and source.get('digest') in actual and actual[source['digest']] == source and
                        source.get('source_version') == requirement.get('source_version')):
                    row.update(resource_metadata='MATCHED_NORMAL_FIXED_DECLARATION_AND_BYTES',
                               profile_digest=source['digest'])
                row['operation'] = requirement.get('operation')
                row['reason'] = 'asset metadata does not close original SDK object, mutable state, callback or effect provenance'
            except (ValueError, TypeError, KeyError) as error:
                row['reason'] = str(error)
            rows.append(row)
    return dict(schema=RULE, status='UNKNOWN', allowed=False, execution_permission=False,
        remaining=['original checked SDK/native class/property/call targets and complete creation provenance',
                   'same current fixed resource declaration and original first effective metadata/weight lookup',
                   'original dtype/device/default, mutable registry/cache/hook and full normal/exception effects',
                   'original SDK object owner/read-use relation; no predictive model construction'], targets=rows)
