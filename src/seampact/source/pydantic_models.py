"""Finite original configuration-model declarations, without importing an SDK.

This is a structural source contract. It deliberately does not emulate
Pydantic, execute validators, or turn a model/field name into permission.
The original native schema path and every selected callback remain separate
normal-worker requirements.
"""
import ast
import hashlib
import json
from dataclasses import dataclass

from seampact.source.frozen_inheritance import FrozenInheritance
from seampact.source.import_only import _metadata
from seampact.source.type_references import TypeReferences, _scoped


RULE = 'source-finite-configuration-model/1'
REGION_RULE = 'source-scoped-configuration-model-read-use/1'
_HOOKS = {'__new__', '__init__', '__getattribute__', '__getattr__', '__setattr__',
          '__delattr__', '__init_subclass__', '__pydantic_init_subclass__',
          '__pydantic_on_complete__', '__get_pydantic_core_schema__',
          '__get_pydantic_json_schema__', '__class_getitem__', '__set_name__', 'model_post_init'}
_FIELDS = {'default', 'description', 'title', 'ge', 'gt', 'le', 'lt',
           'min_length', 'max_length', 'discriminator', 'validate_default'}
_CONFIG = {'extra', 'strict', 'arbitrary_types_allowed', 'validate_default'}


class PydanticUnknown(ValueError):
    pass


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    allow_nan=False, separators=(',', ':')).encode()).hexdigest()


def _site(path, node, symbol=None):
    result = dict(path=path, start=node.lineno, end=node.end_lineno,
                  column=node.col_offset, end_column=node.end_col_offset,
                  ast_sha256=hashlib.sha256(ast.dump(node, include_attributes=False).encode()).hexdigest())
    if symbol is not None:
        result['symbol'] = symbol
    return result


def _literal(node):
    if isinstance(node, ast.Constant) and node.value is Ellipsis:
        return {'required': True}
    try:
        return {'value': _metadata(ast.literal_eval(node))}
    except (ValueError, TypeError, SyntaxError, RecursionError) as error:
        raise PydanticUnknown('configuration declaration contains a non-finite ordinary value') from error


@dataclass(frozen=True)
class ModelPlan:
    """Immutable JSON source facts; never retain an actual model or SDK object."""
    _json: str

    def requirements(self):
        return json.loads(self._json)


class _Models:
    def __init__(self, inspector, path):
        self.inspector, self.path = inspector, path
        self.resolver = FrozenInheritance(inspector)
        self.tree = self.resolver.tree(path)
        self.references = TypeReferences(inspector, self.resolver.tree)
        declarations = [node for node in self.tree.body if isinstance(node, ast.ClassDef)]
        self.definitions = {node.name: node for node in declarations}
        if len(self.definitions) != len(declarations):
            raise PydanticUnknown('repeated original model class binding remains UNKNOWN')
        for marker in _scoped(self.tree.body):
            if (isinstance(marker, ast.Name) and isinstance(marker.ctx, (ast.Store, ast.Del))
                    and marker.id in self.definitions or isinstance(marker, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and marker.name in self.definitions):
                raise PydanticUnknown('original module model class binding is replaced or released')
        self.rows, self.active, self.factories = {}, set(), {}

    def factory(self, name):
        if name in self.factories:
            return
        assignments = []
        for node in self.tree.body:
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if any(isinstance(target, ast.Name) and target.id == name for target in targets):
                    assignments.append(node)
        if len(assignments) != 1 or not isinstance(assignments[0].value, ast.Dict):
            return
        node = assignments[0]
        if not any(isinstance(value, ast.Name) and value.id in self.definitions for value in node.value.values):
            return
        members, keys = [], set()
        for key, value in zip(node.value.keys, node.value.values):
            if (not isinstance(key, ast.Constant) or type(key.value) is not str or key.value in keys or
                    not isinstance(value, ast.Name) or value.id not in self.definitions):
                raise PydanticUnknown('referenced model factory is not one finite original string-to-class mapping')
            keys.add(key.value)
            self.model(value.id)
            members.append(dict(key=key.value, model=self.path + ':' + value.id))
        self.factories[name] = dict(source=_site(self.path, node), members=members,
                                    target_status='REQUIRES_EXACT_ORIGINAL_MAPPING_AND_CLASSES')

    def qualified(self, node):
        return self.references.resolve(self.path, node, node)

    def annotation(self, node):
        if isinstance(node, ast.Constant) and node.value is None:
            return {'kind': 'none'}
        if isinstance(node, (ast.Name, ast.Attribute)):
            name = ast.unparse(node)
            qualified = self.qualified(node)
            if isinstance(node, ast.Name) and node.id in self.definitions:
                self.model(node.id)
                return {'kind': 'model', 'identity': self.path + ':' + node.id}
            if name in {'str', 'int', 'float', 'bool', 'list', 'dict'} and qualified == 'builtins.' + name:
                # Actual original builtins and annotation evaluation remain
                # target requirements, including source shadowing checks.
                return {'kind': name, 'reference': 'builtins.' + name}
            if qualified in {'typing.Any', 'typing.List', 'typing.Dict'}:
                return {'kind': qualified.rpartition('.')[2].lower(), 'reference': qualified}
            raise PydanticUnknown('configuration annotation is outside finite scalar/container/model declarations: ' + name)
        if isinstance(node, ast.Subscript):
            name = self.qualified(node.value) or ast.unparse(node.value)
            arguments = node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
            if name == 'typing.Literal':
                return {'kind': 'literal', 'choices': [_literal(value) for value in arguments]}
            if name == 'typing.Annotated':
                if len(arguments) != 2:
                    raise PydanticUnknown('configuration Annotated metadata is outside the finite Field subset')
                return {'kind': 'annotated', 'value': self.annotation(arguments[0]),
                        'field': self.field(arguments[1])}
            arities = {'typing.Optional': (1,), 'typing.Union': tuple(range(1, 17)),
                       'typing.List': (1,), 'typing.Dict': (2,), 'builtins.list': (1,), 'builtins.dict': (2,)}
            if name not in arities or len(arguments) not in arities[name]:
                raise PydanticUnknown('configuration annotation arity or protocol remains UNKNOWN: ' + name)
            return {'kind': name.rpartition('.')[2].lower(),
                    'items': [self.annotation(value) for value in arguments]}
        raise PydanticUnknown('dynamic or postponed configuration annotations remain UNKNOWN')

    def field(self, node):
        if not isinstance(node, ast.Call):
            return {'default': _literal(node), 'options': {}}
        if self.qualified(node.func) != 'pydantic.Field' or len(node.args) > 1:
            raise PydanticUnknown('configuration field declaration is not the original finite Field call')
        options, default = {}, {'required': True}
        if node.args:
            default = _literal(node.args[0])
        for keyword in node.keywords:
            if keyword.arg is None or keyword.arg in options or keyword.arg == 'default' and node.args:
                raise PydanticUnknown('configuration Field has ambiguous argument expansion')
            if keyword.arg == 'default_factory':
                value = keyword.value
                if not isinstance(value, ast.Name) or value.id not in {'list', 'dict'} or self.qualified(value) != 'builtins.' + value.id:
                    raise PydanticUnknown('configuration default factory is not an exact original empty list/dict factory')
                if default != {'required': True} or 'default' in options:
                    raise PydanticUnknown('configuration Field combines a default and default factory')
                default = {'factory': 'builtins.' + value.id}
                options[keyword.arg] = default
            elif keyword.arg in _FIELDS:
                value = _literal(keyword.value)
                if keyword.arg == 'default':
                    if 'default_factory' in options:
                        raise PydanticUnknown('configuration Field combines a default and default factory')
                    default = value
                    options[keyword.arg] = value
                else:
                    options[keyword.arg] = value
            else:
                raise PydanticUnknown('configuration Field option remains UNKNOWN: ' + keyword.arg)
        return {'default': default, 'options': options, 'source': _site(self.path, node)}

    def method(self, owner, node):
        if isinstance(node, ast.AsyncFunctionDef) or node.name in _HOOKS or any(
                isinstance(value, (ast.Await, ast.Yield, ast.YieldFrom)) for value in ast.walk(node)):
            raise PydanticUnknown('custom model allocation/hooks or suspending validators remain UNKNOWN')
        validators, descriptors = [], []
        for decorator in node.decorator_list:
            if isinstance(decorator, ast.Name) and decorator.id in {'classmethod', 'staticmethod'}:
                if self.qualified(decorator) not in {None, 'builtins.' + decorator.id}:
                    raise PydanticUnknown('configuration validator descriptor is shadowed')
                descriptors.append(decorator.id)
                continue
            if not isinstance(decorator, ast.Call):
                raise PydanticUnknown('configuration method has an unknown decorator')
            qualified = self.qualified(decorator.func)
            if qualified not in {'pydantic.field_validator', 'pydantic.model_validator'}:
                raise PydanticUnknown('configuration callback decorator remains UNKNOWN')
            options = {}
            for keyword in decorator.keywords:
                if keyword.arg not in {'mode', 'check_fields'} or keyword.arg in options:
                    raise PydanticUnknown('configuration callback decorator option remains UNKNOWN')
                options[keyword.arg] = _literal(keyword.value)['value']
            mode = options.get('mode', 'after')
            if mode not in {'before', 'after'}:
                raise PydanticUnknown('configuration wrap/plain callback semantics remain UNKNOWN')
            fields = [_literal(value)['value'] for value in decorator.args]
            if qualified.endswith('field_validator') and (not fields or any(type(value) is not str for value in fields)):
                raise PydanticUnknown('configuration field validator targets are not finite original field names')
            if qualified.endswith('model_validator') and fields:
                raise PydanticUnknown('configuration model validator has positional arguments')
            validators.append(dict(kind=qualified.rpartition('.')[2], fields=fields, mode=mode, options=options,
                                   source=_site(self.path, decorator)))
        if len(validators) > 1 or len(descriptors) > 1:
            raise PydanticUnknown('stacked configuration validator/descriptor protocols remain UNKNOWN')
        # Follow original finite factory members, including configurations
        # which an enabled filter later declines to instantiate as detectors.
        for value in ast.walk(node):
            if isinstance(value, ast.Name) and isinstance(value.ctx, ast.Load):
                if value.id in self.definitions and value.id != owner:
                    self.model(value.id)
                else:
                    self.factory(value.id)
        return dict(name=node.name, source=_site(self.path, node, owner + '.' + node.name),
                    decorators=[ast.unparse(value) for value in node.decorator_list],
                    descriptor=descriptors[0] if descriptors else 'function', validators=validators,
                    effects_status='REQUIRES_ORIGINAL_CALLBACK_SOURCE_AND_TARGET_CHECK')

    def model(self, name):
        if name in self.rows:
            return self.rows[name]
        if name in self.active or len(self.active) >= 16 or len(self.rows) >= 64:
            raise PydanticUnknown('configuration model graph is cyclic or exceeds its finite bound')
        node = self.definitions.get(name)
        if node is None or len(node.bases) != 1 or node.decorator_list or node.keywords:
            raise PydanticUnknown('configuration models require one original finite base and no class decorator/metaclass override')
        self.active.add(name)
        try:
            base = node.bases[0]
            if self.qualified(base) == 'pydantic.BaseModel':
                parent, fields, methods, config = 'pydantic.BaseModel', {}, {}, {}
                field_order, method_order = [], []
            elif isinstance(base, ast.Name) and base.id in self.definitions:
                inherited = self.model(base.id)
                parent = inherited['identity']
                fields, methods, config = (dict(inherited[key]) for key in ('fields', 'methods', 'config'))
                field_order, method_order = (list(inherited[key]) for key in ('field_order', 'method_order'))
            else:
                raise PydanticUnknown('configuration base is not the original BaseModel or finite local model')
            declarations = []
            assigned = set()
            for member in node.body:
                if isinstance(member, ast.Expr) and isinstance(member.value, ast.Constant) and type(member.value.value) is str or isinstance(member, ast.Pass):
                    continue
                if isinstance(member, ast.AnnAssign) and isinstance(member.target, ast.Name):
                    key = member.target.id
                    if key in assigned or key.startswith('_'):
                        raise PydanticUnknown('repeated/private model field declaration remains UNKNOWN')
                    assigned.add(key)
                    if key not in fields:
                        field_order.append(key)
                    fields[key] = dict(annotation=self.annotation(member.annotation),
                                       field=self.field(member.value) if member.value is not None else {'default': {'required': True}, 'options': {}},
                                       source=_site(self.path, member))
                    declarations.append({'field': key})
                elif isinstance(member, ast.Assign) and len(member.targets) == 1 and isinstance(member.targets[0], ast.Name) and member.targets[0].id == 'model_config':
                    if 'model_config' in assigned or not isinstance(member.value, ast.Call) or self.qualified(member.value.func) != 'pydantic.ConfigDict' or member.value.args:
                        raise PydanticUnknown('configuration settings are not one original finite ConfigDict')
                    assigned.add('model_config')
                    options_seen = set()
                    for keyword in member.value.keywords:
                        if keyword.arg not in _CONFIG or keyword.arg in options_seen:
                            raise PydanticUnknown('configuration setting remains UNKNOWN: ' + str(keyword.arg))
                        options_seen.add(keyword.arg)
                        config[keyword.arg] = _literal(keyword.value)['value']
                    declarations.append({'config': _site(self.path, member)})
                elif isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if member.name in assigned:
                        raise PydanticUnknown('repeated model method/field binding remains UNKNOWN')
                    assigned.add(member.name)
                    if member.name in methods and methods[member.name]['validators']:
                        raise PydanticUnknown('overridden inherited validator binding requires separate original decorator-registry proof')
                    if member.name not in methods:
                        method_order.append(member.name)
                    methods[member.name] = self.method(name, member)
                    declarations.append({'method': member.name})
                else:
                    raise PydanticUnknown('configuration class definition contains an unproved executable statement')
            if len(fields) > 128:
                raise PydanticUnknown('configuration field bound exceeds 128')
            row = dict(identity=self.path + ':' + name, symbol=name, base=parent,
                       source=_site(self.path, node, name), fields=fields, methods=methods, config=config,
                       declaration_order=declarations, field_order=field_order, method_order=method_order)
            self.rows[name] = row
            return row
        finally:
            self.active.remove(name)


def compile_model_plan(inspector, path, symbol):
    """Read the original finite model and inherited declarations in source order.

    The plan records all validators, including ones whose fields are absent;
    the SDK's original dispatch decides whether they run. No enabled filter,
    validation result or serialization result is manufactured here.
    """
    if type(path) is not str or type(symbol) is not str or path not in inspector.files:
        raise PydanticUnknown('configuration model source anchor is absent')
    models = _Models(inspector, path)
    root = models.model(symbol)
    body = dict(schema=RULE, source_version=inspector.request['source_version'],
                root=root['identity'], file_sha256=inspector.files[path]['sha256'],
                models=sorted(models.rows.values(), key=lambda row: row['source']['start']),
                factories=models.factories, source_status='STRUCTURE_CHECKED',
                target_status='REQUIRES_NORMAL_WORKER_CHECK', execution_permission=False,
                required=['complete original SDK import/definition and model schema construction effects',
                          'original ordered field/default/callback/union/serialization semantics',
                          'original no-plugin discovery/cache path before any model creation',
                          'same-lock original model/schema/native-target creation association and read/use',
                          'actual callback identities, complete synchronous effects and finite actual values'],
                lifecycle_permissions=[])
    body['digest'] = _digest(body)
    return ModelPlan(json.dumps(body, sort_keys=True, ensure_ascii=False, allow_nan=False))
