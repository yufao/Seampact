"""Grade a collected final delivery in the isolated, pinned rootless image."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import time

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('collection',type=Path,help='completed run_office_spreadsheet.py collection directory')
    parser.add_argument('--allowed-root', type=Path, required=True, help='authorized parent of the collection')
    parser.add_argument('--grading-root', type=Path, required=True, help='private grading-only subset directory')
    parser.add_argument('--grading-manifest', type=Path, help='manifest within grading-root (default: manifest.json)')
    parser.add_argument('--receipt', type=Path, required=True, help='private validated grader receipt')
    parser.add_argument('--image', help='optional image ID; must equal the validated receipt')
    parser.add_argument('--business-container', required=True, help='platform container; its mounts must exclude grading answers')
    args = parser.parse_args()
    directory = args.collection.resolve(strict=True)
    if not directory.is_relative_to(args.allowed_root.resolve(strict=True)):
        raise ValueError('Collection must remain within the authorized office root')
    collection = json.loads((directory/'collection.json').read_text())
    started = time.perf_counter()
    binding = dict(outputs=None, manifest_sha256=None, original_manifest_sha256=None)
    if collection.get('status')=='ADMISSION_FAILED':
        result = dict(case_id=collection['case_id'],status='NOT_ADMITTED',cases=[None]*3,soft=None,hard=None)
    else:
        task = json.loads((directory/'task.json').read_text())
        if task['id']!=collection['task_id'] or task['experiment']['case']!=collection['case_id']:
            raise ValueError('Collection and task identity differ')
        binding.update(outputs=(task.get('evaluation') or {}).get('outputs'),
            manifest_sha256=task['experiment'].get('manifest_sha256'),
            original_manifest_sha256=task['experiment'].get('original_manifest_sha256'))
        grading = args.grading_root.resolve(strict=True)
        manifest = (args.grading_manifest or grading/'manifest.json').resolve(strict=True)
        if not manifest.is_relative_to(grading):
            raise ValueError('Grading manifest must be inside grading-root')
        if sha(manifest)!=task['experiment']['original_manifest_sha256']:
            raise ValueError('Grading subset differs from admitted task source')
        mounts = json.loads(subprocess.check_output(['docker','inspect',args.business_container,
            '--format','{{json .Mounts}}'], text=True))
        for mount in mounts:
            source = mount.get('Source')
            if source and (grading.is_relative_to(Path(source).resolve()) or Path(source).resolve().is_relative_to(grading)):
                raise ValueError('Grading answers are visible to the business container')
        if (collection.get('execution_infrastructure_error') or task.get('workspace_status')!='FINISHED'
                or collection.get('status')=='COLLECTION_TIMEOUT'):
            result = dict(case_id=collection['case_id'],task_id=task['id'],status='UNKNOWN_INFRASTRUCTURE',
                          cases=[None]*3,soft=None,hard=None)
        else:
            candidates = (directory/'delivered').resolve(strict=True)
            if not candidates.is_relative_to(directory):
                raise ValueError('Candidate directory escapes collection')
            delivered = {file['sha256'] for file in task.get('files',[])}
            if not isinstance(binding['outputs'], list) or len(binding['outputs']) != 3:
                raise ValueError('Task has no complete frozen replay identities')
            for index in (1,2,3):
                row = next(item for item in collection['records'] if item['index']==index)
                if binding['outputs'][index-1] != dict(index=index, sha256=row.get('sha256')):
                    raise ValueError('Collected output differs from the final task delivery')
                candidate = candidates/f'replay-{index}.xlsx'
                if row['status']=='DELIVERED':
                    if (candidate.is_symlink() or row['sha256'] not in delivered or not candidate.is_file()
                            or sha(candidate)!=row['sha256']):
                        raise ValueError('Only unchanged, final delivered artifact bytes can be scored')
                elif candidate.exists() or candidate.is_symlink():
                    raise ValueError('Unrecorded output cannot replace missing delivery')
            receipt = json.loads(args.receipt.resolve(strict=True).read_text())
            image = args.image or receipt['image']
            if receipt.get('status') != 'READY_SYNTHETIC_VALIDATED' or not image.startswith('sha256:') or len(image) != 71:
                raise ValueError('A validated receipt with a pinned grader image ID is required')
            if image != receipt['image']:
                raise ValueError('Selected image does not match the validated receipt')
            if receipt.get('grading_only', {}).get('manifest_sha256') != sha(manifest):
                raise ValueError('Validated receipt belongs to a different grading subset')
            actual = subprocess.check_output(['docker','image','inspect',image,'--format','{{.Id}}'],text=True).strip()
            if actual != image:
                raise ValueError('The local grader image does not match its receipt')
            command = ['docker','run','--rm','--pull=never','--network','none','--read-only','--cap-drop','ALL',
                '--security-opt','no-new-privileges','--cpus','1','--memory','2g','--pids-limit','128',
                '--tmpfs','/tmp:rw,nosuid,nodev,size=512m','--env','HOME=/tmp',
                '--mount',f'type=bind,src={grading},dst=/grading,readonly',
                '--mount',f'type=bind,src={candidates},dst=/candidates,readonly',image,
                '/grading/' + manifest.relative_to(grading).as_posix(),collection['case_id'],'/opt/grader/upstream',
                '/candidates/replay-1.xlsx','/candidates/replay-2.xlsx','/candidates/replay-3.xlsx']
            try:
                attempt = subprocess.run(command,capture_output=True,text=True,timeout=600)
            except (OSError, subprocess.TimeoutExpired):
                attempt = None
            if attempt is None or attempt.returncode:
                result = dict(case_id=collection['case_id'],task_id=task['id'],status='UNKNOWN_INFRASTRUCTURE',
                              cases=[None]*3,soft=None,hard=None,error='isolated grader did not complete')
            else:
                try:
                    result = json.loads(attempt.stdout)
                    if (not isinstance(result.get('cases'), list) or len(result['cases']) != 3 or
                            any(value is not None and type(value) is not bool for value in result['cases'])):
                        raise ValueError('Invalid native cases')
                    complete = all(type(value) is bool for value in result['cases'])
                    if complete and type(result.get('hard')) is not bool:
                        raise ValueError('Invalid native hard score')
                    result.update(task_id=task['id'],status='EVALUATED' if complete else 'PARTIAL_OR_UNKNOWN',
                                  grader_image=image)
                except (ValueError, TypeError, AttributeError):
                    result = dict(case_id=collection['case_id'],task_id=task['id'],status='UNKNOWN_INFRASTRUCTURE',
                                  cases=[None]*3,soft=None,hard=None,error='invalid native grader output')
    result.update(binding)
    quality = result.get('hard') if all(type(value) is bool for value in result['cases']) else None
    result.update(grading_wall_ms=(time.perf_counter()-started)*1000,grading_cost_separate=True,quality=quality,
                  common_business_latency_ms=collection.get('common_business_latency_ms'))
    (directory/'native-score.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(result,indent=2))

if __name__=='__main__':
    main()
