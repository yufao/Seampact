"""Read-only target premises tied to an original controlled import occurrence.

No SDK is imported, constructed, called or warmed here. Distribution bytes are
only one premise. The normal import-only consumer must begin and finish the
same independently checked prefix while holding its original source lock.
Unknown cached objects and unsupported mutable protocols remain explicit.
"""
import atexit
import builtins
import enum
import functools
import gc
import hashlib
from importlib import metadata
import io
import math
import os
from pathlib import Path
import re
import stat
import sys
import threading
import time
from types import FunctionType, ModuleType, MemberDescriptorType,GetSetDescriptorType
import warnings
import weakref

from seampact.source.call_value_conditions import CallUnknown, builtin_state, digest, ordinary
from seampact.source.lifecycle_resources import sdk_identity
from seampact.source.sdk_native_kernels import capture_native_kernels,verify_read_kernels,verify_weak_membership


_TYPE, _DICT, _STR = type, dict, str
_MODULE_DICT = ModuleType.__dict__['__dict__']
_CLASS_DICT = type.__dict__['__dict__']
_LRU = functools._lru_cache_wrapper
_CACHE_INFO = _LRU.__dict__['cache_info']
_CACHE_PARAMETERS = _LRU.__dict__.get('cache_parameters')
_LRU_FACTORY=functools.lru_cache
_LRU_FACTORY_CODE=_LRU_FACTORY.__code__
_LRU_FACTORY_DEFAULTS=_LRU_FACTORY.__defaults__
_LRU_FACTORY_GLOBALS=_LRU_FACTORY.__globals__
_GC_REFERENTS=gc.get_referents
_CACHE_INFO_CLASS=functools._CacheInfo
_BUILTIN_NAMESPACE=ModuleType.__dict__['__dict__'].__get__(builtins,ModuleType)
_BUILTIN_ORIGINALS=dict(_BUILTIN_NAMESPACE)
_PATTERN = re.Pattern
_PENDING, _CONTEXTS = {}, weakref.WeakKeyDictionary()


def _original_class(value):
    if _TYPE(value) is type:
        return True
    from seampact.source.abc_kernel import abc_objects,check_abc_kernel
    if check_abc_kernel()['status']!='CHECKED':
        return False
    _,meta=abc_objects()
    if _TYPE(value) is not meta:
        return False
    native=_loaded('_abc').get('_get_dump')
    state=native(value)
    return _TYPE(state) is tuple and len(state)==4 and _TYPE(state[0]) is set and not state[0]


class _OriginalPrefix:
    __slots__ = ()


class RuntimeSDKContext:
    """An in-process identity handle; never native JSON or a candidate value."""
    __slots__ = ('__weakref__',)


def abort_original_import(token):
    """Forget an unfinished proof token without executing source/SDK cleanup."""
    if _TYPE(token) is not _OriginalPrefix:
        return False
    return _PENDING.pop(token,None) is not None


def forget_original_import(context):
    """Release internal evidence references only, preserving SDK lifetimes."""
    if _TYPE(context) is not RuntimeSDKContext:
        return False
    state=_CONTEXTS.pop(context,None)
    if state is None:
        return False
    from seampact.source.sdk_native_kernels import forget_native_kernels
    forget_native_kernels(state.get('native_kernels'))
    return True


def _namespace(module):
    if _TYPE(module) is not ModuleType:
        raise CallUnknown('actual SDK module is not an exact loaded module')
    namespace = _MODULE_DICT.__get__(module, ModuleType)
    if _TYPE(namespace) is not _DICT or any(_TYPE(key) is not _STR for key in namespace):
        raise CallUnknown('actual SDK module namespace has unknown key protocols')
    return namespace


def _loaded(name):
    if _TYPE(sys.modules) is not _DICT:
        raise CallUnknown('actual loaded-module kernel changed')
    return _namespace(sys.modules.get(name))


def _binding(binding):
    # Platform imports are already present in a normal worker. Looking them up
    # cannot execute an SDK, a source package or a new source loader.
    namespace = _loaded('seampact.units')
    if _TYPE(binding) is not namespace.get('NativeBindings'):
        raise CallUnknown('SDK provenance requires the original native binding')
    lock = namespace.get('_native_context_lock')
    if lock is None or not lock._is_owned():
        raise CallUnknown('SDK provenance requires the original locked read/use context')


def begin_original_import(binding, entry, unit, plan, instance_identity, resource_bindings=None, *, asset_plan=None):
    """Start after normal full reservation and before the original prefix runs."""
    _binding(binding)
    from seampact.source.import_only import verify_import_only_plan,_metadata
    resources = _metadata({} if resource_bindings is None else resource_bindings)
    frozen = (binding.resource_binding.snapshot(entry_ids={entry['id']}) if binding.resource_binding is not None
              else dict(requirements=[], bindings={}))
    base = getattr(binding,'call_source_inspector',binding.inspector)
    if (not verify_import_only_plan(base, entry, binding.candidate, frozen, plan)
            or plan.get('activation_prefix', {}).get('status') != 'CHECKED'):
        raise CallUnknown('complete original SDK/source prefix is not independently checked')
    assets = None if asset_plan is None else _metadata(asset_plan)
    if assets is not None:
        if (assets.get('schema') != 'source-sdk-asset-plan/1' or assets != plan.get('sdk_asset_plan')
                or assets.get('source_version') != plan.get('source_version')
                or assets.get('resource_digest') != plan.get('resource_digest')):
            raise CallUnknown('SDK asset plan is not frozen by the same normal source/resource import plan')
        resources = assets.get('bindings',{})
    token = _OriginalPrefix()
    from seampact.source.sdk_value_summaries import _definition
    from seampact.source.sdk_code_identity import verify_original_module_caches
    definitions=[]
    for requirement in (unit.get('call_condition') or {}).get('runtime_protocol_requirements',[]):
        definition=_definition(requirement.get('operation'))
        if definition is not None and definition not in definitions:
            definitions.append(definition)
    files={item['path']:item for definition in definitions for item in definition['sdk']}
    cache_state=verify_original_module_caches(list(files.values()))
    preexisting = {definition['module'] for definition in definitions if definition['module'] in sys.modules}
    _PENDING[token] = dict(binding=binding, entry=entry, unit=unit,
        plan_digest=plan['plan_digest'], source_version=plan['source_version'],
        instance_identity=_metadata(instance_identity), resources=resources, asset_plan=assets,
        preexisting=preexisting,original_module_caches=cache_state,definitions=definitions)
    return token


def _plain_snapshot(value, active=None, depth=0):
    if depth > 16:
        raise CallUnknown('SDK state snapshot exceeds finite native depth')
    kind = _TYPE(value)
    if value is None or kind in (str, int, bool, bytes):
        return (kind, value)
    if kind is object:
        return ('original-opaque-sentinel',id(value))
    if kind is type:
        return ('original-class-reference',value)
    if kind is enum.EnumMeta:
        return ('original-enum-class-reference',id(value))
    if kind is float and math.isfinite(value):
        return (kind, value)
    if kind in (tuple, list, dict, set, frozenset):
        if len(value) > 100000:
            raise CallUnknown('SDK state snapshot exceeds finite native cardinality')
        active = set() if active is None else active
        if id(value) in active:
            raise CallUnknown('SDK state contains a cyclic native container')
        active.add(id(value))
        try:
            if kind is dict:
                return (kind, tuple((_plain_snapshot(key, active, depth+1), _plain_snapshot(item, active, depth+1))
                                    for key,item in value.items()))
            items = tuple(_plain_snapshot(item, active, depth+1) for item in value)
            return (kind, frozenset(items) if kind in (set,frozenset) else items)
        finally:
            active.remove(id(value))
    raise CallUnknown('SDK mutable value has no native state snapshot protocol')


def _function_snapshot(function,active=None,depth=0):
    if _TYPE(function) is not FunctionType:
        raise CallUnknown('SDK callable is not an original Python function')
    if (_TYPE(function.__module__) is not str or _TYPE(function.__qualname__) is not str
            or any(_TYPE(name) is not str for name in function.__code__.co_names)):
        raise CallUnknown('SDK callable metadata/code-name protocol is nonordinary')
    if function.__builtins__ is not _BUILTIN_NAMESPACE:
        raise CallUnknown('SDK callable has a nonoriginal implicit builtin namespace')
    namespace = function.__globals__
    if _TYPE(namespace) is not _DICT or any(_TYPE(key) is not _STR for key in namespace):
        raise CallUnknown('SDK callable globals have unknown key protocols')
    defaults = _reference_state(function.__defaults__)
    kwdefaults = _reference_state(function.__kwdefaults__)
    def unsupported(value):
        if _TYPE(value) is tuple:
            return bool(value and _TYPE(value[0]) is str and value[0]=='unclosed-mutable-object') or any(unsupported(item) for item in value)
        if _TYPE(value) is frozenset:
            return any(unsupported(item) for item in value)
        return False
    if unsupported(defaults) or unsupported(kwdefaults):
        raise CallUnknown('SDK function defaults contain an unsupported mutable object protocol')
    if depth>16:
        raise CallUnknown('original SDK callable closure exceeds finite depth')
    active=set() if active is None else active
    if id(function) in active:
        raise CallUnknown('original SDK callable closure is cyclic')
    active.add(id(function))
    closed=[]
    try:
        for cell in function.__closure__ or ():
            value=cell.cell_contents
            closed.append((cell,value,_function_snapshot(value,active,depth+1) if _TYPE(value) is FunctionType
                           else _plain_snapshot(value)))
    finally:
        active.remove(id(function))
    # Exact references are retained from the checked original import, not
    # authenticated from a file name or a code hash observed after the fact.
    globals_ = tuple((name, namespace.get(name), _reference_state(namespace[name],function.__code__.co_names))
                     for name in function.__code__.co_names if name in namespace)
    return dict(function=function, code=function.__code__, defaults=defaults, kwdefaults=kwdefaults,
                globals=namespace, references=globals_,closure=closed,attributes=_reference_state(function.__dict__))


def _verify_function(snapshot):
    function = snapshot['function']
    current = _function_snapshot(function)
    if (current['code'] is not snapshot['code'] or current['globals'] is not snapshot['globals']
            or current['defaults'] != snapshot['defaults'] or current['kwdefaults'] != snapshot['kwdefaults']
            or current['attributes'] != snapshot['attributes']
            or len(current['references']) != len(snapshot['references'])
            or any(name != old_name or value is not old_value or state != old_state
                   for (name,value,state),(old_name,old_value,old_state) in zip(current['references'],snapshot['references']))):
        raise CallUnknown('original SDK code/default/global binding changed since the checked prefix')
    if len(current['closure'])!=len(snapshot['closure']):
        raise CallUnknown('original SDK callable closure changed')
    for (cell,value,state),(old_cell,old_value,old_state) in zip(current['closure'],snapshot['closure']):
        if cell is not old_cell or value is not old_value:
            raise CallUnknown('original SDK closure cell/object identity changed')
        if _TYPE(value) is FunctionType:
            _verify_function(old_state)
        elif state!=old_state:
            raise CallUnknown('original SDK closure native state changed')


def _reference_state(value,names=(),active=None,depth=0):
    if depth>16:
        raise CallUnknown('original SDK mutable state exceeds finite reference depth')
    active=set() if active is None else active
    if id(value) in active:
        return ('original-alias-reference',id(value))
    kind=_TYPE(value)
    if _TYPE(kind) is enum.EnumMeta:
        descriptor=None
        for cls in type.__dict__['__mro__'].__get__(kind,type):
            members=_CLASS_DICT.__get__(cls,type)
            if '__dict__' in members:
                descriptor=members['__dict__']
                break
        if _TYPE(descriptor) is not GetSetDescriptorType:
            raise CallUnknown('original SDK enum instance dictionary protocol is unsupported')
        data=descriptor.__get__(value,kind)
        active.add(id(value))
        try:
            return ('original-enum-value',id(kind),id(value),_reference_state(data,(),active,depth+1))
        finally:
            active.remove(id(value))
    if _TYPE(value) in (dict,list,tuple,set,frozenset):
        if len(value)>100000:
            raise CallUnknown('original SDK native mutable state exceeds finite cardinality')
        active.add(id(value))
        try:
            if _TYPE(value) is dict:
                items=tuple((_reference_state(key,(),active,depth+1),_reference_state(item,(),active,depth+1))
                            for key,item in value.items())
            else:
                items=tuple(_reference_state(item,(),active,depth+1) for item in value)
                if _TYPE(value) in (set,frozenset):
                    items=frozenset(items)
            return ('native-container',_TYPE(value),items)
        finally:
            active.remove(id(value))
    try:
        return ('native',_plain_snapshot(value))
    except CallUnknown:
        pass
    if _TYPE(value) is FunctionType:
        return ('function',id(value.__code__),_reference_state(value.__defaults__,(),active,depth+1),
                _reference_state(value.__kwdefaults__,(),active,depth+1))
    if _TYPE(value) is ModuleType:
        namespace=_namespace(value)
        selected=[]
        for name in names:
            if name in namespace:
                item=namespace[name]
                state=_reference_state(item,(),active,depth+1)
                selected.append((name,id(item),state))
        return ('module',id(namespace),tuple(selected))
    if _TYPE(kind) is type:
        members=_CLASS_DICT.__get__(kind,type)
        if members.get('__module__')=='nltk.redos' and members.get('__name__','TimedPattern')=='TimedPattern':
            rx,timeout=members.get('_rx'),members.get('_timeout')
            if _TYPE(rx) is MemberDescriptorType and _TYPE(timeout) is MemberDescriptorType:
                return ('original-timed-pattern',id(value),
                    _reference_state(rx.__get__(value,kind),(),active,depth+1),
                    _reference_state(timeout.__get__(value,kind),(),active,depth+1))
        if members.get('__module__')=='_regex':
            return ('original-native-regex',id(kind),id(value))
        if members.get('__module__') in {'regex._regex_core','regex._main','nltk.tokenize.punkt',
                                        'nltk.tabdata','nltk.data','nltk.pathsec'}:
            fields=[]
            active.add(id(value))
            try:
                for cls in type.__dict__['__mro__'].__get__(kind,type):
                    namespace=_CLASS_DICT.__get__(cls,type)
                    if any(_TYPE(name) is not str for name in namespace):
                        raise CallUnknown('SDK instance owner class has unknown namespace keys')
                    for name,descriptor in namespace.items():
                        if name=='__dict__':
                            if _TYPE(descriptor) is not GetSetDescriptorType:
                                raise CallUnknown('SDK instance dictionary descriptor is not native')
                            fields.append((name,_reference_state(descriptor.__get__(value,kind),(),active,depth+1)))
                        elif _TYPE(descriptor) is MemberDescriptorType:
                            try:
                                item=descriptor.__get__(value,kind)
                            except AttributeError:
                                fields.append((name,('UNBOUND_NATIVE_SLOT',)))
                            else:
                                fields.append((name,_reference_state(item,(),active,depth+1)))
                if fields:
                    return ('original-reviewed-sdk-state',id(kind),id(value),tuple(fields))
            finally:
                active.remove(id(value))
    return ('unclosed-mutable-object',id(value))


def _class_state(cls):
    members=_CLASS_DICT.__get__(cls,type)
    if any(_TYPE(name) is not str for name in members):
        raise CallUnknown('original SDK class namespace has unknown key protocols')
    return tuple((name,id(value),_reference_state(value)) for name,value in members.items())


def _lru_function(wrapper):
    if (_TYPE(wrapper) is not _LRU or functools.lru_cache is not _LRU_FACTORY
            or _LRU_FACTORY.__code__ is not _LRU_FACTORY_CODE
            or _LRU_FACTORY.__defaults__ is not _LRU_FACTORY_DEFAULTS
            or _LRU_FACTORY.__globals__ is not _LRU_FACTORY_GLOBALS
            or gc.get_referents is not _GC_REFERENTS):
        raise CallUnknown('original native LRU constructor/reference-read kernel changed')
    _verify_function(_LRU_FACTORY_STATE)
    _verify_function(_UPDATE_WRAPPER_STATE)
    if functools._CacheInfo is not _CACHE_INFO_CLASS or _class_state(_CACHE_INFO_CLASS)!=_CACHE_INFO_STATE:
        raise CallUnknown('original native LRU cache-info class/descriptor kernel changed')
    namespace=wrapper.__dict__
    if _TYPE(namespace) is not dict or any(_TYPE(key) is not str for key in namespace):
        raise CallUnknown('native LRU metadata has an unknown key protocol')
    actual=[item for item in _GC_REFERENTS(wrapper) if _TYPE(item) is FunctionType]
    if len(actual)!=1 or namespace.get('__wrapped__') is not actual[0]:
        raise CallUnknown('native LRU underlying original callable differs from exposed wrapper metadata')
    return actual[0]


def _capture(definition):
    namespace = _loaded(definition['module'])
    original = namespace.get(definition['symbol'])
    functions, classes = [], []
    if definition['methods']:
        if _TYPE(original) is not type:
            raise CallUnknown('original SDK class/metaclass was not created by the checked prefix')
        members = _CLASS_DICT.__get__(original, type)
        for name in definition['methods']:
            function = members.get(name)
            if _TYPE(function) is staticmethod:
                function = function.__func__
            functions.append(_function_snapshot(function))
        classes.append((original,_class_state(original)))
        # Complete finite reviewed progress implementation, including original
        # wrappers, formatting, monitor and write-lock helpers. Imported foreign
        # classes are not silently promoted to reviewed SDK definitions.
        for module_name in ('tqdm.std','tqdm.utils','tqdm._monitor'):
            module=_loaded(module_name)
            for value in module.values():
                if _TYPE(value) is FunctionType and _TYPE(value.__module__) is str and value.__module__==module_name:
                    functions.append(_function_snapshot(value))
                elif _TYPE(value) is type:
                    members=_CLASS_DICT.__get__(value,type)
                    if _TYPE(members.get('__module__')) is not str or members['__module__']!=module_name:
                        continue
                    classes.append((value,_class_state(value)))
                    for member in members.values():
                        methods=(member.fget,member.fset,member.fdel) if _TYPE(member) is property else (
                            member.__func__,) if _TYPE(member) in (staticmethod,classmethod) else (member,)
                        for function in methods:
                            if _TYPE(function) is FunctionType:
                                functions.append(_function_snapshot(function))
    else:
        functions.append(_function_snapshot(original))
        factory = namespace.get('_get_punkt_tokenizer')
        if _TYPE(factory) is not _LRU:
            raise CallUnknown('original tokenizer cache is not the native original functools wrapper')
        original_factory=_lru_function(factory)
        info = _CACHE_INFO(factory)
        if info.currsize:
            raise CallUnknown('first-observed warm tokenizer cache has no original creation provenance')
        functions.append(_function_snapshot(original_factory))
        punkt = _loaded('nltk.tokenize.punkt')
        for name in ('PunktTokenizer','PunktSentenceTokenizer','PunktBaseClass','PunktLanguageVars','PunktToken','PunktParameters'):
            cls = punkt.get(name)
            if not _original_class(cls):
                raise CallUnknown('original tokenizer class/metaclass is unavailable')
            members = _CLASS_DICT.__get__(cls,type)
            selected = []
            for key,value in members.items():
                function = value.fget if _TYPE(value) is property else value.__func__ if _TYPE(value) in (staticmethod,classmethod) else value
                if _TYPE(function) is FunctionType:
                    functions.append(_function_snapshot(function))
                    selected.append((key,value))
            classes.append((cls,_class_state(cls)))
        redos=_loaded('nltk.redos')
        wrapper=redos.get('TimedPattern')
        if _TYPE(wrapper) is not type:
            raise CallUnknown('original TimedPattern class/metaclass is unavailable')
        for value in redos.values():
            if _TYPE(value) is FunctionType and _TYPE(value.__module__) is str and value.__module__=='nltk.redos':
                functions.append(_function_snapshot(value))
        members=_CLASS_DICT.__get__(wrapper,type)
        classes.append((wrapper,_class_state(wrapper)))
        for value in members.values():
            if _TYPE(value) is FunctionType:
                functions.append(_function_snapshot(value))
        # Exact finite helper closure of the reviewed native-directory,
        # English/default-Punkt and regex branches. Methods not selected by the
        # original call still retain their original definitions; no code runs.
        helper_modules={
            'nltk.tokenize.punkt':{'_pair_iter','_punkt_language','_open_model_file','load_punkt_params'},
            'nltk.tabdata':{'rm_nl'},
            'nltk.data':{'normalize_resource_name','find','_assert_no_encoded_bypass','_has_line_boundary',
                         '_normalized_path_escapes','_assert_no_normalized_bypass','_refuse_file_not_ours',
                         '_first_entry_not_ours','text_encoding'},
            'nltk.pathsec':None,'regex._main':None,'regex._regex_core':None,
        }
        helper_classes={
            'nltk.tabdata':{'TabDecoder','PunktDecoder'},'nltk.tokenize.api':{'TokenizerI'},
            'nltk.data':{'PathPointer','FileSystemPathPointer','SeekableUnicodeStreamReader'},
            'regex._main':None,'regex._regex_core':None,
        }
        for module_name,names in helper_modules.items():
            module=_loaded(module_name)
            for name,value in module.items():
                function=_lru_function(value) if _TYPE(value) is _LRU else value
                if (_TYPE(function) is FunctionType and _TYPE(function.__module__) is str and function.__module__==module_name
                        and (names is None or name in names)):
                    functions.append(_function_snapshot(function))
        for module_name,names in helper_classes.items():
            module=_loaded(module_name)
            for name,cls in module.items():
                if names is not None and name not in names:
                    continue
                if _TYPE(cls) not in (type,enum.EnumMeta) and not _original_class(cls):
                    continue
                values=_CLASS_DICT.__get__(cls,type)
                if _TYPE(values.get('__module__')) is not str or values['__module__']!=module_name or names is not None and name not in names:
                    continue
                classes.append((cls,_class_state(cls)))
                for member in values.values():
                    methods=(member.fget,member.fset,member.fdel) if _TYPE(member) is property else (
                        member.__func__,) if _TYPE(member) in (staticmethod,classmethod) else (member,)
                    for function in methods:
                        if _TYPE(function) is FunctionType:
                            functions.append(_function_snapshot(function))
    from seampact.source.sdk_code_identity import verify_function_source
    compiled={}
    for function in functions:
        actual=function['function']
        if actual.__module__=='enum':
            originals=[] if _ENUM_BOOTSTRAP_ERROR is not None else [item for item in _ENUM_BOOTSTRAP[1] if item['function'] is actual]
            if not originals:
                raise CallUnknown('SDK class uses an enum helper outside the original finite flag kernel')
            _verify_function(originals[0])
        else:
            verify_function_source(actual,definition['sdk'],compiled=compiled)
    result=dict(original=original, namespace=namespace, functions=functions, classes=classes)
    if not definition['methods']:
        result['regex_state']=_capture_redos(classes,definition)
    return result


def _pattern_state(pattern,wrapper,redos):
    if _TYPE(pattern) is not wrapper:
        raise CallUnknown('Punkt original regex is not an exact TimedPattern object')
    members=_CLASS_DICT.__get__(wrapper,type)
    rx_descriptor,timeout_descriptor=members.get('_rx'),members.get('_timeout')
    if _TYPE(rx_descriptor) is not MemberDescriptorType or _TYPE(timeout_descriptor) is not MemberDescriptorType:
        raise CallUnknown('original TimedPattern native slot descriptors changed')
    rx=rx_descriptor.__get__(pattern,wrapper)
    timeout=timeout_descriptor.__get__(pattern,wrapper)
    engine=_loaded('regex')
    native=engine.get('Pattern')
    if _TYPE(native) is not type or _TYPE(rx) is not native:
        raise CallUnknown('original TimedPattern engine is not the exact native regex Pattern')
    if timeout is not redos.get('_UNSET'):
        raise CallUnknown('original Punkt regex timeout no longer uses its original default sentinel')
    # The exact native pattern type has immutable engine descriptors; reading
    # these fields does not run a match, compile, wrapper or callback.
    source,flags=rx.pattern,rx.flags
    if _TYPE(source) is not str or _TYPE(flags) is not int:
        raise CallUnknown('original native regex source/flags are not exact ordinary values')
    return (pattern,rx,timeout,source,flags)


def _capture_redos(classes,definition):
    redos=_loaded('nltk.redos')
    wrapper=redos.get('TimedPattern')
    if _TYPE(wrapper) is not type or _TYPE(redos.get('DEFAULT_TIMEOUT')) is not float or redos['DEFAULT_TIMEOUT']!=5.0:
        raise CallUnknown('original redos class/default timeout is not the reviewed finite semantics')
    native_files=[item for item in definition['sdk'] if item['distribution']=='regex' and not item['path'].endswith('.py')]
    if not native_files:
        raise CallUnknown('original third-party regex distribution/native ABI identity is not yet reviewed')
    native_module=_loaded('regex._regex')
    native_origin=native_module.get('__file__')
    if _TYPE(native_origin) is not str:
        raise CallUnknown('original regex native extension has no ordinary loaded origin')
    paths=[]
    for wanted in native_files:
        distribution=metadata.distribution(wanted['distribution'])
        matches=[path for path in distribution.files or () if str(path).replace('\\','/')==wanted['path']]
        if len(matches)!=1:
            raise CallUnknown('original regex native ABI has no unique installed file identity')
        paths.append(matches[0].locate())
    if Path(native_origin) not in paths:
        raise CallUnknown('loaded regex engine origin differs from its reviewed native ABI file')
    patterns=[]
    for cls,_ in classes:
        for value in _CLASS_DICT.__get__(cls,type).values():
            if _TYPE(value) is wrapper:
                patterns.append(_pattern_state(value,wrapper,redos))
    if not patterns:
        raise CallUnknown('original Punkt class regex creation objects are absent')
    return dict(module=redos,wrapper=wrapper,timeout=redos['DEFAULT_TIMEOUT'],sentinel=redos.get('_UNSET'),
                patterns=patterns,native_files=native_files,native_module=native_module,native_origin=native_origin,
                public=_loaded('regex'),main=_loaded('regex._main'),core=_loaded('regex._regex_core'),
                defaults=tuple(_reference_state(_loaded(name).get('DEFAULT_VERSION'))
                               for name in ('regex','regex._main','regex._regex_core')))


def _verify_redos(target):
    state=target['regex_state']
    redos=_loaded('nltk.redos')
    if (redos is not state['module'] or redos.get('TimedPattern') is not state['wrapper']
            or _TYPE(redos.get('DEFAULT_TIMEOUT')) is not float or redos['DEFAULT_TIMEOUT']!=state['timeout']
            or redos.get('_UNSET') is not state['sentinel']
            or sdk_identity(state['native_files'])['status']!='CHECKED'):
        raise CallUnknown('original redos timeout/native engine identity changed')
    if (_loaded('regex._regex') is not state['native_module']
            or state['native_module'].get('__file__')!=state['native_origin']
            or _loaded('regex') is not state['public'] or _loaded('regex._main') is not state['main']
            or _loaded('regex._regex_core') is not state['core']
            or tuple(_reference_state(_loaded(name).get('DEFAULT_VERSION'))
                     for name in ('regex','regex._main','regex._regex_core'))!=state['defaults']):
        raise CallUnknown('original native regex module/origin/default flag state changed')
    for original in state['patterns']:
        current=_pattern_state(original[0],state['wrapper'],redos)
        if any(current[index] is not original[index] for index in (0,1,2)) or current[3:]!=original[3:]:
            raise CallUnknown('original TimedPattern slot/engine/source/flags state changed')
    _verify_enum_kernel()
    return dict(status='CHECKED',effects=['original regex native matching under original 5-second timeout',
                'original lazy finditer timeout and exceptions retained'],native_pattern_objects=len(state['patterns']))


def _enum_kernel_state():
    classes,functions=[],[]
    for cls in (enum.EnumMeta,enum.Enum,enum.Flag,enum.IntFlag):
        classes.append((cls,_class_state(cls)))
        for name,value in _CLASS_DICT.__get__(cls,type).items():
            # Dynamic Enum factories read the evolving sys.modules world and
            # are not called by the reviewed fixed flag-class operations.
            # Their method identity/code remains in the full class snapshot.
            if name in ('_convert_','_create_'):
                continue
            values=(value.fget,value.fset,value.fdel) if _TYPE(value) is property else (
                value.__func__,) if _TYPE(value) in (classmethod,staticmethod) else (value,)
            for function in values:
                if _TYPE(function) is FunctionType:
                    functions.append(_function_snapshot(function))
    return classes,functions


def _verify_enum_kernel():
    if _ENUM_BOOTSTRAP_ERROR is not None:
        raise CallUnknown('original finite enum flag kernel bootstrap is unavailable: '+_ENUM_BOOTSTRAP_ERROR)
    for cls,state in _ENUM_BOOTSTRAP[0]:
        if _class_state(cls)!=state:
            raise CallUnknown('original stdlib enum class/metaclass flag protocol changed')
    for function in _ENUM_BOOTSTRAP[1]:
        _verify_function(function)


def finish_original_import(token):
    """Capture only the freshly completed same original prefix, never a warm SDK."""
    if _TYPE(token) is not _OriginalPrefix or token not in _PENDING:
        raise CallUnknown('SDK prefix provenance token is absent or already consumed')
    pending = _PENDING.pop(token)
    binding = pending['binding']
    _binding(binding)
    unit, entry = pending['unit'],pending['entry']
    receipt = binding.import_only_entries.get(unit['id'])
    if (receipt is None or receipt.get('plan_digest') != pending['plan_digest']
            or receipt.get('instance_identity') != pending['instance_identity']):
        raise CallUnknown('SDK original import occurrence or worker instance differs')
    binding._verify_import_only_target(entry,receipt)
    targets = {}
    for definition in pending['definitions']:
        try:
            if definition['module'] in pending['preexisting']:
                raise CallUnknown('SDK module already existed before this independent original import occurrence')
            if sdk_identity(definition['sdk']).get('status') != 'CHECKED':
                raise CallUnknown('actual installed SDK identity differs from the semantic summary')
            targets[definition['id']] = _capture(definition)
        except (CallUnknown, KeyError, TypeError, ValueError) as error:
            targets[definition['id']] = dict(reason=str(error))
    context = RuntimeSDKContext()
    try:
        kernels=capture_native_kernels()
    except CallUnknown:
        kernels=None
    pending['binding']=weakref.ref(binding)
    _CONTEXTS[context] = dict(**pending,targets=targets,native_kernels=kernels)
    return context


def _resources(binding):
    """Validate the selected first effective ordinary English model tree."""
    from seampact.source.import_only import _metadata
    binding = _metadata(binding)
    if _TYPE(binding) is not _DICT or binding.get('kind') != 'directory':
        raise CallUnknown('normal frozen NLTK directory resource declaration is absent')
    root = binding.get('path')
    wanted = binding.get('required_files')
    if _TYPE(root) is not str or _TYPE(wanted) is not list or not wanted:
        raise CallUnknown('normal NLTK content manifest is incomplete')
    paths = _loaded('nltk.data').get('path')
    if _TYPE(paths) is not list or not paths or any(_TYPE(item) is not str for item in paths):
        raise CallUnknown('actual original NLTK path list is not an ordinary finite root list')
    selected = Path(root)
    if not selected.is_absolute():
        raise CallUnknown('selected NLTK resource root is not an absolute normal binding')
    matches = [index for index,item in enumerate(paths) if selected == Path(item)]
    if len(matches) != 1:
        raise CallUnknown('selected NLTK resource has no unique original effective root')
    for earlier in paths[:matches[0]]:
        base = Path(earlier)
        # Preserve original directory and zip fallback resolution. An earlier
        # possible directory/zip hit needs its own reviewed read protocol.
        candidates = (base/'tokenizers/punkt_tab/english',base/'tokenizers.zip',
                      base/'tokenizers/punkt_tab.zip',base/'tokenizers/punkt_tab/english.zip')
        if base.suffix == '.zip' and base.exists() or any(os.path.lexists(path) for path in candidates):
            raise CallUnknown('an earlier original NLTK lookup root can change first-hit resolution')
    for component in [selected,*selected.parents]:
        details = component.lstat()
        if not stat.S_ISDIR(details.st_mode) or stat.S_ISLNK(details.st_mode):
            raise CallUnknown('NLTK resource root/parent has a nonordinary path protocol')
    from seampact.source.sdk_assets import _tree
    full = binding.get('full_tree_manifest')
    if _TYPE(full) is not list or binding.get('manifest_digest') != digest(full):
        raise CallUnknown('normal complete NLTK owner/mode/content manifest is absent')
    root_details=selected.lstat()
    identity=dict(mode=stat.S_IMODE(root_details.st_mode),owner=getattr(root_details,'st_uid',None))
    if binding.get('root_identity') != identity:
        raise CallUnknown('actual NLTK root owner/mode differs from its frozen binding')
    if os.name=='posix' and (root_details.st_mode&0o022 or root_details.st_uid not in (0,os.getuid())):
        raise CallUnknown('NLTK root owner/mode permits an unaccounted writer')
    actual_tree=_tree(selected)
    if actual_tree != full:
        raise CallUnknown('actual complete NLTK file/directory owner/mode/bytes/content manifest changed')
    checked = {}
    for item in wanted:
        if (_TYPE(item) is not _DICT or _TYPE(item.get('path')) is not str
                or _TYPE(item.get('sha256')) is not str):
            raise CallUnknown('NLTK content manifest row is not native and frozen')
        relative = Path(item['path'])
        if relative.is_absolute() or '..' in relative.parts:
            raise CallUnknown('NLTK resource manifest escapes its declared root')
        checked[relative.as_posix()] = item['sha256']
    required = {'tokenizers/punkt_tab/english/'+name for name in
                ('collocations.tab','sent_starters.txt','abbrev_types.txt','ortho_context.tab')}
    if not required <= checked.keys():
        raise CallUnknown('the original four English Punkt files are not declared in the selected tree')
    actual = {row['path']:row['sha256'] for row in actual_tree if row['kind']=='file'}
    if actual != checked:
        raise CallUnknown('the complete actual NLTK resource tree differs from its frozen manifest')
    return dict(status='CHECKED',files=len(actual),manifest_digest=digest(sorted(actual.items())))


def _nltk_guards(context, target):
    namespace = target['namespace']
    factory = namespace.get('_get_punkt_tokenizer')
    _lru_function(factory)
    if _CACHE_INFO(factory).currsize:
        raise CallUnknown('warm cached tokenizer objects require original continuous object provenance')
    pathsec=_loaded('nltk.pathsec')
    if pathsec.get('_ALLOWED_ROOTS_CACHE') is not None or pathsec.get('_LAST_DATA_PATHS') is not None:
        raise CallUnknown('first-observed warm reader-root cache requires original continuous path-object provenance')
    punkt = _loaded('nltk.tokenize.punkt')
    cls = punkt.get('PunktLanguageVars')
    if _TYPE(cls) is not type:
        raise CallUnknown('actual Punkt language class/metaclass is unsupported')
    members = _CLASS_DICT.__get__(cls,type)
    ending = members.get('sent_end_chars')
    if _TYPE(ending) is not tuple or any(_TYPE(item) is not str for item in ending) or ending != ('.','?','!'):
        raise CallUnknown('actual original candidate sentence-ending characters changed')
    from seampact.source.sdk_assets import original_nltk_paths
    paths,semantics=original_nltk_paths()
    assets=context.get('asset_plan')
    if assets is None or assets.get('path_semantics') != dict(**semantics,original_effective_paths=paths):
        raise CallUnknown('original current NLTK path/environment semantics differ from the normal asset plan')
    actual_paths=_loaded('nltk.data').get('path')
    if _TYPE(actual_paths) is not list or any(_TYPE(value) is not str for value in actual_paths) or actual_paths != paths:
        raise CallUnknown('actual loaded NLTK path state differs from original unchanged environment construction')
    decoder=verify_read_kernels(context.get('native_kernels'))
    if decoder['status']!='CHECKED':
        raise CallUnknown('original source reader/decoder prerequisites: '+'; '.join(decoder['remaining']))
    return dict(empty_original_cache=dict(status='CHECKED'),resource_tree=_resources(context['resources'].get('nltk')),
                regexp=_verify_redos(target),reader_decoder=decoder)


def _tqdm_guards(context,target):
    namespace = target['namespace']
    cls = target['original']
    members = _CLASS_DICT.__get__(cls,type)
    if members.get('monitor') is not None:
        raise CallUnknown('existing progress monitor requires maintained original creator/thread provenance')
    instances = members.get('_instances')
    weak_members = _CLASS_DICT.__get__(weakref.WeakSet,type)
    if (_TYPE(instances) is not weakref.WeakSet
            or any(name in weak_members for name in ('data','__getattribute__','__getattr__'))):
        raise CallUnknown('progress instance weak-set access protocol changed')
    data = object.__getattribute__(instances,'data')
    if _TYPE(data) is not set or len(data):
        raise CallUnknown('progress instance membership is not the original empty native weak set')
    verify_weak_membership(instances)
    if _TYPE(sys.stderr) is not io.TextIOWrapper:
        raise CallUnknown('actual original progress stream is not the supported native text stream')
    monitor = _loaded('tqdm._monitor')
    originals = dict(Thread=threading.Thread,Event=threading.Event,current_thread=threading.current_thread,
                     time=time.time,warn=warnings.warn,atexit=atexit)
    if any(monitor.get(name) is not value for name,value in originals.items()) or namespace.get('time') is not time.time:
        raise CallUnknown('original monitor thread/event/clock/warning/atexit kernel binding changed')
    interval = members.get('monitor_interval')
    if _TYPE(interval) is not int or interval != 10:
        raise CallUnknown('progress monitor interval is not the original unchanged configuration')
    write_lock=namespace.get('TqdmDefaultWriteLock')
    native_thread=_loaded('_thread').get('RLock')
    if _TYPE(write_lock) is not type or _TYPE(native_thread) is not type:
        raise CallUnknown('original progress lock class/native type is unavailable')
    lock_fields=_CLASS_DICT.__get__(write_lock,type)
    if _TYPE(lock_fields.get('th_lock')) is not native_thread:
        raise CallUnknown('original progress class lock is not the supported native recursive lock')
    from seampact.source.sdk_native_kernels import verify_native_kernels
    kernels=verify_native_kernels(context.get('native_kernels'))
    if kernels['status']!='CHECKED':
        raise CallUnknown('original progress native-effect prerequisites: '+'; '.join(kernels['remaining']))
    return dict(native_effect_region=kernels,original_monitor_state=dict(status='CHECKED'),
                retained_effects=['original lock/semaphore/thread creation','original stream/ioctl/environment effects',
                                  'original normal/interruption/exception cleanup and close'])


def verify_actual_target(definition, requirement, context=None):
    """Consume a trusted summary and a live original-prefix identity context."""
    guards, remaining = {}, []
    try:
        from seampact.source.sdk_value_summaries import _definition, RULE
        from seampact.source.import_only import _metadata
        definition,requirement = _metadata(definition),_metadata(requirement)
        trusted = _definition(requirement.get('operation')) if _TYPE(requirement) is _DICT else None
        if (trusted is None or definition != trusted or requirement.get('schema') != RULE
                or requirement.get('summary') != trusted or requirement.get('summary_digest') != digest(trusted)
                or requirement.get('input_status') != 'CHECKED' or requirement.get('execution_permission') is not False):
            raise CallUnknown('actual target has no exact trusted source summary/input requirement')
        if _TYPE(context) is not RuntimeSDKContext or context not in _CONTEXTS:
            raise CallUnknown('independently checked original SDK import occurrence is not available')
        state = _CONTEXTS[context]
        binding=state['binding']()
        if binding is None:
            raise CallUnknown('original SDK source binding is no longer retained by its normal owner')
        _binding(binding)
        if requirement.get('source_version') != state['source_version']:
            raise CallUnknown('source SDK requirement belongs to another frozen version')
        target = state['targets'].get(definition['id'],{})
        if 'reason' in target:
            raise CallUnknown(target['reason'])
        if not builtin_state() or sdk_identity(definition['sdk']).get('status') != 'CHECKED':
            raise CallUnknown('actual original native kernel or installed SDK identity changed')
        if (any(_TYPE(name) is not str for name in _BUILTIN_NAMESPACE)
                or len(_BUILTIN_NAMESPACE)!=len(_BUILTIN_ORIGINALS)
                or any(name not in _BUILTIN_NAMESPACE or _BUILTIN_NAMESPACE[name] is not value
                       for name,value in _BUILTIN_ORIGINALS.items())):
            raise CallUnknown('original SDK implicit builtin operation namespace changed')
        if _loaded(definition['module']) is not target['namespace'] or target['namespace'].get(definition['symbol']) is not target['original']:
            raise CallUnknown('actual original SDK object/module identity changed')
        operation=requirement['operation']
        if operation in ('nltk.sent_tokenize','tqdm.tqdm'):
            top,symbol=operation.split('.')
            if _loaded(top).get(symbol) is not target['original']:
                raise CallUnknown('actual original SDK top-level exported operation alias changed')
        for function in target['functions']:
            _verify_function(function)
        for cls,members in target['classes']:
            if _TYPE(cls) is not enum.EnumMeta and not _original_class(cls):
                raise CallUnknown('actual original SDK class/metaclass or virtual registry changed')
            if _class_state(cls) != members:
                raise CallUnknown('actual original SDK class/method/descriptor binding changed')
        guards.update(original_import_occurrence=dict(status='CHECKED'),
                      code_defaults_globals_and_classes=dict(status='CHECKED'))
        guards.update(_nltk_guards(state,target) if not definition['methods'] else _tqdm_guards(state,target))
        # A direct global reference snapshot catches rebinding and native
        # container mutations. It does not establish all dynamically imported
        # helpers, wrapper internals or transitive mutable module reads.
    except (CallUnknown,KeyError,TypeError,ValueError,OSError) as error:
        remaining.append(str(error))
    return dict(schema='actual-original-sdk-target/1',status='UNKNOWN' if remaining else 'CHECKED',
                allowed=not remaining,guards=guards,remaining=remaining,
                lifecycle_permission='NOT_GRANTED')


# As with the core ABC kernel, this platform bootstrap runs before any normal
# source prefix. No inspected SDK is imported or evaluated by this capture.
try:
    _ENUM_BOOTSTRAP=_enum_kernel_state()
    _ENUM_BOOTSTRAP_ERROR=None
except (CallUnknown,ValueError,TypeError) as error:
    _ENUM_BOOTSTRAP=None
    _ENUM_BOOTSTRAP_ERROR=str(error)
_LRU_FACTORY_STATE=_function_snapshot(_LRU_FACTORY)
_UPDATE_WRAPPER_STATE=_function_snapshot(functools.update_wrapper)
_CACHE_INFO_STATE=_class_state(_CACHE_INFO_CLASS)
