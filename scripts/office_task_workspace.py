"""Confirm an office task's workspace is released before using its results.

The old project cleanup route reconciles *every* unresolved lease in the
project before applying its conversation filter; this helper never calls it.
The task-scoped route is absent on older servers, where the failed request has
no effect and the helper continues with read-only confirmation.
"""
import time


TERMINAL = frozenset({'COMPLETED', 'BLOCKED', 'FAILED', 'INTERRUPTED', 'CANCELLED',
                      'CANCELED', 'TIMEOUT', 'TIMED_OUT', 'STOPPED'})


class WorkspaceUnresolved(RuntimeError):
    """The task was not observed both terminal and workspace FINISHED."""
    def __init__(self, message, *, task=None, events=()):
        super().__init__(message)
        self.task = task
        self.events = list(events)


def ensure_workspace_finished(api, task_id, *, attempts=3, delay=2, evidence=None, reconcile=None):
    """Return the latest confirmed task, or raise WorkspaceUnresolved.

    api(path, body=None) is the driver's existing JSON API function. evidence,
    if supplied, is called with each observation. By default the new strictly
    task-scoped server endpoint is used; a custom reconcile callback must offer
    the same isolation. The old project-wide endpoint does not qualify.
    A rejection or lost response is followed by a fresh GET before any decision.
    """
    if not isinstance(task_id, str) or not task_id or '/' in task_id:
        raise ValueError('A valid task ID is required')
    if type(attempts) is not int or attempts < 1 or delay < 0:
        raise ValueError('Attempts and delay must be bounded and nonnegative')
    events, latest = [], None

    def record(**row):
        events.append(row)
        if evidence is not None:
            evidence(row)

    for attempt in range(attempts):
        try:
            latest = api('/api/tasks/' + task_id)
            if latest.get('id') != task_id:
                raise ValueError('Task identity differs from requested ID')
            record(attempt=attempt + 1, action='get', status=latest.get('status'),
                   workspace_status=latest.get('workspace_status'), task=latest)
        except Exception as error:
            record(attempt=attempt + 1, action='get_error', error_type=type(error).__name__, error=str(error))
            latest = None
        if latest is not None and latest.get('status') in TERMINAL:
            if latest.get('workspace_status') == 'FINISHED':
                return latest
            if latest.get('workspace_status') != 'FINISHED':
                scope = latest.get('workspace') or {}
                project = scope.get('project_id') or latest.get('project_id')
                conversation = scope.get('conversation_id') or latest.get('conversation_id')
                if (scope.get('task_id') not in (None, task_id) or not project or not conversation or
                        latest.get('project_id') not in (None, project)):
                    record(attempt=attempt + 1, action='scope_unverified')
                else:
                    try:
                        result = (reconcile(latest) if reconcile is not None else
                                  api('/api/tasks/workspace/reconcile', dict(task_id=task_id,
                                      project_id=project, conversation_id=conversation)))
                        record(attempt=attempt + 1, action='reconcile', result=result)
                    except Exception as error:
                        record(attempt=attempt + 1, action='reconcile_error', error_type=type(error).__name__, error=str(error))
                    # Always re-read, including after concurrent rejection or timeout.
                    try:
                        latest = api('/api/tasks/' + task_id)
                        if latest.get('id') != task_id:
                            raise ValueError('Task identity differs from requested ID')
                        record(attempt=attempt + 1, action='get_after_reconcile', task=latest)
                        if latest.get('status') in TERMINAL and latest.get('workspace_status') == 'FINISHED':
                            return latest
                    except Exception as error:
                        record(attempt=attempt + 1, action='get_error', error_type=type(error).__name__, error=str(error))
                        latest = None
        if attempt + 1 < attempts:
            time.sleep(delay)
    raise WorkspaceUnresolved('Task workspace release could not be confirmed; evidence retained', task=latest, events=events)
