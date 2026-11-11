"""Freeze actual ordinary SDK assets selected by original lookup semantics.

This is a read-only resource premise for an independently checked source
prefix. It cannot select a private path, import an SDK, suppress a download,
or establish the rest of an SDK import/call effect region.
"""
import hashlib
import os
from pathlib import Path
import stat
import sys
from types import FunctionType

from seampact.source.call_value_conditions import digest, CallUnknown

SCHEMA = 'source-sdk-asset-plan/1'
_TYPE, _STR, _DICT = type, str, dict
_ENVIRON = type(os.environ)
_ENV_METHODS = {name:_ENVIRON.__dict__[name] for name in ('__getitem__','__iter__','__len__')}
_JOIN, _EXPANDUSER = os.path.join, os.path.expanduser
_PATH_CODES = (_JOIN.__code__, _EXPANDUSER.__code__)
_ENV_GET = _ENVIRON.get
_ENV_GET_CODE = _ENV_GET.__code__
_ENV_ENCODERS = {name:object.__getattribute__(os.environ,name) for name in ('encodekey','decodevalue')}
_ENV_ENCODER_CODES = {name:function.__code__ if _TYPE(function) is FunctionType else None
                      for name,function in _ENV_ENCODERS.items()}
_LOOKUPS = ('tokenizers/punkt', 'tokenizers/punkt_tab/english')
_ENGLISH = ('collocations.tab','sent_starters.txt','abbrev_types.txt','ortho_context.tab')


def original_nltk_paths():
    """The reviewed data.py path construction, using unchanged actual state."""
    if (_TYPE(os.environ) is not _ENVIRON or any(_ENVIRON.__dict__.get(name) is not method
            for name,method in _ENV_METHODS.items()) or os.path.join is not _JOIN
            or os.path.expanduser is not _EXPANDUSER or (_JOIN.__code__,_EXPANDUSER.__code__) != _PATH_CODES
            or _ENVIRON.get is not _ENV_GET or _ENV_GET.__code__ is not _ENV_GET_CODE
            or _ENVIRON.__getattribute__ is not object.__getattribute__
            or any(object.__getattribute__(os.environ,name) is not function
                   or _TYPE(function) is FunctionType and function.__code__ is not _ENV_ENCODER_CODES[name]
                   for name,function in _ENV_ENCODERS.items())
            or _TYPE(object.__getattribute__(os.environ,'_data')) is not _DICT or _TYPE(sys.prefix) is not _STR):
        raise CallUnknown('original native environment/path-construction kernel changed')
    keys = ('NLTK_DATA','APPENGINE_RUNTIME','HOME','USERPROFILE','HOMEDRIVE','HOMEPATH','APPDATA')
    environment = {}
    for key in keys:
        try:
            environment[key] = _ENV_METHODS['__getitem__'](os.environ,key)
        except KeyError:
            pass
    if any(_TYPE(value) is not _STR for value in environment.values()):
        raise CallUnknown('actual original path environment is not ordinary native text')
    if os.name == 'posix' and 'HOME' not in environment:
        raise CallUnknown('original home lookup without HOME requires an independently checked account-name kernel')
    if os.name not in ('posix','nt'):
        raise CallUnknown('original SDK asset path platform is outside the finite profile')
    result = [_EXPANDUSER(value) for value in environment.get('NLTK_DATA','').split(os.pathsep) if value]
    if 'APPENGINE_RUNTIME' not in environment and _EXPANDUSER('~/') != '~/':
        result.append(_EXPANDUSER('~/nltk_data'))
    result.extend((_JOIN(sys.prefix,'nltk_data'), _JOIN(sys.prefix,'share','nltk_data'),
                   _JOIN(sys.prefix,'lib','nltk_data')))
    if os.name == 'nt':
        result.extend((_JOIN(environment.get('APPDATA','C:\\'),'nltk_data'),
                       'C:\\nltk_data','D:\\nltk_data','E:\\nltk_data'))
    else:
        result.extend(('/usr/share/nltk_data','/usr/local/share/nltk_data',
                       '/usr/lib/nltk_data','/usr/local/lib/nltk_data'))
    if len(result) > 64 or any(_TYPE(value) is not _STR or not Path(value).is_absolute() for value in result):
        raise CallUnknown('original SDK lookup has unsupported relative or excessive effective roots')
    return result, dict(platform=os.name,prefix=sys.prefix,cwd=os.getcwd(),environment=environment)


def _ordinary_directory(path):
    for component in [path,*path.parents]:
        details = component.lstat()
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISDIR(details.st_mode):
            raise CallUnknown('SDK lookup uses a nonordinary directory or symbolic path component')


def _first_root(paths, resource):
    selected = []
    for index,root in enumerate(paths):
        base = Path(root)
        if base.suffix == '.zip' and os.path.lexists(base):
            raise CallUnknown('original zip lookup needs a separately checked archive/read protocol')
        candidate = base.joinpath(*resource.split('/'))
        # These are possible original fallback archives, not new search roots.
        archives = [base/'tokenizers.zip',base/'tokenizers/punkt.zip',
                    base/'tokenizers/punkt_tab.zip',base/'tokenizers/punkt_tab/english.zip']
        if os.path.lexists(candidate):
            _ordinary_directory(candidate)
            selected.append(dict(resource=resource,first_root_index=index,path=str(base)))
            break
        if any(os.path.lexists(path) for path in archives):
            raise CallUnknown('an earlier original archive fallback can change the effective SDK resource')
    if not selected:
        raise CallUnknown('original SDK resource is absent; its original download branch remains reachable')
    return selected[0]


def _tree(root):
    """Complete finite native tree, including modes and original relative order."""
    root = Path(root)
    _ordinary_directory(root)
    root_details = root.lstat()
    if os.name == 'posix' and (root_details.st_mode & 0o022 or root_details.st_uid not in (0,os.getuid())):
        raise CallUnknown('SDK asset root owner/mode permits an unaccounted writer')
    rows = []
    for current,dirs,files in os.walk(root,followlinks=False):
        for name in sorted([*dirs,*files]):
            path = Path(current)/name
            details = path.lstat()
            directory, file = stat.S_ISDIR(details.st_mode), stat.S_ISREG(details.st_mode)
            if stat.S_ISLNK(details.st_mode) or not (directory or file):
                raise CallUnknown('SDK asset tree includes an unsupported native entry protocol')
            if os.name == 'posix' and (details.st_mode & 0o022 or details.st_uid not in (0,os.getuid())):
                raise CallUnknown('SDK asset owner/mode permits an unaccounted writer')
            row = dict(path=path.relative_to(root).as_posix(),kind='directory' if directory else 'file',
                       mode=stat.S_IMODE(details.st_mode),owner=getattr(details,'st_uid',None),bytes=details.st_size)
            if file:
                row['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
            rows.append(row)
            if len(rows) > 100000:
                raise CallUnknown('SDK asset tree exceeds the finite manifest cardinality')
    return sorted(rows,key=lambda row:row['path'])


def compile_sdk_asset_plan(inspector, entry, candidate, frozen_resources, operations):
    """Build actual-host resource facts; the caller freezes them in its plan."""
    from seampact.source.sdk_value_summaries import _definition
    from seampact.source.lifecycle_resources import sdk_identity
    from seampact.source.import_only import _metadata
    result = dict(schema=SCHEMA,status='UNKNOWN',source_version=inspector.request.get('source_version'),
        entry_digest=digest(entry),candidate_digest=digest(candidate),resource_digest=digest(frozen_resources),
        operation_requirements=[],original_lookup=[],bindings={},path_semantics=None,remaining=[])
    try:
        operations = _metadata(operations)
        if _TYPE(operations) is not list or not operations or any(_TYPE(value) is not _STR for value in operations):
            raise CallUnknown('original finite SDK operation roots are absent')
        definitions = []
        for operation in operations:
            definition = _definition(operation)
            if definition is None:
                raise CallUnknown('selected SDK operation has no trusted original lookup semantics')
            if definition['id'] == 'finite-original-sentence-slices/1' and definition not in definitions:
                definitions.append(definition)
        if not definitions:
            raise CallUnknown('selected original operations do not consume a reviewed SDK asset lookup')
        for definition in definitions:
            result['operation_requirements'].append(dict(summary_digest=digest(definition),sdk=definition['sdk']))
            if sdk_identity(definition['sdk'])['status'] != 'CHECKED':
                raise CallUnknown('actual installed SDK source identity differs from reviewed lookup semantics')
        paths,semantics = original_nltk_paths()
        result['path_semantics'] = dict(**semantics,original_effective_paths=paths)
        lookups = [_first_root(paths,resource) for resource in _LOOKUPS]
        result['original_lookup'] = lookups
        if len({row['path'] for row in lookups}) != 1:
            raise CallUnknown('separate original resource roots require an independently checked multi-root read binding')
        root = lookups[0]['path']
        manifest = _tree(root)
        native_files = [{key:row[key] for key in ('path','sha256','bytes')} for row in manifest if row['kind']=='file']
        required = {'tokenizers/punkt_tab/english/'+name for name in _ENGLISH}
        if not required <= {row['path'] for row in native_files}:
            raise CallUnknown('original English Punkt resource does not contain all four original decoder files')
        result['bindings']['nltk'] = dict(kind='directory',path=root,required_files=native_files,
            full_tree_manifest=manifest,manifest_digest=digest(manifest),
            root_identity=dict(mode=stat.S_IMODE(Path(root).lstat().st_mode),
                               owner=getattr(Path(root).lstat(),'st_uid',None)),
            original_effective_paths_digest=digest(paths),lookup_digest=digest(lookups))
        result['status'] = 'CHECKED'
    except (CallUnknown,OSError,ValueError,TypeError,KeyError) as error:
        result['remaining'].append(str(error))
    return dict(result,asset_plan_digest=digest(result))


def verify_sdk_asset_plan(inspector,entry,candidate,frozen_resources,operations,plan):
    """Recompute actual lookup/bytes/owner/mode; metadata status is insufficient."""
    try:
        from seampact.source.import_only import _metadata
        plan = _metadata(plan)
        return plan.get('status') == 'CHECKED' and plan == compile_sdk_asset_plan(
            inspector,entry,candidate,frozen_resources,operations)
    except (CallUnknown,OSError,ValueError,TypeError,KeyError):
        return False
