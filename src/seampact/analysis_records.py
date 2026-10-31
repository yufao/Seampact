"""Immutable full checker results, navigated without resending whole trees."""

import copy

from .inspection import digest
import json


def identity(value):
    return digest(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode())


def save_report(store, job, report, *, tool, candidate=None, input_digest=None):
    reference = job['id'] + '-report-' + str(len(job.setdefault('report_refs', [])))
    record = dict(report=copy.deepcopy(report), tool=tool, stage=job.get('pipeline_stage', 'analysis'),
                  candidate=copy.deepcopy(candidate), input_digest=input_digest)
    record['digest'] = identity(record)
    store.create('analysis_tool_report', reference, record)
    job['report_refs'].append(reference)
    if candidate is not None:
        job['last_check'] = dict(report_ref=reference, stage=record['stage'], input_digest=input_digest)
    return reference


def load_report(store, job, report_ref):
    if report_ref not in job.get('report_refs', []):
        raise ValueError('report reference does not belong to this analysis')
    record = store.get('analysis_tool_report', report_ref)
    if not record or record.get('digest') != identity({k:v for k,v in record.items() if k != 'digest'}):
        raise ValueError('saved check report is unavailable or its digest changed')
    return record


def read_report(store, job, report_ref, path='', offset=0, count=20):
    if type(offset) is not int or offset < 0 or type(count) is not int or not 1 <= count <= 2000:
        raise ValueError('report page needs a nonnegative offset and count between 1 and 2000')
    value = load_report(store, job, report_ref)['report']
    if not isinstance(path, str) or path and not path.startswith('/'):
        raise ValueError('report path must be a JSON pointer')
    if path:
        try:
            for part in path[1:].split('/'):
                key = part.replace('~1', '/').replace('~0', '~')
                value = value[int(key)] if isinstance(value, list) and key.isdecimal() else value[key]
        except (IndexError, KeyError, TypeError, ValueError) as error:
            raise ValueError('report path does not identify a saved value') from None
    def describe(item):
        if isinstance(item, (dict, list)):
            return dict(type=type(item).__name__, count=len(item))
        if isinstance(item, str) and len(item) > 2000:
            return dict(type='str', characters=len(item), preview=item[:2000], more=True)
        return dict(type=type(item).__name__, value=item)
    result = dict(schema='analysis-report-page/1', report_ref=report_ref, path=path,
                  offset=offset, admission_granted=False)
    if isinstance(value, (dict, list)):
        items = list(value.items()) if isinstance(value, dict) else list(enumerate(value))
        count = min(count, 50)
        result.update(total=len(items), items=[dict(key=key, **describe(item)) for key,item in items[offset:offset+count]],
                      next_offset=offset+count if offset+count < len(items) else None)
    elif isinstance(value, str):
        result.update(total=len(value), text=value[offset:offset+count],
                      next_offset=offset+count if offset+count < len(value) else None)
    else:
        result.update(value=value, next_offset=None)
    return result
