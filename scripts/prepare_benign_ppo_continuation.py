"""Freeze a new benign 96-task training protocol from a completed PPO parent.

This only reads recorded inputs and writes new protocol/source files. It never
starts a model, task or training job. The server independently rechecks the
parent ledger and checkpoint before accepting a transfer.
"""
import argparse
import copy
import hashlib
import json
from pathlib import Path, PurePosixPath
import subprocess
import sys

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE / 'src'))
from seampact.scheduling.ppo_transfer import SEMANTIC_MODULES, _semantic_digest, source_fingerprints


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path, obj):
    with path.open('x', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, allow_nan=False)
        f.write('\n')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--parent-protocol', type=Path, required=True)
    p.add_argument('--parent-receipt', type=Path, required=True)
    p.add_argument('--selection', type=Path, required=True, help='frozen 96-training/64-development selection')
    p.add_argument('--grader-argv', type=Path, required=True, help='original pinned separate grader argv JSON')
    p.add_argument('--data-root', type=Path, required=True, help='host directory mounted as /data')
    p.add_argument('--output', type=Path, required=True, help='new directory below data root')
    p.add_argument('--diagnostic-selection', type=Path, help='optional completed 24-case selection for overlap accounting')
    args = p.parse_args()
    root = args.data_root.resolve(strict=True)
    parent = args.parent_protocol.resolve(strict=True)
    parent.relative_to(root)
    out = args.output.resolve()
    out.relative_to(root)
    if out == root or out.exists():
        raise ValueError('Output must be a new directory under the data root')
    receipt = json.loads(args.parent_receipt.read_bytes())
    plan = json.loads(parent.read_bytes())
    parent_sha = sha(parent)
    checkpoint = receipt.get('final_checkpoint') or {}
    if (receipt.get('parent_protocol_sha256') != parent_sha or receipt.get('cursor_status') != 'COMPLETE'
            or receipt.get('last_status') != 'COMPLETE' or receipt.get('sha256_verified') is not True
            or checkpoint != receipt.get('series_checkpoint') or not checkpoint.get('sha256')
            or not receipt.get('parent_training_run_id')):
        raise ValueError('The supplied parent receipt is not a completed, verified checkpoint')
    if not plan.get('continuous_training') or len(plan['cases']['training']) != 433:
        raise ValueError('Expected the completed frozen 433-case parent protocol')
    grader = json.loads(args.grader_argv.read_bytes())
    if (not isinstance(grader, list) or grader.count('{collection}') != 1
            or grader.count('--allowed-root') != 1 or any(not isinstance(x, str) or not x for x in grader)
            or grader.index('--allowed-root') + 1 >= len(grader)):
        raise ValueError('Expected the pinned separate native grader argv')
    def local(server_path):
        path = PurePosixPath(server_path)
        if not path.is_absolute() or not path.is_relative_to('/data') or '..' in path.parts:
            raise ValueError('Manifest must be under the platform data mapping')
        mapped = (root / Path(*path.relative_to('/data').parts)).resolve(strict=True)
        mapped.relative_to(root)
        return mapped
    def server(path):
        return '/data/' + Path(path).resolve().relative_to(root).as_posix()
    cases, sources = {}, {}
    locations = {'agentdojo': {'manifest': plan['manifest'], 'manifest_sha256': plan['manifest_sha256']},
                 **{k: plan['source_manifests'][k] for k in ('spreadsheet', 'privacy', 'office_rag')}}
    for source, item in locations.items():
        manifest = local(item['manifest'])
        if sha(manifest) != item['manifest_sha256']:
            raise ValueError('Frozen runtime manifest SHA differs: ' + source)
        data = json.loads(manifest.read_bytes())
        if source == 'spreadsheet' and not data.get('runtime', {}).get('inputs_only'):
            raise ValueError('Expected the pinned inputs-only spreadsheet manifest')
        for row in data['cases']:
            if row['id'] in cases:
                raise ValueError('Overlapping source case identities')
            cases[row['id']] = row
            sources[row['id']] = source
    def normal(identity):
        row, source = cases[identity], sources[identity]
        if row.get('split') != 'train_pool':
            return False
        if row.get('injections') or row.get('injection_task_id') or row.get('attack_template'):
            return False
        if source == 'agentdojo':
            return identity.endswith(':clean')
        if source == 'privacy':
            return identity.endswith(':normal') and row.get('variant') == 'normal'
        return source == 'spreadsheet' or source == 'office_rag' and row.get('variant') == 'normal'
    selection = json.loads(args.selection.read_bytes())['training']
    block_indices = selection['block_indices_zero_based']
    if (len(block_indices) != len(set(block_indices)) or block_indices != sorted(block_indices)
            or any(type(i) is not int or i < 0 for i in block_indices)):
        raise ValueError('Selection block indices are not unique and ordered')
    selected_blocks, position = [], 0
    for index, block in enumerate(plan['arrival_schedule']['training']):
        width = len(block['offsets_ms'])
        ids = plan['cases']['training'][position:position + width]
        position += width
        if index in block_indices:
            selected_blocks.append((index, copy.deepcopy(block), ids))
    original = [identity for _, _, ids in selected_blocks for identity in ids]
    if (position != 433 or len(selected_blocks) != len(block_indices) or len(original) != 96
            or len(set(original)) != 96 or original != selection['case_ids']
            or any(identity not in cases for identity in original)):
        raise ValueError('Selected 96 cases do not match complete parent blocks')
    used = set(original)
    chosen_blocks, replacements = [], []
    for index, block, ids in selected_blocks:
        result = []
        for identity in ids:
            if normal(identity):
                result.append(identity)
                continue
            source, old = sources[identity], cases[identity]
            if source not in {'agentdojo', 'privacy'}:
                raise ValueError('Risk replacement is not an approved same-source normal task')
            available = [x for x in plan['cases']['training'] if x not in used and x in cases
                         and sources[x] == source and normal(x)
                         and not cases[x].get('rag_adaptation')]
            if not available:
                raise ValueError('No unused clean same-source training replacement')
            order = {x: i for i, x in enumerate(plan['cases']['training'])}
            available.sort(key=lambda x: (cases[x].get('family_id') != old.get('family_id'),
                                          cases[x].get('suite') != old.get('suite'), order[x]))
            replacement = available[0]
            used.add(replacement); result.append(replacement)
            replacements.append(dict(block_index=index, position=len(result)-1,
                original_case_id=identity, replacement_case_id=replacement, source=source,
                same_family=cases[replacement].get('family_id') == old.get('family_id'),
                same_suite=cases[replacement].get('suite') == old.get('suite')))
        if len(result) > 1 and any(cases[x].get('rag_adaptation') or sources[x] == 'office_rag' for x in result):
            raise ValueError('A shared RAG fixture cannot enter a mixed arrival block')
        chosen_blocks.append((block, result))
    chosen = [identity for _, ids in chosen_blocks for identity in ids]
    if len(replacements) != 12 or len(chosen) != 96 or len(set(chosen)) != 96:
        raise ValueError('Expected exactly 12 substitutions and 96 distinct benign tasks')
    counts = {source: sum(sources[x] == source for x in chosen) for source in locations}
    if counts != {'agentdojo':12, 'spreadsheet':71, 'privacy':5, 'office_rag':8}:
        raise ValueError('Four-source distribution differs from the frozen selection')
    overlap = None
    if args.diagnostic_selection:
        diagnostic = json.loads(args.diagnostic_selection.read_bytes())['selected']
        diagnostic_ids = [row['case_id'] for row in diagnostic]
        if len(diagnostic_ids) != 24 or len(set(diagnostic_ids)) != 24:
            raise ValueError('Diagnostic selection must contain 24 distinct cases')
        overlap_ids = [identity for identity in chosen if identity in set(diagnostic_ids)]
        overlap = dict(count=len(overlap_ids), case_ids=overlap_ids,
                       diagnostic_selection_sha256=sha(args.diagnostic_selection),
                       meaning='training-case reuse, not an independent evaluation')
    # The source identities come from the actual recorded platform and runner
    # revisions, and all must match the current scheduler's feature/action code.
    launches = receipt['recorded_launch_revisions']
    revisions = [launches['launch.json']['platform_revision'], launches['launch.json']['runner_revision'],
                 launches['continuation-1794811957-launch.json']['revision']]
    image = receipt.get('currently_running_platform_image', '')
    if image.split()[-1:] != [revisions[0]]:
        raise ValueError('Recorded platform image revision does not match the parent launch')
    expected = source_fingerprints()
    snapshots = {}
    for revision in revisions:
        for name in SEMANTIC_MODULES:
            data = subprocess.check_output(['git', '-C', str(CODE), 'show',
                revision + ':src/seampact/scheduling/' + name])
            if _semantic_digest(name, data) != expected[name]:
                raise ValueError('A recorded parent source revision differs: ' + name)
            if revision == revisions[0]:
                snapshots[name] = data
    # The same task list, order, calibration and arrivals are used for both arms.
    child = copy.deepcopy(plan)
    child['cases']['training'] = chosen
    child['arrival_schedule']['training'] = [block for block, _ in chosen_blocks]
    calibration_blocks = []
    calibration_cases = []
    for block, ids in chosen_blocks:
        if len(calibration_cases) + len(ids) > 16:
            break
        calibration_blocks.append(block); calibration_cases.extend(ids)
        if len(calibration_cases) == 16:
            break
    if not calibration_cases:
        raise ValueError('No complete benign calibration block')
    child['training_calibration'] = dict(source_split='training', case_ids=calibration_cases,
        arrival_schedule=calibration_blocks)
    rag = [x for x in chosen if sources[x] == 'office_rag']
    if any(x not in plan.get('prebuild', []) or not cases[x].get('rag_adaptation') for x in rag):
        raise ValueError('Selected normal RAG cases lack their original frozen prebuild')
    child['prebuild'] = rag
    child['benign_training_only'] = True
    child['reward_denominator'] = 433
    child.pop('development_evidence', None)
    out.mkdir(parents=True, exist_ok=False)
    source_dir = out / 'parent-source' / 'src' / 'seampact' / 'scheduling'
    source_dir.mkdir(parents=True)
    for name, content in snapshots.items():
        (source_dir / name).write_bytes(content)
    full = copy.deepcopy(child)
    full['parent_transfer'] = dict(protocol=server(parent), protocol_sha256=parent_sha,
        run_id=receipt['parent_training_run_id'], checkpoint_sha256=checkpoint['sha256'],
        mode='full', parent_source_dir=server(source_dir))
    save(out / 'full.json', full)
    save(out / 'cold.json', child)
    for mode in ('full', 'cold'):
        argv = list(grader)
        argv[argv.index('--allowed-root') + 1] = str(out / (mode + '-run'))
        save(out / (mode + '-grader-argv.json'), argv)
    save(out / 'selection.json', dict(schema='office-benign-continuation-selection/1',
        rule='preserve 18 frozen selected parent blocks and replace each risk case with an unused normal case from the same source',
        parent_protocol_sha256=parent_sha, selection_sha256=sha(args.selection),
        parent_receipt_sha256=sha(args.parent_receipt), parent_run_id=receipt['parent_training_run_id'],
        original_grader_argv_sha256=sha(args.grader_argv),
        parent_checkpoint_sha256=checkpoint['sha256'], source_revisions=revisions,
        semantic_sources=expected, source_counts=counts, original_case_ids=original, case_ids=chosen,
        original_block_indices=block_indices, arrival_schedule=[block for block, _ in chosen_blocks],
        calibration_cases=calibration_cases, substitutions=replacements, prebuild=rag, reward_denominator=433,
        diagnostic_overlap=overlap,
        scope='new benign four-source workload; 12 substituted cases mean this is not the original 96-task selection'))
    print(json.dumps(dict(output=str(out), training_cases=len(chosen), full_sha256=sha(out/'full.json'),
                          cold_sha256=sha(out/'cold.json')), indent=2))


if __name__ == '__main__':
    main()
