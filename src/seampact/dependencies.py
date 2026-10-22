"""Source-anchored resource requirements and finite original-parameter bindings."""

import ast
import copy
import hashlib
import json
import os
import re
from pathlib import Path, PurePosixPath, PureWindowsPath

from .inspection import units
from .store import now

DEPENDENCY_RULES = """
资源配置由用户保存，依赖需求和原参数映射必须由你从原源码提出。预处理工件和最终候选必须提供dependencies数组，逐项枚举已发现的原必需及可选外部服务、权重和配套模型资源；已有用户字面配置、原签名默认值、固定路径或尚未提供资源，均不是省略需求的理由。只在已检查的目标及原初始化范围确实没有发现此类需求时填[]，并在missing说明仍未覆盖的范围；[]本身不证明资源发现完整。
资源发现与可执行映射分别提交。已发现需求即使没有当前有限规则支持的映射，也必须保留其源码证据、受影响entry_ids、原读取时点和owner，保留未核对/空mappings及具体missing，由检查器返回MISSING；不得为使入口READY而删除需求、改用用户字面配置绕过，或用无关辅助入口代替原目标。不要编造参数、引用或加载依据来消除MISSING。
沿原目标的构造、初始化及请求路径检查依赖，区分基座、adapter、processor/checkpoint等实际资源及其原配置关系，不把其中一项代替整组。required按原owner实际的无条件加载或分支条件填写；拆分不能省略原初始化必需工作。最终候选应保留预处理发现且仍影响所选入口的需求；因入口范围改变而不再适用的需求须在missing说明源码依据，不能静默丢弃。资源未发现完整、映射未覆盖及资源尚未配置要分别说明。
dependency格式：{id:"ASCII需求标识",kind:"service或weight",description:"实际用途",configurable:true或false,required:true或false,entry_ids:["依赖它的候选entry.id"],read_time:"constructor或call或process_start",owner_scope:"原对象/模块/请求作用域说明",evidence:[{path,unit:"file",start,end}],mappings:[{entry_id,location:"constructor.arguments或entry.fixed_arguments或initializers.原方法或binding_program.arguments或environment",parameter:"原真实参数/变量",field:"base_url或model或credential或path或parameters.非敏感参数"}]}。
service与本地weight不得互换。原本地from_pretrained/torch.load等必须保留原模型对象路径；不能因为有API连接而改成HTTP或手写包装。service的base_url与model等只填真实原配置入口；credential只能映射到源码可核对的原认证调用参数，秘密从不进入分析上下文。原代码只在导入读环境时，当前同进程宿主不能临时改os.environ；保留MISSING。environment仅支持原CLI process_start且有原os.getenv/os.environ.get依据，值经子进程env传递不进命令行。
每个mapping的evidence必须同时覆盖原参数定义和该参数进入原调用的位置；base_url/model/parameters.xxx须有同名原调用keyword，base_url也可进入原requests/httpx等HTTP调用的URL位置参数；credential须有原认证keyword（api_key/token/password/access_token/auth_token）。权重path须进入原from_pretrained/torch.load/safetensors加载调用；原构造参数还允许同一作用域顺序中的单赋值局部变量或receiver直接属性，以及已核对pathlib.Path导入的透明包装、参数唯一一次同名Path(parameter)规范化、已为Path的值与字面相对子路径的/连接、一层固定等长tuple/list字面解包。构造函数内已核对且未遮蔽的joblib.load也可作原加载依据。evidence须覆盖参数、每次赋值及加载调用。除此以外的重赋、删除、动态下标、分支赋值、嵌套作用域、跨方法或自定义转换不属于该有限规则；不推断任意对象属性或动态配置数据流，覆盖不了则保留具体MISSING。CLI environment须覆盖原环境读取，服务或加载调用也须有上述字段依据。依赖引用必须实际出现在所映射的entry字段，不能仅列mappings。
参数值可引用用户所选资源：{"$dependency":{"id":"需求id","field":"base_url/model/credential/path/parameters.xxx"}}，只能放在该需求mappings已列的原参数位置。引用不是原函数参数类型；运行边界由引擎解析为原值。资源未选择仍提交真实需求与MISSING，不自选连接、模型或假权重。
原固定权重路径使用configurable:false,mappings:[]，placement:{expression:"来自evidence内的实际原AST表达式字符串",required_files:["原必要相对文件"]}。有限检查支持原字面路径、Path(__file__).parent及/连接、os.path.join/dirname/abspath；路径名变量/动态计算不能猜位置。引擎显示原执行cwd/文件基准下实际放置位置，不覆盖路径或省掉原必需加载。未覆盖位置保留MISSING。
resource_catalog只提供非敏感连接、权重状态及已有选择；配置存在不等于服务能力、权重完整或原目标已运行。用户选择不改变入口与边界，W/H/R及生命周期规则继续适用。
"""


class EnvironmentGap(ValueError):
    """A file/credential/package fact of the process that would execute the entry."""


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        allow_nan=False).encode()).hexdigest()


def reference(value):
    if not isinstance(value, dict) or "$dependency" not in value:
        return None
    if set(value) != {"$dependency"} or not isinstance(value["$dependency"], dict):
        raise ValueError("resource reference requires only $dependency")
    ref = value["$dependency"]
    if set(ref) != {"id", "field"} or not all(isinstance(v, str) and v for v in ref.values()):
        raise ValueError("resource reference requires dependency id and field")
    return ref


def _source(inspector, path, cell="file"):
    return dict(units(path, inspector.data(path)))[cell]


def _callable(inspector, entry, location):
    path, cell = entry["path"], entry.get("unit", "file")
    tree = ast.parse(_source(inspector, path, cell))
    symbol = (entry.get("constructor") or {}).get("symbol") if location == "constructor.arguments" else entry["symbol"]
    if location.startswith("initializers."):
        symbol = entry["symbol"].split(".")[0] + "." + location.split(".")[-1]
    if location == "binding_program.arguments":
        program = entry.get("binding_program") or {}
        path, cell = program.get("path", entry["path"]), program.get("unit", "file")
        tree = ast.parse(_source(inspector, path, cell))
        symbol = program.get("scope")
    node = tree
    for part in (symbol or "").split("."):
        matches = [n for n in getattr(node, "body", []) if isinstance(n,
            (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name == part]
        if len(matches) != 1:
            raise ValueError("dependency parameter has no unique original callable")
        node = matches[0]
    if isinstance(node, ast.ClassDef):
        matches = [n for n in node.body if isinstance(n, ast.FunctionDef) and n.name == "__init__"]
        if len(matches) != 1:
            raise ValueError("dependency constructor needs its explicit original __init__")
        node = matches[0]
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        raise ValueError("dependency does not identify an original function")
    return node, path, cell


def _covered(evidence, path, cell, node):
    return any(item["path"] == path and item.get("unit", "file") == cell and
        item["start"] <= node.lineno and getattr(node, "end_lineno", node.lineno) <= item["end"]
        for item in evidence)


def _destination(entry, location, parameter):
    if location == "entry.fixed_arguments":
        values = entry.get("fixed_arguments", {})
    elif location == "constructor.arguments":
        values = (entry.get("constructor") or {}).get("arguments", {})
    elif location == "binding_program.arguments":
        values = (entry.get("binding_program") or {}).get("arguments", {})
    elif location == "environment":
        values = entry.get("environment", {})
    elif location.startswith("initializers."):
        matches = [item for item in entry.get("initializers", []) if item.get("symbol") == location[len("initializers."):]]
        if len(matches) != 1:
            raise ValueError("dependency initializer destination is absent or ambiguous")
        values = matches[0].get("arguments", {})
    else:
        raise ValueError("unsupported original dependency parameter location")
    if not isinstance(values, dict) or parameter not in values:
        raise ValueError("dependency reference is absent from its actual destination")
    return values[parameter]


def _loader(call):
    name = ast.unparse(call.func)
    return name.endswith(".from_pretrained") or name in {
        "torch.load", "safetensors.torch.load_file", "safetensors.safe_open"}


def _field_call(call, field, matches, path_loaders=()):
    keywords = ({"api_key", "token", "password", "access_token", "auth_token"} if field == "credential"
                else {field.split(".", 1)[1]} if field.startswith("parameters.") else {field})
    if field == "path":
        if ast.unparse(call.func) in path_loaders:
            return any(matches(value) for value in list(call.args[:1]) +
                       [keyword.value for keyword in call.keywords if keyword.arg == "filename"])
        return _loader(call) and any(matches(value) for value in list(call.args) +
            [keyword.value for keyword in call.keywords if keyword.arg in
                {"pretrained_model_name_or_path", "model_name_or_path", "path", "filename", "f"}])
    if field == "base_url" and isinstance(call.func, ast.Attribute):
        method = call.func.attr
        if method in {"get", "post", "put", "delete", "head", "patch", "options", "stream", "request"}:
            position = 1 if method in {"stream", "request"} else 0
            if len(call.args) > position and matches(call.args[position]):
                return True
            if any(keyword.arg == "url" and matches(keyword.value) for keyword in call.keywords):
                return True
    return any(keyword.arg in keywords and matches(keyword.value) for keyword in call.keywords)


def _calls(node):
    """Do not treat an uninvoked nested definition as the outer parameter's sink."""
    stack = list(getattr(node, "body", []))
    while stack:
        current = stack.pop()
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        if isinstance(current, ast.Call):
            yield current
        stack.extend(ast.iter_child_nodes(current))


def _constructor_path_call(inspector, node, path, cell, parameter, evidence):
    """Finite constructor path origins; preserve all original execution logic."""
    def scope_nodes(scope):
        stack = list(getattr(scope, "body", []))
        while stack:
            current = stack.pop()
            yield current
            if not isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                stack.extend(ast.iter_child_nodes(current))

    tree = ast.parse(_source(inspector, path, cell))
    module_nodes, local_nodes = list(scope_nodes(tree)), list(scope_nodes(node))
    arguments = node.args.posonlyargs + node.args.args + node.args.kwonlyargs
    receiver = arguments[0].arg if arguments else None

    def key(value):
        if isinstance(value, ast.Name):
            return ("local", value.id)
        if (isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name)
                and value.value.id == receiver and value.attr != "__dict__"):
            return ("receiver", value.attr)
        return None

    writes, dynamic_receiver = {}, False
    for value in ast.walk(node):
        if isinstance(value, (ast.Name, ast.Attribute)) and isinstance(value.ctx, (ast.Store, ast.Del)):
            identity = key(value)
            if identity is not None:
                writes[identity] = writes.get(identity, 0) + 1
        if value is not node and isinstance(value, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            identity = ("local", value.name)
            writes[identity] = writes.get(identity, 0) + 1
        if isinstance(value, (ast.Import, ast.ImportFrom)):
            for alias in value.names:
                identity = ("local", alias.asname or alias.name.split(".")[0])
                writes[identity] = writes.get(identity, 0) + 1
        if isinstance(value, ast.Subscript) and isinstance(value.ctx, (ast.Store, ast.Del)):
            base = value.value
            while isinstance(base, ast.Subscript):
                base = base.value
            identity = key(base)
            if identity is not None:
                writes[identity] = writes.get(identity, 0) + 1
            if (isinstance(base, ast.Attribute) and isinstance(base.value, ast.Name)
                    and base.value.id == receiver and base.attr == "__dict__"):
                dynamic_receiver = True
        if (isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
                and value.func.id in {"setattr", "delattr"} and value.args
                and isinstance(value.args[0], ast.Name) and value.args[0].id == receiver):
            dynamic_receiver = True

    def bindings(values, name):
        return sum((isinstance(value, ast.Name) and value.id == name and isinstance(value.ctx, (ast.Store, ast.Del)))
            or (isinstance(value, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and value.name == name)
            or (isinstance(value, (ast.Import, ast.ImportFrom)) and any(
                (alias.asname or alias.name.split(".")[0]) == name for alias in value.names))
            for value in values)

    def imported(module, member):
        expressions = set()
        for statement in tree.body:
            if isinstance(statement, ast.ImportFrom) and statement.module == module and statement.level == 0:
                imports = [(alias.asname or alias.name, alias.asname or alias.name)
                           for alias in statement.names if alias.name == member]
            elif isinstance(statement, ast.Import):
                imports = [(alias.asname or module, (alias.asname or module) + "." + member)
                           for alias in statement.names if alias.name == module]
            else:
                continue
            for name, expression in imports:
                if (bindings(module_nodes, name) == 1 and bindings(local_nodes, name) == 0
                        and all(argument.arg != name for argument in arguments)
                        and not any(isinstance(value, ast.Attribute) and ast.unparse(value) == expression
                            and isinstance(value.ctx, (ast.Store, ast.Del)) for value in module_nodes + local_nodes)
                        and not any(isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
                            and value.func.id in {"setattr", "delattr"} and value.args
                            and isinstance(value.args[0], ast.Name) and value.args[0].id == name
                            for value in module_nodes + local_nodes)
                        and not any(isinstance(value, ast.Subscript) and isinstance(value.ctx, (ast.Store, ast.Del))
                            and isinstance(value.value, ast.Attribute) and value.value.attr == "__dict__"
                            and isinstance(value.value.value, ast.Name) and value.value.value.id == name
                            for value in module_nodes + local_nodes)):
                    expressions.add(expression)
        return expressions

    wrappers, loaders = imported("pathlib", "Path"), imported("joblib", "load")
    parameter_key = ("local", parameter)
    normalizations = [statement for statement in node.body
        if isinstance(statement, (ast.Assign, ast.AnnAssign)) and statement.value is not None
        and ((isinstance(statement, ast.Assign) and len(statement.targets) == 1
              and key(statement.targets[0]) == parameter_key)
             or isinstance(statement, ast.AnnAssign) and key(statement.target) == parameter_key)
        and isinstance(statement.value, ast.Call) and ast.unparse(statement.value.func) in wrappers
        and len(statement.value.args) == 1 and not statement.value.keywords
        and isinstance(statement.value.args[0], ast.Name) and statement.value.args[0].id == parameter
        and _covered(evidence, path, cell, statement)]
    if writes.get(parameter_key, 0) and (writes[parameter_key] != 1 or len(normalizations) != 1):
        return False

    # False is a resource-origin string/Path; True additionally proves a Path
    # wrapper. Only the latter may use the finite literal-child / expression.
    known = {parameter_key: False}

    def relative_literal(value):
        if not isinstance(value, ast.Constant) or not isinstance(value.value, str) or not value.value or "\0" in value.value:
            return False
        text = value.value
        return (not PurePosixPath(text).is_absolute() and not PureWindowsPath(text).drive
                and not text.startswith(("/", "\\"))
                and all(part not in {"", ".", ".."} for part in re.split(r"[\\/]", text)))

    def origin(value):
        identity = key(value)
        if identity in known:
            return known[identity]
        if (isinstance(value, ast.Call) and ast.unparse(value.func) in wrappers
                and len(value.args) == 1 and not value.keywords and origin(value.args[0]) is not None):
            return True
        if (isinstance(value, ast.BinOp) and isinstance(value.op, ast.Div)
                and origin(value.left) is True and relative_literal(value.right)):
            return True
        return None

    transparent = lambda value: origin(value) is not None

    # Only direct, ordered assignments establish aliases. Branch assignments and
    # nested definitions cannot supply a definitely created original binding.
    for statement in node.body:
        if any(_field_call(call, "path", transparent, path_loaders=loaders) and _covered(evidence, path, cell, call)
               for call in _calls(ast.Module(body=[statement], type_ignores=[]))):
            return True
        if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
            target, value = statement.targets[0], statement.value
        elif isinstance(statement, ast.AnnAssign) and statement.value is not None:
            target, value = statement.target, statement.value
        else:
            continue
        if isinstance(target, (ast.Tuple, ast.List)) and isinstance(value, (ast.Tuple, ast.List)):
            if len(target.elts) != len(value.elts) or not target.elts:
                continue
            pairs = list(zip(target.elts, value.elts))
        else:
            pairs = [(target, value)]
        identities = [key(target) for target, value in pairs]
        origins = [origin(value) for target, value in pairs]
        if (None in identities or len(set(identities)) != len(identities) or None in origins
                or not _covered(evidence, path, cell, statement)
                or any(writes.get(identity) != 1 or (identity[0] == "receiver" and
                    (dynamic_receiver or writes.get(("local", receiver), 0))) for identity in identities)):
            continue
        known.update(zip(identities, origins))
    return False


def _path_expression(node, root, file):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name) and node.id == "__file__":
        return str(file)
    if isinstance(node, ast.Attribute) and node.attr == "parent":
        return str(Path(_path_expression(node.value, root, file)).parent)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return str(Path(_path_expression(node.left, root, file)) / _path_expression(node.right, root, file))
    if isinstance(node, ast.Call) and not node.keywords:
        name = ast.unparse(node.func)
        args = [_path_expression(a, root, file) for a in node.args]
        if name in {"Path", "pathlib.Path", "str"} and len(args) == 1:
            return args[0]
        if name in {"os.path.join", "posixpath.join", "ntpath.join"}:
            return str(Path(args[0]).joinpath(*args[1:]))
        if name == "os.path.dirname" and len(args) == 1:
            return str(Path(args[0]).parent)
        if name in {"os.path.abspath", "os.path.realpath"} and len(args) == 1:
            value = Path(args[0]); return str((root / value).resolve() if not value.is_absolute() else value.resolve())
        if isinstance(node.func, ast.Attribute) and node.func.attr in {"resolve", "absolute"} and not args:
            value = Path(_path_expression(node.func.value, root, file))
            return str((root / value).resolve() if not value.is_absolute() else value.resolve())
    raise ValueError("fixed resource location is outside the finite original path-expression rule")


def check_requirements(inspector, candidate):
    """Check source anchors, parameter destinations and fixed path expressions."""
    if getattr(inspector, "target_runtime", None):
        return inspector.target_runtime.call("check_requirements", candidate=candidate)
    result = []
    definitions = candidate.get("dependencies", [])
    if not isinstance(definitions, list) or any(not isinstance(item, dict) for item in definitions):
        raise ValueError("dependencies must be an array of requirement objects")
    entries = {e.get("id"): e for e in candidate.get("entries", []) if isinstance(e, dict)}
    seen = set()
    for index, requirement in enumerate(definitions):
        row = copy.deepcopy(requirement)
        if not isinstance(row.get("id"), str) or not row["id"]:
            row["id"] = "invalid-dependency-" + str(index)
        row.update(status="MISSING", reason=None, execution="NOT_RUN")
        try:
            identity = requirement.get("id")
            if not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", identity) or identity in seen:
                raise ValueError("dependency needs a unique nonempty id")
            seen.add(identity)
            if row.get("kind") not in {"service", "weight"}:
                raise ValueError("dependency kind must be service or weight")
            if type(row.get("configurable")) is not bool or type(row.get("required", True)) is not bool:
                raise ValueError("dependency needs explicit configurable and boolean required")
            if row.get("read_time") not in {"constructor", "call", "process_start"} or not isinstance(row.get("owner_scope"), str) or not row["owner_scope"].strip():
                raise ValueError("dependency needs its original read time and owner scope")
            evidence = row.get("evidence")
            if not isinstance(evidence, list) or not evidence:
                raise ValueError("dependency needs original source evidence")
            texts = []
            for item in evidence:
                text = _source(inspector, item["path"], item.get("unit", "file"))
                if not (type(item.get("start")) is int and type(item.get("end")) is int and
                        1 <= item["start"] <= item["end"] <= len(text.splitlines())):
                    raise ValueError("dependency evidence line range is invalid")
                texts.append((item, text))
            mappings = row.get("mappings", [])
            if not isinstance(mappings, list):
                raise ValueError("dependency mappings must be an array")
            affected = row.get("entry_ids", [])
            if not isinstance(affected, list) or set(affected) - set(entries):
                raise ValueError("dependency entry_ids must reference proposed entries")
            for mapping in mappings:
                entry = entries.get(mapping.get("entry_id"))
                if entry is None:
                    raise ValueError("dependency mapping entry is absent")
                location, parameter, field = mapping.get("location", ""), mapping.get("parameter"), mapping.get("field")
                if not isinstance(parameter, str) or not parameter.isidentifier():
                    raise ValueError("dependency needs an original explicit parameter name")
                if field not in {"base_url", "model", "credential", "path", "service_version"} and not (
                        isinstance(field, str) and field.startswith("parameters.")):
                    raise ValueError("unsupported resource field")
                if (row["kind"] == "weight") != (field == "path"):
                    raise ValueError("weight paths and service fields cannot be interchanged")
                ref = reference(_destination(entry, location, parameter))
                if ref != {"id": identity, "field": field}:
                    raise ValueError("dependency reference does not occupy its declared original destination")
                if location == "environment":
                    if entry.get("kind") != "cli" or row.get("read_time") != "process_start":
                        raise ValueError("environment injection requires an original CLI process-start binding; in-process environment is never changed")
                    tree = ast.parse(_source(inspector, entry["path"], entry.get("unit", "file")))
                    reads = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and ast.unparse(n.func)
                        in {"os.getenv", "os.environ.get"} and n.args and isinstance(n.args[0], ast.Constant)
                        and n.args[0].value == parameter]
                    if not reads:
                        raise ValueError("original environment read is not anchored")
                    checked_read = False
                    for read in reads:
                        if not _covered(evidence, entry["path"], entry.get("unit", "file"), read):
                            continue
                        scopes = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                                  and n.lineno <= read.lineno <= n.end_lineno]
                        scope = min(scopes, key=lambda n: n.end_lineno - n.lineno) if scopes else tree
                        aliases = set()
                        for statement in ast.walk(scope):
                            if isinstance(statement, (ast.Assign, ast.AnnAssign)) and statement.value is read:
                                targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
                                if _covered(evidence, entry["path"], entry.get("unit", "file"), statement):
                                    aliases.update(target.id for target in targets if isinstance(target, ast.Name))
                        writes = {name: sum(isinstance(n, ast.Name) and n.id == name and isinstance(n.ctx, (ast.Store, ast.Del))
                                           for n in ast.walk(scope)) for name in aliases}
                        aliases = {name for name in aliases if writes[name] == 1}
                        matches = lambda value: value is read or isinstance(value, ast.Name) and value.id in aliases
                        if any(_field_call(call, field, matches) and
                               _covered(evidence, entry["path"], entry.get("unit", "file"), call) for call in _calls(scope)):
                            checked_read = True
                            break
                    if not checked_read:
                        raise ValueError("environment evidence must cover its read and original field/loader/authentication call")
                else:
                    if location not in {"constructor.arguments", "entry.fixed_arguments", "binding_program.arguments"} and not location.startswith("initializers."):
                        raise ValueError("unsupported original dependency parameter location")
                    node, path, cell = _callable(inspector, entry, location)
                    args = node.args.posonlyargs + node.args.args + node.args.kwonlyargs
                    original = next((arg for arg in args if arg.arg == parameter), None)
                    if original is None:
                        raise ValueError("dependency destination is not an original explicit parameter")
                    if not _covered(evidence, path, cell, original):
                        raise ValueError("dependency evidence must cover its original parameter definition")
                    matches = lambda value: isinstance(value, ast.Name) and value.id == parameter
                    direct = any(_field_call(call, field, matches) and _covered(evidence, path, cell, call) for call in _calls(node))
                    path_flow = (field == "path" and location == "constructor.arguments" and
                                 _constructor_path_call(inspector, node, path, cell, parameter, evidence))
                    reassigned = any(
                        isinstance(n, ast.Name) and n.id == parameter and isinstance(n.ctx, (ast.Store, ast.Del))
                        or isinstance(n, (ast.Import, ast.ImportFrom)) and any(
                            (alias.asname or alias.name.split(".")[0]) == parameter for alias in n.names)
                        or n is not node and isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                            and n.name == parameter for n in ast.walk(node))
                    if reassigned and not path_flow:
                        raise ValueError("reassigned resource parameter needs a separate original data-flow binding")
                    if not direct and not path_flow:
                        raise ValueError("dependency evidence must cover the original field/loader/authentication call")
                if mapping["entry_id"] not in affected:
                    affected.append(mapping["entry_id"])
            row["entry_ids"] = affected
            if row["configurable"] and not mappings:
                raise ValueError("configurable dependency needs original parameter mappings")
            if not row["configurable"]:
                if row["kind"] != "weight" or mappings:
                    raise ValueError("fixed placement supports original weight locations without parameter overrides")
                placement = row.get("placement") or {}
                expression = placement.get("expression")
                found = []
                for item, text in texts:
                    for node in ast.walk(ast.parse(text)):
                        if isinstance(node, ast.expr) and hasattr(node, "lineno") and item["start"] <= node.lineno <= item["end"] and ast.unparse(node) == expression:
                            found.append((item, node))
                if not found:
                    raise ValueError("fixed path expression is not present in the cited original source")
                item, node = found[0]
                tree = ast.parse(_source(inspector, item["path"], item.get("unit", "file")))
                aliases = set()
                for statement in ast.walk(tree):
                    if isinstance(statement, (ast.Assign, ast.AnnAssign)) and statement.value is not None and ast.dump(statement.value) == ast.dump(node):
                        targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
                        if _covered(evidence, item["path"], item.get("unit", "file"), statement):
                            aliases.update(target.id for target in targets if isinstance(target, ast.Name))
                aliases = {name for name in aliases if sum(isinstance(n, ast.Name) and n.id == name and isinstance(n.ctx, (ast.Store, ast.Del))
                                                         for n in ast.walk(tree)) == 1}
                matches = lambda value: ast.dump(value) == ast.dump(node) or isinstance(value, ast.Name) and value.id in aliases
                if not any(isinstance(call, ast.Call) and _field_call(call, "path", matches) and
                           _covered(evidence, item["path"], item.get("unit", "file"), call) for call in ast.walk(tree)):
                    raise ValueError("fixed placement evidence must cover its original model-loader call")
                required = placement.get("required_files", [])
                if not isinstance(required, list) or any(not isinstance(name, str) or not name or Path(name).is_absolute()
                    or ".." in Path(name).parts for name in required):
                    raise ValueError("fixed placement requires original relative necessary-file names")
                path = Path(_path_expression(node, inspector.root, inspector.root / item["path"]))
                path = (inspector.root / path).resolve() if not path.is_absolute() else path.resolve()
                row["placement"] = dict(placement, resolved_path=str(path), execution_environment="platform_process",
                    original_cwd=str(inspector.root), contents="NOT_LOADED")
            row.update(status="MAPPING_CHECKED", source_version=inspector.request["source_version"],
                       semantic_match="SOURCE_AND_PARAMETER_BOUND; capability compatibility remains unverified")
        except (ValueError, KeyError, TypeError, SyntaxError, OSError) as error:
            # Keep source-derived details but never echo runtime credentials.
            row["reason"] = str(error) if isinstance(error, ValueError) else "source requirement, original destination or finite placement could not be checked"
        row["definition_digest"] = fingerprint(requirement)
        result.append(row)
    return result


class DependencyBindings:
    """An installation's frozen nonsecret configuration and private key resolver."""
    def __init__(self, registry, inspector, candidate):
        self.registry, self.inspector = registry, inspector
        self.rows = {row["id"]: row for row in check_requirements(inspector, candidate)}
        self.selected, self.keys = {}, {}
        # None raises; a list collects facts that a separately declared host re-checks.
        self.environment_gaps = None
        # A runtime-group host checks its own files instead of the platform's record.
        self.host_check = None
        source = inspector.request.get("source_id", "")
        selections = inspector.request.get("dependency_bindings", {})
        for identity, row in self.rows.items():
            selection = selections.get(identity)
            if selection:
                self.selected[identity] = copy.deepcopy(selection)

    def _environment(self, message):
        if self.environment_gaps is None:
            raise EnvironmentGap(message)
        self.environment_gaps.append(message)

    def snapshot(self, entry_ids=None):
        rows = {k:v for k,v in self.rows.items() if entry_ids is None or set(v.get("entry_ids", [])) & set(entry_ids)}
        return copy.deepcopy(dict(requirements=list(rows.values()),
                                  bindings={k:v for k,v in self.selected.items() if k in rows}))

    def _selection(self, identity):
        row = self.rows.get(identity)
        if not row or row["status"] != "MAPPING_CHECKED":
            raise ValueError("resource dependency has no checked original mapping")
        selection = self.selected.get(identity)
        if not selection or selection.get("definition_digest") != row["definition_digest"]:
            raise ValueError("resource dependency is unselected or its original mapping has changed")
        return row, selection

    def value(self, value, entry_id, location, parameter, runtime=False):
        ref = reference(value)
        if ref is None:
            return value
        row, selection = self._selection(ref["id"])
        if not any(m.get("entry_id") == entry_id and m.get("location") == location and
                   m.get("parameter") == parameter and m.get("field") == ref["field"] for m in row["mappings"]):
            raise ValueError("resource reference does not match its checked original parameter")
        if row["kind"] == "weight":
            check = (self.host_check(selection["resource"]) if self.host_check
                     else selection["resource"].get("check", {}))
            if str(check.get("status", "")).lower() not in {"files_present", "identity_verified", "ready", "present", "verified"}:
                self._environment("weight resource required files have not been checked or are missing in this execution environment")
            elif check.get("necessary_files") == "not_declared":
                raise ValueError("weight resource needs the original necessary-file list or shard index")
            return selection["resource"]["path"]
        connection = selection["connection"]
        field = ref["field"]
        if field == "credential":
            available = ((connection.get("auth") or {}).get("mode") == "none" or
                         bool(self.registry.profiles.connection_key(connection["id"]))) if self.host_check else \
                selection.get("credential_available")
            if not available:
                self._environment("selected authentication material is unavailable in this execution environment")
            if not runtime:
                return ""  # source/interface checks never resolve secret bytes
            self._freeze_credential(ref["id"], connection)
            return self.keys[ref["id"]]
        if field.startswith("parameters."):
            return connection.get("parameters", {})[field.split(".", 1)[1]]
        result = connection.get(field)
        if result is None or result == "":
            raise ValueError("selected connection field is unavailable")
        return result

    def values(self, values, entry_id, location, runtime=False):
        return {parameter: self.value(value, entry_id, location, parameter, runtime)
                for parameter, value in values.items()}

    def check_entry(self, entry_id):
        for identity, row in self.rows.items():
            if entry_id not in row.get("entry_ids", []) or not row.get("required", True):
                continue
            if row["status"] != "MAPPING_CHECKED":
                raise ValueError("required source resource mapping is unresolved")
            if row["configurable"]:
                self._selection(identity)
                for mapping in row["mappings"]:
                    if mapping["entry_id"] == entry_id:
                        value = {"$dependency": {"id": identity, "field": mapping["field"]}}
                        self.value(value, entry_id, mapping["location"], mapping["parameter"], runtime=False)
            else:
                placement = row["placement"]
                target = Path(placement["resolved_path"])
                required = placement.get("required_files", [])
                if target.is_dir() and not required:
                    raise ValueError("required fixed model directory needs its original necessary-file list")
                if not target.exists() or not os.access(target, os.R_OK) or any(
                        not (target / name).is_file() or not os.access(target / name, os.R_OK) for name in required):
                    self._environment("required original fixed weight location is missing in this execution environment; see placement details")

    def _freeze_credential(self, identity, connection):
        profiles = self.registry.profiles
        with profiles.lock:
            if identity in self.keys:
                return
            current = profiles.connection_profile(connection["id"])
            if current.get("auth") != connection.get("auth") or current.get("config_digest") != connection.get("config_digest"):
                raise ValueError("resource authentication or connection changed; select and rebind for a new runtime")
            key = profiles.connection_key(connection["id"])
            if (connection.get("auth") or {}).get("mode") != "none" and not key:
                raise ValueError("selected authentication material is unavailable")
            self.keys[identity] = key

    def freeze_credentials(self, entry_ids=None):
        for identity, selection in self.selected.items():
            if entry_ids is not None and not set(self.rows[identity].get("entry_ids", [])) & set(entry_ids):
                continue
            connection = selection.get("connection")
            if connection:
                self._freeze_credential(identity, connection)

    def redact(self, error):
        message = str(error)
        for key in self.keys.values():
            if key:
                message = message.replace(key, "[private credential]")
        return message


class Dependencies:
    def __init__(self, store, profiles, resources):
        self.store, self.profiles, self.resources = store, profiles, resources

    def public(self):
        rows = [row for row in self.store.list("resource_dependency")
                if row.get("active", True) and row.get("status") != "SUPERSEDED"]
        current = {row["id"]: row for row in rows}
        selections = []
        for selection in self.store.list("dependency_binding"):
            row = current.get(selection.get("dependency_id"))
            if (row and row.get("status") == "MAPPING_CHECKED" and selection.get("status") == "SELECTED"
                    and selection.get("source_id") == row.get("source_id")
                    and selection.get("definition_digest") == row.get("definition_digest")):
                selections.append(selection)
        return rows, selections

    def record(self, inspector, candidate):
        source = inspector.request.get("source_id", "")
        rows = check_requirements(inspector, candidate)
        current = {row["id"] for row in rows}
        for previous in self.store.list("resource_dependency"):
            if previous.get("source_id") == source and previous.get("origin_id") not in current:
                if previous.get("active", True) or previous.get("status") != "SUPERSEDED":
                    previous.update(active=False, status="SUPERSEDED", superseded_at=now())
                    self.store.save("resource_dependency", previous["id"], previous)
        for row in rows:
            identity = source + ":" + row["id"]
            item = dict(row, origin_id=row["id"], id=identity, source_id=source,
                        active=True, checked_at=now(), execution="NOT_RUN")
            previous = self.store.get("resource_dependency", identity)
            if previous and previous.get("definition_digest") == item["definition_digest"]:
                item["execution"] = previous.get("execution", "NOT_RUN")
                if "last_execution" in previous:
                    item["last_execution"] = copy.deepcopy(previous["last_execution"])
            self.store.save("resource_dependency", identity, item)
        return rows

    def selections(self, source_id):
        return {row["origin_id"]: row for row in self.public()[1] if row["source_id"] == source_id}

    def record_execution(self, source_id, entry_id, success, snapshot=None):
        """Record native return/raise, without treating a return as task correctness."""
        if type(success) is not bool:
            raise ValueError("native call outcome requires a boolean return/raise indicator")
        selections = (snapshot or {}).get("bindings", {}) if isinstance(snapshot, dict) else {}
        requirements = {row.get("id"): row for row in (snapshot or {}).get("requirements", [])
                        if isinstance(row, dict)} if isinstance(snapshot, dict) else {}
        evidence = []
        for identity, selection in selections.items():
            row = requirements.get(identity, {})
            if entry_id not in row.get("entry_ids", []):
                continue
            connection, resource = selection.get("connection", {}), selection.get("resource", {})
            evidence.append(dict(dependency_id=identity, definition_digest=row.get("definition_digest"),
                binding_digest=selection.get("revision_digest"), connection_id=connection.get("id"),
                connection_digest=connection.get("config_digest"), resource_id=resource.get("id"),
                resource_digest=resource.get("config_digest")))
        represented = {item["dependency_id"] for item in evidence}
        for identity, row in requirements.items():
            if identity not in represented and entry_id in row.get("entry_ids", []):
                evidence.append(dict(dependency_id=identity, definition_digest=row.get("definition_digest"),
                                     binding_digest=None, connection_id=None, connection_digest=None,
                                     resource_id=None, resource_digest=None))
        outcome = dict(at=now(), source_id=source_id, entry_id=entry_id,
            native_call_outcome="RETURNED" if success else "RAISED",
            target_execution="SUCCEEDED" if success else "FAILED", task_correctness="NOT_EVALUATED",
            scope="original callable returned or raised; not a correctness or service-capability verdict",
            resources=evidence, configuration_evidence="FROZEN_BINDING" if snapshot is not None else "UNKNOWN")
        for row in self.store.list("resource_dependency"):
            if row.get("source_id") != source_id or entry_id not in row.get("entry_ids", []):
                continue
            frozen = requirements.get(row.get("origin_id"))
            if frozen is None or frozen.get("definition_digest") != row.get("definition_digest"):
                continue
            row.update(execution=outcome["target_execution"], last_execution=copy.deepcopy(outcome))
            self.store.save("resource_dependency", row["id"], row)
            selected = self.store.get("dependency_binding", row["id"])
            frozen_selection = selections.get(row.get("origin_id"))
            if (selected and frozen_selection and selected.get("definition_digest") == frozen.get("definition_digest")
                    and selected.get("revision_digest") == frozen_selection.get("revision_digest")):
                selected.update(execution=outcome["target_execution"], last_execution=copy.deepcopy(outcome))
                self.store.save("dependency_binding", selected["dependency_id"], selected)
        return outcome

    def select(self, body):
        row = self.store.get("resource_dependency", body["dependency_id"])
        if (not row or not row.get("active", True) or row["source_id"] != body["harness_id"]
                or row["status"] != "MAPPING_CHECKED"):
            raise ValueError("dependency has no current checked original source requirement")
        if not row["configurable"]:
            raise ValueError("original fixed resource location must be prepared in place")
        item = dict(dependency_id=row["id"], origin_id=row["origin_id"], source_id=row["source_id"],
            definition_digest=row["definition_digest"], bound_at=now(), status="SELECTED", execution="NOT_RUN")
        if row["kind"] == "service":
            connection = next((c for c in self.profiles.public_connections() if c["id"] == body["connection_id"]), None)
            if connection is None:
                raise ValueError("service connection is absent")
            item.update(connection_id=connection["id"], connection=connection,
                credential_available=bool(connection.get("key_usable")) or connection.get("auth", {}).get("mode") == "none")
        else:
            resource = next((w for w in self.resources.public_weights() if w["id"] == body["resource_id"]), None)
            if resource is None:
                raise ValueError("weight resource is absent")
            item.update(resource_id=resource["id"], resource=resource)
        item["revision_digest"] = fingerprint(item)
        self.store.save("dependency_binding", row["id"], item)
        return item

    def check_fixed(self, body):
        row = self.store.get("resource_dependency", body["dependency_id"])
        if (not row or not row.get("active", True) or row.get("status") == "SUPERSEDED"
                or row["source_id"] != body["harness_id"]):
            raise ValueError("dependency is absent")
        if row.get("configurable"):
            raise ValueError("check its selected weight or connection resource")
        placement = row.get("placement") or {}
        target = Path(placement["resolved_path"])
        missing = [n for n in placement.get("required_files", []) if not (target / n).is_file()]
        if not target.exists():
            missing.insert(0, str(target))
        row["placement"] = dict(placement, missing=missing)
        row["check"] = dict(status="MISSING" if missing else "FILES_PRESENT", checked_at=now(),
                            resolved_path=str(target), missing_files=missing, target_execution="NOT_RUN")
        self.store.save("resource_dependency", row["id"], row)
        return row
