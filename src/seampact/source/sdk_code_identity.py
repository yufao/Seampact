"""Authenticate loaded SDK Python code against reviewed source, without import.

Installed source/RECORD identity does not authenticate an unchecked or stale
bytecode cache. Source is compiled but never executed, and loaded code must
match the exact original qualified definition or a captured stdlib wrapper.
"""
import contextlib
import hashlib
from importlib import metadata
import importlib.util
import marshal
import struct
from pathlib import Path
import sys
from types import CodeType,FunctionType

from seampact.source.call_value_conditions import CallUnknown
from seampact.source.lifecycle_resources import sdk_identity

_TYPE=type
_PACK=struct.pack
_CONTEXTMANAGER=contextlib.contextmanager
_CONTEXT_GLOBALS=_CONTEXTMANAGER.__globals__
_CONTEXT_CLASS=contextlib._GeneratorContextManager
_CONTEXT_CLASS_CODE={name:value.__code__ for name,value in _CONTEXT_CLASS.__dict__.items()
                     if _TYPE(value) is FunctionType}


def _constant(value):
    kind=_TYPE(value)
    if kind is float:
        return ('float-bits', _PACK('>d', value))
    if kind is complex:
        return ('complex-bits', _PACK('>dd', value.real, value.imag))
    if value is None or value is Ellipsis or kind in (str,bytes,int,bool):
        return (kind,value)
    if kind is CodeType:
        return ('code',code_shape(value))
    if kind in (tuple,frozenset):
        values=tuple(_constant(item) for item in value)
        return (kind,frozenset(values) if kind is frozenset else values)
    raise CallUnknown('loaded SDK bytecode contains a non-native constant protocol')


def code_shape(code):
    if _TYPE(code) is not CodeType:
        raise CallUnknown('loaded SDK code is not an exact native code object')
    fields=('co_argcount','co_posonlyargcount','co_kwonlyargcount','co_nlocals','co_stacksize',
            'co_flags','co_code','co_names','co_varnames','co_freevars','co_cellvars',
            'co_firstlineno','co_name','co_qualname','co_filename','co_lnotab','co_linetable','co_exceptiontable')
    values=tuple((name,_constant(getattr(code,name))) for name in fields if hasattr(code,name))
    constants=tuple(_constant(value) for value in code.co_consts)
    return values,constants


def _codes(code,path=()):
    for value in code.co_consts:
        if _TYPE(value) is CodeType:
            current=(*path,value.co_name)
            yield current,value
            yield from _codes(value,current)


def _compiled(requirement):
    if sdk_identity([requirement]).get('status')!='CHECKED':
        raise CallUnknown('reviewed SDK Python source/RECORD identity does not match')
    distribution=metadata.distribution(requirement['distribution'])
    wanted=[path for path in distribution.files or () if str(path).replace('\\','/')==requirement['path']]
    if len(wanted)!=1:
        raise CallUnknown('reviewed SDK Python source has no unique installed file')
    source=wanted[0].locate()
    data=source.read_bytes()
    if hashlib.sha256(data).hexdigest()!=requirement['sha256']:
        raise CallUnknown('SDK Python source changed during actual code authentication')
    # The original source loader compiles with the installed source filename.
    # A relative RECORD entry is evidence selection, not that code filename.
    return compile(data,str(source),'exec',dont_inherit=True,optimize=sys.flags.optimize)


def verify_original_module_caches(requirements):
    """Check full module/class/default code before the normal original import.

    Existing bytecode is not deleted or invalidated. A missing cache leaves the
    original loader's source compilation and optional cache write unchanged.
    The caller separately establishes that same original controlled loader.
    """
    rows=[]
    for requirement in requirements:
        if not requirement['path'].endswith('.py'):
            continue
        expected=_compiled(requirement)
        distribution=metadata.distribution(requirement['distribution'])
        matches=[path for path in distribution.files or () if str(path).replace('\\','/')==requirement['path']]
        if len(matches)!=1:
            raise CallUnknown('SDK source/cache binding has no unique installed original file')
        source=matches[0].locate()
        cache=Path(importlib.util.cache_from_source(str(source)))
        if not cache.exists():
            rows.append(dict(path=requirement['path'],status='CHECKED',cache='ABSENT_ORIGINAL_SOURCE_COMPILATION'))
            continue
        details=cache.lstat()
        import stat
        if not stat.S_ISREG(details.st_mode) or stat.S_ISLNK(details.st_mode) or details.st_size>max(1048576,source.stat().st_size*16):
            raise CallUnknown('SDK cache uses an unsupported file protocol or finite byte size')
        data=cache.read_bytes()
        if len(data)<16 or data[:4]!=importlib.util.MAGIC_NUMBER or int.from_bytes(data[4:8],'little')&~3:
            raise CallUnknown('existing SDK bytecode cache header is not the original interpreter format')
        try:
            actual=marshal.loads(data[16:])
        except (EOFError,ValueError,TypeError) as error:
            raise CallUnknown('existing SDK bytecode cache cannot be read by its original native format') from error
        if _TYPE(actual) is not CodeType or code_shape(actual)!=code_shape(expected):
            raise CallUnknown('existing SDK module/class/default bytecode differs from reviewed original source')
        rows.append(dict(path=requirement['path'],status='CHECKED',cache='FULL_ORIGINAL_MODULE_CODE_MATCHED'))
    return dict(status='CHECKED' if rows else 'UNKNOWN',files=rows,
                original_cache_creation_and_failure_effects='RETAINED')


def _module(path):
    return path.removesuffix('.py').replace('/','.').removesuffix('.__init__')


def verify_function_source(function,requirements,*,expected_module=None,expected_qualname=None,compiled=None):
    if _TYPE(function) is not FunctionType:
        raise CallUnknown('SDK function is not an exact original Python function')
    module=function.__module__ if expected_module is None else expected_module
    qualified=function.__qualname__ if expected_qualname is None else expected_qualname
    if (_TYPE(module) is not str or _TYPE(qualified) is not str or function.__module__!=module
            or function.__qualname__!=qualified):
        raise CallUnknown('loaded SDK function module/qualified definition identity changed')
    selected=[item for item in requirements if item['path'].endswith('.py') and _module(item['path'])==module]
    if len(selected)!=1:
        raise CallUnknown('loaded SDK callable has no unique reviewed original source module')
    selected=selected[0]
    key=(module,selected['sha256'])
    if compiled is None:
        tree=_compiled(selected)
    else:
        if key not in compiled:
            compiled[key]=_compiled(selected)
        tree=compiled[key]
    path=tuple(part for part in qualified.split('.') if part!='<locals>')
    originals=[code for candidate,code in _codes(tree) if candidate==path]
    actual=code_shape(function.__code__)
    if any(code_shape(code)==actual for code in originals):
        if _TYPE(sys.modules) is not dict or _TYPE(sys.modules.get(module)) is not type(contextlib):
            raise CallUnknown('original SDK source module is not already loaded')
        original=sys.modules[module].__dict__
        if (_TYPE(original) is not dict or any(_TYPE(key) is not str for key in original)
                or function.__globals__ is not original):
            raise CallUnknown('SDK function code was copied into a nonoriginal global namespace')
        modules={_module(item['path']) for item in requirements if item['path'].endswith('.py')}
        for cell in function.__closure__ or ():
            value=cell.cell_contents
            if _TYPE(value) is FunctionType:
                if value.__module__ not in modules:
                    raise CallUnknown('SDK wrapper closes a Python callback outside reviewed source modules')
                verify_function_source(value,requirements,compiled=compiled)
        return dict(status='CHECKED',module=module,qualname=qualified,source_sha256=selected['sha256'])
    # contextmanager updates the wrapper's metadata to the wrapped SDK
    # function. Authenticate both the platform wrapper and original SDK body.
    namespace=function.__dict__
    if _TYPE(namespace) is not dict or any(_TYPE(key) is not str for key in namespace):
        raise CallUnknown('SDK wrapper metadata has unknown key protocols')
    wrapped=namespace.get('__wrapped__')
    wrappers=[code for candidate,code in _codes(_CONTEXTMANAGER.__code__) if candidate==('helper',)]
    if (_TYPE(wrapped) is FunctionType and function.__globals__ is _CONTEXT_GLOBALS
            and contextlib.contextmanager is _CONTEXTMANAGER and contextlib._GeneratorContextManager is _CONTEXT_CLASS
            and any(code_shape(code)==actual for code in wrappers)
            and all(_TYPE(_CONTEXT_CLASS.__dict__.get(name)) is FunctionType
                    and _CONTEXT_CLASS.__dict__[name].__code__ is code for name,code in _CONTEXT_CLASS_CODE.items())):
        closure=dict(zip(function.__code__.co_freevars,(cell.cell_contents for cell in function.__closure__ or ())))
        if closure.get('func') is not wrapped:
            raise CallUnknown('original SDK contextmanager wrapper no longer closes its original function')
        return verify_function_source(wrapped,requirements,expected_module=module,expected_qualname=qualified,compiled=compiled)
    raise CallUnknown('loaded SDK bytecode does not match its reviewed original qualified source definition')
