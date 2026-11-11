"""Finite original SDK result facts, with independent actual-target premises.

The catalog is executable platform evidence, never supplied by a candidate.
Neither an operation name nor matching distribution bytes closes the loaded
object, mutable state, resource, exception or effect obligations below.
"""
import ast
import copy
import hashlib
from dataclasses import dataclass

from seampact.source.call_value_conditions import Shape, digest, ordinary, CallUnknown

RULE = 'source-finite-sdk-result/1'
GAP = 'original SDK result summary target premises require actual worker verification'
_TYPE, _DICT, _STR, _TUPLE = type, dict, str, tuple


def _file(distribution, version, path, sha256):
    return dict(distribution=distribution, version=version, path=path, sha256=sha256)


_TQDM = dict(
    id='finite-original-progress-passthrough/1',
    operations=['tqdm.tqdm', 'tqdm.std.tqdm'], module='tqdm.std', symbol='tqdm',
    sdk=[_file('tqdm', '4.64.0', 'tqdm/__init__.py', '2e27b31c8a804cadc53a28ffdfae5e31cbb5d543e68a398314aa2097bd9f1e9e'),
         _file('tqdm', '4.64.0', 'tqdm/std.py', 'dc3f2a7d1fc2751235d80774e459bd9dd99f1650f933a1efb4ada09fe2900c01'),
         _file('tqdm', '4.64.0', 'tqdm/utils.py', '2af1340ccdbc5fffcd1d82a01a5e6352b93a08ce4157ad06e0d7f9e484cf7892'),
         _file('tqdm', '4.64.0', 'tqdm/_monitor.py', '524bbe0cf5a0cc9edd3b908ad3cc4a24af84fc5eaa43e241de4b2e5dccd2611d')],
    methods=['__new__', '__init__', '__iter__', 'update', 'close'],
    result=dict(kind='ORIGINAL_FINITE_PASSTHROUGH', order='original input order',
                identity='same original elements and multiplicity on complete normal consumption'),
    effects=['original constructor and mutable class/instance membership',
             'original native stderr/display/clock/lock/thread/atexit effects',
             'original update and finally close, including interruption and exceptions'],
    unresolved=['complete original class/module mutable state and all creator provenance',
                'actual native stream/clock/lock/thread and supported default protocols',
                'normal/exception/interruption effect region and retained source consumer'])
_NLTK = dict(
    id='finite-original-sentence-slices/1',
    operations=['nltk.sent_tokenize', 'nltk.tokenize.sent_tokenize'], module='nltk.tokenize', symbol='sent_tokenize',
    sdk=[_file('nltk', '3.10.4', 'nltk/__init__.py', 'ff8f32f6956dbbf928bb3807fede1c2e4f36f949564ac0f56d8b28a5643550ca'),
         _file('nltk', '3.10.4', 'nltk/tokenize/__init__.py', 'a020531b1bdeaf4b52cbf8118884d48d98a4a680e79cd2b227ff4622f25c9fde'),
         _file('nltk', '3.10.4', 'nltk/tokenize/api.py', 'c54d6071e7617300b5e59b1571a771ba2ef16692b0a590f7ff97bae70186868c'),
         _file('nltk', '3.10.4', 'nltk/tokenize/punkt.py', '13396705806d7a1c271052c0ee6e310af51be0dbc11abf81c7f7cd0325b92cac'),
         _file('nltk', '3.10.4', 'nltk/tabdata.py', '315e8c514d1889cfc66069343cc5242581fd73acbfb726f27ab45b849248ff4b'),
         _file('nltk', '3.10.4', 'nltk/redos.py', '4b36c61d1e172545d675c68b156d9294ab1246aec1a73bd7c89226c35e135285'),
         _file('nltk', '3.10.4', 'nltk/pathsec.py', 'fba39db41995899ce5e07c3287e28fb52b06ee5be99e5b7423b0c36ea89d124f'),
         _file('nltk', '3.10.4', 'nltk/data.py', 'e3f252726ad65639b97bd463da171653f0632b02d9325a89d54a5080151a7233'),
         _file('regex', '2026.9.29', 'regex/__init__.py', '039934ae6f0b9fb1cab1f1bef2c11661e85e93ccc22847bab105b94065009925'),
         _file('regex', '2026.9.29', 'regex/_main.py', 'b3ef1c11f7a7721e6e0fe462d8bba85b32fb814ca6210d07bb17da1bb128e436'),
         _file('regex', '2026.9.29', 'regex/_regex_core.py', '604eb46253402336c66362f32c4a20af095d3bd0b5505501b44e6077e92951a2'),
         _file('regex', '2026.9.29', 'regex/_regex.cpython-310-x86_64-linux-gnu.so', 'f1d79ebfa1c71e9faa5a11cd2fa09a4f0344a7371b68d7d049c69546a4b89abb')],
    methods=[], result=dict(kind='FINITE_EXACT_STR_SLICES', order='original span order',
                            exact_values='UNKNOWN', exact_length='UNKNOWN',
                            cardinality_bound='original .?! candidate characters plus one; no sentence decision evaluated'),
    effects=['original lru cache hit or original PunktTokenizer construction on miss',
             'original resource lookup/read/decoding/regexp and normal exceptions'],
    unresolved=['original function/factory/class/method/default/global and cached-object provenance',
                'actual original language, PunktLanguageVars sent_end_chars (.?!), period-context/realignment regexp and native resource/read/decoder protocols',
                'matching frozen punkt_tab resources at actual first effective lookup root',
                'normal/exception effect region and retained source consumer'])
_SUMMARIES = (_TQDM, _NLTK)


@dataclass(eq=False)
class SDKIteration:
    """An immediate conditional passthrough proof, never an SDK replacement."""
    items: tuple
    site: int
    sources: tuple
    requirement: dict


@dataclass(frozen=True)
class SDKListDomain(Shape):
    """Outer list and bounded string domain; no invented sentence or count."""
    max_item_length: int
    max_length: object = None


def _definition(target):
    if _TYPE(target) is not _STR:
        return None
    return next((definition for definition in _SUMMARIES if target in definition['operations']), None)


def _plain_fingerprint(value):
    if _TYPE(value) is _TUPLE:
        return dict(kind='tuple', values=[_plain_fingerprint(item) for item in value])
    if _TYPE(value) is list:
        return dict(kind='list', values=[_plain_fingerprint(item) for item in value])
    return dict(kind=_TYPE(value).__name__, value=ordinary(value))


def summarize_call(checker, target, args, kwargs, path, node):
    """Consume the source site and exact finite input; do not run an SDK.

    A recognized operation with unproved inputs still exports its independent
    target requirements, but never receives an invented successful result.
    """
    definition = _definition(target)
    if definition is None or checker.call_context is None:
        return None
    definition = copy.deepcopy(definition)
    requirement = dict(schema=RULE, summary=definition,
        summary_digest=digest(definition), target_status='REQUIRES_NORMAL_WORKER_CHECK',
        execution_permission=False, source_version=checker.inspector.request.get('source_version'),
        source_call=dict(path=path, source_sha256=checker.inspector.files[path]['sha256'],
            line=node.lineno, col=node.col_offset,
            ast_sha256=hashlib.sha256(ast.dump(node, include_attributes=False).encode()).hexdigest()),
        operation=target, input_status='UNKNOWN', input_digest=None)
    value = None
    expanded = (any(isinstance(part, ast.Starred) for part in node.args)
                or any(k.arg is None for k in node.keywords)
                or not checker.sdk_source.unmodified(target))
    if checker.call_context.compiling:
        requirement.update(input_status='REQUIRES_ACTUAL_CALL',
            input_condition='exact actual ordinary arguments, original omitted defaults and finite result-domain bounds')
        checker.protocol(RULE, path, node, target_requirements=requirement,
                         result_domain=copy.deepcopy(definition['result']),
                         input_status=requirement['input_status'])
        # This is a parameterized source plan, never a text sample, sentence
        # count, iterator instance or ready target. Its actual trace and target
        # conditions are consumed again on the normal worker.
        return (Shape('list') if definition['id'] == _NLTK['id'] else None), requirement
    if not expanded and definition['id'] == _TQDM['id'] and len(args) == 1 and not set(kwargs) - {'desc', 'total'}:
        items = checker.iteration_items(node.args[0], args[0]) if node.args else None
        options = (('desc' not in kwargs or _TYPE(kwargs['desc']) is _STR)
                   and ('total' not in kwargs or _TYPE(kwargs['total']) is int and kwargs['total'] >= 0))
        if _TYPE(items) is _TUPLE and len(items) <= 64 and options:
            try:
                fingerprint = _plain_fingerprint(items)
                requirement.update(input_status='CHECKED', input_digest=digest([fingerprint, kwargs]))
                value = SDKIteration(items, id(node), tuple(args), requirement)
            except CallUnknown:
                pass
    if not expanded and definition['id'] == _NLTK['id'] and 1 <= len(args) <= 2 and not set(kwargs) - {'language'}:
        language = args[1] if len(args) == 2 else kwargs.get('language', 'english')
        if (not (len(args) == 2 and 'language' in kwargs) and _TYPE(args[0]) is _STR
                and len(args[0]) <= 65536 and _TYPE(language) is _STR and language == 'english'):
            requirement.update(input_status='CHECKED', input_digest=digest([args[0], language]),
                               original_language=language)
            if checker.ordinary_value_operation('str'):
                # Punkt's original period-context regexp consumes one of the
                # original .?! characters per candidate; overlap suppression,
                # sentence decisions and boundary realignment add no slices.
                # The independent target premise retains those exact original
                # language-variable/regexp identities, including cache state.
                maximum = sum(_STR.count(args[0], character) for character in '.?!') + 1
                value = SDKListDomain('list', len(args[0]), maximum)
    checker.protocol(RULE, path, node, target_requirements=requirement,
                     result_domain=copy.deepcopy(definition['result']),
                     input_status=requirement['input_status'])
    verified = getattr(checker.call_context,'runtime_verified',())
    if (requirement['input_status'] != 'CHECKED' or _TYPE(verified) is not tuple
            or any(_TYPE(value) is not _STR for value in verified)
            or digest(requirement) not in verified):
        checker.negative_gap(path,node,GAP)
    return value, requirement


def verify_sdk_targets(requirements, context=None):
    """Read actual loaded identities and retain every unclosed state premise.

    This deliberately does not accept a boolean callback, candidate flag or
    externally supplied effect receipt. Its context is an internal identity
    created around an independently checked normal import occurrence; JSON
    fields and first-observed warm objects cannot supply it.
    """
    from seampact.source.sdk_runtime_targets import verify_actual_target
    rows = []
    if _TYPE(requirements) is not list or not requirements:
        return dict(schema='actual-finite-sdk-targets/1', status='UNKNOWN', allowed=False, targets=[])
    for requirement in requirements:
        row = dict(status='UNKNOWN', object_identity='UNKNOWN', effect_state='UNKNOWN', remaining=[])
        try:
            if _TYPE(requirement) is not _DICT or any(_TYPE(key) is not _STR for key in requirement):
                raise CallUnknown('SDK target requirement metadata is not native')
            definition = _definition(requirement.get('operation'))
            if (definition is None or requirement.get('schema') != RULE
                    or requirement.get('execution_permission') is not False
                    or requirement.get('target_status') != 'REQUIRES_NORMAL_WORKER_CHECK'
                    or requirement.get('summary') != definition
                    or requirement.get('summary_digest') != digest(definition)
                    or requirement.get('input_status') != 'CHECKED'):
                raise CallUnknown('source SDK summary or actual input premise is not the trusted finite definition')
            row.update(operation=requirement['operation'], summary_digest=requirement['summary_digest'],
                       remaining=copy.deepcopy(definition['unresolved']))
            observed = verify_actual_target(definition,requirement,context)
            row.update(status=observed['status'],allowed=observed['allowed'],
                       actual_target=observed,remaining=observed['remaining'])
        except (CallUnknown, OSError, ValueError, TypeError, KeyError) as error:
            row['reason'] = str(error)
        rows.append(row)
    checked = all(row.get('allowed') is True and row.get('status') == 'CHECKED' for row in rows)
    return dict(schema='actual-finite-sdk-targets/1',status='CHECKED' if checked else 'UNKNOWN',
                allowed=checked,targets=rows,
                reason=None if checked else 'original SDK mutable state/resource/effect premises are not fully checked')
