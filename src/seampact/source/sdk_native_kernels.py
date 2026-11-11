"""Read-only original stdlib/native premises for finite progress effects.

Native lock, semaphore and thread creation are retained original effects, not
absence-of-work claims. This module never imports a missing kernel to obtain a
premise, constructs synchronization objects, starts threads or probes ioctl.
"""
import atexit
import builtins
import codecs
import encodings.utf_8
import io
import hashlib
import os
from pathlib import Path
import platform
import sys
import sysconfig
import threading
import time
import weakref
from types import BuiltinFunctionType, FunctionType, ModuleType

from seampact.source.call_value_conditions import CallUnknown

_TYPE,_DICT,_STR=type,dict,str
_MODULE_DICT=ModuleType.__dict__['__dict__']
_CLASS_DICT=type.__dict__['__dict__']
_CONTEXTS=weakref.WeakKeyDictionary()
_THREADING=threading
_THREAD_METHODS={
    threading.Thread:('__init__','start','join','_bootstrap','_bootstrap_inner','_stop','is_alive'),
    threading.Event:('__init__','set','wait','is_set'),
    threading.Condition:('__init__','wait','notify','notify_all'),
}
_ATEXIT_REGISTER=atexit.register
_CLOCKS=(time.time,time.monotonic)
_RLOCK_FACTORY=threading.RLock
_RLOCK_NATIVE=getattr(threading,'_CRLock',None)
_NATIVE_OS=(os.fstat,os.stat,os.getpid)
_BUILTINS=ModuleType.__dict__['__dict__'].__get__(builtins,ModuleType)
_ENVIRON_TYPE=type(os.environ)
_ENVIRON_OBJECT=os.environ
_ENVIRON_METHODS={name:getattr(_ENVIRON_TYPE,name) for name in ('__getitem__','__iter__','__len__','get')}
_ENVIRON_CONVERTERS={name:object.__getattribute__(os.environ,'__dict__').get(name)
                     for name in ('encodekey','decodekey','encodevalue','decodevalue')}
_MODULE_MEMBERS={
    '_thread':('start_new_thread','allocate_lock','LockType','RLock','get_ident'),
    'array':('array',),'fcntl':('ioctl',),'termios':('TIOCGWINSZ',),
    'multiprocessing':('RLock','get_context','set_start_method'),
    'multiprocessing.context':('_default_context','BaseContext','DefaultContext','_concrete_contexts'),
    'multiprocessing.synchronize':('RLock','SemLock','sem_unlink','_multiprocessing'),
    '_multiprocessing':('SemLock','sem_unlink'),
    'multiprocessing.process':('current_process','_current_process'),
    'multiprocessing.util':('debug','_logger','register_after_fork','_afterfork_registry','_afterfork_counter'),
    'tempfile':('_RandomNameSequence','_Random'),
}
_STDLIB_SOURCE={
    'threading':('threading.py','a21926e636bec8c2e5579f3e79cce144c36379ef93c4596252af29970983553d'),
    'multiprocessing.context':('multiprocessing/context.py','bd8628d1e140d933d40ed14b51009820c1b0b9b5b6c71971df5fc761da65bd08'),
    'multiprocessing.synchronize':('multiprocessing/synchronize.py','c357514f2359baf9570adcce2a6818a2456477019eaa138f26f8fcc2b6f467b0'),
    'multiprocessing.util':('multiprocessing/util.py','027d6642308f741f94d44fc79c23e4d1a1c275159823720ccb05b0a8e705450a'),
    'multiprocessing.process':('multiprocessing/process.py','9a5f6ca49fb78452678bed74f9b09491c688571f9f09edb97758933294356513'),
    'tempfile':('tempfile.py','676356b31756112053515fc1d550a99faf0f4bbaff6ccd4cc8a56310474f5c3a'),
}
_SEMLOCK_NATIVE='eb7b8de20e6fb7321b1802d30e00c0870e2013d0d547593b36d24ccf9ebed9ba'
_READ_ORIGINALS={name:getattr(os,name) for name in ('open','close','fdopen','fstat','stat','lstat','read','lseek')}
_READ_OPEN=builtins.open
_CODEC_LOOKUP=codecs.lookup
_UTF8_INFO=_CODEC_LOOKUP('utf-8')
_CODEC_METHODS={name:getattr(codecs,name) for name in ('getdecoder','getincrementaldecoder','lookup','register','unregister')
                if hasattr(codecs,name)}
_UTF8_MODULE=encodings.utf_8
_UTF8_MEMBERS={name:getattr(_UTF8_MODULE,name) for name in ('decode','encode','IncrementalDecoder','IncrementalEncoder','getregentry')}
_REMOVE_DEAD_WEAKREF=weakref._remove_dead_weakref


class NativeKernelContext:
    __slots__=('__weakref__',)


def forget_native_kernels(context):
    """Drop only this proof handle; never close/join/reclaim a native object."""
    if _TYPE(context) is not NativeKernelContext:
        return False
    return _CONTEXTS.pop(context,None) is not None


def _namespace(module):
    if _TYPE(module) is not ModuleType:
        raise CallUnknown('original stdlib/native module is not already loaded')
    result=_MODULE_DICT.__get__(module,ModuleType)
    if _TYPE(result) is not _DICT or any(_TYPE(key) is not _STR for key in result):
        raise CallUnknown('original kernel module has unknown key protocols')
    return result


def _loaded(name):
    if _TYPE(sys.modules) is not _DICT:
        raise CallUnknown('loaded-module discovery kernel is not native')
    return _namespace(sys.modules.get(name))


def _function(function, *, closure_classes=()):
    if _TYPE(function) is not FunctionType:
        raise CallUnknown('original kernel helper is not an exact Python function')
    if function.__builtins__ is not _BUILTINS or any(_TYPE(name) is not str for name in function.__code__.co_names):
        raise CallUnknown('original kernel helper implicit builtin/name protocol changed')
    closure=[]
    for cell in function.__closure__ or ():
        value=cell.cell_contents
        if (value is not None and _TYPE(value) not in (type,FunctionType,BuiltinFunctionType,int,float,str,bool)
                and not any(value is class_ for class_ in closure_classes)):
            raise CallUnknown('kernel helper has an unreviewed closure protocol')
        closure.append((cell,value,value.__code__ if _TYPE(value) is FunctionType else None))
    namespace=function.__globals__
    if _TYPE(namespace) is not _DICT or any(_TYPE(key) is not _STR for key in namespace):
        raise CallUnknown('kernel helper globals have unknown key protocols')
    refs=[]
    for name in function.__code__.co_names:
        if name not in namespace:
            continue
        value=namespace[name]
        attrs=()
        if _TYPE(value) is ModuleType:
            module=_namespace(value)
            attrs=tuple((key,module[key]) for key in function.__code__.co_names if key in module)
        refs.append((name,value,attrs))
    refs=tuple(refs)
    return function,function.__code__,function.__defaults__,function.__kwdefaults__,namespace,refs,tuple(closure)


def _same_function(state):
    function,code,defaults,kwdefaults,namespace,refs,closure=state
    if (function.__code__ is not code or function.__defaults__ is not defaults
            or function.__kwdefaults__ is not kwdefaults or function.__globals__ is not namespace
            or any(name not in namespace or namespace[name] is not value
                   or attrs and any(_namespace(value).get(key) is not original for key,original in attrs)
                   for name,value,attrs in refs)):
        raise CallUnknown('original stdlib code/default/global binding changed')
    actual=function.__closure__ or ()
    if len(actual)!=len(closure) or any(cell is not original or cell.cell_contents is not value
            or _TYPE(value) is FunctionType and value.__code__ is not code
            for cell,(original,value,code) in zip(actual,closure)):
        raise CallUnknown('original stdlib closure object/code binding changed')


def capture_native_kernels():
    """Observe existing native roots at the checked prefix boundary only."""
    functions,classes=[],[]
    if threading.RLock is not _RLOCK_FACTORY or getattr(threading,'_CRLock',None) is not _RLOCK_NATIVE:
        raise CallUnknown('original native thread-lock factory changed before SDK prefix completion')
    if _RLOCK_STATE is not None:
        _same_function(_RLOCK_STATE)
        functions.append(_RLOCK_STATE)
    for original in _THREAD_ALL_EARLY:
        _same_function(original)
        functions.append(original)
    for cls,names in _THREAD_METHODS.items():
        members=_CLASS_DICT.__get__(cls,type)
        if any(_TYPE(key) is not str for key in members):
            raise CallUnknown('thread kernel class namespace has unknown keys')
        selected=[]
        for name in names:
            function=members.get(name)
            original=_THREAD_EARLY[(cls,name)]
            if function is not original[0]:
                raise CallUnknown('original thread method changed before checked SDK import completion')
            _same_function(original)
            functions.append(original)
            selected.append((name,function))
        classes.append((cls,tuple(selected)))
    modules={}
    for name in _MODULE_MEMBERS:
        if name in sys.modules:
            namespace=_namespace(sys.modules[name])
            members=tuple((key,namespace[key]) for key in _MODULE_MEMBERS[name] if key in namespace)
            modules[name]=(sys.modules[name],namespace,members)
    mp=None
    if 'multiprocessing.context' in modules:
        namespace=modules['multiprocessing.context'][1]
        current=namespace.get('_default_context')
        cls=namespace.get('DefaultContext')
        if _TYPE(cls) is type and _TYPE(current) is cls:
            data=object.__getattribute__(current,'__dict__')
            if _TYPE(data) is _DICT and all(_TYPE(key) is str for key in data):
                mp=(current,cls,dict(data))
                for name in ('get_context','RLock','get_start_method'):
                    function=_CLASS_DICT.__get__(cls,type).get(name)
                    if function is None:
                        base=namespace.get('BaseContext')
                        function=_CLASS_DICT.__get__(base,type).get(name) if _TYPE(base) is type else None
                    if _TYPE(function) is FunctionType:
                        functions.append(_function(function))
                base=namespace.get('BaseContext')
                if _TYPE(base) is type:
                    function=_CLASS_DICT.__get__(base,type).get('get_start_method')
                    if _TYPE(function) is FunctionType:
                        functions.append(_function(function))
    if 'multiprocessing.synchronize' in modules:
        namespace=modules['multiprocessing.synchronize'][1]
        for class_name,names in (('SemLock',('__init__','_make_name')),('RLock',('__init__',))):
            cls=namespace.get(class_name)
            if _TYPE(cls) is type:
                selected=[]
                for name in names:
                    method=_CLASS_DICT.__get__(cls,type).get(name)
                    function=method.__func__ if _TYPE(method) in (staticmethod,classmethod) else method
                    functions.append(_function(function))
                    selected.append((name,method))
                classes.append((cls,tuple(selected)))
    for module_name,names in (('multiprocessing.process',('current_process',)),
                              ('multiprocessing.util',('debug','register_after_fork'))):
        if module_name in modules:
            namespace=modules[module_name][1]
            for name in names:
                if _TYPE(namespace.get(name)) is FunctionType:
                    functions.append(_function(namespace[name]))
    if 'tempfile' in modules:
        cls=modules['tempfile'][1].get('_RandomNameSequence')
        if _TYPE(cls) is type:
            members=_CLASS_DICT.__get__(cls,type)
            selected=[('characters',members.get('characters'))]
            for name in ('__iter__','__next__','rng'):
                method=members.get(name)
                function=method.fget if _TYPE(method) is property else method
                functions.append(_function(function))
                selected.append((name,method))
            classes.append((cls,tuple(selected)))
        random_class=modules['tempfile'][1].get('_Random')
        if _TYPE(random_class) is type:
            members=_CLASS_DICT.__get__(random_class,type)
            selected=[]
            for name in ('seed','choices','_randbelow'):
                function=members.get(name)
                if _TYPE(function) is FunctionType:
                    functions.append(_function(function))
                    selected.append((name,function))
            classes.append((random_class,tuple(selected)))
    weak_members=_CLASS_DICT.__get__(weakref.WeakValueDictionary,type)
    selected=[]
    for name in ('__init__','__setitem__','_commit_removals'):
        function=weak_members.get(name)
        original=_WEAK_CLASS_EARLY[(weakref.WeakValueDictionary,name)]
        if function is not original[0]:
            raise CallUnknown('original weak-registry class changed before SDK prefix completion')
        _same_function(original)
        functions.append(original)
        selected.append((name,function))
    classes.append((weakref.WeakValueDictionary,tuple(selected)))
    selected=[]
    for name in ('__new__','__init__'):
        original=_WEAK_CLASS_EARLY[(weakref.KeyedRef,name)]
        function=_CLASS_DICT.__get__(weakref.KeyedRef,type).get(name)
        if _TYPE(function) is staticmethod:
            function=function.__func__
        if function is not original[0]:
            raise CallUnknown('original weak-reference class changed before SDK prefix completion')
        _same_function(original)
        functions.append(original)
        selected.append((name,_CLASS_DICT.__get__(weakref.KeyedRef,type).get(name)))
    selected.append(('key',_WEAK_KEY_SLOT))
    classes.append((weakref.KeyedRef,tuple(selected)))
    selected=[]
    for name in _WEAK_SET_NAMES:
        original=_WEAK_CLASS_EARLY[(weakref.WeakSet,name)]
        function=_CLASS_DICT.__get__(weakref.WeakSet,type).get(name)
        if function is not original[0]:
            raise CallUnknown('original progress weak-set class changed before SDK prefix completion')
        _same_function(original)
        functions.append(original)
        selected.append((name,function))
    classes.append((weakref.WeakSet,tuple(selected)))
    context=NativeKernelContext()
    _CONTEXTS[context]=dict(functions=functions,classes=classes,modules=modules,mp=mp)
    return context


def verify_weak_membership(instance):
    """Read the original empty progress collection and native remove callback."""
    if _TYPE(instance) is not weakref.WeakSet:
        raise CallUnknown('original progress membership is not an exact WeakSet')
    members=_CLASS_DICT.__get__(weakref.WeakSet,type)
    if any(name in members for name in ('data','__getattribute__','__getattr__')):
        raise CallUnknown('original progress membership access protocol changed')
    values=object.__getattribute__(instance,'__dict__')
    if (_TYPE(values) is not dict or any(_TYPE(key) is not str for key in values)
            or _TYPE(values.get('data')) is not set or values['data']
            or _TYPE(values.get('_pending_removals')) is not list or values['_pending_removals']
            or _TYPE(values.get('_iterating')) is not set or values['_iterating']):
        raise CallUnknown('original empty progress membership has unreviewed active/removal state')
    callback=values.get('_remove')
    if (_TYPE(callback) is not FunctionType or callback.__code__ is not _WEAK_SET_REMOVE_CODE
            or callback.__closure__ or _TYPE(callback.__defaults__) is not tuple or len(callback.__defaults__)!=1):
        raise CallUnknown('original progress weak-set removal callback identity changed')
    reference=callback.__defaults__[0]
    if _TYPE(reference) is not weakref.ReferenceType or weakref.ReferenceType.__call__(reference) is not instance:
        raise CallUnknown('original progress weak-set callback owner differs')
    return dict(status='CHECKED')


def verify_read_kernels(context):
    """Check original native secure-open and UTF-8 decoder definitions only."""
    try:
        if _TYPE(context) is not NativeKernelContext or context not in _CONTEXTS:
            raise CallUnknown('original read/decoder kernel occurrence is not available')
        if builtins.open is not _READ_OPEN or any(getattr(os,name) is not value for name,value in _READ_ORIGINALS.items()):
            raise CallUnknown('original native descriptor open/read/stat/close kernel changed')
        if any(getattr(codecs,name) is not value for name,value in _CODEC_METHODS.items()):
            raise CallUnknown('original decoder lookup/registration kernel definition changed')
        if (sys.modules.get('encodings.utf_8') is not _UTF8_MODULE
                or any(getattr(_UTF8_MODULE,name) is not value for name,value in _UTF8_MEMBERS.items())
                or _CODEC_LOOKUP('utf-8') is not _UTF8_INFO):
            raise CallUnknown('original UTF-8 codec info/decoder constructor identity changed')
        for cls,state in _UTF8_CLASSES:
            members=_CLASS_DICT.__get__(cls,type)
            if len(members)!=len(state) or any(name not in members or members[name] is not value for name,value in state):
                raise CallUnknown('original UTF-8 decoder class/native descriptor changed')
        for function in _READ_FUNCTIONS:
            _same_function(function)
        return dict(status='CHECKED',allowed=True,effects=['original native read-only fd/inode checks, read/seek/close retained',
            'original UTF-8 strict/incremental decoding and original exceptions retained'],lifecycle_permission='NOT_GRANTED')
    except (CallUnknown,TypeError,ValueError,AttributeError,OSError) as error:
        return dict(status='UNKNOWN',allowed=False,remaining=[str(error)],lifecycle_permission='NOT_GRANTED')


def _stdio():
    stream=sys.stderr
    if _TYPE(stream) is not io.TextIOWrapper:
        raise CallUnknown('progress default stderr is not the original supported native stream')
    # Exact native descriptors are used rather than object methods or a probe
    # write; this reads the existing stream/descriptor without changing it.
    if io.TextIOBase.closed.__get__(stream,io.TextIOWrapper):
        raise CallUnknown('original native progress stream is closed')
    descriptor=io.TextIOWrapper.fileno(stream)
    if _TYPE(descriptor) is not int or descriptor<0:
        raise CallUnknown('original progress stream has no native descriptor')
    details=os.fstat(descriptor)
    return dict(status='CHECKED',scope='original native text stream and currently valid descriptor',
                device=details.st_dev,inode=details.st_ino,mode=details.st_mode)


def _terminal(state):
    if sys.platform!='linux' or platform.python_implementation()!='CPython':
        raise CallUnknown('reviewed progress terminal/semaphore kernel is Linux CPython')
    required=dict(array=('array',),fcntl=('ioctl',),termios=('TIOCGWINSZ',))
    for module,names in required.items():
        old=state['modules'].get(module)
        if old is None or sys.modules.get(module) is not old[0] or _loaded(module) is not old[1]:
            raise CallUnknown('original terminal kernel import domain is not independently prepared: '+module)
        for name in names:
            if name not in old[1]:
                raise CallUnknown('original terminal native member is absent: '+module+'.'+name)
    if _TYPE(_loaded('fcntl')['ioctl']) is not BuiltinFunctionType or _TYPE(_loaded('termios')['TIOCGWINSZ']) is not int:
        raise CallUnknown('original ioctl/TIOCGWINSZ native protocol is unavailable')
    if (os.environ is not _ENVIRON_OBJECT or _TYPE(os.environ) is not _ENVIRON_TYPE
            or any(getattr(_ENVIRON_TYPE,name) is not method for name,method in _ENVIRON_METHODS.items())):
        raise CallUnknown('original terminal fallback environment read protocol changed')
    data=object.__getattribute__(os.environ,'__dict__')
    if _TYPE(data) is not dict or any(data.get(name) is not value for name,value in _ENVIRON_CONVERTERS.items()):
        raise CallUnknown('original terminal environment string converters changed')
    for name in ('COLUMNS','LINES'):
        value=os.environ.get(name)
        if value is not None and _TYPE(value) is not str:
            raise CallUnknown('original terminal exception-fallback environment value is nonordinary')
    return dict(status='CHECKED',effects=['original TIOCGWINSZ native local-buffer read',
                'original caught ioctl failure and unchanged ordinary environment fallback'])


def _multiprocessing(state):
    required=('multiprocessing','multiprocessing.context','multiprocessing.synchronize','_multiprocessing',
              'multiprocessing.process','multiprocessing.util','tempfile')
    for name in required:
        old=state['modules'].get(name)
        if old is None or sys.modules.get(name) is not old[0] or _loaded(name) is not old[1]:
            raise CallUnknown('original semaphore import closure is not independently prepared: '+name)
    current=state['mp']
    if current is None:
        raise CallUnknown('original multiprocessing default context has no checked provenance')
    owner,cls,initial=current
    namespace=_loaded('multiprocessing.context')
    if namespace.get('_default_context') is not owner or _TYPE(owner) is not cls:
        raise CallUnknown('original multiprocessing context identity changed')
    actual=object.__getattribute__(owner,'__dict__')
    if _TYPE(actual) is not _DICT or any(_TYPE(key) is not str for key in actual):
        raise CallUnknown('original multiprocessing context has unknown state protocols')
    if actual.keys()!=initial.keys() or any(actual[name] is not value for name,value in initial.items() if name!='_actual_context'):
        raise CallUnknown('original multiprocessing default context changed since its checked occurrence')
    concrete=namespace.get('_concrete_contexts')
    if _TYPE(concrete) is not dict or any(_TYPE(key) is not str for key in concrete):
        raise CallUnknown('original finite multiprocessing context collection has unknown protocols')
    effective=actual.get('_actual_context')
    if effective is None:
        effective=actual.get('_default_context')
    fork=concrete.get('fork')
    fork_class=namespace.get('ForkContext')
    if (_TYPE(fork_class) is not type or _TYPE(fork) is not fork_class or effective is not fork
            or _CLASS_DICT.__get__(fork_class,type).get('_name')!='fork'):
        raise CallUnknown('actual original default context is outside reviewed fork semaphore effects')
    fork_data=object.__getattribute__(fork,'__dict__')
    if _TYPE(fork_data) is not dict or fork_data:
        raise CallUnknown('original fork context has unreviewed per-instance method/state overrides')
    for context_class in (cls,namespace.get('BaseContext'),fork_class):
        if _TYPE(context_class) is not type:
            raise CallUnknown('original context class/metaclass is unavailable')
        members=_CLASS_DICT.__get__(context_class,type)
        if any(name in members for name in ('__getattribute__','__getattr__')):
            raise CallUnknown('original context instance access protocol has an unreviewed override')
    get_context=_CLASS_DICT.__get__(cls,type).get('get_context')
    if (_TYPE(get_context) is not FunctionType or _TYPE(get_context.__defaults__) is not tuple
            or len(get_context.__defaults__)!=1 or get_context.__defaults__[0] is not None):
        raise CallUnknown('original omitted multiprocessing context selection default changed')
    native=_loaded('_multiprocessing').get('SemLock')
    if _TYPE(native) is not type or native.__module__!='_multiprocessing':
        raise CallUnknown('original semaphore native class is unavailable')
    if sys.version_info[:3]!=(3,10,22):
        raise CallUnknown('reviewed semaphore source/native ABI is CPython 3.10.22')
    stdlib=Path(sysconfig.get_path('stdlib'))
    from seampact.source.sdk_code_identity import _codes,code_shape
    expected_codes={}
    for module,(relative,sha256) in _STDLIB_SOURCE.items():
        namespace=_loaded(module)
        filename=namespace.get('__file__')
        expected=stdlib/relative
        if _TYPE(filename) is not str or Path(filename)!=expected:
            raise CallUnknown('original finite stdlib source module origin differs: '+module)
        data=expected.read_bytes()
        if hashlib.sha256(data).hexdigest()!=sha256:
            raise CallUnknown('original finite stdlib semantic source identity differs: '+module)
        expected_codes[module]=compile(data,str(expected),'exec',dont_inherit=True,optimize=sys.flags.optimize)
    for snapshot in state['functions']:
        function=snapshot[0]
        module=function.__module__
        if module not in expected_codes:
            continue
        qualified=tuple(part for part in function.__qualname__.split('.') if part!='<locals>')
        candidates=[code for path,code in _codes(expected_codes[module]) if path==qualified]
        if (function.__globals__ is not _loaded(module)
                or not any(code_shape(function.__code__)==code_shape(code) for code in candidates)):
            raise CallUnknown('loaded original stdlib method code differs from reviewed source: '+module)
    extension=_loaded('_multiprocessing').get('__file__')
    expected=stdlib/'lib-dynload/_multiprocessing.cpython-310-x86_64-linux-gnu.so'
    if _TYPE(extension) is not str or Path(extension)!=expected or hashlib.sha256(expected.read_bytes()).hexdigest()!=_SEMLOCK_NATIVE:
        raise CallUnknown('actual original semaphore extension origin/native ABI bytes differ')
    sync=_loaded('multiprocessing.synchronize')
    if _TYPE(sync.get('RLock')) is not type or _TYPE(sync.get('SemLock')) is not type:
        raise CallUnknown('original Python semaphore classes are unavailable')
    if _TYPE(sync.get('RECURSIVE_MUTEX')) is not int or sync['RECURSIVE_MUTEX']!=0:
        raise CallUnknown('original native recursive-mutex operation constant changed')
    for cls in (sync['RLock'],sync['SemLock']):
        members=_CLASS_DICT.__get__(cls,type)
        if any(name in members for name in ('__new__','__getattribute__','__getattr__','__setattr__','__delattr__','__del__')):
            raise CallUnknown('original semaphore class has an unreviewed constructor/instance protocol override')
    process=_loaded('multiprocessing.process')
    current=process.get('_current_process')
    if current is None:
        raise CallUnknown('original semaphore process owner is absent')
    current_type=_TYPE(current)
    if _TYPE(current_type) is not type or _CLASS_DICT.__get__(current_type,type).get('__module__')!='multiprocessing.process':
        raise CallUnknown('original semaphore current-process object has an unknown class')
    values=object.__getattribute__(current,'__dict__')
    config=values.get('_config') if _TYPE(values) is dict else None
    if _TYPE(config) is not dict or any(_TYPE(key) is not str for key in config) or _TYPE(config.get('semprefix')) is not str:
        raise CallUnknown('original semaphore name prefix is not an ordinary source process configuration')
    for source_class in type.__dict__['__mro__'].__get__(current_type,type):
        if source_class is object:
            continue
        source_members=_CLASS_DICT.__get__(source_class,type)
        if any(name in source_members for name in ('__getattribute__','__getattr__','_config')):
            raise CallUnknown('original current-process config access has an unreviewed descriptor/override')
    util=_loaded('multiprocessing.util')
    if util.get('_logger') is not None:
        raise CallUnknown('original semaphore debug logger may invoke unreviewed handlers')
    registry=util.get('_afterfork_registry')
    if _TYPE(registry) is not weakref.WeakValueDictionary:
        raise CallUnknown('original semaphore after-fork registry has an unknown collection protocol')
    weak_members=_CLASS_DICT.__get__(weakref.WeakValueDictionary,type)
    if any(name in weak_members for name in ('data','__getattribute__','__getattr__')):
        raise CallUnknown('original semaphore registry access protocol changed')
    registry_data=object.__getattribute__(registry,'__dict__')
    if _TYPE(registry_data) is not dict or _TYPE(registry_data.get('data')) is not dict:
        raise CallUnknown('original semaphore registry storage is not native')
    callback=registry_data.get('_remove')
    if (_TYPE(callback) is not FunctionType or callback.__code__ is not _WEAK_REMOVE_CODE
            or callback.__globals__ is not _MODULE_DICT.__get__(weakref,ModuleType)
            or callback.__closure__ or _TYPE(callback.__defaults__) is not tuple or len(callback.__defaults__)!=2):
        raise CallUnknown('original after-fork weak-registry removal callback identity changed')
    owner_ref,remove=callback.__defaults__
    if (_TYPE(owner_ref) is not weakref.ReferenceType or weakref.ReferenceType.__call__(owner_ref) is not registry
            or remove is not _REMOVE_DEAD_WEAKREF or weakref._remove_dead_weakref is not _REMOVE_DEAD_WEAKREF):
        raise CallUnknown('original after-fork weak callback owner/native removal binding changed')
    random_class=_loaded('tempfile').get('_RandomNameSequence')
    semlock_class=sync['SemLock']
    sequence=_CLASS_DICT.__get__(semlock_class,type).get('_rand')
    if _TYPE(random_class) is not type or _TYPE(sequence) is not random_class:
        raise CallUnknown('original finite semaphore random-name sequence has an unknown protocol')
    members=_CLASS_DICT.__get__(random_class,type)
    if _TYPE(members.get('characters')) is not str or any(name in members for name in ('__getattribute__','__getattr__')):
        raise CallUnknown('original semaphore random-name character/access protocol changed')
    sequence_data=object.__getattribute__(sequence,'__dict__')
    if (_TYPE(sequence_data) is not dict or any(_TYPE(key) is not str for key in sequence_data)
            or set(sequence_data)-{'_rng','_rng_pid'}):
        raise CallUnknown('original semaphore random-name instance has unknown mutable fields')
    if '_rng' in sequence_data:
        rng=sequence_data['_rng']
        rng_class=_loaded('tempfile').get('_Random')
        if _TYPE(rng_class) is not type or _TYPE(rng) is not rng_class:
            raise CallUnknown('original semaphore random generator has an unknown class')
        rng_data=object.__getattribute__(rng,'__dict__')
        if (_TYPE(rng_data) is not dict or any(_TYPE(key) is not str for key in rng_data)
                or set(rng_data)-{'gauss_next'}
                or rng_data.get('gauss_next') is not None and _TYPE(rng_data.get('gauss_next')) is not float):
            raise CallUnknown('original semaphore random generator has unreviewed instance overrides')
    return dict(status='CHECKED',effects=['original RLock/SemLock construction and normal exceptions retained',
                'original fork context unlinks native semaphore immediately',
                'original at-most-100 collision retries and native random-name state retained',
                'original after-fork weak registration retained; no tracker child on this fork path'],
                lifecycle_permission='NOT_GRANTED')


def verify_native_kernels(context):
    guards,remaining={},[]
    try:
        if _TYPE(context) is not NativeKernelContext or context not in _CONTEXTS:
            raise CallUnknown('original native-kernel occurrence is not available')
        state=_CONTEXTS[context]
        if (sys.modules.get('threading') is not _THREADING or atexit.register is not _ATEXIT_REGISTER
                or (time.time,time.monotonic)!=_CLOCKS or (os.fstat,os.stat,os.getpid)!=_NATIVE_OS):
            raise CallUnknown('original native clock/process/atexit kernel binding changed')
        if threading.RLock is not _RLOCK_FACTORY or getattr(threading,'_CRLock',None) is not _RLOCK_NATIVE:
            raise CallUnknown('original native thread-lock factory/class binding changed')
        for function in state['functions']:
            _same_function(function)
        for module,namespace,members in state['modules'].values():
            if _namespace(module) is not namespace or any(namespace.get(name) is not value for name,value in members):
                raise CallUnknown('original native/stdlib module member identity changed')
        for cls,methods in state['classes']:
            members=_CLASS_DICT.__get__(cls,type)
            if any(members.get(name) is not function for name,function in methods):
                raise CallUnknown('original threading class/method identity changed')
        for cls,namespace in _THREAD_CLASSES_EARLY:
            current=_CLASS_DICT.__get__(cls,type)
            if len(current)!=len(namespace) or any(name not in current or current[name] is not value for name,value in namespace):
                raise CallUnknown('original threading class constructor/descriptor namespace changed')
        guards['thread_clock_atexit']=dict(status='CHECKED',effects='original thread/event creation/start/join and registration retained')
        guards['stdio']=_stdio()
        for name,checker in (('terminal',_terminal),('semaphore',_multiprocessing)):
            try:
                guards[name]=checker(state)
            except CallUnknown as error:
                guards[name]=dict(status='UNKNOWN',reason=str(error))
                remaining.append(str(error))
    except (CallUnknown,TypeError,ValueError,AttributeError,OSError) as error:
        remaining.append(str(error))
    return dict(schema='actual-progress-native-kernels/1',status='UNKNOWN' if remaining else 'CHECKED',
                allowed=not remaining,guards=guards,remaining=remaining,lifecycle_permission='NOT_GRANTED')


_UTF8_CLASSES=[]
_READ_FUNCTIONS=[]
_THREAD_EARLY={(cls,name):_function(_CLASS_DICT.__get__(cls,type)[name])
               for cls,names in _THREAD_METHODS.items() for name in names}
_THREAD_CLASSES_EARLY=[(cls,tuple(_CLASS_DICT.__get__(cls,type).items())) for cls in _THREAD_METHODS]
_RLOCK_STATE=_function(_RLOCK_FACTORY) if _TYPE(_RLOCK_FACTORY) is FunctionType else None
_THREAD_ALL_EARLY=[]
for _thread_class in _THREAD_METHODS:
    for _thread_member in _CLASS_DICT.__get__(_thread_class,type).values():
        _thread_functions=(_thread_member.fget,_thread_member.fset,_thread_member.fdel) if _TYPE(_thread_member) is property else (
            _thread_member.__func__,) if _TYPE(_thread_member) in (staticmethod,classmethod) else (_thread_member,)
        for _thread_function in _thread_functions:
            if _TYPE(_thread_function) is FunctionType:
                _THREAD_ALL_EARLY.append(_function(_thread_function))
_WEAK_CLASS_EARLY={}
_WEAK_SET_NAMES=('__init__','add','remove','discard','copy','__iter__','__len__','_commit_removals')
for _weak_class,_weak_names in ((weakref.WeakValueDictionary,('__init__','__setitem__','_commit_removals')),
                                (weakref.KeyedRef,('__new__','__init__')),(weakref.WeakSet,_WEAK_SET_NAMES)):
    for _weak_name in _weak_names:
        _weak_function=_CLASS_DICT.__get__(_weak_class,type)[_weak_name]
        if _TYPE(_weak_function) is staticmethod:
            _weak_function=_weak_function.__func__
        _WEAK_CLASS_EARLY[(_weak_class,_weak_name)]=_function(_weak_function)
_WEAK_KEY_SLOT=_CLASS_DICT.__get__(weakref.KeyedRef,type)['key']
_WEAK_SET_REMOVE_CODE=next(code for code in weakref.WeakSet.__init__.__code__.co_consts
                           if _TYPE(code).__name__=='code' and code.co_name=='_remove')
_WEAK_REMOVE_CODE=next(code for code in weakref.WeakValueDictionary.__init__.__code__.co_consts
                       if _TYPE(code).__name__=='code' and code.co_name=='remove')
for _reader_class in (_UTF8_MEMBERS['IncrementalDecoder'],_UTF8_MEMBERS['IncrementalEncoder']):
    _reader_members=_CLASS_DICT.__get__(_reader_class,type)
    _UTF8_CLASSES.append((_reader_class,tuple(_reader_members.items())))
    for _reader_member in _reader_members.values():
        if _TYPE(_reader_member) is FunctionType:
            _READ_FUNCTIONS.append(_function(_reader_member))
for _reader_function in [*_CODEC_METHODS.values(),*_UTF8_MEMBERS.values(),*_READ_ORIGINALS.values()]:
    if _TYPE(_reader_function) is FunctionType:
        _READ_FUNCTIONS.append(_function(_reader_function))
