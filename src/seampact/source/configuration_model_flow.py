"""Normal source replay of finite original configuration model operations.

Only the supported normal-result domain is propagated. The original native
model/schema/callback targets, full SDK creation effects, reader conditions
and exception paths are retained as independent UNKNOWN obligations.
"""
import ast
from dataclasses import dataclass, replace

from seampact.source.pydantic_models import RULE, REGION_RULE, PydanticUnknown, compile_model_plan
from seampact.source.frozen_configuration_sequences import configuration_model_result
from seampact.source.call_value_conditions import digest as proof_digest


UNHANDLED = object()
GAP = 'finite configuration model requires original SDK creation, mutation, callback and native read-use target checks'


def _target_obligation(checker, plan, path, node):
    verified = getattr(checker.call_context, 'runtime_verified', ()) if checker.call_context is not None else ()
    if type(verified) is not tuple or any(type(value) is not str for value in verified) or proof_digest(plan) not in verified:
        checker.negative_gap(path, node, GAP)


@dataclass(frozen=True)
class ModelDump:
    receiver: object


def _model(cls):
    from seampact.source.dependency_closure import _Class, _External
    current, seen = cls, set()
    while type(current) is _Class and id(current) not in seen and len(seen) < 16:
        seen.add(id(current))
        if len(current.bases) != 1:
            return False
        current = current.bases[0]
    return type(current) is _External and current.name == 'pydantic.BaseModel' and not current.local


def _plan(checker, cls):
    if not hasattr(checker, 'configuration_model_plans'):
        checker.configuration_model_plans = {}
    key = (cls.path, cls.name)
    if key not in checker.configuration_model_plans:
        checker.configuration_model_plans[key] = compile_model_plan(checker.inspector, *key).requirements()
    plan = checker.configuration_model_plans[key]
    row = next(row for row in plan['models'] if row['identity'] == cls.path + ':' + cls.name)
    return plan, row


def model_definition(checker, cls, path, node):
    if not _model(cls):
        return
    try:
        plan, _ = _plan(checker, cls)
        checker.protocol(RULE, path, node, target_requirements=plan,
                         operation='original finite model declaration; SDK activation remains separate')
    except PydanticUnknown as error:
        checker.negative_gap(path, node, str(error))


def _source_class(checker, identity):
    path, name = identity.rsplit(':', 1)
    return checker.module(path).get(name)


def _callbacks(checker, cls, row, *, kind, mode, field=None):
    from seampact.source.dependency_closure import _Function
    order = row['method_order']
    if mode == 'before':
        order = list(reversed(order))
    result = []
    for name in order:
        metadata = row['methods'][name]
        for decorator in metadata['validators']:
            if decorator['kind'] != kind or decorator['mode'] != mode or field is not None and field not in decorator['fields']:
                continue
            source = metadata['source']
            owner = _source_class(checker, source['path'] + ':' + source['symbol'].split('.')[0])
            from seampact.source.dependency_closure import _Class
            if type(owner) is not _Class:
                raise PydanticUnknown('original model callback source class is absent')
            function = owner.attributes.get(name)
            if type(function) is not _Function:
                raise PydanticUnknown('original model callback source target is absent')
            # Replay the original callback body, retaining its original source
            # AST/decorators in the plan. The SDK descriptor dispatch itself
            # stays an explicit target requirement, never a removed wrapper.
            function = replace(function, method_kind='classmethod' if metadata['descriptor'] == 'classmethod' else 'staticmethod')
            result.append((function, metadata['descriptor']))
    return result


def _invoke_callback(checker, function, descriptor, cls, value, path, scope, node, *, model_after=False):
    from seampact.source.dependency_closure import _LiteralNone
    expected = len(function.node.args.posonlyargs) + len(function.node.args.args)
    arguments = [cls, value] if descriptor == 'classmethod' else [value]
    if expected != len(arguments) or function.node.args.vararg or function.node.args.kwarg or function.node.args.kwonlyargs:
        raise PydanticUnknown('original configuration callback binding/ValidationInfo protocol remains UNKNOWN')
    result = checker.invoke(function, arguments, {}, path, scope, node)
    if result is None or model_after and result is not value:
        raise PydanticUnknown('original configuration callback normal return/self identity remains UNKNOWN')
    return result


def _validate(checker, annotation, value, path, scope, node):
    from seampact.source.dependency_closure import _Instance, _LiteralNone, _Unsupported
    kind = annotation['kind']
    if value is None:
        raise PydanticUnknown('configuration field result contains an UNKNOWN value')
    if kind == 'any':
        return value
    if kind == 'none' and type(value) is _LiteralNone:
        return value
    if kind in {'str', 'int', 'float', 'bool'} and type(value).__name__ == kind:
        return value
    if kind == 'optional':
        return value if type(value) is _LiteralNone else _validate(checker, annotation['items'][0], value, path, scope, node)
    if kind == 'literal':
        if any(type(value) is type(choice.get('value')) and value == choice.get('value') for choice in annotation['choices']):
            return value
    if kind == 'annotated':
        return _validate(checker, annotation['value'], value, path, scope, node)
    if kind == 'list' and type(value) is list and len(value) <= 1024:
        item = annotation.get('items')
        return list(value) if not item else [_validate(checker, item[0], value, path, scope, node) for value in value]
    if kind == 'dict' and type(value) is dict and len(value) <= 1024 and all(type(key) is str for key in value):
        items = annotation.get('items')
        return dict(value) if not items else {
            _validate(checker, items[0], key, path, scope, node): _validate(checker, items[1], item, path, scope, node)
            for key, item in value.items()}
    if kind == 'union':
        def accepts_without_callback(annotation, candidate):
            leaf = annotation['kind']
            if leaf in {'str', 'int', 'float', 'bool'}:
                return type(candidate).__name__ == leaf
            if leaf == 'list' and type(candidate) is list:
                return all(accepts_without_callback(annotation['items'][0], item) for item in candidate)
            if leaf == 'model' and type(candidate) is dict:
                model = _source_class(checker, annotation['identity'])
                _, metadata = _plan(checker, model)
                return not any(callback['kind'] == 'model_validator' and callback['mode'] == 'before'
                               for method in metadata['methods'].values() for callback in method['validators'])
            return False
        # Only disjoint scalar/container input forms with no model-before
        # callback on a rejected candidate have this narrow normal result.
        def rejected_without_callback(annotation, candidate):
            leaf = annotation['kind']
            if leaf in {'str', 'int', 'float', 'bool'}:
                return True
            if leaf == 'list' and type(candidate) is list:
                return all(rejected_without_callback(annotation['items'][0], item) for item in candidate)
            if leaf == 'model' and type(candidate) not in (dict, _Instance):
                model = _source_class(checker, annotation['identity'])
                _, metadata = _plan(checker, model)
                return not any(callback['kind'] == 'model_validator' and callback['mode'] == 'before'
                               for method in metadata['methods'].values() for callback in method['validators'])
            return False
        options = [item for item in annotation['items'] if accepts_without_callback(item, value)]
        if len(options) == 1:
            rejected_safe = all(item is options[0] or rejected_without_callback(item, value) for item in annotation['items'])
            if rejected_safe:
                return _validate(checker, options[0], value, path, scope, node)
        raise PydanticUnknown('original native union selection/revalidation/callback order remains UNKNOWN')
    if kind == 'model':
        expected = _source_class(checker, annotation['identity'])
        if type(value) is dict:
            result = model_construct(checker, expected, [], value, path, scope, node)
            if result is UNHANDLED or type(result) is _Unsupported:
                raise PydanticUnknown('nested original configuration model result remains UNKNOWN')
            return result
        if type(value) is _Instance and value.local_class is expected:
            raise PydanticUnknown('existing native model revalidation/after-callback order remains UNKNOWN')
    raise PydanticUnknown('original native field coercion/constraints/result is outside the proved finite normal domain')


def model_construct(checker, cls, args, kwargs, path, scope, node):
    from seampact.source.dependency_closure import _Instance, _Unsupported, _configuration_value
    if not _model(cls):
        return UNHANDLED
    try:
        plan, row = _plan(checker, cls)
        checker.protocol(RULE, path, node, target_requirements=plan, operation='original finite model construction')
        _target_obligation(checker, plan, path, node)
        if args or type(kwargs) is not dict or any(type(name) is not str for name in kwargs):
            raise PydanticUnknown('original BaseModel constructor kwargs are not completely bound')
        original = dict(kwargs)  # Original **data binding; nested aliases retained.
        value = original
        for function, descriptor in _callbacks(checker, cls, row, kind='model_validator', mode='before'):
            value = _invoke_callback(checker, function, descriptor, cls, value, path, scope, node)
        if type(value) is not dict:
            raise PydanticUnknown('original before-model result is not an exact ordinary configuration mapping')
        instance = _Instance(cls.name + '()', {}, cls)
        for name in row['field_order']:
            metadata = row['fields'][name]
            supplied = name in value
            if supplied:
                field = value[name]
                for function, descriptor in _callbacks(checker, cls, row, kind='field_validator', mode='before', field=name):
                    field = _invoke_callback(checker, function, descriptor, cls, field, path, scope, node)
                field = _validate(checker, metadata['annotation'], field, path, scope, node)
                for function, descriptor in _callbacks(checker, cls, row, kind='field_validator', mode='after', field=name):
                    field = _invoke_callback(checker, function, descriptor, cls, field, path, scope, node)
            else:
                default = metadata['field']['default']
                if default.get('required'):
                    raise PydanticUnknown('required original configuration field is missing')
                if row['config'].get('validate_default') or metadata['field']['options'].get('validate_default', {}).get('value'):
                    raise PydanticUnknown('configuration default validation callback dispatch remains UNKNOWN')
                field = [] if default.get('factory') == 'builtins.list' else {} if default.get('factory') == 'builtins.dict' else _configuration_value(default.get('value'))
            instance.attributes[name] = field
        extras = {name: item for name, item in value.items() if name not in row['fields']}
        if extras and row['config'].get('extra') == 'forbid':
            raise PydanticUnknown('original finite configuration contains forbidden extra fields')
        if row['config'].get('extra') == 'allow':
            instance.attributes.update(extras)
        for function, descriptor in _callbacks(checker, cls, row, kind='model_validator', mode='after'):
            instance = _invoke_callback(checker, function, descriptor, cls, instance, path, scope, node, model_after=True)
        if not hasattr(checker, 'configuration_model_instances'):
            checker.configuration_model_instances = {}
        checker.configuration_model_instances[id(instance)] = (instance, frozenset(value), plan)
        configuration_model_result(checker, instance, [*kwargs.values(), kwargs])
        return instance
    except (PydanticUnknown, ValueError, KeyError) as error:
        checker.negative_gap(path, node, str(error))
        return _Unsupported('finite_configuration_model_result_unknown', path, node)


def model_attribute(checker, parent, name, path, node, *, calling=False):
    from seampact.source.dependency_closure import _Instance, _Super, _External, _Class
    if not calling or name != 'model_dump':
        return UNHANDLED
    if type(parent) is _Super and _model(parent.cls) and type(parent.receiver) is _Instance:
        base = parent.cls.bases[0]
        member = checker.class_attribute(base, name) if type(base) is _Class else base
        if type(member) is _External and member.name in {'pydantic.BaseModel', 'pydantic.BaseModel.model_dump'}:
            return ModelDump(parent.receiver)
    if type(parent) is _Instance and _model(parent.local_class):
        member = checker.class_attribute(parent.local_class, name)
        if type(member) is _External and member.name == 'pydantic.BaseModel.model_dump':
            return ModelDump(parent)
    return UNHANDLED


def model_call(checker, function, args, kwargs, path, scope, node):
    if type(function) is not ModelDump:
        return UNHANDLED
    cls = function.receiver.local_class
    plan, row = _plan(checker, cls)
    _target_obligation(checker, plan, path, node)
    checker.protocol(RULE, path, node, target_requirements=plan, operation='original native model_dump')
    from seampact.source.dependency_closure import _Unsupported, _Instance, _LiteralNone
    try:
        if args or any(type(key) is not str for key in kwargs):
            raise PydanticUnknown('original native dump arguments are not fully bound')
        supported = {'mode', 'include', 'exclude', 'exclude_unset', 'exclude_defaults', 'exclude_none', 'by_alias'}
        if any(key not in supported for key in kwargs) or kwargs.get('mode', 'python') != 'python':
            raise PydanticUnknown('original native dump mode/options remain UNKNOWN')
        if 'include' in kwargs and type(kwargs['include']) is not _LiteralNone or kwargs.get('exclude_defaults', False) is not False:
            raise PydanticUnknown('original native dump include/default-comparison semantics remain UNKNOWN')
        if 'by_alias' in kwargs and type(kwargs['by_alias']) is not _LiteralNone and kwargs['by_alias'] is not False:
            raise PydanticUnknown('original native dump alias semantics remain UNKNOWN')
        excluded = set()
        if 'exclude' in kwargs and type(kwargs['exclude']) is not _LiteralNone:
            expressions = [value.value for value in node.keywords if value.arg == 'exclude'] if isinstance(node, ast.Call) else []
            if len(expressions) != 1 or not isinstance(expressions[0], ast.Set) or any(
                    not isinstance(value, ast.Constant) or type(value.value) is not str for value in expressions[0].elts):
                raise PydanticUnknown('original native dump exclusions are not a finite exact source string set')
            excluded = {value.value for value in expressions[0].elts}
        exclude_none, exclude_unset = kwargs.get('exclude_none', False), kwargs.get('exclude_unset', False)
        if type(exclude_none) is not bool or type(exclude_unset) is not bool:
            raise PydanticUnknown('original native dump exclusion switches are UNKNOWN')
        state = getattr(checker, 'configuration_model_instances', {}).get(id(function.receiver))
        if state is None or state[0] is not function.receiver:
            raise PydanticUnknown('original model field-set construction state is UNKNOWN')

        def plain(value):
            if type(value) in (str, int, float, bool, _LiteralNone):
                return value
            if type(value) is list and len(value) <= 1024:
                return [plain(item) for item in value]
            if type(value) is dict and len(value) <= 1024 and all(type(key) is str for key in value):
                return {key: plain(item) for key, item in value.items()}
            # Base/subclass nested serialization depends on declared schema,
            # not simply the instance's own model_dump override.
            raise PydanticUnknown('original native nested/subclass serialization remains UNKNOWN')

        result = {}
        for key, value in function.receiver.attributes.items():
            if key in excluded or exclude_none and type(value) is _LiteralNone or exclude_unset and key not in state[1]:
                continue
            result[key] = plain(value)
        configuration_model_result(checker, result, [function.receiver])
        return result
    except (PydanticUnknown, ValueError, KeyError) as error:
        checker.negative_gap(path, node, str(error))
        return _Unsupported('finite_configuration_native_dump_unknown', path, node)
