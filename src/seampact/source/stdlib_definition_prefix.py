"""Finite existing-stdlib imports and original List[builtin] annotations.

The worker's platform bootstrap supplies the original objects. No annotation,
module, cache, RNG or SDK is evaluated here to obtain a premise. This does not
cover arbitrary typing subscription, forward references or dynamic classes.
"""
import ast
import builtins
import sys
from types import FunctionType,ModuleType

RULE='source-stdlib-definition-prefix/1'
_TYPE,_DICT,_STR=type,dict,str
_MODULE_DICT=ModuleType.__dict__['__dict__']
_CLASS_DICT=type.__dict__['__dict__']
_IMPORT=builtins.__import__
_BUILTINS={'str':str,'float':float}


def _namespace(module):
    if _TYPE(module) is not ModuleType:
        raise ValueError('original stdlib module is not already available')
    namespace=_MODULE_DICT.__get__(module,ModuleType)
    if _TYPE(namespace) is not _DICT or any(_TYPE(key) is not _STR for key in namespace):
        raise ValueError('stdlib module namespace has an unknown key protocol')
    return namespace


def _constant(value):
    if value is None or _TYPE(value) in (str,int,bool,float):
        return (_TYPE(value),value)
    if _TYPE(value) is tuple:
        return (tuple,tuple(_constant(item) for item in value))
    if _TYPE(value) is dict and all(_TYPE(key) is _STR for key in value):
        return (dict,tuple((key,_constant(item)) for key,item in value.items()))
    return ('original-reference',id(value))


def _functions(namespace,classes):
    pending=[value for value in namespace.values() if _TYPE(value) is FunctionType]
    for cls in classes:
        for value in _CLASS_DICT.__get__(cls,type).values():
            if _TYPE(value) in (staticmethod,classmethod):
                value=value.__func__
            if _TYPE(value) is FunctionType:
                pending.append(value)
    results,seen=[],set()
    while pending:
        function=pending.pop()
        if id(function) in seen:
            continue
        seen.add(id(function))
        globals_=function.__globals__
        if _TYPE(globals_) is not dict:
            raise ValueError('stdlib annotation function globals are not native')
        references=tuple((name,globals_[name]) for name in function.__code__.co_names if name in globals_)
        closure=tuple(cell.cell_contents for cell in function.__closure__ or ())
        for value in closure:
            if _TYPE(value) is FunctionType:
                pending.append(value)
        wrapped=function.__dict__.get('__wrapped__')
        if _TYPE(wrapped) is FunctionType:
            pending.append(wrapped)
        results.append((function,function.__code__,_constant(function.__defaults__),
                        _constant(function.__kwdefaults__),globals_,references,closure))
    return results


_MODULES={name:sys.modules.get(name) for name in ('os','random','typing')}
_SPECS={}
for _name,_module_value in _MODULES.items():
    if _TYPE(_module_value) is ModuleType:
        _spec=_MODULE_DICT.__get__(_module_value,ModuleType).get('__spec__')
        if _spec is not None and _TYPE(_TYPE(_spec)) is type:
            _members=_CLASS_DICT.__get__(_TYPE(_spec),type)
            _SPECS[_name]=(_spec,_TYPE(_spec),_members.get('__dict__'),tuple(_members.items()))
_TYPING=None
_ERROR=None
try:
    typing=_namespace(_MODULES['typing'])
    alias=typing.get('List')
    classes=tuple(typing[name] for name in ('_SpecialGenericAlias','_GenericAlias','_BaseGenericAlias'))
    if any(_TYPE(cls) is not type for cls in classes) or _TYPE(alias) is not classes[0]:
        raise ValueError('platform List alias is outside the finite original typing family')
    _TYPING=dict(namespace=typing,alias=alias,classes=tuple((cls,tuple(_CLASS_DICT.__get__(cls,type).items())) for cls in classes),
                 fields=_constant(object.__getattribute__(alias,'__dict__')),functions=_functions(typing,classes))
except (ValueError,KeyError,TypeError,AttributeError) as error:
    _ERROR=str(error)


def _module(name):
    if builtins.__import__ is not _IMPORT or _TYPE(sys.modules) is not dict:
        raise ValueError('original stdlib import kernel changed')
    module=_MODULES.get(name)
    if module is None or sys.modules.get(name) is not module:
        raise ValueError('original already-loaded stdlib module identity changed')
    namespace=_namespace(module)
    spec=namespace.get('__spec__')
    record=_SPECS.get(name)
    if record is None or spec is not record[0] or _TYPE(spec) is not record[1]:
        raise ValueError('original stdlib module import state is not supported')
    members=_CLASS_DICT.__get__(record[1],type)
    if (len(members)!=len(record[3]) or any(_TYPE(key) is not str for key in members)
            or any(members.get(key) is not value for key,value in record[3])):
        raise ValueError('original module-spec access protocol changed')
    state=record[2].__get__(spec,record[1])
    if _TYPE(state) is not dict or any(_TYPE(key) is not str for key in state) or state.get('_initializing') is not False:
        raise ValueError('original stdlib module is not completely initialized')
    return namespace


def _typing():
    namespace=_module('typing')
    if _ERROR is not None or _TYPING is None or namespace is not _TYPING['namespace']:
        raise ValueError('original finite annotation kernel is unavailable: '+str(_ERROR))
    if namespace.get('List') is not _TYPING['alias'] or _constant(object.__getattribute__(_TYPING['alias'],'__dict__'))!=_TYPING['fields']:
        raise ValueError('original List alias fields changed')
    for cls,members in _TYPING['classes']:
        current=_CLASS_DICT.__get__(cls,type)
        if (len(current)!=len(members) or any(_TYPE(name) is not str for name in current)
                or any(current.get(name) is not value for name,value in members)):
            raise ValueError('original typing subscription class protocol changed')
    for function,code,defaults,kwdefaults,globals_,references,closure in _TYPING['functions']:
        if (function.__code__ is not code or function.__globals__ is not globals_
                or any(_TYPE(name) is not str for name in globals_)
                or _constant(function.__defaults__)!=defaults or _constant(function.__kwdefaults__)!=kwdefaults
                or any(globals_.get(name) is not value for name,value in references)
                or len(function.__closure__ or ())!=len(closure)
                or any(cell.cell_contents is not value for cell,value in zip(function.__closure__ or (),closure))):
            raise ValueError('original typing function/code/default/global/closure binding changed')


def import_fact(module,member):
    if module not in _MODULES or member not in (module,'List') or member=='List' and module!='typing':
        return None
    fact=dict(schema=RULE,operation='ORIGINAL_ALREADY_LOADED_STDLIB_IMPORT',module=module,member=member,
              status='UNKNOWN',execution_permission=False,
              required=['same worker original supported import kernel and completely initialized module',
                        'original module namespace and requested export; no SDK/model construction'])
    try:
        namespace=_module(module)
        if member=='List':
            _typing()
            if namespace.get('List') is not _TYPING['alias']:
                raise ValueError('original List export changed')
        fact['status']='CHECKED'
    except (ValueError,KeyError,TypeError,AttributeError) as error:
        fact['reason']=str(error)
    return fact


def annotation_fact(checker,node,env,path):
    if not isinstance(node,ast.Subscript) or not isinstance(node.slice,ast.Name):
        return None
    from seampact.source.dependency_closure import _External
    def external(expression):
        if isinstance(expression,ast.Name):
            value=env.get(expression.id)
            return value.name if _TYPE(value) is _External and not value.local else None
        if isinstance(expression,ast.Attribute):
            parent=external(expression.value)
            return parent+'.'+expression.attr if parent else None
        return None
    if external(node.value)!='typing.List':
        return None
    qualified=checker.sdk_source.type_references.resolve(path,node.slice,node)
    name=qualified.removeprefix('builtins.') if _TYPE(qualified) is str else None
    if name not in _BUILTINS or not checker.sdk_source.unmodified('typing.List'):
        return None
    fact=dict(schema=RULE,operation='ORIGINAL_LIST_BUILTIN_ANNOTATION',module='typing',member='List',argument=name,
              status='UNKNOWN',execution_permission=False,
              required=['original List subscription, helper, cache and class construction protocols',
                        'exact native builtin type operand; original evaluation/cache/exception order retained'])
    try:
        _typing()
        if _MODULE_DICT.__get__(builtins,ModuleType).get(name) is not _BUILTINS[name]:
            raise ValueError('original annotation builtin type changed')
        fact['status']='CHECKED'
    except (ValueError,KeyError,TypeError,AttributeError) as error:
        fact['reason']=str(error)
    return fact


def verify_requirements(requirements):
    from seampact.source.import_only import _metadata
    requirements=_metadata(requirements)
    if _TYPE(requirements) is not list:
        return False
    for fact in requirements:
        if (_TYPE(fact) is not dict or fact.get('schema')!=RULE or fact.get('execution_permission') is not False
                or fact.get('operation') not in ('ORIGINAL_ALREADY_LOADED_STDLIB_IMPORT','ORIGINAL_LIST_BUILTIN_ANNOTATION')):
            return False
        current=import_fact(fact.get('module'),fact.get('member'))
        if current is None or current.get('status')!='CHECKED':
            return False
        if fact.get('operation')=='ORIGINAL_LIST_BUILTIN_ANNOTATION':
            _typing()
            name=fact.get('argument')
            if name not in _BUILTINS or _MODULE_DICT.__get__(builtins,ModuleType).get(name) is not _BUILTINS[name]:
                return False
    return True
