"""Finite original import regions, separate from their worker prerequisites.

The leaves retain the complete original SDK utility/definition effects in the
selected working set. They do not grant a model construction, neural call,
copy or release. Installed bytes establish identity, never the effect leaf.
Unreviewed optional dependency effects remain explicit target frontiers.
"""
import ast
import base64
import builtins
import configparser
import copy
import csv
import hashlib
import io
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import stat
import sys
from types import FunctionType, ModuleType, BuiltinMethodType

from seampact.source.call_value_conditions import CallUnknown, digest

RULE = 'source-sdk-import-region/1'
GAP = 'original finite SDK import region requires actual worker prerequisites'
FINDER = 'source-sdk-original-resource-find/1'
_TYPE, _DICT, _STR, _LIST = type, dict, str, list
_RESOURCES = ('tokenizers/punkt', 'tokenizers/punkt_tab/english')
_MODULE_DICT=ModuleType.__dict__['__dict__']
_CLASS_DICT=type.__dict__['__dict__']
_BUILTINS=tuple(_MODULE_DICT.__get__(builtins,ModuleType).items())
_LOOKUP_ERROR=LookupError
_NATIVE_PATTERN=re.Pattern


def _capture_reader_kernel():
    from seampact.source.sdk_native_kernels import _function
    modules=(os,os.path,stat,csv,configparser,hashlib,io)
    names={
        os:('stat','lstat','scandir','walk','getcwd','fspath','getenv','environ','path'),
        os.path:('abspath','join','expanduser','expandvars','isdir','isfile','lexists','normpath'),
        stat:('S_ISREG','S_ISLNK','S_ISDIR'),csv:('reader',),
        configparser:('ConfigParser','RawConfigParser','SectionProxy'),
        hashlib:('sha256',),io:('open','StringIO'),
    }
    rows=[]
    functions=[]
    for module in modules:
        namespace=_MODULE_DICT.__get__(module,ModuleType)
        selected=tuple((name,namespace[name]) for name in names[module])
        rows.append((module,namespace,selected))
        functions.extend(_function(value) for _,value in selected if _TYPE(value) is FunctionType)
    # Exact Path/ConfigParser methods must be checked before using any of them;
    # their ordinary file paths are not permission to execute replaced getters.
    classes=(set(Path.__mro__)|set(PurePosixPath.__mro__)|set(configparser.ConfigParser.__mro__)
             |set(type(os.environ).__mro__))
    import pathlib
    objects=[]
    for name in ('_posix_flavour','_windows_flavour','_normal_accessor'):
        value=pathlib.__dict__.get(name)
        if value is not None:
            classes.update(type(value).__mro__)
            namespace=object.__getattribute__(value,'__dict__')
            objects.append((value,tuple(namespace.items())))
    class_rows=[]
    for class_ in classes:
        if class_ is object:
            continue
        namespace=_CLASS_DICT.__get__(class_,type)
        class_rows.append((class_,tuple(namespace.items())))
        for value in namespace.values():
            if _TYPE(value) is FunctionType:
                functions.append(_function(value,closure_classes=tuple(classes)))
            elif _TYPE(value) in (staticmethod,classmethod) and _TYPE(value.__func__) is FunctionType:
                functions.append(_function(value.__func__,closure_classes=tuple(classes)))
            elif _TYPE(value) is property:
                functions.extend(_function(function,closure_classes=tuple(classes)) for function in (value.fget,value.fset,value.fdel)
                                 if _TYPE(function) is FunctionType)
    env_type=type(os.environ)
    env_methods=tuple((name,getattr(env_type,name)) for name in ('__getitem__','__iter__','__len__','get'))
    functions.extend(_function(value) for _,value in env_methods if _TYPE(value) is FunctionType)
    env_data=object.__getattribute__(os.environ,'__dict__')
    env_fields=tuple((name,env_data.get(name)) for name in ('encodekey','decodekey','encodevalue','decodevalue'))
    functions.extend(_function(value) for _,value in env_fields if _TYPE(value) is FunctionType)
    return dict(modules=rows,classes=class_rows,objects=objects,functions=functions,
        env_type=env_type,environment=os.environ,env_methods=env_methods,env_fields=env_fields)


try:
    _READER_KERNEL=_capture_reader_kernel()
    _READER_KERNEL_ERROR=None
except (CallUnknown,AttributeError,TypeError,ValueError) as error:
    # An unsupported host cannot stop source compilation or the core import.
    # Only the actual worker's independent prerequisite check is refused.
    _READER_KERNEL=None
    _READER_KERNEL_ERROR=str(error)


def _verify_reader_kernel():
    from seampact.source.sdk_native_kernels import _same_function
    if _READER_KERNEL is None:
        raise CallUnknown('SDK native metadata reader is outside the finite platform profile: '+_READER_KERNEL_ERROR)
    namespace=_MODULE_DICT.__get__(builtins,ModuleType)
    if (len(namespace)!=len(_BUILTINS) or any(_TYPE(name) is not str for name in namespace)
            or any(namespace.get(name) is not value for name,value in _BUILTINS)
            or namespace.get('LookupError') is not _LOOKUP_ERROR):
        raise CallUnknown('original SDK preflight builtin/LookupError identity changed')
    kernel=_READER_KERNEL
    for module,namespace,selected in kernel['modules']:
        actual=_MODULE_DICT.__get__(module,ModuleType)
        if actual is not namespace or any(_TYPE(name) is not str for name in actual) or any(
                actual.get(name) is not value for name,value in selected):
            raise CallUnknown('original SDK native resolver/environment/metadata reader changed')
    for class_,items in kernel['classes']:
        namespace=_CLASS_DICT.__get__(class_,type)
        if (len(namespace)!=len(items) or any(_TYPE(name) is not str for name in namespace)
                or any(namespace.get(name) is not value for name,value in items)):
            raise CallUnknown('original SDK native metadata-reader class protocol changed')
    for value,items in kernel['objects']:
        namespace=object.__getattribute__(value,'__dict__')
        if (type(namespace) is not dict or len(namespace)!=len(items)
                or any(_TYPE(name) is not str for name in namespace)
                or any(namespace.get(name) is not original for name,original in items)):
            raise CallUnknown('original SDK native path-reader state changed')
    if os.environ is not kernel['environment'] or _TYPE(os.environ) is not kernel['env_type']:
        raise CallUnknown('original SDK native environment object changed')
    for name,value in kernel['env_methods']:
        if getattr(kernel['env_type'],name) is not value:
            raise CallUnknown('original SDK native environment method changed')
    fields=object.__getattribute__(os.environ,'__dict__')
    if (_TYPE(fields) is not dict or any(_TYPE(name) is not str for name in fields)
            or any(fields.get(name) is not value for name,value in kernel['env_fields'])):
        raise CallUnknown('original SDK native environment converter changed')
    data=fields.get('_data')
    if (_TYPE(data) is not dict or any(_TYPE(key) not in (str,bytes) or _TYPE(value) not in (str,bytes)
                                    for key,value in data.items())):
        raise CallUnknown('original SDK native environment backing data is nonordinary')
    for state in kernel['functions']:
        function=state[0]
        if function is os.path.expandvars:
            # CPython's original expandvars lazily retains a native regexp.
            # Its original read is allowed to create that cache naturally;
            # never prewarm it or accept an arbitrary replacement callback.
            refs=[]
            for name,original,attrs in state[5]:
                current=state[4].get(name)
                if current is not original and name in ('_varprog','_varprogb','_varsub','_varsubb'):
                    if name.startswith('_varsub'):
                        if not isinstance(current,BuiltinMethodType) or current.__name__!='sub':
                            raise CallUnknown('original expandvars cache is not its native regexp method')
                        pattern=current.__self__
                        expected=state[4].get('_varpattern')
                        if name=='_varsubb' and _TYPE(expected) is str:
                            expected=expected.encode('ascii')
                    else:
                        pattern=current
                        expected=rb'\$(\w+|\{[^}]*\})' if name=='_varprogb' else r'\$(\w+|\{[^}]*\})'
                    if (_TYPE(pattern) is not _NATIVE_PATTERN or pattern.pattern!=expected
                            or pattern.flags!=256):
                        raise CallUnknown('original expandvars native cache has a different pattern/protocol')
                    refs.append((name,current,attrs))
                else:
                    refs.append((name,original,attrs))
            state=(*state[:5],tuple(refs),state[6])
        _same_function(state)


def _sdk(name, version, record, files, effects, optional):
    return dict(id='original-' + name + '-import-region/1', distribution=name,
        version=version, record_sha256=record,
        sdk=[dict(distribution=name, version=version, path=path, sha256=sha)
             for path,sha in files],
        region='complete original import/definition and SDK-owned utility initialization',
        effects=effects,
        effect_boundary=dict(coverage='ALL_ORIGINAL_NORMAL_AND_EXCEPTION_EFFECTS',
            ownership='original SDK utility state and native/platform dependencies; no source owner arguments',
            charge='same full original working set, import/setup/failure costs for every strategy',
            omitted_effects=[], model_construction=False, entry_invocation=False,
            lifecycle_permission='NOT_GRANTED'),
        optional_frontiers=optional,
        required=['same frozen source/entry/candidate/resources and actual complete reservation',
                  'original standard resolver, source/native ABI and bytecode-cache identities',
                  'no source-owner callback, unknown plugin, loader or concurrent writer',
                  'actual original ENV/cache/resource predicates; no preparatory SDK execution'])


_NLTK = _sdk('nltk','3.10.4','3998efc98bdecc1f51b38b6455062a1d5306e98df3a2555c586688dcc132b7e3',[
    ('nltk/__init__.py','ff8f32f6956dbbf928bb3807fede1c2e4f36f949564ac0f56d8b28a5643550ca'),
    ('nltk/downloader.py','eb2f5cdf31718e13bdab03d95a7913fe420653a662c16da801048718f7625dbf'),
    ('nltk/data.py','e3f252726ad65639b97bd463da171653f0632b02d9325a89d54a5080151a7233'),
    ('nltk/pathsec.py','fba39db41995899ce5e07c3287e28fb52b06ee5be99e5b7423b0c36ea89d124f'),
    ('nltk/tokenize/__init__.py','a020531b1bdeaf4b52cbf8118884d48d98a4a680e79cd2b227ff4622f25c9fde')],
    ['original contained VERSION read, module/class/lazy-object definitions',
     'original Downloader utility construction and directory/stat/permission probes',
     'original optional NumPy/cluster and tkinter-spec branches; original exceptions retained'],
    ['nltk/__init__.py:117 optional numpypy import: actual absence/original finite external initializer not yet bound',
     'nltk/__init__.py:145-175 collocations/decorators/featstruct/grammar/probability/text/util/jsontags and chunk/classify/inference/metrics/parse/tag/translate/tree/sem/stem import initializers: external dependency dispatch not yet closed',
     'nltk/__init__.py:190 tkinter find_spec then downloader.download_gui: actual standard resolver/spec branch has no complete original effect proof'])
_NUMPY = _sdk('numpy','1.26.4','47b71200e458f938e83ce23ec0998c5cd64e34fdada67bb28543ae480e1d1cac',[
    ('numpy/__init__.py','22cd1535fa14d74ef6f457cca149ffdc80875f460be313b8f895273f78bc402e'),
    ('numpy/_distributor_init.py','20acb64c7c26bb9520063b556f06c3f47f80a7cb9a509a0f2766ed43825ab5da'),
    ('numpy/__config__.py','4376cec1158a1485c750c2c4f58d1fe1d7e61b1ef6f8815b42f8e655345a4ffc')],
    ['original core/native/BLAS/FFT/random/linear-algebra import and registry initialization',
     'original ones/dot sanity operation and failure, warning filters and utility definitions',
     'original hugepage/promotion configuration and reload checks'],
    ['numpy/__init__.py:130-153 core/lib/linalg/fft/polynomial/random/ctypeslib/ma/matrixlib native initializer closure: actual linked BLAS/C-runtime resolver/ABI not independently bound'])
_TORCH = _sdk('torch','2.7.1+cu128','c0bd6661b32633d1fa9bfcaba4b376ada652ecb0015c8107e46942fd2c87c364',[
    ('torch/__init__.py','9801e75447c7f585545f989f8a21940b60e0c4cc888effb2f08e739664b4b904'),
    ('torch/cuda/__init__.py','4bd2da05273edd47203133b45843ece0e59234af28204f2ed5cb10d6175a4c67'),
    ('torch/_utils_internal.py','c679d367bfe3baa911370f970fcd53c3299cd597876966e66e4666d54ccc6b76')],
    ['original native global-deps/C-extension/CUDA dependency load and native initialization',
     'original global operator/class registries, shared-memory-manager and after-fork registrations',
     'original logging/backend/lazy-CUDA definitions and original failures'],
    ['torch/__init__.py:34 typing_extensions.ParamSpec and original imported torch/nn/functional utility initializer external dependency dispatch not yet closed',
     'torch/__init__.py:301-409 native global-deps/CUDA preload: complete actual external shared-library resolution/ABI is not yet bound'])
_TRANSFORMERS = _sdk('transformers','4.41.2','8f0f4785458213b7e1e4db97072370eaa1ebfa9664945bab3ff0ad8086a1d636',[
    ('transformers/__init__.py','b5b2ec77b2935835131de2ac3497383d66eb55986e9b785bd8d7ac840c44fce7'),
    ('transformers/utils/import_utils.py','6ae7764dfa7257c1d602824f3947a8692558ea5b0fc35e6e0cbb01bc36841b87'),
    ('transformers/utils/hub.py','300fc1c14edfcc1ef869402e669a1ccc2cbe9dd060c42f9fa3a369a0f4c7f45b')],
    ['original dependency/version availability checks and LazyModule creation/export resolution',
     'original AutoConfig/AutoTokenizer/AutoModel class definitions and lazy mappings',
     'original cache migration/version metadata attempts, warning/logging and failures'],
    ['transformers/__init__.py:29 dependency_versions_check and utils initializer external dependency/version-availability resolver domain not yet closed',
     'transformers/utils/hub.py:36-70 huggingface_hub/requests/filelock/tqdm utility initializer effects are outside the reviewed cache predicate leaf',
     'transformers/models/auto/{configuration_auto,tokenization_auto,modeling_auto}.py lazy export initializer dispatch: complete actual availability-selected external import closure not yet bound'])
_TQDM = _sdk('tqdm','4.64.0','972eb2cbb3e2aaf15c89582dfe00e909c693be1f0dff3b006bd84abab74c5cea',[
    ('tqdm/__init__.py','2e27b31c8a804cadc53a28ffdfae5e31cbb5d543e68a398314aa2097bd9f1e9e'),
    ('tqdm/std.py','dc3f2a7d1fc2751235d80774e459bd9dd99f1650f933a1efb4ada09fe2900c01'),
    ('tqdm/utils.py','2af1340ccdbc5fffcd1d82a01a5e6352b93a08ce4157ad06e0d7f9e484cf7892'),
    ('tqdm/_monitor.py','524bbe0cf5a0cc9edd3b908ad3cc4a24af84fc5eaa43e241de4b2e5dccd2611d')],
    ['original std/utils/monitor definitions and original CLI/GUI/pandas export definitions',
     'original platform stream/lock/clock/thread utility imports and failures'],
    ['tqdm/__init__.py:2-6 _tqdm_pandas/cli/gui original export initializer external dependency dispatch has not been source-reviewed'])
_DEFINITIONS = (_NLTK,_NUMPY,_TORCH,_TRANSFORMERS,_TQDM)
_EXPORTS = {
    'nltk': (None,'nltk'), 'nltk.tokenize': ('sent_tokenize',),
    'numpy': (None,'numpy'), 'torch': (None,'torch','nn'),
    'torch.nn': (None,'nn','functional'), 'torch.nn.functional': (None,'functional'),
    'transformers': ('AutoConfig','AutoTokenizer','AutoModelForSeq2SeqLM','AutoModelForSequenceClassification'),
    'tqdm': ('tqdm',),
}


def import_fact(module, imported=None):
    """A source conditional leaf; never inspect controller-installed SDKs."""
    if (_TYPE(module) is not _STR or module not in _EXPORTS
            or imported not in _EXPORTS[module]):
        return None
    definition = next(row for row in _DEFINITIONS if row['distribution']==module.split('.')[0])
    return dict(schema=RULE,status='CHECKED',source_status='STRUCTURE_CHECKED',
        target_status='REQUIRES_NORMAL_WORKER_CHECK',execution_permission=False,
        operation='IMPORT',module=module,imported=imported,
        summary=copy.deepcopy(definition),summary_digest=digest(definition))


def attribute_fact(qualified):
    """Only the original module links and named exports used by these leaves."""
    if _TYPE(qualified) is not _STR:
        return None
    module,_,symbol=qualified.rpartition('.')
    fact=import_fact(module,symbol)
    if qualified in ('nltk.data','nltk.data.find'):
        fact=import_fact('nltk')
    if fact is not None:
        fact.update(operation='ATTRIBUTE',qualified=qualified)
    return fact


def _site(checker,path,node):
    return dict(path=path,source_sha256=checker.inspector.files[path]['sha256'],
        line=node.lineno,col=node.col_offset,
        ast_sha256=hashlib.sha256(ast.dump(node,include_attributes=False).encode()).hexdigest())


def import_requirement(checker,path,node,module,imported=None):
    fact=import_fact(module,imported)
    if fact is None or getattr(node,'level',0):
        return None
    fact.update(source_version=checker.inspector.request.get('source_version'),
                source_site=_site(checker,path,node))
    checker.protocol(RULE,path,node,target_requirements=fact)
    return fact


def attribute_requirement(checker,path,node,qualified):
    fact=attribute_fact(qualified)
    if fact is None or not checker.sdk_source.unmodified(qualified):
        return None
    fact.update(source_version=checker.inspector.request.get('source_version'),
                source_site=_site(checker,path,node))
    checker.protocol(RULE,path,node,target_requirements=fact)
    return fact


def finder_requirement(checker,path,node,target,args,kwargs):
    """Conditional normal exit, without invoking or fabricating a PathPointer."""
    if (target != 'nltk.data.find' or _TYPE(args) not in (list,tuple) or len(args)!=1
            or _TYPE(args[0]) is not _STR or args[0] not in _RESOURCES
            or _TYPE(kwargs) is not dict or kwargs
            or not checker.sdk_source.unmodified(target)):
        return None
    fact=import_fact('nltk')
    fact.update(schema=FINDER,operation='ORIGINAL_RESOURCE_FIND',resource=args[0],
        source_version=checker.inspector.request.get('source_version'),
        source_site=_site(checker,path,node),
        asset_operations=['nltk.sent_tokenize'],resource_requirements=list(_RESOURCES),
        normal_result='original nonescaping PathPointer; no replacement object is executed',
        normal_exit_condition='actual original first effective ordinary root and complete bound resource tree',
        excluded_exception='LookupError only under independently verified original lookup premises',
        retained_exceptions='all other original exceptions propagate in original order',
        source_effects='original finder/read/pathsec protocols retained; no download/cache prewarm')
    checker.protocol(FINDER,path,node,target_requirements=fact)
    return fact


def asset_operations(requirements):
    """The normal plan consumes this once; no full-method replay is required."""
    rows=_metadata(requirements)
    return ['nltk.sent_tokenize'] if any(row.get('schema')==FINDER or
        row.get('schema')==RULE and row.get('summary',{}).get('distribution')=='nltk'
        for row in rows) else []


def asset_obligations(requirements):
    operations=asset_operations(requirements)
    return dict(operations=operations,resources=list(_RESOURCES) if operations else [])


def finder_normal_exit(requirement):
    """A non-value control token consumed only for the original LookupError arm.

    The token never stands for a PathPointer or suppresses another exception.
    The normal source plan still exports this precise worker requirement.
    """
    if (_TYPE(requirement) is not dict or requirement.get('schema')!=FINDER
            or requirement.get('operation')!='ORIGINAL_RESOURCE_FIND'
            or requirement.get('resource') not in _RESOURCES
            or requirement.get('summary')!=_NLTK
            or requirement.get('summary_digest')!=digest(_NLTK)):
        raise CallUnknown('finder control token lacks the original finite requirement')
    return dict(kind='ORIGINAL_CONDITIONAL_NORMAL_EXIT',schema=FINDER,
        requirement_digest=digest(requirement),excluded_exception='builtins.LookupError',
        result_value_available=False,worker_check_required=True)


def _metadata(value):
    from seampact.source.import_only import _metadata as read
    return read(value)


def _directory(path):
    """A finite ordinary resolver root; no archive/custom path hooks execute."""
    path=Path(path)
    for item in (path,*path.parents):
        details=item.lstat()
        if not stat.S_ISDIR(details.st_mode) or stat.S_ISLNK(details.st_mode):
            raise CallUnknown('SDK resolver uses a symbolic/nonordinary directory')
    return path


def resolver_domain(paths=None):
    paths=sys.path if paths is None else paths
    if _TYPE(paths) is not list or len(paths)>2048 or any(_TYPE(path) is not str for path in paths):
        raise CallUnknown('original SDK resolver path domain is not a finite native list')
    result=[]
    for value in paths:
        path=Path(os.path.abspath(value))
        if not os.path.lexists(path):
            continue
        if not path.is_dir():
            raise CallUnknown('original SDK resolver includes an archive/non-directory search domain')
        result.append(_directory(path))
    return result


def _bundle(definition,roots):
    """Authenticate the full recorded primary bundle, including all native files.

    This guard is identity only. The independent semantic effect frontier below
    is deliberately not discharged by a successful RECORD comparison.
    """
    name,version=definition['distribution'],definition['version']
    record_digest=definition['record_sha256']
    if record_digest is None:
        raise CallUnknown('complete original '+name+' RECORD identity is not yet frozen')
    chosen=None
    for root in roots:
        # Standard source/extension precedence cannot silently select an earlier
        # shadow module. Package roots must be original native directories.
        shadows=[root/(name+suffix) for suffix in ('.py','.pyc','.so')]
        shadows.extend(root.glob(name+'.*.so'))
        if any(os.path.lexists(path) for path in shadows):
            raise CallUnknown('an earlier SDK shadow module changes original resolution')
        package=root/name
        if os.path.lexists(package):
            _directory(package)
            if not (package/'__init__.py').is_file():
                raise CallUnknown('SDK namespace/sourceless package is outside the original profile')
            chosen=root
            break
    if chosen is None:
        raise CallUnknown('original '+name+' bundle is absent on this worker')
    metadata=chosen/(name+'-'+version+'.dist-info')
    _directory(metadata)
    record=(metadata/'RECORD').read_bytes()
    if hashlib.sha256(record).hexdigest()!=record_digest:
        raise CallUnknown('complete original '+name+' RECORD changed')
    rows=list(csv.reader(io.StringIO(record.decode('utf-8'))))
    if not rows or len(rows)>100000 or any(len(row)!=3 for row in rows):
        raise CallUnknown('SDK RECORD has an unsupported finite structure')
    files,seen=[],set()
    for relative,encoded,size in rows:
        pure=PurePosixPath(relative)
        if (pure.is_absolute() or '..' in pure.parts or not pure.parts
                or relative in seen):
            # Wheel console scripts may intentionally be outside site-packages;
            # they are never imported in this region and are not silently read.
            if pure.parts[:1]==('..',):
                continue
            raise CallUnknown('SDK RECORD has an ambiguous package file path')
        seen.add(relative)
        if pure.parts[0] not in (name,metadata.name) and not pure.parts[0].startswith(name+'.'):
            continue
        path=chosen.joinpath(*pure.parts)
        details=path.lstat()
        if not stat.S_ISREG(details.st_mode) or stat.S_ISLNK(details.st_mode):
            raise CallUnknown('SDK bundle has a nonordinary file protocol')
        if not encoded:
            if relative.endswith('/RECORD') or relative.endswith('.pyc'):
                continue
            raise CallUnknown('SDK importable file has no original integrity entry')
        hasher=hashlib.sha256()
        with path.open('rb') as source:
            for block in iter(lambda:source.read(1048576),b''):
                hasher.update(block)
        actual=base64.urlsafe_b64encode(hasher.digest()).decode('ascii').rstrip('=')
        if encoded!='sha256='+actual or size!=str(details.st_size):
            raise CallUnknown('original SDK bundle file/size differs from its frozen RECORD')
        if relative.endswith('.py'):
            files.append(dict(distribution=name,version=version,path=relative,
                              sha256=hasher.hexdigest()))
    package_roots=[chosen/name,*[path for path in chosen.iterdir() if path.name.startswith(name+'.')]]
    for package in package_roots:
        if not package.is_dir():
            continue
        for current,dirs,names in os.walk(package,followlinks=False):
            for entry in [*dirs,*names]:
                path=Path(current)/entry
                details=path.lstat()
                if stat.S_ISLNK(details.st_mode):
                    raise CallUnknown('SDK import package contains a symbolic extra member')
                relative=path.relative_to(chosen).as_posix()
                if path.is_file() and not relative.endswith('.pyc') and relative not in seen:
                    raise CallUnknown('SDK import package contains an unrecorded member')
    expected={row['path']:row['sha256'] for row in definition['sdk']}
    actual={row['path']:row['sha256'] for row in files}
    if any(actual.get(path)!=sha for path,sha in expected.items() if path.endswith('.py')):
        raise CallUnknown('original import leaf source differs from the complete installed bundle')
    return dict(root=str(chosen),package=str(chosen/name),python_files=files,
                record_sha256=record_digest,member_count=len(seen))


def _source_qualified(tree,node):
    aliases={}
    for statement in ast.walk(tree):
        if type(statement) is ast.Import:
            for alias in statement.names:
                local=alias.asname or alias.name.split('.')[0]
                aliases.setdefault(local,set()).add(alias.name if alias.asname else alias.name.split('.')[0])
        elif type(statement) is ast.ImportFrom and not statement.level:
            for alias in statement.names:
                aliases.setdefault(alias.asname or alias.name,set()).add((statement.module or '')+'.'+alias.name)
    def resolve(value):
        if type(value) is ast.Name:
            names=aliases.get(value.id,set())
            return next(iter(names)) if len(names)==1 else None
        if type(value) is ast.Attribute:
            parent=resolve(value.value)
            return parent+'.'+value.attr if parent else None
        return None
    return resolve(node)


def _verify_site(row,inspector):
    site=row.get('source_site',{})
    path=site.get('path')
    if (path not in inspector.files or inspector.files[path]['sha256']!=site.get('source_sha256')
            or row.get('source_version')!=inspector.request.get('source_version')):
        raise CallUnknown('actual SDK source import/finder site changed')
    data=inspector.data(path)
    if hashlib.sha256(data).hexdigest()!=site.get('source_sha256'):
        raise CallUnknown('actual frozen source bytes changed before SDK import')
    tree=ast.parse(data)
    nodes=[node for node in ast.walk(tree) if getattr(node,'lineno',None)==site.get('line')
        and getattr(node,'col_offset',None)==site.get('col')
        and hashlib.sha256(ast.dump(node,include_attributes=False).encode()).hexdigest()==site.get('ast_sha256')]
    if len(nodes)!=1:
        raise CallUnknown('normal SDK requirement does not identify one original source syntax site')
    node=nodes[0]
    if row['schema']==RULE and row.get('operation')=='IMPORT':
        if type(node) is ast.Import:
            valid=row.get('imported') is None and any(alias.name==row.get('module') for alias in node.names)
        else:
            valid=(type(node) is ast.ImportFrom and not node.level and node.module==row.get('module')
                   and any(alias.name==row.get('imported') for alias in node.names))
        if not valid:
            raise CallUnknown('normal SDK import requirement differs from its original syntax')
    elif row['schema']==FINDER:
        if (type(node) is not ast.Call or len(node.args)!=1 or node.keywords
                or type(node.args[0]) is not ast.Constant or type(node.args[0].value) is not str
                or node.args[0].value!=row.get('resource') or row.get('resource') not in _RESOURCES
                or _source_qualified(tree,node.func)!='nltk.data.find'):
            raise CallUnknown('normal SDK finder obligation differs from its original finite call')
    elif (row.get('operation')!='ATTRIBUTE' or type(node) is not ast.Attribute
            or _source_qualified(tree,node)!=row.get('qualified')):
        raise CallUnknown('normal SDK requirement is not an original supported syntax operation')
    return node


def torch_backend_domain(roots):
    """Cover every native directory/egg-info/dist-info discovery source.

    No metadata entry point is loaded or invoked. Arbitrary resolver objects,
    archives and installed torch.backends callbacks are outside this leaf.
    """
    checked=[]
    for root in roots:
        for child in sorted(root.iterdir(),key=lambda path:path.name):
            if child.name.endswith('.egg'):
                raise CallUnknown('original torch backend resolver includes an unreviewed egg domain')
            if not child.name.endswith(('.dist-info','.egg-info')):
                continue
            if child.is_file():
                # Single-file egg metadata has no adjacent entry_points.txt.
                if child.name.endswith('.egg-info') and not child.is_symlink():
                    continue
                raise CallUnknown('original backend metadata has a non-directory protocol')
            _directory(child)
            path=child/'entry_points.txt'
            if not os.path.lexists(path):
                continue
            if path.is_symlink() or not path.is_file():
                raise CallUnknown('original backend entry-point metadata is nonordinary')
            data=path.read_bytes()
            if len(data)>1048576:
                raise CallUnknown('original backend metadata exceeds its finite size bound')
            parser=configparser.ConfigParser(interpolation=None)
            parser.read_string(data.decode('utf-8'))
            if parser.has_section('torch.backends') and parser.items('torch.backends'):
                raise CallUnknown('an original torch.backends callback is reachable and not reviewed')
            checked.append(dict(path=str(path),sha256=hashlib.sha256(data).hexdigest()))
    return dict(status='CHECKED',files=checked,callbacks=[])


def transformers_cache_domain():
    """Original two migration predicates, retaining original version-write effects."""
    environment=dict(os.environ)
    if any(_TYPE(key) is not str or _TYPE(value) is not str for key,value in environment.items()):
        raise CallUnknown('original transformer cache environment is nonordinary')
    get=lambda key,default=None:environment.get(key,default)
    home=os.path.expanduser('~')
    if home=='~':
        raise CallUnknown('original transformer cache home semantics are unresolved')
    hf_home=os.path.expandvars(os.path.expanduser(get('HF_HOME',os.path.join(
        get('XDG_CACHE_HOME',os.path.join(home,'.cache')),'huggingface'))))
    hub=os.path.expandvars(os.path.expanduser(get('HF_HUB_CACHE',get('HUGGINGFACE_HUB_CACHE',
        os.path.join(hf_home,'hub')))))
    target=get('TRANSFORMERS_CACHE',get('PYTORCH_TRANSFORMERS_CACHE',get('PYTORCH_PRETRAINED_BERT_CACHE',hub)))
    torch_home=get('TORCH_HOME',os.path.join(get('XDG_CACHE_HOME','~/.cache'),'torch'))
    old=os.path.join(torch_home,'transformers')
    if (os.path.isdir(old) and not os.path.isdir(hub) and not any(key in environment for key in
            ('PYTORCH_PRETRAINED_BERT_CACHE','PYTORCH_TRANSFORMERS_CACHE','TRANSFORMERS_CACHE'))):
        raise CallUnknown('original old-cache shutil.move branch remains reachable')
    version_path=Path(target)/'version.txt'
    version=0
    if version_path.is_file():
        if version_path.is_symlink() or version_path.stat().st_size>65536:
            raise CallUnknown('original cache version metadata is nonordinary/unbounded')
        try:
            version=int(version_path.read_text())
        except ValueError:
            version=0
    nonempty=os.path.isdir(target) and len(os.listdir(target))>0
    if version<1 and nonempty:
        raise CallUnknown('original transformers migration may inspect metadata/contact remote resources')
    return dict(status='CHECKED',target=target,hub=hub,old_cache=old,version=version,
        original_version_write='retained, including failure/logging' if version<1 else 'not selected',
        original_migration='not selected by original predicates',environment=environment)


def verify_before_sdk_import(requirements,asset_plan,normalbinding,*,entry,unit,plan,instance_identity=None):
    """Normal-worker guard; read native metadata only, never import an SDK."""
    guards,remaining=[],[]
    try:
        _verify_reader_kernel()
        from seampact.source.sdk_runtime_targets import _binding
        _binding(normalbinding)
        rows=_metadata(requirements)
        if _TYPE(rows) is not list or not rows:
            raise CallUnknown('normal SDK import requirements are absent')
        from seampact.source.import_only import _metadata as read
        plan=read(plan)
        inspector=getattr(normalbinding,'call_source_inspector',normalbinding.inspector)
        frozen=(normalbinding.resource_binding.snapshot(entry_ids={entry['id']})
                if normalbinding.resource_binding is not None else dict(requirements=[],bindings={}))
        if (plan.get('source_version')!=inspector.request.get('source_version')
                or plan.get('entry_digest')!=digest(entry)
                or plan.get('candidate_digest')!=digest(normalbinding.candidate)
                or plan.get('resource_digest')!=digest(frozen)):
            raise CallUnknown('SDK prefix has a different normal source/entry/candidate/resource binding')
        guards.append(dict(kind='NORMAL_SOURCE_ENTRY_CANDIDATE_RESOURCE_BINDING',status='CHECKED'))
        definitions=[]
        for row in rows:
            if row.get('schema') not in (RULE,FINDER):
                raise CallUnknown('SDK import target requirement has an unknown schema')
            definition=next((item for item in _DEFINITIONS if item==row.get('summary')),None)
            if definition is None or row.get('summary_digest')!=digest(definition) or row.get('execution_permission') is not False:
                raise CallUnknown('SDK import effect leaf is not the original internal semantic definition')
            _verify_site(row,inspector)
            if definition not in definitions:
                definitions.append(definition)
        roots=resolver_domain()
        guards.append(dict(kind='FINITE_NATIVE_RESOLVER_DOMAIN',status='CHECKED',paths=[str(root) for root in roots]))
        if sys.implementation.name!='cpython' or sys.version_info[:3]!=(3,10,22) or sys.platform!='linux':
            remaining.append('actual SDK native/import ABI differs from the finite CPython 3.10.22 Linux profile')
        for definition in definitions:
            name=definition['distribution']
            frontiers=definition['optional_frontiers']
            if frontiers:
                remaining.extend(frontiers)
                guards.append(dict(kind='ORIGINAL_COMPLETE_BUNDLE_AND_CODE_CACHE',distribution=name,
                    status='NOT_CHECKED',reason='named semantic frontier already prevents this original import region'))
                continue
            try:
                if any(key==name or key.startswith(name+'.') for key in sys.modules):
                    raise CallUnknown('preexisting '+name+' objects lack this original before-import provenance')
                bundle=_bundle(definition,roots)
                from seampact.source.sdk_code_identity import verify_original_module_caches
                cache=verify_original_module_caches(bundle['python_files'])
                guards.append(dict(kind='ORIGINAL_COMPLETE_BUNDLE_AND_CODE_CACHE',distribution=name,
                    status='CHECKED',member_count=bundle['member_count'],cache=cache))
                if name=='numpy':
                    package=Path(bundle['package'])
                    if any(path.name.startswith('_distributor_init_local.') or path.name=='_distributor_init_local'
                           for path in package.iterdir()):
                        raise CallUnknown('original NumPy vendor-local initialization branch is reachable')
                    guards.append(dict(kind='ORIGINAL_NUMPY_VENDOR_LOCAL_ABSENT',status='CHECKED'))
                if name=='torch':
                    if os.environ.get('TORCH_DEVICE_BACKEND_AUTOLOAD','1')=='1':
                        guards.append(dict(kind='ORIGINAL_TORCH_BACKEND_DOMAIN',**torch_backend_domain(roots)))
                    for key in ('TORCH_CUDA_SANITIZER','TORCH_COMPILE_STROBELIGHT','TORCH_LOGS','TORCH_LOGLEVEL'):
                        if os.environ.get(key):
                            remaining.append('original enabled '+key+' branch requires its separate finite effect leaf')
                if name=='transformers':
                    guards.append(dict(kind='ORIGINAL_TRANSFORMERS_CACHE_BRANCHES',**transformers_cache_domain()))
            except (CallUnknown,OSError,ValueError,TypeError,KeyError,SyntaxError) as error:
                remaining.append(str(error))
        operations=asset_operations(rows)
        if operations:
            if remaining:
                guards.append(dict(kind='ORIGINAL_NLTK_FIND_NORMAL_EXIT',status='NOT_CHECKED',
                    reason='earlier named import prerequisites remain unknown',resources=list(_RESOURCES)))
            else:
                from seampact.source.sdk_assets import verify_sdk_asset_plan
                if not verify_sdk_asset_plan(inspector,entry,normalbinding.candidate,frozen,operations,asset_plan):
                    remaining.append('original NLTK first-root/full-tree asset binding does not independently recheck')
                else:
                    guards.append(dict(kind='ORIGINAL_NLTK_FIND_NORMAL_EXIT',status='CHECKED',resources=list(_RESOURCES)))
    except (CallUnknown,OSError,ValueError,TypeError,KeyError,AttributeError) as error:
        remaining.append(str(error))
    return dict(schema=RULE,status='UNKNOWN' if remaining else 'CHECKED',allowed=not remaining,
        guards=guards,remaining=list(dict.fromkeys(remaining)),execution_permission=False,
        lifecycle_permission='NOT_GRANTED')
