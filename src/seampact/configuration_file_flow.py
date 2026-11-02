"""Finite original CLI -> YAML leaf -> service consumer witnesses.

Only scalar leaves of plain block maps are replaced. The original YAML bytes,
loader, argument parser and consumer remain the source of execution semantics.
"""

import ast
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from .configuration_flow import ConfigurationFlow
from .execution_context import process_context
from .resource_flow import External, Target, RESOURCE, _scope_nodes, _writes, _contains


class ConfigurationFileUnsupported(ValueError):
    pass


def plain_yaml_leaves(source):
    """Locate unambiguous one-line leaves without executing YAML constructors."""
    leaves, stack, declared, child_indents = {}, [(-1, ())], set(), {}
    last_indent, last_mapping = -1, True
    for number, line in enumerate(source.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        match = re.fullmatch(r'( *)([A-Za-z_][A-Za-z_0-9]*):(.*)', line)
        if not match or '\t' in line:
            raise ConfigurationFileUnsupported('configuration needs finite plain YAML block mappings')
        indent, key, value = len(match[1]), match[2], match[3].strip()
        if indent > last_indent and not last_mapping:
            raise ConfigurationFileUnsupported('YAML scalar cannot own an indented mapping')
        while stack[-1][0] >= indent:
            stack.pop()
        parent = stack[-1][1]
        if parent in child_indents and child_indents[parent] != indent:
            raise ConfigurationFileUnsupported('YAML mapping indentation is inconsistent')
        child_indents[parent] = indent
        path = stack[-1][1] + (key,)
        if path in declared:
            raise ConfigurationFileUnsupported('duplicate YAML configuration key')
        declared.add(path)
        # Anchors and aliases change scalar identity. A custom scalar tag is
        # admitted only after checking its original environment-only reader.
        tag = None
        tagged = re.fullmatch(r'(![A-Za-z_][A-Za-z_0-9-]*)\s+(.+)', value)
        if tagged:
            tag, value = tagged.groups()
        if re.search(r'(^|\s)[!&*|>]|^%|^---|^\.\.\.', value):
            raise ConfigurationFileUnsupported('YAML tags, aliases or multiline values require another rule')
        if value and not value.startswith('#'):
            if value.startswith(('{', '[')):
                raise ConfigurationFileUnsupported('flow-style YAML containers require another rule')
            leaves[path] = dict(start=number, end=number, indent=indent, key=key)
            if tag:
                leaves[path]['tag'] = tag
        else:
            stack.append((indent, path))
        last_indent, last_mapping = indent, not value or value.startswith('#')
    return leaves


@dataclass
class Parser:
    options: dict = field(default_factory=dict)
    invalid: bool = False


@dataclass
class Namespace:
    values: dict


@dataclass(frozen=True)
class ConfigurationPath:
    pass


@dataclass(frozen=True)
class ConfigurationStream:
    pass


class UrlOrigin(str):
    """A resource marker retaining the original literal URL path transform."""
    def __new__(cls, suffix=''):
        value = super().__new__(cls, RESOURCE)
        value.suffix = suffix
        return value

    def __reduce__(self):
        return (UrlOrigin, (self.suffix,))

    def __eq__(self, other):
        return self.suffix == other.suffix if isinstance(other, UrlOrigin) else super().__eq__(other)

    __hash__ = str.__hash__


class FileFlow(ConfigurationFlow):
    def __init__(self, inspector, entry, mapping, evidence):
        super().__init__(inspector, evidence, mapping['field'], None, entry)
        self.mapping = mapping
        self.loaded, self.suffixes = False, set()
        self.tag_readers = set()
        self.unknown_origin_escape = False
        self.configuration = {}
        current = self.configuration
        for key in mapping['parameter_path'][:-1]:
            current[key] = {}
            current = current[key]
        current[mapping['parameter_path'][-1]] = UrlOrigin() if self.field == 'base_url' else RESOURCE

    def sink(self, function, args, keywords):
        matched = super().sink(function, args, keywords)
        if matched and self.field == 'base_url':
            origins = [v for v in args + list(keywords.values()) if isinstance(v, UrlOrigin)]
            self.suffixes.update(v.suffix for v in origins)
        return matched

    def environment_tag_reader(self, constructor):
        """A local scalar -> optional strip -> getenv reader has no writes."""
        if not isinstance(constructor, Target) or not isinstance(constructor.node, ast.FunctionDef):
            return False
        node = constructor.node
        if node.decorator_list or node.args.defaults or node.args.kwarg or node.args.vararg or node.args.kwonlyargs or len(node.args.args) != 2 or node.args.posonlyargs:
            return False
        loader, scalar = [a.arg for a in node.args.args]
        values, base = set(), None
        if not 2 <= len(node.body) <= 4 or not isinstance(node.body[-1], ast.Return):
            return False
        for statement in node.body[:-1]:
            if not isinstance(statement, ast.Assign) or len(statement.targets) != 1 or not isinstance(statement.targets[0], ast.Name):
                return False
            value = statement.value
            if (base is None and isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute) and
                    isinstance(value.func.value, ast.Name) and value.func.value.id == loader and value.func.attr == 'construct_scalar' and
                    len(value.args) == 1 and isinstance(value.args[0], ast.Name) and value.args[0].id == scalar and not value.keywords):
                base = statement.targets[0].id
            elif not (isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute) and
                    isinstance(value.func.value, ast.Name) and value.func.value.id in values and value.func.attr == 'strip' and
                    len(value.args) <= 1 and all(isinstance(a, ast.Constant) and isinstance(a.value, str) for a in value.args) and not value.keywords):
                return False
            if statement.targets[0].id in values or statement.targets[0].id in {loader, scalar}:
                return False
            values.add(statement.targets[0].id)
        call = node.body[-1].value
        if not isinstance(call, ast.Call) or len(call.args) != 2 or call.keywords or not all(isinstance(a, ast.Name) for a in call.args):
            return False
        function = self.resolve(call.func, {}, constructor)
        proved = isinstance(function, External) and function.name in {'os.getenv', 'os.environ.get'} and call.args[0].id in values and call.args[1].id == base
        if proved:
            self.require(constructor.path, node, 'original_yaml_environment_scalar_constructor')
        return proved

    def resolve(self, expression, env, target):
        if isinstance(expression, ast.Attribute):
            receiver = self.resolve(expression.value, env, target)
            if isinstance(receiver, Namespace):
                return receiver.values.get(expression.attr)
        return super().resolve(expression, env, target)

    def invalidate(self, value):
        if self.field == 'base_url' and _contains(value):
            self.unknown_origin_escape = True
        if isinstance(value, Parser):
            value.invalid = True
        elif isinstance(value, Namespace):
            value.values.clear()
        else:
            super().invalidate(value)

    def clear_destination(self, destination, env, target):
        if isinstance(destination, ast.Attribute):
            receiver = self.resolve(destination.value, env, target)
            if isinstance(receiver, Namespace):
                receiver.values.pop(destination.attr, None)
        super().clear_destination(destination, env, target)

    def expression(self, node, env, target, trail, depth):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add) and self.field == 'base_url':
            left = self.expression(node.left, env, target, trail, depth)
            if isinstance(left, UrlOrigin) and isinstance(node.right, ast.Constant) and isinstance(node.right.value, str):
                if not node.right.value.startswith('/') or not re.fullmatch(r'/[A-Za-z0-9_./-]*', node.right.value):
                    raise ConfigurationFileUnsupported('original URL suffix is outside the finite literal path rule')
                self.require(target.path, node, 'original_url_suffix')
                return UrlOrigin(left.suffix + node.right.value)
            return None
        if not isinstance(node, ast.Call):
            return super().expression(node, env, target, trail, depth)
        function = self.resolve(node.func, env, target)
        name = function.name if isinstance(function, External) else None
        if isinstance(node.func, ast.Attribute) and node.func.attr in {'strip', 'rstrip'}:
            origin = self.expression(node.func.value, env, target, trail, depth)
            if isinstance(origin, UrlOrigin) and len(node.args) <= 1 and all(isinstance(a, ast.Constant) and a.value in {'/', ' '} for a in node.args) and not node.keywords:
                suffix = origin.suffix.rstrip(node.args[0].value if node.args else None)
                return UrlOrigin(suffix)
        if name == 'urllib.request.Request' and self.field == 'base_url':
            url = next((k.value for k in node.keywords if k.arg == 'url'), node.args[0] if node.args else None)
            origin = self.expression(url, env, target, trail, depth) if url is not None else None
            if isinstance(origin, UrlOrigin):
                self.suffixes.add(origin.suffix)
        if name in {'yaml.add_constructor', 'yaml.SafeLoader.add_constructor'}:
            if not node.args or not isinstance(node.args[0], ast.Constant) or not isinstance(node.args[0].value, str) or not node.args[0].value.startswith('!') or node.args[0].value.startswith('!!'):
                raise ConfigurationFileUnsupported('original YAML loader changes standard scalar construction')
            constructor = self.resolve(node.args[1], env, target) if len(node.args) == 2 and not node.keywords else None
            if name == 'yaml.SafeLoader.add_constructor' and self.environment_tag_reader(constructor):
                self.tag_readers.add(node.args[0].value)
                self.require(target.path, node, 'original_yaml_environment_tag_registration')
            else:
                self.tag_readers.discard(node.args[0].value)
                self.require(target.path, node, 'original_unused_yaml_tag_registration')
            return None
        if name in {'yaml.add_implicit_resolver', 'yaml.add_path_resolver', 'yaml.add_multi_constructor',
                    'yaml.SafeLoader.add_implicit_resolver', 'yaml.SafeLoader.add_path_resolver', 'yaml.SafeLoader.add_multi_constructor'}:
            raise ConfigurationFileUnsupported('original YAML loader changes plain scalar interpretation')
        if name == 'argparse.ArgumentParser':
            if node.args or any(k.arg not in {'description', 'epilog', 'prog', 'add_help', 'allow_abbrev'} or not isinstance(k.value, ast.Constant) for k in node.keywords):
                raise ConfigurationFileUnsupported('custom CLI parser construction is unsupported')
            self.require(target.path, node, 'original_parser')
            return Parser()
        receiver = self.resolve(node.func.value, env, target) if isinstance(node.func, ast.Attribute) else None
        if isinstance(receiver, Parser):
            if receiver.invalid:
                return None
            if node.func.attr == 'add_argument':
                options = [a.value for a in node.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]
                if len(options) != len(node.args) or not options or any(not s.startswith('--') for s in options):
                    raise ConfigurationFileUnsupported('configuration injection needs original named single-value CLI options')
                keywords = {k.arg: k.value for k in node.keywords}
                if any(k not in {'help', 'type', 'required', 'default', 'dest', 'metavar', 'choices'} for k in keywords):
                    raise ConfigurationFileUnsupported('variable or custom original CLI action is unsupported')
                if 'type' in keywords and (not isinstance(keywords['type'], ast.Name) or keywords['type'].id != 'str' or _writes(list(_scope_nodes(self.tree(target.path))), 'str')):
                    raise ConfigurationFileUnsupported('configuration CLI converter must preserve the path string')
                if 'dest' in keywords:
                    if not isinstance(keywords['dest'], ast.Constant) or not isinstance(keywords['dest'].value, str):
                        raise ConfigurationFileUnsupported('dynamic CLI destination')
                    parameter = keywords['dest'].value
                else:
                    parameter = max(options, key=len)[2:].replace('-', '_')
                for option in options:
                    if option in receiver.options:
                        raise ConfigurationFileUnsupported('duplicate original CLI option')
                    receiver.options[option] = parameter
                self.require(target.path, node, 'original_cli_parameter')
                return None
            if node.func.attr == 'parse_args' and not node.args and not node.keywords:
                command = self.entry.get('command', [])
                cwd = process_context(self.inspector, self.entry)['cwd']
                if len(command) < 2 or command[0] not in {'python', 'python3'} or (PurePosixPath(cwd) / command[1]).as_posix() != self.entry['path']:
                    raise ConfigurationFileUnsupported('CLI command must invoke the original Python script')
                argv, values = command[2:], {}
                if len(argv) % 2:
                    raise ConfigurationFileUnsupported('CLI options need unique single values')
                for flag, value in zip(argv[::2], argv[1::2]):
                    parameter = receiver.options.get(flag)
                    if parameter is None or parameter in values or not isinstance(value, str) or value.startswith('-'):
                        raise ConfigurationFileUnsupported('dynamic, duplicate or ambiguous original CLI options')
                    if parameter == self.mapping['parameter']:
                        if value != '{' + parameter + '}':
                            raise ConfigurationFileUnsupported('configuration option needs a whole original parameter placeholder')
                        values[parameter] = ConfigurationPath()
                    else:
                        values[parameter] = None
                if self.mapping['parameter'] not in values:
                    raise ConfigurationFileUnsupported('configuration parameter is not passed to the original CLI')
                self.require(target.path, node, 'original_parse_args')
                return Namespace(values)
            receiver.invalid = True
            return None
        if isinstance(node.func, ast.Name) and node.func.id == 'open' and not self.module_member(target.path, 'open'):
            if node.args and self.expression(node.args[0], env, target, trail, depth) == ConfigurationPath():
                if len(node.args) > 2 or len(node.args) == 2 and not (isinstance(node.args[1], ast.Constant) and node.args[1].value in {'r', 'rt'}):
                    raise ConfigurationFileUnsupported('configuration is not opened in original text read mode')
                if any(k.arg != 'encoding' or not isinstance(k.value, ast.Constant) for k in node.keywords):
                    raise ConfigurationFileUnsupported('unsupported original configuration open options')
                self.require(target.path, node, 'original_configuration_open')
                return ConfigurationStream()
        if name == 'yaml.safe_load' and len(node.args) == 1 and not node.keywords:
            if any(changed == 'yaml' or changed.startswith('yaml.SafeLoader') for changed in self.invalid_external):
                raise ConfigurationFileUnsupported('original YAML loader state escapes or is modified')
            if self.expression(node.args[0], env, target, trail, depth) == ConfigurationStream():
                self.require(target.path, node, 'original_configuration_loader')
                self.loaded = True
                # Loading twice gives distinct original objects, not aliases.
                import copy
                return copy.deepcopy(self.configuration)
        return super().expression(node, env, target, trail, depth)

    def statements(self, body, env, target, trail, depth, returns):
        for statement in body:
            destinations = statement.targets if isinstance(statement, (ast.Assign, ast.Delete)) else [statement.target] if isinstance(statement, (ast.AnnAssign, ast.AugAssign)) else []
            for destination in destinations:
                if isinstance(destination, ast.Subscript):
                    receiver = self.resolve(destination.value, env, target)
                    if isinstance(receiver, External) and receiver.name.startswith('yaml.'):
                        raise ConfigurationFileUnsupported('original YAML loader tables are modified')
                if isinstance(destination, ast.Attribute):
                    receiver = self.resolve(destination.value, env, target)
                    if isinstance(receiver, Parser):
                        receiver.invalid = True
                    elif isinstance(receiver, Namespace):
                        receiver.values.pop(destination.attr, None)
                    elif isinstance(receiver, External) and receiver.name.startswith('yaml'):
                        raise ConfigurationFileUnsupported('original YAML loader identity is modified')
            if super().statements([statement], env, target, trail, depth, returns):
                return True


def configuration_file_flow(inspector, entry, mapping, evidence):
    flow = None
    try:
        parameter, keys = mapping.get('parameter'), mapping.get('parameter_path')
        filename = mapping.get('configuration_file')
        if entry.get('kind') != 'cli' or entry.get('unit', 'file') != 'file':
            raise ConfigurationFileUnsupported('configuration files require the original process CLI')
        if not isinstance(parameter, str) or not isinstance(keys, list) or not keys or any(not isinstance(k, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*', k) for k in keys):
            raise ConfigurationFileUnsupported('configuration mapping needs its original CLI parameter and literal key path')
        if not isinstance(filename, str) or '\\' in filename or ':' in filename or PurePosixPath(filename).is_absolute() or '..' in PurePosixPath(filename).parts:
            raise ConfigurationFileUnsupported('configuration file must be frozen repository-relative YAML')
        if (entry.get('fixed_arguments') or {}).get(parameter) != filename:
            raise ConfigurationFileUnsupported('configuration parameter must retain its frozen original YAML path')
        cwd = process_context(inspector, entry)['cwd']
        path = (PurePosixPath(cwd) / filename).as_posix()
        if path not in inspector.files or not path.endswith(('.yaml', '.yml')):
            raise ConfigurationFileUnsupported('original YAML file is absent from the frozen source')
        source = inspector.data(path)
        if isinstance(source, bytes):
            source = source.decode('utf-8')
        leaves = plain_yaml_leaves(source)
        if tuple(keys) not in leaves:
            raise ConfigurationFileUnsupported('mapped YAML field is not an original single-line scalar leaf')
        flow = FileFlow(inspector, entry, mapping, evidence)
        synthetic = ast.Constant(value=None)
        synthetic.lineno, synthetic.end_lineno = 1, max(1, len(source.splitlines()))
        flow.require(path, synthetic, 'original_yaml_configuration')
        tree = flow.tree(entry['path'])
        flow.statements(tree.body, {}, Target(entry['path'], tree), [], 0, [])
        if any(leaf.get('tag') not in flow.tag_readers for leaf in leaves.values() if leaf.get('tag')):
            raise ConfigurationFileUnsupported('YAML tag has no original finite side-effect-free environment scalar reader')
        if len(flow.suffixes) > 1:
            raise ConfigurationFileUnsupported('multiple original URL suffixes cannot share one configuration leaf')
        if any(flow.suffixes) and (flow.unknown_origin_escape or flow.source_gaps):
            raise ConfigurationFileUnsupported('URL transformation cannot preserve an unresolved original resource consumer')
        proved = bool(flow.proof) and flow.loaded and not flow.unresolved and not flow.limit_reached
        return dict(proved=proved, rule='original_cli_yaml_scalar_consumer/1',
            configuration_file=path, parameter_path=keys, value_span=leaves[tuple(keys)],
            original_url_suffix=next(iter(flow.suffixes), ''), trace=flow.requirements,
            missing=flow.unresolved, facts=flow.source_gaps,
            issue_code=None if proved else 'evidence_incomplete' if flow.unresolved else 'configuration_flow_unsupported',
            reason=None if proved else 'selected YAML leaf must reach its original service consumer through the original CLI and loader')
    except (ConfigurationFileUnsupported, ValueError, KeyError, TypeError, SyntaxError, OSError) as error:
        return dict(proved=False, issue_code='configuration_flow_unsupported', reason=str(error),
            missing=flow.unresolved if flow else [], trace=flow.requirements if flow else [], facts=[])
