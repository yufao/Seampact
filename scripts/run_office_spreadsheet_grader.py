"""Grade a collected final delivery in the isolated, pinned rootless image."""
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from seampact.spreadsheet_evidence import infrastructure_error_resolved
from seampact.isolated_execution import ISOLATION_RECEIPT, mount_source_identity
from seampact.office_spreadsheet import SOURCE_REVISION
from seampact.project_spaces import _identity

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _separate(grading, source):
    source = Path(source).resolve()
    if grading.is_relative_to(source) or source.is_relative_to(grading):
        raise ValueError('Grading answers overlap a business-visible root')


def verify_business_home(home, task, grading, *, infrastructure_failure=False):
    """Authenticate native-host isolation using read-only platform records."""
    home, grading = Path(home).resolve(strict=True), Path(grading).resolve(strict=True)
    _separate(grading, home)
    database = (home / 'platform.sqlite3').resolve(strict=True)
    if not database.is_relative_to(home):
        raise ValueError('Business database escapes its home')
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as connection:
        connection.execute('PRAGMA query_only=ON')
        connection.execute('BEGIN')
        def get(kind, identity):
            row = connection.execute('SELECT data FROM documents WHERE kind=? AND id=?', (kind, identity)).fetchone()
            if row is None:
                raise ValueError('Business task ownership evidence is missing')
            return json.loads(row[0])
        task_id = _identity(task.get('id'))
        recorded = get('task', task_id)
        for key in ('id', 'project_id', 'conversation_id', 'status', 'workspace', 'workspace_status', 'python_calls',
                    'files', 'experiment', 'spreadsheet_replay'):
            if recorded.get(key) != task.get(key):
                raise ValueError('Collected task differs from its business-home record: ' + key)
        for key in ('outputs', 'source_revision', 'solution_sha256', 'delivered_solution', 'execution_infrastructure_error'):
            if (recorded.get('evaluation') or {}).get(key) != (task.get('evaluation') or {}).get(key):
                raise ValueError('Collected replay evidence differs from the business task: ' + key)
        project_id, conversation_id = map(_identity, (recorded.get('project_id'), recorded.get('conversation_id')))
        get('project', project_id)
        session, lease = get('project_session', conversation_id), get('project_lease', task_id)
    scope = dict(project_id=project_id, conversation_id=conversation_id, task_id=task_id)
    if (session.get('project_id') != project_id or recorded.get('workspace') != scope
            or any(lease.get(key) != value for key, value in scope.items())):
        raise ValueError('Business task and workspace ownership do not match')
    if infrastructure_failure:
        return dict(mode='native_host', status='UNKNOWN_INFRASTRUCTURE', isolation_verified=False,
                    workspace_status=lease.get('status'))
    if (lease.get('status') != 'FINISHED' or recorded.get('workspace_status') != 'FINISHED'
            or recorded.get('status') not in {'COMPLETED', 'FAILED', 'BLOCKED', 'CANCELLED', 'INTERRUPTED'}):
        raise ValueError('Business task and workspace must have confirmed terminal ownership')
    project = home / 'projects' / project_id
    scratch = project / 'tasks' / task_id / 'scratch'
    inputs = project / 'tasks' / task_id / 'inputs' if lease.get('private_inputs') else project / 'data'
    standard = [(inputs, '/data', True), (project / 'sessions' / conversation_id / 'workspace', '/workspace', False),
                (scratch, '/scratch', False)]
    calls, images, seen = recorded.get('python_calls', []), set(), set()
    if not isinstance(calls, list):
        raise ValueError('Native-host scoring requires actual Python isolation receipts')
    if not calls:
        evaluation = recorded.get('evaluation') or {}
        if (evaluation.get('outputs') != [dict(index=i, sha256=None) for i in (1, 2, 3)]
                or recorded.get('spreadsheet_replay') or recorded.get('files') or recorded.get('artifacts')
                or evaluation.get('solution_sha256') or evaluation.get('delivered_solution')):
            raise ValueError('Unexecuted task cannot claim replay or delivered artifacts without isolation receipts')
        return dict(mode='native_host', status='NO_EXECUTION', isolation_verified=False,
                    verified_python_calls=0, images=[], workspace_status='FINISHED')
    for call in calls:
        receipt = call.get('isolation_receipt') or {}
        if (not isinstance(call.get('call_id'), str) or call['call_id'] in seen or call.get('stopped') is not True
                or call.get('create_uncertain') or receipt.get('schema') != ISOLATION_RECEIPT
                or receipt.get('verified_before_start') is not True or receipt.get('scope') != scope
                or receipt.get('container_name') != call.get('container')
                or not re.fullmatch(r'[0-9a-f]{64}', receipt.get('container_id', ''))
                or not re.fullmatch(r'sha256:[0-9a-f]{64}', receipt.get('image', ''))
                or receipt.get('command') != ['python', '-I', '-'] or receipt.get('working_directory') != '/workspace'):
            raise ValueError('Python execution lacks a complete, settled isolation receipt')
        seen.add(call['call_id'])
        images.add(receipt['image'])
        host = receipt.get('sandbox') or {}
        if (host.get('NetworkMode') != 'none' or host.get('ReadonlyRootfs') is not True
                or host.get('Privileged') is not False or host.get('CapAdd')
                or set(host.get('CapDrop') or []) != {'ALL'}
                or set(host.get('SecurityOpt') or []) not in ({'no-new-privileges'}, {'no-new-privileges=true'})
                or host.get('NanoCpus') != 1000000000 or host.get('Memory') != 512 * 1024 * 1024
                or host.get('PidsLimit') != 64 or (host.get('LogConfig') or {}).get('Type') != 'none'
                or set(host.get('Tmpfs') or {}) != {'/tmp'}
                or set(host['Tmpfs']['/tmp'].split(',')) != {'rw', 'nosuid', 'nodev', 'noexec', 'size=64m'}):
            raise ValueError('Python execution isolation settings are incomplete or unsafe')
        requested = receipt.get('requested_mounts') or []
        if len(requested) == 4:
            data = next((Path(m['source']).resolve() for m in requested if m.get('target') == '/data'), None)
            if data is None or not data.is_relative_to(scratch.resolve()):
                raise ValueError('Replay input escapes task scratch')
            parts = data.relative_to(scratch.resolve()).parts
            if (len(parts) != 3 or not re.fullmatch(r'spreadsheet-replay-[0-9a-f]{32}', parts[0])
                    or parts[1] not in {'1', '2', '3'} or parts[2] != 'data'):
                raise ValueError('Replay mount has no task-owned frozen namespace')
            base = data.parent
            expected = [(base / 'data', '/data', True), (base / 'workspace', '/workspace', False),
                        (base / 'scratch', '/scratch', False), (base / 'solution.py', '/solution.py', True)]
        else:
            expected = standard
        expected = {(mount_source_identity(str(path.resolve())), target, readonly) for path, target, readonly in expected}
        requested_set = {(mount_source_identity(m['source']), m['target'], m['read_only']) for m in requested}
        actual = receipt.get('mounts') or []
        actual_set = {(mount_source_identity(m['Source']), m['Destination'], not m['RW']) for m in actual if m.get('Type') == 'bind'}
        daemon = receipt.get('daemon_mounts') or []
        daemon_set = {(mount_source_identity(m['source']), m['target'], m['read_only']) for m in daemon}
        if (requested_set != expected or actual_set != expected or daemon_set != expected
                or len(actual) != len(requested) or len(requested) != len(expected) or len(daemon) != len(expected)):
            raise ValueError('Actual Python mounts differ from the owned project/session roots')
        for mount in requested:
            _separate(grading, mount['source'])
    if len(images) != 1:
        raise ValueError('Python executions did not retain one fixed business image')
    return dict(mode='native_host', status='VERIFIED', isolation_verified=True,
                verified_python_calls=len(calls), images=sorted(images), workspace_status='FINISHED')


def finish_result(directory, result, binding, started, collection):
    result.update(binding)
    quality = result.get('hard') if all(type(value) is bool for value in result['cases']) else None
    result.update(grading_wall_ms=(time.perf_counter()-started)*1000, grading_cost_separate=True, quality=quality,
                  common_business_latency_ms=collection.get('common_business_latency_ms'))
    (directory/'native-score.json').write_text(json.dumps(result, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(result, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('collection',type=Path,help='completed run_office_spreadsheet.py collection directory')
    parser.add_argument('--allowed-root', type=Path, required=True, help='authorized parent of the collection')
    parser.add_argument('--grading-root', type=Path, required=True, help='private grading-only subset directory')
    parser.add_argument('--grading-manifest', type=Path, help='manifest within grading-root (default: manifest.json)')
    parser.add_argument('--receipt', type=Path, required=True, help='private validated grader receipt')
    parser.add_argument('--image', help='optional image ID; must equal the validated receipt')
    business = parser.add_mutually_exclusive_group(required=True)
    business.add_argument('--business-container', help='platform container; its mounts must exclude grading answers')
    business.add_argument('--business-home', type=Path, help='native platform home; verify actual task-owned Python isolation receipts')
    args = parser.parse_args()
    directory = args.collection.resolve(strict=True)
    if not directory.is_relative_to(args.allowed_root.resolve(strict=True)):
        raise ValueError('Collection must remain within the authorized office root')
    collection = json.loads((directory/'collection.json').read_text(encoding='utf-8'))
    started = time.perf_counter()
    binding = dict(outputs=None, manifest_sha256=None, original_manifest_sha256=None)
    if collection.get('status')=='ADMISSION_FAILED':
        result = dict(case_id=collection['case_id'],status='NOT_ADMITTED',cases=[None]*3,soft=None,hard=None)
    else:
        task = json.loads((directory/'task.json').read_text(encoding='utf-8'))
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
        unknown_infrastructure = bool(((collection.get('execution_infrastructure_error') or
            (task.get('evaluation') or {}).get('execution_infrastructure_error')) and not infrastructure_error_resolved(task))
            or task.get('workspace_status') != 'FINISHED' or collection.get('status') == 'COLLECTION_TIMEOUT')
        if args.business_home:
            binding['business_isolation'] = verify_business_home(args.business_home, task, grading,
                infrastructure_failure=unknown_infrastructure)
        else:
            mounts = json.loads(subprocess.check_output(['docker','inspect',args.business_container,
                '--format','{{json .Mounts}}'], text=True, encoding='utf-8'))
            for mount in mounts:
                source = mount.get('Source')
                if source:
                    _separate(grading, source)
        if unknown_infrastructure:
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
            receipt = json.loads(args.receipt.resolve(strict=True).read_text(encoding='utf-8'))
            image = args.image or receipt['image']
            if receipt.get('status') != 'READY_SYNTHETIC_VALIDATED' or not re.fullmatch(r'sha256:[0-9a-f]{64}', image):
                raise ValueError('A validated receipt with a pinned grader image ID is required')
            if image != receipt['image']:
                raise ValueError('Selected image does not match the validated receipt')
            if receipt.get('grading_only', {}).get('manifest_sha256') != sha(manifest):
                raise ValueError('Validated receipt belongs to a different grading subset')
            actual = subprocess.check_output(['docker','image','inspect',image,'--format','{{.Id}}'],
                                             text=True, encoding='utf-8').strip()
            if actual != image:
                raise ValueError('The local grader image does not match its receipt')
            container = 'seampact-office-grader-' + uuid.uuid4().hex
            command = ['docker','run','--name',container,'--rm','--pull=never','--network','none','--read-only','--cap-drop','ALL',
                '--security-opt','no-new-privileges','--cpus','1','--memory','2g','--pids-limit','128',
                '--tmpfs','/tmp:rw,nosuid,nodev,size=512m','--env','HOME=/tmp',
                '--mount',f'type=bind,src={grading},dst=/grading,readonly',
                '--mount',f'type=bind,src={candidates},dst=/candidates,readonly',image,
                '/grading/' + manifest.relative_to(grading).as_posix(),collection['case_id'],'/opt/grader/upstream',
                '/candidates/replay-1.xlsx','/candidates/replay-2.xlsx','/candidates/replay-3.xlsx']
            cleanup = None
            try:
                attempt = subprocess.run(command,capture_output=True,text=True,encoding='utf-8',timeout=600)
            except subprocess.TimeoutExpired:
                attempt = None
                try:
                    stopped = subprocess.run(['docker','rm','-f',container],capture_output=True,timeout=30)
                    cleanup = 'REMOVED' if stopped.returncode == 0 else 'UNCONFIRMED'
                except (OSError, subprocess.TimeoutExpired):
                    cleanup = 'UNCONFIRMED'
            except OSError:
                attempt = None
            if attempt is None or attempt.returncode:
                result = dict(case_id=collection['case_id'],task_id=task['id'],status='UNKNOWN_INFRASTRUCTURE',
                              cases=[None]*3,soft=None,hard=None,error='isolated grader did not complete',
                              grader_container=container, grader_cleanup=cleanup)
            else:
                try:
                    result = json.loads(attempt.stdout)
                    if (result.get('case_id') != collection['case_id'] or result.get('source_revision') != SOURCE_REVISION
                            or not isinstance(result.get('cases'), list) or len(result['cases']) != 3 or
                            any(value is not None and type(value) is not bool for value in result['cases'])):
                        raise ValueError('Invalid native cases')
                    cases = result['cases']
                    complete = None not in cases
                    hard = False if False in cases else True if complete else None
                    soft = sum(cases) / 3 if complete else None
                    if (type(result.get('hard')) is not type(hard) or result['hard'] != hard
                            or type(result.get('soft')) is bool or result.get('soft') != soft):
                        raise ValueError('Invalid native aggregate score')
                    result.update(task_id=task['id'],status='EVALUATED' if complete else 'PARTIAL_OR_UNKNOWN',
                                  grader_image=image, grader_container=container)
                except (ValueError, TypeError, AttributeError, KeyError):
                    result = dict(case_id=collection['case_id'],task_id=task['id'],status='UNKNOWN_INFRASTRUCTURE',
                                  cases=[None]*3,soft=None,hard=None,error='invalid native grader output')
    finish_result(directory, result, binding, started, collection)

if __name__=='__main__':
    main()
