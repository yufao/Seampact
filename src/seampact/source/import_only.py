"""Source proof for the original import/definition prefix, never its execution.

The normal controller and worker replay this plan against their own frozen
binding and resources. SDK effects require a separate trusted semantic leaf;
an installed distribution name, file hash or candidate flag cannot add one.
"""
import copy
import math

from seampact.source.call_value_conditions import Context, contextual, digest


_TYPE, _DICT, _LIST, _STR, _INT, _BOOL, _FLOAT = type, dict, list, str, int, bool, float


def _metadata(value, active=None, depth=0):
    if depth > 64:
        raise ValueError('frozen import-plan metadata exceeds finite depth 64')
    kind = _TYPE(value)
    if value is None or kind is _STR or kind is _INT or kind is _BOOL:
        return value
    if kind is _FLOAT and math.isfinite(value):
        return value
    if kind is not _DICT and kind is not _LIST:
        raise ValueError('frozen import-plan metadata is not native JSON')
    active = set() if active is None else active
    if id(value) in active:
        raise ValueError('frozen import-plan metadata is cyclic')
    active.add(id(value))
    try:
        if kind is _LIST:
            return [_metadata(item, active, depth + 1) for item in value]
        if any(_TYPE(key) is not _STR for key in value):
            raise ValueError('frozen import-plan metadata has an unknown key protocol')
        return {key: _metadata(item, active, depth + 1) for key, item in value.items()}
    finally:
        active.remove(id(value))


def compile_import_only_plan(inspector, entry, candidate, frozen_resources):
    """Freeze only a complete original module prefix; do not enter a callable.

    CHECKED is a source plan, consumed with the normal reservation, current
    source/resource/loader checks and the actual worker identity. It does not
    authorize constructors, initializers, model loads or detection.
    """
    result = dict(schema='source-import-only-plan/1', status='UNKNOWN',
                  source_version=inspector.request.get('source_version'),
                  entry_id=None, path=None, entry_digest=None, candidate_digest=None,
                  resource_digest=None, modules_order=[], activation_prefix=None,
                  top_calls=[], target_requirements=[], reason=None)
    try:
        entry, candidate = _metadata(entry), _metadata(candidate)
        resources = _metadata(frozen_resources)
        if _TYPE(entry) is not _DICT or _TYPE(candidate) is not _DICT or _TYPE(resources) is not _DICT:
            raise ValueError('import-only entry/candidate/resources must be frozen JSON objects')
        result.update(entry_id=entry.get('id'), path=entry.get('path'),
                      entry_digest=digest(entry), candidate_digest=digest(candidate), resource_digest=digest(resources))
        if (entry.get('kind') not in {'function', 'method'} or not result['source_version']
                or not isinstance(entry.get('path'), str) or not entry['path'].endswith('.py')
                or entry['path'] not in inspector.files
                or entry not in candidate.get('entries', [])):
            raise ValueError('import-only preparation requires the exact frozen original Python entry')
        from seampact.source.dependency_closure import _Closure
        source = contextual(inspector, Context(compiling=True, initialization_only=True))
        checker = _Closure(source, candidate)
        checker.import_roots = {}
        checker.sdk_source.bind_activation(entry, candidate, checker=checker)
        checker.module(entry['path'])  # No constructor, initializer or entry.
        prefix = checker.activation_prefix()
        result.update(activation_prefix=prefix, top_calls=copy.deepcopy(checker.external_calls),
                      modules_order=[event['path'] for event in prefix['events'] if event['kind'] == 'MODULE_BEGIN'])
        result['target_requirements'] = [
            dict(kind='CURRENT_FROZEN_SOURCE_AND_RESOURCE_BINDING', source_version=result['source_version'],
                 entry_digest=result['entry_digest'], candidate_digest=result['candidate_digest'], resource_digest=result['resource_digest']),
            dict(kind='ORIGINAL_CONTROLLED_SOURCE_LOADER', scope='same frozen source/context; no added prefix code'),
            dict(kind='COMPLETE_SELECTED_WORKING_SET_RESERVED', scope='all strategies pay original import/setup/failure cost'),
            dict(kind='MODULE_PREFIX_ONLY', prohibited=['entry constructor','source initializers','NN/model construction','entry/detection invocation']),
        ]
        from seampact.source.activation_scope import RULE as ACTIVATION_RULE, GAP as ACTIVATION_GAP
        from seampact.source.call_value_conditions import runtime_requirements
        from seampact.source.sdk_import_summaries import RULE as IMPORT_RULE, FINDER, GAP as IMPORT_GAP, asset_operations
        from seampact.source.stdlib_definition_prefix import RULE as STDLIB_RULE
        pending = runtime_requirements(dict(external_protocols=checker.protocols))
        prefix_targets = [row for row in pending if row['schema'] in {ACTIVATION_RULE, IMPORT_RULE, FINDER}]
        if prefix_targets:
            result['runtime_protocol_requirements'] = prefix_targets
        stdlib = [copy.deepcopy(row['target_requirements']) for row in checker.protocols
                  if row.get('rule') == STDLIB_RULE]
        if stdlib:
            result['stdlib_requirements'] = stdlib
        operations = asset_operations(prefix_targets)
        assets_checked = True
        if operations:
            from seampact.source.sdk_assets import compile_sdk_asset_plan
            result['sdk_asset_plan'] = compile_sdk_asset_plan(inspector, entry, candidate, resources, operations)
            assets_checked = result['sdk_asset_plan']['status'] == 'CHECKED'
        if (prefix['status'] != 'CHECKED' or checker.unknown or
                any(row.get('reason') not in {ACTIVATION_GAP, IMPORT_GAP} for row in checker.negative_scope_gaps) or
                checker.negative_scope_gaps and not prefix_targets or not assets_checked):
            result['reason'] = 'original import/definition activation or its effect premises remain UNKNOWN'
        else:
            result.update(status='CHECKED', reason=None)
    except (ValueError, KeyError, TypeError, SyntaxError, OSError) as error:
        result['reason'] = str(error)
    return dict(result, plan_digest=digest(result))


def verify_import_only_plan(inspector, entry, candidate, frozen_resources, plan):
    """Replay all source/resource identities; an UNKNOWN plan never permits it."""
    if _TYPE(plan) is not _DICT:
        return False
    try:
        supplied = _metadata(plan)
        return supplied.get('status') == 'CHECKED' and supplied == compile_import_only_plan(
            inspector, entry, candidate, frozen_resources)
    except (ValueError, KeyError, TypeError, SyntaxError, OSError):
        return False
