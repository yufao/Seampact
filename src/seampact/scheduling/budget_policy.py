"""Frozen multi-resource epsilon constraints and block settlement."""
import copy
import math


def _number(v):
    return type(v) in (int, float) and math.isfinite(v)


def dimensions(spec):
    return ['ram', 'vram', 'latency', 'unfinished'] + ['quality:' + q['metric'] for q in spec['quality_budgets']]


def validate(spec, algorithm, mode, episodes):
    if spec is None:
        return None
    fields = {'schema', 'points', 'scales', 'resource_keys', 'resource_units', 'dual_step', 'initial_multipliers', 'blocks', 'normalizer', 'api', 'quality_budgets'}
    if (not isinstance(spec, dict) or set(spec) != fields | ({'freeze_point'} if mode == 'freeze' else set())
            or spec.get('schema') != 'budget-policy/2' or algorithm != 'ppo' or mode not in {'train', 'freeze'}):
        raise ValueError('budget-policy/2 requires explicit fields and PPO train/freeze')
    quality = spec['quality_budgets']
    if not isinstance(quality, list):
        raise ValueError('quality_budgets must be a list')
    ids = set()
    for q in quality:
        if (not isinstance(q, dict) or set(q) != {'metric', 'undelivered_loss'} or not isinstance(q['metric'], str)
                or not q['metric'] or q['metric'] in ids or not _number(q['undelivered_loss'])
                or not 0 <= q['undelivered_loss'] <= 1):
            raise ValueError('quality budgets require distinct metric and explicit undelivered loss')
        ids.add(q['metric'])
    names = dimensions(spec)
    if (not isinstance(spec['initial_multipliers'], dict) or set(spec['initial_multipliers']) != set(names)
            or any(not _number(x) or x < 0 for x in spec['initial_multipliers'].values())):
        raise ValueError('initial_multipliers must explicitly cover all dimensions')
    if (not isinstance(spec['scales'], dict) or set(spec['scales']) != set(names)
            or any(not _number(x) or x <= 0 for x in spec['scales'].values())):
        raise ValueError('each component needs an explicit positive scale')
    keys = spec['resource_keys']
    if (not isinstance(keys, dict) or set(keys) != {'ram', 'vram'} or
            any(not isinstance(group, list) or not group or any(not isinstance(x, str) or not x for x in group)
                for group in keys.values())):
        raise ValueError('resource_keys must list RAM and VRAM declared integrals')
    flat = keys['ram'] + keys['vram']
    if len(flat) != len(set(flat)):
        raise ValueError('resource integral keys must be distinct')
    if (not isinstance(spec['resource_units'], dict) or set(spec['resource_units']) != {'ram', 'vram'}
            or any(unit not in ('MiB', 'GiB') for unit in spec['resource_units'].values())):
        raise ValueError('declare RAM and VRAM resource units as MiB or GiB')
    if any(not _number(spec[k]) or spec[k] <= 0 for k in ('normalizer', 'dual_step')):
        raise ValueError('normalizer and dual_step must be positive')
    if spec['api'] not in ({'status': 'NOT_APPLICABLE'}, {'status': 'UNKNOWN'}):
        raise ValueError('API status must be NOT_APPLICABLE or UNKNOWN')
    points = spec['points']
    if not isinstance(points, list) or not points:
        raise ValueError('points must be nonempty')
    point_ids = set()
    for p in points:
        if (not isinstance(p, dict) or set(p) != {'id', 'objective', 'limits'} or not isinstance(p['id'], str)
                or not p['id'] or p['id'] in point_ids or p['objective'] not in ('ram', 'vram')
                or not isinstance(p['limits'], dict) or set(p['limits']) != set(names) - {p['objective']}
                or any(not _number(v) or v < 0 for v in p['limits'].values())):
            raise ValueError('point needs an objective and all other component limits')
        point_ids.add(p['id'])
    if mode == 'freeze' and spec['freeze_point'] not in point_ids:
        raise ValueError('freeze_point must identify a point')
    if not isinstance(spec['blocks'], list) or not spec['blocks']:
        raise ValueError('blocks must be a nonempty pre-frozen schedule')
    window_ids, counts, paired_groups, paired_strata, group_definitions = [], {p: 0 for p in point_ids}, {}, {}, {}
    for block in spec['blocks']:
        if (not isinstance(block, dict) or set(block) != {'windows', 'point', 'quality', 'cost_weights'} or block['point'] not in point_ids
                or not isinstance(block['windows'], list) or not block['windows']
                or any(type(w) is not int or w < 0 for w in block['windows'])
                or not isinstance(block['quality'], dict) or set(block['quality']) != ids):
            raise ValueError('block needs windows, point and all quality denominators')
        weights = block['cost_weights']
        if (not isinstance(weights, dict) or set(weights) != {'ram', 'vram', 'latency', 'unfinished'}
                or any(not isinstance(values, list) or len(values) != len(block['windows'])
                       or any(not _number(v) or v < 0 for v in values) or not any(values)
                       for values in weights.values())):
            raise ValueError('block cost weights must explicitly cover each component and window')
        if mode == 'freeze' and block['point'] != spec['freeze_point']:
            raise ValueError('frozen blocks must use freeze_point')
        counts[block['point']] += 1
        window_ids.extend(block['windows'])
        for metric, groups in block['quality'].items():
            if not isinstance(groups, list) or not groups:
                raise ValueError('quality denominator requires applicable groups')
            seen_groups, seen_requests = set(), set()
            for group in groups:
                if (not isinstance(group, dict) or set(group) != {'group_id', 'stratum', 'weight', 'requests'}
                        or not isinstance(group['group_id'], str) or not group['group_id'] or group['group_id'] in seen_groups
                        or not isinstance(group['stratum'], str) or not group['stratum']
                        or not _number(group['weight']) or group['weight'] < 0 or not isinstance(group['requests'], list)
                        or not group['requests']):
                    raise ValueError('quality group identity, weight or requests invalid')
                seen_groups.add(group['group_id'])
                for mapping, identity in ((paired_groups, group['group_id']), (paired_strata, group['stratum'])):
                    per_point = mapping.setdefault((metric, identity), {p: 0 for p in point_ids})
                    per_point[block['point']] += 1
                for request in group['requests']:
                    if (not isinstance(request, dict) or set(request) != {'task_id', 'weight'}
                            or not isinstance(request['task_id'], str) or not request['task_id']
                            or request['task_id'] in seen_requests or not _number(request['weight']) or request['weight'] < 0):
                        raise ValueError('quality request identity or weight invalid')
                    seen_requests.add(request['task_id'])
                if not math.isclose(sum(x['weight'] for x in group['requests']), 1.0, abs_tol=1e-9):
                    raise ValueError('within-group request weights must sum to one')
                signature = (group['stratum'], tuple(sorted((x['task_id'], x['weight']) for x in group['requests'])))
                prior = group_definitions.setdefault((metric, group['group_id']), signature)
                if prior != signature:
                    raise ValueError('a group must retain its stratum, requests and weights across blocks')
            if not math.isclose(sum(x['weight'] for x in groups), 1.0, abs_tol=1e-9):
                raise ValueError('group weights must sum to one')
    if window_ids != list(range(episodes)):
        raise ValueError('blocks must partition all windows once in order')
    if mode == 'train' and max(counts.values()) - min(counts.values()) > 1:
        raise ValueError('budget schedule must balance point counts')
    if mode == 'train' and any(not count for count in counts.values()):
        raise ValueError('every budget point needs at least one collection block')
    if mode == 'train' and any(max(v.values()) - min(v.values()) > 1
                               for v in [*paired_groups.values(), *paired_strata.values()]):
        raise ValueError('quality group and stratum rotation must balance budget points')
    return copy.deepcopy(spec)


def validate_cohorts(spec, task_ids, windows, criteria_for_task):
    if not spec:
        return {}
    definitions = {}
    for block in spec['blocks']:
        members = [t for w in block['windows'] for t in windows[w]['task_ids']]
        if len(members) != len(set(members)):
            raise ValueError('task identities must be unique within each block')
        for metric, groups in block['quality'].items():
            for group in groups:
                for request in group['requests']:
                    identity = request['task_id']
                    if identity not in members or identity not in task_ids:
                        raise ValueError('quality member must belong to its frozen block')
                    criterion = criteria_for_task(identity)
                    dim = next((d for d in criterion['dimensions'] if d['id'] == metric), None)
                    if (dim is None or dim.get('kind') == 'cost' or dim.get('level') == 'module'
                            or not dim.get('applicable', True) or not dim.get('critical', True)
                            or dim.get('bounds') != dim['range']):
                        raise ValueError('quality metric must be applicable, bounded and critical')
                    signature = [copy.deepcopy(dim), criterion.get('prompt'), copy.deepcopy(criterion.get('teacher')),
                                 copy.deepcopy(criterion.get('evidence_fields'))]
                    if metric in definitions and definitions[metric] != signature:
                        raise ValueError('quality metric semantics must match across the run')
                    definitions[metric] = signature
    return definitions


def multiplier_count(spec):
    return len(dimensions(spec))


def condition(spec, index, dual):
    names, point = dimensions(spec), spec['points'][index]
    if len(dual) != len(names):
        raise ValueError('multipliers do not match dimensions')
    return ([float(point['objective'] == 'ram'), float(point['objective'] == 'vram')]
            + [v for name in names for v in (float(name in point['limits']), float(point['limits'].get(name, 0)))]
            + list(dual))


def condition_size(spec):
    return 2 + 3 * len(dimensions(spec))


def training_semantics(spec):
    return {k: copy.deepcopy(v) for k, v in spec.items() if k not in ('freeze_point', 'blocks')} if spec else spec


def budget_index(spec, seed, number):
    block = next(b for b in spec['blocks'] if number in b['windows'])
    return next(i for i, p in enumerate(spec['points']) if p['id'] == block['point'])


def _resource(spec, name, values):
    result = sum(values[key] for key in spec['resource_keys'][name])
    return result / 1024.0 if spec['resource_units'][name] == 'MiB' else result


def settle_block(items, spec, block, index, dual, training):
    names, point = dimensions(spec), spec['points'][index]
    totals, errors, records = {name: 0.0 for name in names}, [], {}
    for position, item in enumerate(items):
        weights = {name: values[position] for name, values in block['cost_weights'].items()}
        envelope = item['costs'].get('resource_envelope') or {}
        values = envelope.get('resource_seconds') or {}
        for name in ('ram', 'vram'):
            keys = spec['resource_keys'][name]
            if envelope.get('status') != 'KNOWN' or any(not _number(values.get(key)) for key in keys):
                errors.append('resource_unknown:' + name)
            else:
                totals[name] += weights[name] * _resource(spec, name, values) / spec['scales'][name]
        service = item['service']
        arrived = service['arrived']
        if not arrived or arrived != len(item['task_ids']):
            errors.append('arrival_coverage')
        totals['latency'] += weights['latency'] * item['parallelism']['charged_task_seconds'] / spec['scales']['latency']
        totals['unfinished'] += weights['unfinished'] * (arrived - service['completed']) / max(1, arrived) / spec['scales']['unfinished']
        if not item['eligible'] or item.get('release', {}).get('left'):
            errors.append(item.get('exclusion_reason') or 'window_ineligible')
        if any(row['transition']['cost_at_decision'].get('resource_status') != 'KNOWN'
               or not isinstance(row['transition']['cost_at_decision'].get('resource_seconds'), dict)
               or any(not _number(row['transition']['cost_at_decision']['resource_seconds'].get(key))
                      for name in ('ram', 'vram') for key in spec['resource_keys'][name]) for row in item['transitions']):
            errors.append('resource_decision_unknown')
    by_task = {t['task_id']: (item, t) for item in items for t in item['tasks']}
    for quality in spec['quality_budgets']:
        metric, rows = quality['metric'], []
        for group in block['quality'][metric]:
            for request in group['requests']:
                found = by_task.get(request['task_id'])
                if found is None:
                    errors.append('quality_unknown:' + metric)
                    rows.append(dict(group_id=group['group_id'], stratum=group['stratum'], task_id=request['task_id'],
                                     group_weight=group['weight'], request_weight=request['weight'],
                                     loss=None, provider=None, window_id=None, credit_index=None))
                    continue
                item, task = found
                loss, origin = None, None
                if task.get('deadline_status') == 'UNFINISHED' or task.get('status') in ('FAILED', 'UNFINISHED'):
                    loss, origin = quality['undelivered_loss'], 'undelivered'
                else:
                    result = next((m for m in (task.get('feedback') or {}).get('metrics', []) if m['id'] == metric), {})
                    value = result.get('normalized')
                    if result.get('status') == 'KNOWN' and _number(value) and 0 <= value <= 1 and result.get('direction') in ('maximize', 'minimize'):
                        loss = 1 - value if result['direction'] == 'maximize' else value
                        origin = result.get('provider')
                    else:
                        errors.append('quality_unknown:' + metric)
                if loss is not None:
                    totals['quality:' + metric] += group['weight'] * request['weight'] * loss / spec['scales']['quality:' + metric]
                credit = task.get('quality_credit_index')
                if origin == 'undelivered':
                    credit = len(item['transitions']) - 1 if item['transitions'] else None
                if credit is not None and (type(credit) is not int or not 0 <= credit < len(item['transitions'])):
                    errors.append('quality_credit_unknown:' + metric)
                    credit = None
                rows.append(dict(group_id=group['group_id'], stratum=group['stratum'], task_id=request['task_id'], group_weight=group['weight'],
                                 request_weight=request['weight'], loss=loss, provider=origin,
                                 window_id=item['window_id'], credit_index=credit))
        records[metric] = rows
    reported = dict(totals)
    for error in errors:
        if error.startswith('resource_unknown:'):
            reported[error.split(':', 1)[1]] = None
        elif error.startswith('quality_unknown:'):
            reported['quality:' + error.split(':', 1)[1]] = None
        elif error == 'arrival_coverage':
            reported['latency'] = reported['unfinished'] = None
    record = dict(schema='budget-settlement/2', point=point['id'], objective=point['objective'], components=names,
                  normalized=reported, known_partial=totals, quality=records, api=copy.deepcopy(spec['api']), multipliers=list(dual),
                  status='INELIGIBLE' if errors else 'EVALUATED', errors=errors,
                  resource_basis='declared envelope integral proxy in GiB-seconds', cost_weights=copy.deepcopy(block['cost_weights']))
    if not errors:
        record['lagrangian_return'] = (-totals[point['objective']] -
            sum(dual[i] * totals[n] for i, n in enumerate(names) if n in point['limits']))
    for item in items:
        item['budget_policy'] = copy.deepcopy(record)
        item['budget_policy']['multipliers_after'] = list(dual)
    if errors:
        for item in items:
            item['eligible'], item['reward'] = False, None
            item['exclusion_reason'] = item.get('exclusion_reason') or errors[0]
        return False
    for position, item in enumerate(items):
        weights = {name: values[position] for name, values in block['cost_weights'].items()}
        history = item['transitions']
        starts = [row['transition']['cost_at_decision'] for row in history]
        end = dict(resource_seconds=item['costs']['resource_envelope']['resource_seconds'],
                   charged_task_seconds=item['parallelism']['charged_task_seconds'],
                   missed_arrivals=item['service']['arrived'] - item['service']['completed'])
        for choice, (row, start, stop) in enumerate(zip(history, starts, starts[1:] + [end])):
            cost = {name: 0.0 for name in names}
            for name in ('ram', 'vram'):
                cost[name] = weights[name] * (_resource(spec, name, stop['resource_seconds']) -
                                       _resource(spec, name, start['resource_seconds'])) / spec['scales'][name]
            cost['latency'] = weights['latency'] * (stop['charged_task_seconds'] - start['charged_task_seconds']) / spec['scales']['latency']
            cost['unfinished'] = weights['unfinished'] * (stop['missed_arrivals'] - start['missed_arrivals']) / item['service']['arrived'] / spec['scales']['unfinished']
            for metric, entries in records.items():
                cost['quality:' + metric] = sum(e['loss'] * e['group_weight'] * e['request_weight'] /
                    spec['scales']['quality:' + metric] for e in entries if e['window_id'] == item['window_id'] and e['credit_index'] == choice)
            row['transition'].update(normalized_cost_delta=cost, multipliers=list(dual),
                reward=-cost[point['objective']] - sum(dual[i] * cost[n] for i, n in enumerate(names) if n in point['limits']),
                block_normalizer=spec['normalizer'])
        contribution = {name: 0.0 for name in names}
        for name in ('ram', 'vram'):
            contribution[name] = weights[name] * _resource(spec, name, end['resource_seconds']) / spec['scales'][name]
        contribution['latency'] = weights['latency'] * end['charged_task_seconds'] / spec['scales']['latency']
        contribution['unfinished'] = weights['unfinished'] * end['missed_arrivals'] / item['service']['arrived'] / spec['scales']['unfinished']
        for metric, entries in records.items():
            contribution['quality:' + metric] = sum(e['loss'] * e['group_weight'] * e['request_weight'] /
                spec['scales']['quality:' + metric] for e in entries if e['window_id'] == item['window_id'])
        item['reward'] = (-contribution[point['objective']] -
                          sum(dual[i] * contribution[n] for i, n in enumerate(names) if n in point['limits']))
    return True


def update_dual(items, spec, index, dual):
    point = spec['points'][index]
    totals = items[0]['budget_policy']['normalized']
    for i, name in enumerate(dimensions(spec)):
        if name in point['limits']:
            dual[i] = max(0.0, dual[i] + spec['dual_step'] * (totals[name] - point['limits'][name]))
    for item in items:
        item['budget_policy']['multipliers_after'] = list(dual)
