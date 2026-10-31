"""Source-only proof for a final original CLI/YACS resource override.

No source or YAML is executed. Earlier finite YAML merges are retained as
ordered facts; the selected value is established by the original final
merge_from_list, rather than by evaluating a YAML document on the platform.
"""

import ast
import re
from pathlib import PurePosixPath

from .inspection import units
from .execution_context import process_context
from .resource_flow import _scope_nodes, _writes


class Unsupported(ValueError):
    pass


class OverrideProof:
    def __init__(self, inspector, entry, mapping, evidence):
        self.inspector, self.entry, self.mapping, self.evidence = inspector, entry, mapping, evidence
        self.trees, self.trace, self.missing, self.facts = {}, [], [], []
        self.resolving = set()

    def source(self, path):
        if path not in self.inspector.files:
            raise Unsupported('configuration source is outside the frozen source scope: ' + path)
        return dict(units(path, self.inspector.data(path)))['file']

    def tree(self, path):
        if path not in self.trees:
            self.trees[path] = ast.parse(self.source(path))
        return self.trees[path]

    def cite(self, path, node, kind):
        row = dict(path=path, unit='file', start=node.lineno, end=node.end_lineno, kind=kind)
        if row not in self.trace:
            self.trace.append(row)
        if not any(e['path'] == path and e.get('unit', 'file') == 'file' and
                   e['start'] <= node.lineno and node.end_lineno <= e['end'] for e in self.evidence):
            if row not in self.missing:
                self.missing.append(row)

    def module(self, path, module, level=0):
        parent = PurePosixPath(path).parent
        if level:
            for _ in range(level - 1):
                parent = parent.parent
            choices = [parent / module.replace('.', '/')]
        else:
            choices = [PurePosixPath(self.entry['path']).parent / module.replace('.', '/'),
                       PurePosixPath(module.replace('.', '/'))]
        found = set()
        for base in choices:
            found.update(p for p in (base.as_posix() + '.py', (base / '__init__.py').as_posix())
                         if p in self.inspector.files)
        if len(found) > 1:
            raise Unsupported('ambiguous original configuration import')
        return next(iter(found), None)

    def resolve(self, path, name):
        key = path, name
        if key in self.resolving:
            raise Unsupported('cyclic original configuration alias')
        tree = self.tree(path)
        if _writes(list(_scope_nodes(tree)), name) != 1:
            raise Unsupported('configuration identity is reassigned or not unique: ' + name)
        self.resolving.add(key)
        try:
            for node in tree.body:
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    for alias in node.names:
                        if (alias.asname or alias.name.split('.')[0]) != name:
                            continue
                        if any(isinstance(n, (ast.Attribute, ast.Subscript)) and isinstance(n.ctx, (ast.Store, ast.Del)) and
                               (self.text(n).startswith(name + '.') or self.text(n).startswith(name + '[')) for n in _scope_nodes(tree)):
                            raise Unsupported('imported configuration or loader identity is modified')
                        external_module = not self.module(path, node.module or '', node.level) if isinstance(node, ast.ImportFrom) else not self.module(path, alias.name)
                        if external_module and any(isinstance(n, ast.Call) and any(
                                isinstance(v, (ast.Name, ast.Attribute)) and self.text(v) == name
                                for v in list(n.args) + [k.value for k in n.keywords])
                               for n in _scope_nodes(tree)):
                            raise Unsupported('imported configuration or loader identity escapes through a call')
                        self.cite(path, node, 'original_import')
                        if isinstance(node, ast.ImportFrom):
                            target = self.module(path, node.module or '', node.level)
                            if target:
                                return self.resolve(target, alias.name)
                            if node.level:
                                raise Unsupported('original configuration module is unavailable')
                            return ('external', (node.module or '') + '.' + alias.name)
                        target = self.module(path, alias.name)
                        return ('module', target) if target else ('external', alias.name)
                if isinstance(node, ast.FunctionDef) and node.name == name:
                    self.cite(path, node, 'configuration_function')
                    return ('function', path, node)
                if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) and node.targets[0].id == name:
                    self.cite(path, node, 'configuration_assignment')
                    if isinstance(node.value, ast.Name):
                        return self.resolve(path, node.value.id)
                    if isinstance(node.value, ast.Call) and self.external(path, node.value.func) == 'yacs.config.CfgNode':
                        if node.value.args or any(k.arg != 'new_allowed' or not isinstance(k.value, ast.Constant) for k in node.value.keywords):
                            raise Unsupported('unsupported original CfgNode construction')
                        return ('configuration', path, name, node)
            raise Unsupported('original configuration identity is unavailable: ' + name)
        finally:
            self.resolving.remove(key)

    def external(self, path, node):
        if isinstance(node, ast.Name):
            result = self.resolve(path, node.id)
            return result[1] if result[0] == 'external' else None
        if isinstance(node, ast.Attribute):
            root = self.external(path, node.value)
            return root + '.' + node.attr if root else None
        return None

    @staticmethod
    def text(node):
        return ast.unparse(node)

    def references(self, node, names, prefixes=()):
        # Attribute chains are values: reading a scalar leaf does not export
        # its mutable parent. Containers can still export the parent itself.
        if isinstance(node, (ast.Name, ast.Attribute)):
            text = self.text(node)
            return text in names or any(text.startswith(prefix) for prefix in prefixes)
        return any(self.references(child, names, prefixes) for child in ast.iter_child_nodes(node))

    def parser(self, path, key, placeholder, opts):
        tree = self.tree(path)
        parses = [n for n in tree.body if isinstance(n, ast.Assign) and len(n.targets) == 1 and
                  isinstance(n.targets[0], ast.Name) and isinstance(n.value, ast.Call) and
                  isinstance(n.value.func, ast.Attribute) and n.value.func.attr == 'parse_args']
        if len(parses) != 1:
            raise Unsupported('CLI needs one original top-level parse_args call')
        parsed = parses[0]
        if parsed.value.args or parsed.value.keywords or not isinstance(parsed.value.func.value, ast.Name):
            raise Unsupported('dynamic original CLI parsing is unsupported')
        parser_name, args_name = parsed.value.func.value.id, parsed.targets[0].id
        if _writes(list(_scope_nodes(tree)), args_name) != 1:
            raise Unsupported('parsed original CLI namespace is reassigned')
        constructors = [n for n in tree.body if isinstance(n, ast.Assign) and len(n.targets) == 1 and
                        isinstance(n.targets[0], ast.Name) and n.targets[0].id == parser_name]
        if len(constructors) != 1 or _writes(list(_scope_nodes(tree)), parser_name) != 1:
            raise Unsupported('original CLI parser identity is not unique')
        constructor = constructors[0]
        if not isinstance(constructor.value, ast.Call) or self.external(path, constructor.value.func) != 'argparse.ArgumentParser':
            raise Unsupported('CLI is not an original argparse parser')
        if constructor.lineno >= parsed.lineno or constructor.value.args or any(
                k.arg not in {'description', 'epilog', 'prog', 'add_help', 'allow_abbrev'} or
                not isinstance(k.value, ast.Constant) for k in constructor.value.keywords):
            raise Unsupported('custom original argparse construction is unsupported')
        for n in _scope_nodes(tree):
            if isinstance(n, (ast.Assign, ast.AnnAssign, ast.NamedExpr)) and n.value is not None and self.references(n.value, {parser_name}):
                raise Unsupported('original parser escapes through an alias')
            if isinstance(n, ast.Call) and any(self.references(v, {parser_name}) for v in list(n.args) + [k.value for k in n.keywords]):
                raise Unsupported('original parser escapes through a call')
            if isinstance(n, (ast.Attribute, ast.Subscript)) and isinstance(n.ctx, (ast.Store, ast.Del)) and (
                    self.text(n).startswith(parser_name + '.') or self.text(n).startswith(parser_name + '[')):
                raise Unsupported('original parser methods are modified')
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and self.text(n.func.value) == parser_name:
                declarations = [s.value for s in tree.body if isinstance(s, ast.Expr) and isinstance(s.value, ast.Call)
                                and constructor.end_lineno < s.lineno < parsed.lineno]
                if n is not parsed.value and (n.func.attr != 'add_argument' or n not in declarations):
                    raise Unsupported('nested or dynamic original parser mutation')
            if isinstance(n, ast.Attribute) and self.text(n.value) == parser_name:
                if not any(isinstance(call, ast.Call) and call.func is n for call in _scope_nodes(tree)):
                    raise Unsupported('original parser attribute escapes its declarations')
        self.cite(path, constructor, 'original_parser')
        self.cite(path, parsed, 'original_parse_args')
        options, defaults, remainder = {}, {}, []
        for statement in tree.body:
            if not isinstance(statement, ast.Expr) or not isinstance(statement.value, ast.Call):
                continue
            call = statement.value
            if not isinstance(call.func, ast.Attribute) or self.text(call.func.value) != parser_name:
                continue
            if call.func.attr != 'add_argument' or statement.lineno > parsed.lineno:
                raise Unsupported('original parser is mutated outside finite add_argument declarations')
            self.cite(path, statement, 'original_cli_parameter')
            flags = [ast.literal_eval(n) for n in call.args]
            if not flags or any(not isinstance(f, str) for f in flags):
                raise Unsupported('dynamic original CLI options')
            kw = {k.arg: k.value for k in call.keywords}
            if None in kw or any(k not in {'help', 'default', 'nargs', 'type', 'action', 'dest', 'required', 'choices', 'metavar'} for k in kw):
                raise Unsupported('unsupported original argparse behavior')
            dest = ast.literal_eval(kw['dest']) if 'dest' in kw else max(flags, key=len).lstrip('-').replace('-', '_')
            if 'nargs' in kw and self.external(path, kw['nargs']) == 'argparse.REMAINDER':
                if flags != [opts] or dest != opts or any(k not in {'help', 'default', 'nargs', 'type', 'action', 'metavar'} for k in kw):
                    raise Unsupported('only the declared original remainder parameter is supported')
                if ('type' in kw and (self.text(kw['type']) != 'str' or _writes(list(_scope_nodes(tree)), 'str')) or (
                        'action' in kw and ast.literal_eval(kw['action']) != 'store')):
                    raise Unsupported('original remainder must preserve the literal CLI strings')
                remainder.append(statement)
                continue
            if any(not f.startswith('-') for f in flags) or 'nargs' in kw:
                raise Unsupported('other positional or variable-length CLI arguments are unsupported')
            if 'type' in kw and (self.text(kw['type']) not in {'str', 'int', 'float'} or _writes(list(_scope_nodes(tree)), self.text(kw['type']))):
                raise Unsupported('custom CLI converters are unsupported')
            action = ast.literal_eval(kw['action']) if 'action' in kw else 'store'
            if action not in {'store', 'store_true', 'store_false'}:
                raise Unsupported('custom CLI actions are unsupported')
            if 'default' in kw:
                defaults[dest] = ast.literal_eval(kw['default'])
            for flag in flags:
                if flag in options:
                    raise Unsupported('ambiguous CLI option')
                options[flag] = (dest, action)
        if len(remainder) != 1:
            raise Unsupported('original CLI needs one argparse.REMAINDER parameter')
        command = self.entry.get('command', [])
        indices = [i for i, token in enumerate(command[:2]) if token == path or token == './' + path]
        if len(indices) != 1:
            raise Unsupported('CLI command does not invoke the frozen original script')
        argv = command[indices[0] + 1:]
        if argv[-2:] != [key, '{' + placeholder + '}'] or argv.count(key) != 1 or argv.count('{' + placeholder + '}') != 1:
            raise Unsupported('resource override must be the final unique literal-key/whole-placeholder pair')
        offset = 0
        while offset < len(argv) - 2:
            token = argv[offset]
            if token not in options:
                raise Unsupported('dynamic or conflicting remainder options are unsupported')
            dest, action = options[token]
            offset += 1
            if action == 'store':
                if offset >= len(argv) - 2:
                    raise Unsupported('CLI option lacks its single value')
                value = argv[offset]
                # Whole task placeholders are confined to known single-value options.
                # They can never become a successful extra remainder key/value pair.
                if value.startswith('-') and value not in {'-1'}:
                    raise Unsupported('ambiguous CLI option value')
                defaults[dest] = value
                offset += 1
            else:
                defaults[dest] = action == 'store_true'
        return args_name, defaults, parsed

    def static_string(self, node, args_name, values):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.JoinedStr):
            result = ''
            for part in node.values:
                if isinstance(part, ast.Constant) and isinstance(part.value, str):
                    result += part.value
                elif isinstance(part, ast.FormattedValue) and part.conversion == -1 and part.format_spec is None and isinstance(part.value, ast.Attribute) and self.text(part.value.value) == args_name:
                    value = values.get(part.value.attr)
                    if not isinstance(value, str) or re.search(r'[{}]', value):
                        raise Unsupported('configuration filename depends on a dynamic CLI value')
                    result += value
                else:
                    raise Unsupported('dynamic configuration filename')
            return result
        raise Unsupported('dynamic configuration filename')

    def yaml(self, filename, path, node):
        cwd = process_context(self.inspector, self.entry)['cwd']
        target = PurePosixPath(cwd) / filename
        if target.is_absolute() or '..' in target.parts or '\\' in filename:
            raise Unsupported('configuration file must remain in its original frozen directory')
        target = target.as_posix()
        source = self.source(target)
        # Only ordinary block mappings/sequences and scalar/list values. YAML
        # tags, anchors, directives and multiline values need a separate rule.
        for line in source.splitlines():
            code = line.strip()
            if not code or code.startswith('#'):
                continue
            if not re.match(r'(?:[A-Za-z_][A-Za-z_0-9]*\s*:|-[ \t]+)', code) or re.search(r'(^|\s)[!&*|>]|\t|^%|^---|^\.\.\.', line):
                raise Unsupported('YAML is outside the finite plain block configuration rule')
        fake = ast.Constant(value=None)
        fake.lineno, fake.end_lineno = 1, max(1, len(source.splitlines()))
        self.cite(target, fake, 'original_yaml_configuration')
        self.facts.append(dict(kind='configuration_merge', method='merge_from_file', path=path,
                               start=node.lineno, end=node.end_lineno, configuration_path=target,
                               selected_value='SHADOWED_BY_FINAL_CLI_OVERRIDE'))

    def update(self, path, node, key, opts, cli_values):
        if node.decorator_list or node.args.defaults or node.args.posonlyargs or node.args.vararg or node.args.kwarg or node.args.kwonlyargs or len(node.args.args) != 2:
            raise Unsupported('configuration updater needs two original explicit parameters')
        cfg, args = [a.arg for a in node.args.args]
        all_nodes = list(_scope_nodes(node))
        if _writes(all_nodes, cfg) or _writes(all_nodes, args):
            raise Unsupported('configuration updater parameters are reassigned')
        final = []
        for index, statement in enumerate(node.body):
            candidate = statement
            if isinstance(statement, ast.If) and self.text(statement.test) == args + '.' + opts and not statement.orelse and len(statement.body) == 1:
                candidate = statement.body[0]
            if isinstance(candidate, ast.Expr) and isinstance(candidate.value, ast.Call):
                call = candidate.value
                if self.text(call.func) == cfg + '.merge_from_list' and len(call.args) == 1 and not call.keywords and self.text(call.args[0]) == args + '.' + opts:
                    final.append(index)
        if len(final) != 1:
            raise Unsupported('original final remainder merge is not unique and unconditional for nonempty opts')
        boundary = final[0]
        if not node.body or self.text(node.body[0]) != cfg + '.defrost()':
            raise Unsupported('original updater must defrost before merging')
        if [self.text(s) for s in node.body[boundary + 1:]] != [cfg + '.freeze()']:
            raise Unsupported('configuration is changed or escapes after the CLI override')
        def prelude(statements):
            for statement in statements:
                if isinstance(statement, ast.If):
                    # Earlier conditionals are overwritten by the final merge.
                    # Their bodies still must be finite original merges.
                    if any(isinstance(n, ast.Call) for n in ast.walk(statement.test)):
                        raise Unsupported('dynamic pre-override condition')
                    prelude(statement.body)
                    prelude(statement.orelse)
                elif isinstance(statement, ast.Try):
                    if statement.finalbody or statement.orelse:
                        raise Unsupported('unsupported configuration try/finally')
                    prelude(statement.body)
                    for handler in statement.handlers:
                        if any(not isinstance(s, ast.Pass) for s in handler.body):
                            raise Unsupported('unsupported configuration exception branch')
                elif isinstance(statement, ast.Pass):
                    continue
                elif isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call):
                    call = statement.value
                    if call.keywords:
                        raise Unsupported('dynamic configuration merge keywords')
                    name = self.text(call.func)
                    if name == cfg + '.defrost' and not call.args:
                        continue
                    if name == cfg + '.merge_from_file' and len(call.args) == 1:
                        self.yaml(self.static_string(call.args[0], args, cli_values), path, call)
                    elif name == cfg + '.merge_from_list' and len(call.args) == 1 and isinstance(call.args[0], (ast.List, ast.Tuple)):
                        pairs = call.args[0].elts
                        if len(pairs) % 2 or any(not isinstance(k, ast.Constant) or not isinstance(k.value, str) for k in pairs[::2]):
                            raise Unsupported('dynamic pre-override configuration keys')
                        for value in pairs[1::2]:
                            if any(isinstance(n, ast.Call) and not (isinstance(n.func, ast.Name) and n.func.id == 'tuple' and len(n.args) == 1 and isinstance(n.args[0], ast.Attribute) and self.text(n.args[0].value) == args and not n.keywords) for n in ast.walk(value)):
                                raise Unsupported('dynamic pre-override configuration value')
                        if any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == 'tuple' for value in pairs[1::2] for n in ast.walk(value)) and _writes(list(_scope_nodes(self.tree(path))), 'tuple'):
                            raise Unsupported('original tuple converter is shadowed')
                        self.facts.append(dict(kind='configuration_merge', method='merge_from_list', path=path,
                            start=call.lineno, end=call.end_lineno, keys=[k.value for k in pairs[::2]], selected_value='SHADOWED_BY_FINAL_CLI_OVERRIDE'))
                    else:
                        raise Unsupported('unsupported call before the final configuration override')
                else:
                    raise Unsupported('unsupported statement before the final configuration override')
        prelude(node.body[:boundary])
        self.facts.append(dict(kind='configuration_merge', method='merge_from_list', path=path,
            start=node.body[boundary].lineno, end=node.body[boundary].end_lineno,
            configuration_key=key, selected_value='SELECTED_WEIGHT_PATH', source='original_cli_remainder'))

    def run(self):
        mapping, entry = self.mapping, self.entry
        key, override = mapping.get('configuration_key'), mapping.get('cli_override')
        if entry.get('kind') != 'cli' or mapping.get('location') != 'entry.fixed_arguments' or mapping.get('field') != 'path' or mapping.get('parameter_path'):
            raise Unsupported('configuration overrides support only original CLI weight placeholders')
        if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+', key) or not isinstance(override, dict) or set(override) != {'parameter'} or not isinstance(override['parameter'], str) or not override['parameter'].isidentifier():
            raise Unsupported('configuration override needs a dotted original key and remainder parameter')
        # CfgNode uses normal Python attribute lookup before its mapping keys.
        # Such names cannot establish the dotted field assumed by this rule.
        reserved = {'clear', 'copy', 'fromkeys', 'get', 'items', 'keys', 'pop', 'popitem', 'setdefault', 'update', 'values',
                    'clone', 'dump', 'freeze', 'defrost', 'is_frozen', 'is_new_allowed', 'set_new_allowed',
                    'merge_from_file', 'merge_from_list', 'merge_from_other_cfg', 'load_cfg',
                    'register_deprecated_key', 'register_renamed_key', 'key_is_deprecated', 'key_is_renamed', 'raise_key_rename_error'}
        if any(part.startswith('_') or part in reserved for part in key.split('.')):
            raise Unsupported('configuration key collides with original CfgNode attributes')
        path, opts = entry['path'], override['parameter']
        args, values, parsed = self.parser(path, key, mapping['parameter'], opts)
        tree = self.tree(path)
        updates = []
        for statement in tree.body:
            if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call):
                call = statement.value
                if len(call.args) == 2 and not call.keywords and isinstance(call.args[0], ast.Name) and isinstance(call.args[1], ast.Name) and call.args[1].id == args:
                    target = self.resolve(path, call.func.id) if isinstance(call.func, ast.Name) else None
                    if target and target[0] == 'function':
                        updates.append((statement, call.args[0].id, target))
        if len(updates) != 1 or updates[0][0].lineno <= parsed.end_lineno:
            raise Unsupported('original CLI needs one configuration update after its parser')
        update, cfg, target = updates[0]
        origin = self.resolve(path, cfg)
        if origin[0] != 'configuration':
            raise Unsupported('configuration object is not an original yacs.config.CfgNode')
        cfg_path, cfg_name = origin[1:3]
        definitions = [n for n in self.tree(cfg_path).body if isinstance(n, ast.Assign) and len(n.targets) == 1 and self.text(n.targets[0]) == cfg_name + '.' + key]
        if len(definitions) != 1 or not isinstance(definitions[0].value, ast.Constant) or not isinstance(definitions[0].value.value, str):
            raise Unsupported('original configuration key lacks one string schema declaration')
        self.cite(cfg_path, definitions[0], 'original_configuration_key')
        # Every parent must be a genuine CfgNode too, with no field alias writes.
        declarations = [origin[3], definitions[0]]
        for length in range(1, len(key.split('.'))):
            parent = cfg_name + '.' + '.'.join(key.split('.')[:length])
            parents = [n for n in self.tree(cfg_path).body if isinstance(n, ast.Assign) and len(n.targets) == 1 and self.text(n.targets[0]) == parent]
            if len(parents) != 1 or not isinstance(parents[0].value, ast.Call) or self.external(cfg_path, parents[0].value.func) != 'yacs.config.CfgNode':
                raise Unsupported('configuration key parent is not a unique original CfgNode')
            if parents[0].value.args or any(k.arg != 'new_allowed' or not isinstance(k.value, ast.Constant)
                                           for k in parents[0].value.keywords):
                raise Unsupported('unsupported original CfgNode parent construction')
            declarations.append(parents[0])
            self.cite(cfg_path, parents[0], 'original_configuration_parent')
        origin_field = cfg_name + '.' + key
        origin_objects = {cfg_name} | {cfg_name + '.' + '.'.join(key.split('.')[:i]) for i in range(1, len(key.split('.')))}
        protected = {cfg_name + '.' + '.'.join(key.split('.')[:i]) for i in range(1, len(key.split('.')) + 1)}
        protected.update(cfg_name + '.' + name for name in reserved)
        for n in _scope_nodes(self.tree(cfg_path)):
            if isinstance(n, (ast.Attribute, ast.Subscript)) and isinstance(n.ctx, (ast.Store, ast.Del)):
                if (self.text(n) in protected or self.text(n).startswith(cfg_name + '.__') or
                        self.text(n).startswith(cfg_name + '[')) and not any(n is d.targets[0] for d in declarations):
                    raise Unsupported('original configuration schema or methods are modified')
            if isinstance(n, (ast.Assign, ast.AnnAssign, ast.NamedExpr)) and n.value is not None and self.references(n.value, origin_objects):
                raise Unsupported('original CfgNode has a mutable module alias')
            if isinstance(n, ast.Call) and any(self.references(v, origin_objects) for v in list(n.args) + [k.value for k in n.keywords]):
                raise Unsupported('original CfgNode escapes before its updater')
        self.cite(path, update, 'original_configuration_update')
        self.update(target[1], target[2], key, opts, values)
        consumers = []
        imported_loaders = {a.asname or a.name for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == 'torch' and not n.level for a in n.names if a.name == 'load'}
        for statement in tree.body:
            if statement.lineno <= update.end_lineno:
                continue
            for call in ast.walk(statement):
                if isinstance(call, ast.Call) and call.args and (isinstance(call.func, ast.Attribute) and call.func.attr == 'load' or isinstance(call.func, ast.Name) and call.func.id in imported_loaders):
                    try:
                        loader = self.external(path, call.func)
                    except Unsupported:
                        loader = None
                    if loader == 'torch.load':
                        consumers.append((statement, call))
        if len(consumers) != 1:
            raise Unsupported('configuration resource needs one original torch.load consumer')
        statement, loader = consumers[0]
        if not isinstance(statement, (ast.Assign, ast.AnnAssign, ast.Expr)) or statement.value is not loader:
            raise Unsupported('original loader must be the complete top-level value expression')
        # The original device conversion is finite and needed by TruFor. Other
        # nested calls or computed expressions in loader arguments can change
        # the path/namespace while Python evaluates this same statement.
        for value in loader.args[1:] + [k.value for k in loader.keywords]:
            if isinstance(value, (ast.Constant, ast.Name)):
                continue
            if (isinstance(value, ast.Call) and self.external(path, value.func) == 'torch.device' and
                    len(value.args) == 1 and not value.keywords and isinstance(value.args[0], (ast.Constant, ast.Name))):
                continue
            raise Unsupported('computed original loader options are outside the finite configuration rule')
        if any(k.arg is None for k in loader.keywords):
            raise Unsupported('dynamic original loader keyword expansion')
        field = cfg + '.' + key
        mutable_names = {cfg, args} | {cfg + '.' + '.'.join(key.split('.')[:i]) for i in range(1, len(key.split('.')))}
        loaded = self.text(loader.args[0])
        before = [s for s in tree.body if update.end_lineno < s.lineno < statement.lineno]
        if loaded != field:
            aliases = [s for s in before if isinstance(s, ast.Assign) and len(s.targets) == 1 and self.text(s.targets[0]) == loaded and self.text(s.value) == field]
            guards = [s for s in before if isinstance(s, ast.If) and self.text(s.test) == field and len(s.body) == 1 and isinstance(s.body[0], ast.Assign) and len(s.body[0].targets) == 1 and self.text(s.body[0].targets[0]) == loaded and self.text(s.body[0].value) == field and s.orelse and all(isinstance(x, ast.Raise) for x in s.orelse)]
            if len(aliases) + len(guards) != 1 or not isinstance(loader.args[0], ast.Name) or _writes(list(_scope_nodes(tree)), loaded) != 1:
                raise Unsupported('configuration field does not reach the original loader by a unique guarded or direct alias')
            self.cite(path, (aliases + guards)[0], 'original_configuration_to_loader')
        for s in [s for s in tree.body if s.lineno <= statement.lineno and s is not update]:
            # Any original namespace write or mutable configuration escape before
            # the consumer invalidates the proof, even before update_config.
            for n in _scope_nodes(ast.Module(body=[s], type_ignores=[])):
                if isinstance(n, (ast.Assign, ast.AnnAssign, ast.NamedExpr)) and n.value is not None and self.references(n.value, mutable_names):
                    raise Unsupported('mutable configuration or namespace alias escapes the finite chain')
                def mutable(value):
                    return self.references(value, mutable_names, (args + '.',))
                if isinstance(n, ast.Call) and (self.text(n.func).startswith(args + '.') or self.text(n.func).startswith(cfg + '.') or any(mutable(a) for a in n.args) or any(mutable(k.value) for k in n.keywords)):
                    raise Unsupported('configuration or parsed namespace escapes before its loader')
                if isinstance(n, (ast.Attribute, ast.Subscript)) and isinstance(n.ctx, (ast.Store, ast.Del)) and (self.text(n).startswith(args + '.') or self.text(n).startswith(args + '[')):
                    raise Unsupported('original parsed CLI namespace is modified')
        for s in before + [statement]:
            for n in ast.walk(s):
                if isinstance(n, (ast.Attribute, ast.Subscript)) and isinstance(n.ctx, (ast.Store, ast.Del)) and self.text(n).startswith(cfg + '.'):
                    raise Unsupported('configuration is modified after its final CLI override')
                if isinstance(n, ast.Call) and (self.text(n.func).startswith(cfg + '.') or any(self.text(a) == cfg for a in n.args) or any(self.text(k.value) == cfg for k in n.keywords)):
                    raise Unsupported('configuration escapes after its final CLI override')
                if isinstance(n, ast.Attribute) and isinstance(n.ctx, (ast.Store, ast.Del)) and self.text(n.value) == args:
                    raise Unsupported('original parsed CLI namespace is modified')
        self.cite(path, loader, 'original_weight_loader')
        return dict(proved=not self.missing, rule='original_cli_final_yacs_override/1',
                    issue_code='evidence_incomplete' if self.missing else None,
                    missing=self.missing, trace=self.trace, facts=self.facts,
                    configuration_key=key, selected_value='SELECTED_WEIGHT_PATH')


def configuration_override_flow(inspector, entry, mapping, evidence):
    proof = OverrideProof(inspector, entry, mapping, evidence)
    try:
        return proof.run()
    except (Unsupported, ValueError, TypeError, KeyError, SyntaxError, OSError) as error:
        return dict(proved=False, issue_code='evidence_incomplete' if proof.missing else 'configuration_flow_unsupported',
                    reason=str(error), missing=proof.missing, trace=proof.trace, facts=proof.facts)
