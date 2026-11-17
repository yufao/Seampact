"""Conservative evidence check for scoring after an earlier execution error."""
import re


def completed_delivered_replays(task):
    """Prove three successful, delivered replays of the same frozen solution.

    This checks recorded identities only; the separate grader must still verify
    the actual delivered bytes, private manifest and scorer. Never change the
    original infrastructure-error or execution records.
    """
    if not isinstance(task, dict) or task.get('workspace_status') != 'FINISHED':
        return False
    replay = task.get('spreadsheet_replay') or {}
    evaluation = task.get('evaluation') or {}
    experiment = task.get('experiment') or {}
    if not all(isinstance(x, dict) for x in (replay, evaluation, experiment)):
        return False
    solution = evaluation.get('solution_sha256')
    def digest(value):
        return isinstance(value, str) and bool(re.fullmatch(r'[0-9a-f]{64}', value))
    if (not digest(solution) or evaluation.get('delivered_solution') is not True or
            replay.get('status') != 'REPLAYED' or replay.get('task_id') != task.get('id') or
            not task.get('id') or replay.get('project_id') != task.get('project_id') or
            not task.get('project_id') or replay.get('case_id') != experiment.get('case') or
            not experiment.get('case') or replay.get('solution_sha256') != solution):
        return False
    rows, outputs, files = replay.get('replays'), evaluation.get('outputs'), task.get('files')
    if not all(isinstance(x, list) for x in (rows, outputs, files)) or len(rows) != 3 or len(outputs) != 3:
        return False
    if not all(isinstance(f, dict) for f in files):
        return False
    if not any(f.get('name') == 'solution.py' and f.get('sha256') == solution for f in files):
        return False
    for index, (row, output) in enumerate(zip(rows, outputs), 1):
        if not isinstance(row, dict) or not isinstance(output, dict):
            return False
        artifact = row.get('output')
        if (not isinstance(artifact, dict) or row.get('index') != index or output.get('index') != index or
                row.get('status') != 'COMPLETED' or type(row.get('exit_code')) is not int or
                row['exit_code'] != 0 or row.get('solution_sha256') != solution or
                not digest(row.get('input_sha256')) or not digest(output.get('sha256')) or
                artifact.get('replay_index') != index or artifact.get('solution_sha256') != solution or
                artifact.get('sha256') != output['sha256']):
            return False
        if not any(f.get('replay_index') == index and f.get('sha256') == output['sha256'] and
                   f.get('solution_sha256') == solution for f in files):
            return False
    return True


def recovered_development_errors(task):
    """Prove each recorded development exception was followed by an exact successful retry."""
    if not isinstance(task, dict) or task.get('workspace_status') != 'FINISHED':
        return False
    evaluation = task.get('evaluation') or {}
    if (not isinstance(evaluation, dict) or not evaluation.get('execution_infrastructure_error') or
            evaluation.get('execution_infrastructure_error_source') != 'development'):
        return False
    attempts, calls = task.get('spreadsheet_execution_attempts'), task.get('python_calls')
    if not isinstance(attempts, list) or not isinstance(calls, list) or not attempts:
        return False
    if not all(isinstance(c, dict) and isinstance(c.get('call_id'), str) for c in calls):
        return False
    by_id = {c['call_id']: c for c in calls}
    if len(by_id) != len(calls):
        return False
    failures, attempt_ids = [], set()
    for position, item in enumerate(attempts):
        if not isinstance(item, dict) or item.get('status') not in {'ERROR', 'RETURNED'}:
            return False
        identity = item.get('arguments_sha256')
        scope = item.get('scope')
        if (not isinstance(identity, str) or not re.fullmatch(r'[0-9a-f]{64}', identity) or
                not isinstance(scope, dict) or scope.get('task_id') != task.get('id') or
                scope.get('project_id') != task.get('project_id') or not scope.get('project_id') or
                scope.get('conversation_id') != task.get('conversation_id') or not scope.get('conversation_id') or
                not isinstance(item.get('image'), str) or not re.fullmatch(
                    r'(?:sha256:[0-9a-f]{64}|[^\s@]+@sha256:[0-9a-f]{64})', item['image'])):
            return False
        call = by_id.get(item.get('python_call_id'))
        if (not call or item['python_call_id'] in attempt_ids or call.get('stopped') is not True or
                call.get('create_uncertain') or call.get('status') !=
                ('FAILED' if item['status'] == 'ERROR' else item.get('result_status'))):
            return False
        attempt_ids.add(item['python_call_id'])
        if item['status'] == 'ERROR':
            if not isinstance(item.get('error_type'), str) or not item['error_type']:
                return False
            failures.append((position, item))
    if not failures or failures[-1][1]['error_type'] != evaluation['execution_infrastructure_error']:
        return False
    for position, failed in failures:
        if not any(item.get('status') == 'RETURNED' and item.get('result_status') == 'COMPLETED' and
                   type(item.get('exit_code')) is int and item['exit_code'] == 0 and
                   item.get('arguments_sha256') == failed['arguments_sha256'] and
                   item.get('scope') == failed['scope'] and item.get('image') == failed['image']
                   for item in attempts[position + 1:]):
            return False
    return True


def infrastructure_error_resolved(task):
    return completed_delivered_replays(task) or recovered_development_errors(task)
