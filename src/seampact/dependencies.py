"""Source-anchored resource requirements and finite original-parameter bindings."""

import ast
import copy
import hashlib
import json
import os
import re
from pathlib import Path, PurePosixPath, PureWindowsPath

from .inspection import units
from .intake import inside_platform_checkout
from .store import now
from .resource_flow import parameter_flow
from .configuration_flow import environment_flow, configuration_facts
from .configuration_binding import configuration_override_flow
from .process_environment import process_environment_flow
from .resource_origins import resource_origin_facts

DEPENDENCY_RULES = """
资源配置由用户保存，依赖需求和原参数映射必须由你从原源码提出。预处理工件和最终候选必须提供dependencies数组，逐项枚举已发现的原必需及可选外部服务、权重和配套模型资源；已有用户字面配置、原签名默认值、固定路径或尚未提供资源，均不是省略需求的理由。只在已检查的目标及原初始化范围确实没有发现此类需求时填[]，并在missing说明仍未覆盖的范围；[]本身不证明资源发现完整。
资源发现与可执行映射分别提交。已发现需求即使没有当前有限规则支持的映射，也必须保留其源码证据、受影响entry_ids、原读取时点和owner，保留未核对/空mappings及具体missing，由检查器返回MISSING；不得为使入口READY而删除需求、改用用户字面配置绕过，或用无关辅助入口代替原目标。不要编造参数、引用或加载依据来消除MISSING。
可配置依赖按受影响入口逐项核对，每个entry_ids中的入口都需自己的原参数映射和证据；一个入口通过不能替另一个入口放行。检查反馈的entry_checks给出逐入口状态与原因，selection_allowed只表示至少有入口可选择资源，不表示所有入口就绪，也不表示原服务已启动。
宿主/编排入口通过函数或子进程间接使用已发现的服务/权重时，同样属于受影响入口；不能仅因为客户端构造在下游就省略宿主的需求。未核对跨进程配置传递时保留该入口的需求和映射缺口，不将无映射宿主写成资源闭合。
mapping可提供自己的evidence:[{path,unit:"file",start,end}]；提供时独立核对该证据，不能用空值或不完整范围回退到依赖总证据。省略mapping.evidence时使用依赖的evidence。每份映射证据须覆盖完整参数定义及完整调用表达式（包含多行调用的结束行），跨文件时分别引用各文件。
沿原目标的构造、初始化及请求路径检查依赖，区分基座、adapter、processor/checkpoint等实际资源及其原配置关系，不把其中一项代替整组。required按原owner实际的无条件加载或分支条件填写；拆分不能省略原初始化必需工作。最终候选应保留预处理发现且仍影响所选入口的需求；因入口范围改变而不再适用的需求须在missing说明源码依据，不能静默丢弃。资源未发现完整、映射未覆盖及资源尚未配置要分别说明。
dependency格式：{id:"ASCII需求标识",kind:"service或weight",description:"实际用途",configurable:true或false,required:true或false,entry_ids:["依赖它的候选entry.id"],read_time:"constructor或call或process_start",owner_scope:"原对象/模块/请求作用域说明",evidence:[{path,unit:"file",start,end}],mappings:[{entry_id,location:"constructor.arguments或entry.fixed_arguments或initializers.原方法或binding_program.arguments或environment",parameter:"原真实参数/变量",field:"base_url或model或credential或path或parameters.非敏感参数"}]}。
service与本地weight不得互换。原本地from_pretrained/torch.load等必须保留原模型对象路径；不能因为有API连接而改成HTTP或手写包装。service的base_url与model等只填真实原配置入口；credential只能映射到源码可核对的原认证调用参数，秘密从不进入分析上下文。原代码只在导入读环境时，当前同进程宿主不能临时改os.environ；保留MISSING。environment仅支持原CLI process_start且有原os.getenv/os.environ.get依据，值经子进程env传递不进命令行。
environment映射的read_time描述平台注入边界，应为process_start；即使原子进程后来在构造器或请求方法消费该变量，也不能把这个注入时点填成constructor/call。原实际消费位置继续写在owner_scope和源码证据中。CLI候选的错误read_time和缺失/不匹配资源引用属于候选可修项；修正后仍须完整核对配置传递，不自动通过。
每个mapping的evidence必须同时覆盖原参数定义和该参数进入原调用的位置；base_url/model/parameters.xxx须有同名原调用keyword，base_url也可进入原requests/httpx等HTTP调用的URL位置参数；credential须有原认证keyword（api_key/token/password/access_token/auth_token）。权重path须进入原from_pretrained/torch.load/safetensors加载调用；原构造参数还允许同一作用域顺序中的单赋值局部变量或receiver直接属性，以及已核对pathlib.Path导入的透明包装、参数唯一一次同名Path(parameter)规范化、已为Path的值与字面相对子路径的/连接、一层固定等长tuple/list字面解包。构造函数内已核对且未遮蔽的joblib.load也可作原加载依据。evidence须覆盖参数、每次赋值及加载调用。除此以外的重赋、删除、动态下标、分支赋值、嵌套作用域、跨方法或自定义转换不属于该有限规则；不推断任意对象属性或动态配置数据流，覆盖不了则保留具体MISSING。CLI environment须覆盖原环境读取，服务或加载调用也须有上述字段依据。依赖引用必须实际出现在所映射的entry字段，不能仅列mappings。
参数值可引用用户所选资源：{"$dependency":{"id":"需求id","field":"base_url/model/credential/path/parameters.xxx"}}，只能放在该需求mappings已列的原参数位置。引用不是原函数参数类型；运行边界由引擎解析为原值。资源未选择仍提交真实需求与MISSING，不自选连接、模型或假权重。
原函数通过配置字典读服务字段时，可在entry.fixed_arguments映射增加parameter_path:["原固定键", "原子键"]，parameter仍为原显式函数参数；引用放在该参数字典的同一键路径。仅支持1至8层字符串字面键、同一原函数或未遮蔽的词法闭包读取、单赋值局部别名；evidence须覆盖原参数、每个别名赋值和原字段调用。base_url允许保留原字符串字面后缀拼接，平台不自动增删后缀，所选连接必须符合原代码期望的基址。配置字典其余值仍须来自用户明确配置或现有值来源规则，不能自行编造整份配置。原字典的重赋、变更、动态键、字典前缀外传、类/lambda作用域或跨函数传参仍不支持；动态代码、命名空间/栈帧反射、动态调用目标、资源别名变更或外传不属于有限静态规则，须保留MISSING。该核对仅证明可见原语法中的参数来源，不证明任意外部Python调用不会反射改写。CLI/YAML文件参数不能冒充原函数字典参数。无需认证时，个别原SDK要求的非密钥占位符须由用户配置明确提供，不能把真实凭据写成候选字面值。
原固定权重路径使用configurable:false,mappings:[]，placement:{expression:"来自evidence内的实际原AST表达式字符串",required_files:["原必要相对文件"]}。有限检查支持原字面路径、Path(__file__).parent及/连接、os.path.join/dirname/abspath；路径名变量/动态计算不能猜位置。引擎显示原执行cwd/文件基准下实际放置位置，不覆盖路径或省掉原必需加载。未覆盖位置保留MISSING。
resource_catalog只提供非敏感连接、权重状态及已有选择；配置存在不等于服务能力、权重完整或原目标已运行。用户选择不改变入口与边界，W/H/R及生命周期规则继续适用。
新增有限参数传递规则：可核对冻结 Python 中唯一且未遮蔽的函数/构造器调用，连接实参—形参、单赋值别名和同一接收者的单写属性，最多追踪8层；对象后续方法须从当前原入口实际调用到达，不能引用未调用方法冒充使用。每步参数、赋值和完整调用都要有evidence，检查反馈给出缺失的源码范围。解析真实导入后支持LiteLLM的api_base对应base_url，以及原OpenAI客户端中实际model字段/字面键请求字典；不改原SDK字段。动态目标、覆盖、多来源分支值、别名逃逸或循环来源不能确定时保留parameter_flow_unsupported；这只核对资源使用关系，不授予跳过、并行或生命周期许可。
资源反馈resource_origins区分模型/附加checkpoint、Hub或本地目录、cache及下载目的地。from_pretrained的Hub标识不能当作仓库下固定路径，cache_dir不能当作模型文件；原分支选模型的配置参数不能直接替换成路径。有现成原路径参数时绑定已检查的本地文件/目录；只能经Hub/复杂配置消费的来源保留resource_locator_unsupported，不下载或改原算法。原构造后加载权重、设备和模式设置须沿原initializers或已支持的完整B-CALL保留；缺口不能靠删除依赖或随机初始化消除。
CLI environment可沿唯一真实导入/调用追踪原模块单赋值常量、无自定义方法的普通dataclass字段及原对象单写属性；仅真实SDK字段或urllib.request.Request进入urlopen的URL、JSON model字段、Bearer Authorization可作消费者证据。各导入、环境读取、字段、赋值和完整调用均须覆盖。配置工厂或客户端名称本身不证明消费；未调用方法、动态读取、环境覆盖、遮蔽及缺失源文件保留具体MISSING。configuration_flow中的SOURCE_SIGNAL只记录配置来源、合并/条件覆盖次序和原字段加载事实，selected_value:UNRESOLVED不表示YAML/YACS值已求解，不可据此放行或将Hub标识当本地路径。原CLI权重可在entry.fixed_arguments声明whole-placeholder资源引用，mapping另填configuration_key:"原点分字段"及cli_override:{parameter:"原argparse.REMAINDER参数"}；command最后须为该字面字段和{资源参数占位符}，read_time须为process_start。检查器须核对真实YACS导入、原字符串键声明、原工作目录的冻结YAML、最后merge_from_list及该字段到torch.load，无后续覆盖或动态剩余选项。此前YAML值不在平台执行，原CLI保持原加载/合并/推理。
"""


class EnvironmentGap(ValueError):
    """A file/credential/package fact of the process that would execute the entry."""


class MappingGap(ValueError):
    def __init__(self, reason, code, action, fields=(), evidence=()):
        super().__init__(reason)
        self.issue = dict(issue_code=code, repair_action=action,
                          field_paths=list(fields), evidence=list(evidence))


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


def has_reference(value):
    if reference(value) is not None:
        return True
    children = value.values() if isinstance(value, dict) else value if isinstance(value, list) else ()
    return any(has_reference(child) for child in children)


def entry_uses_dependency(entry, identity):
    if isinstance(entry, dict):
        ref = reference(entry)
        return ref.get("id") == identity if ref else any(entry_uses_dependency(v, identity) for v in entry.values())
    return isinstance(entry, list) and any(entry_uses_dependency(v, identity) for v in entry)


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


def _evidence_sources(inspector, evidence):
    if not isinstance(evidence, list) or not evidence:
        raise ValueError("dependency needs original source evidence")
    texts = []
    for item in evidence:
        if not isinstance(item, dict):
            raise ValueError("dependency evidence must contain source ranges")
        text = _source(inspector, item["path"], item.get("unit", "file"))
        if not (type(item.get("start")) is int and type(item.get("end")) is int and
                1 <= item["start"] <= item["end"] <= len(text.splitlines())):
            raise ValueError("dependency evidence line range is invalid")
        texts.append((item, text))
    return texts


def _destination(entry, location, parameter, parameter_path=()):
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
    value = values[parameter]
    for key in parameter_path:
        if not isinstance(value, dict) or key not in value:
            raise ValueError("dependency dictionary field is absent from its actual destination")
        value = value[key]
    return value


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
    def truth(value):
        try:
            return bool(ast.literal_eval(value))
        except (ValueError, TypeError):
            return None

    def ends(value):
        if isinstance(value, (ast.Return, ast.Raise)):
            return True
        if isinstance(value, ast.If):
            known = truth(value.test)
            blocks = [value.body if known else value.orelse] if known is not None else [value.body, value.orelse]
            return all(block and any(ends(statement) for statement in block) for block in blocks)
        return False

    def body(statements):
        for statement in statements:
            yield from visit(statement)
            if ends(statement):
                break

    def visit(current):
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            return
        if isinstance(current, (ast.If, ast.IfExp)):
            yield from visit(current.test)
            known = truth(current.test)
            for branch in ((current.body,) if known is True else (current.orelse,) if known is False else (current.body, current.orelse)):
                yield from body(branch) if isinstance(branch, list) else visit(branch)
            return
        if isinstance(current, ast.While) and truth(current.test) is False:
            yield from body(current.orelse)
            return
        if isinstance(current, (ast.For, ast.AsyncFor)) and isinstance(current.iter, (ast.List, ast.Tuple, ast.Set)) and not current.iter.elts:
            yield from body(current.orelse)
            return
        if isinstance(current, ast.Call):
            yield current
        for field, value in ast.iter_fields(current):
            if field in {"body", "orelse", "finalbody"} and isinstance(value, list):
                yield from body(value)
            elif isinstance(value, list):
                for child in value:
                    if isinstance(child, ast.AST):
                        yield from visit(child)
            elif isinstance(value, ast.AST):
                yield from visit(value)
    yield from body(getattr(node, "body", []))


def _finite_dictionary_scope(node, tree):
    """Reject visible dynamic namespace access, including imported aliases.

    This is a finite source rule, not a claim about arbitrary Python callees.
    Static named external calls retain their original, unproved semantics.
    """
    reflective = {"exec", "eval", "compile", "globals", "locals", "vars", "dir",
                  "getattr", "setattr", "delattr", "__import__", "__builtins__",
                  "currentframe", "_getframe", "getframeinfo", "getouterframes",
                  "getinnerframes", "getattr_static", "getmembers", "attrgetter"}
    namespaces = {"builtins", "inspect", "importlib", "ctypes"}
    frame_attributes = {"f_locals", "f_globals", "f_builtins", "gi_frame", "cr_frame", "tb_frame"}
    bindings = {}
    for statement in tree.body:
        if isinstance(statement, (ast.Import, ast.ImportFrom)):
            for alias in statement.names:
                name = alias.asname or alias.name.split(".")[0]
                origin = ((statement.module or "").split(".")[0]
                          if isinstance(statement, ast.ImportFrom) else alias.name.split(".")[0])
                bindings.setdefault(name, []).append(origin in namespaces or alias.name in reflective)
        elif isinstance(statement, (ast.Assign, ast.AnnAssign)) and statement.value is not None:
            targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    bindings.setdefault(target.id, []).append(statement.value)

    def unsafe_name(name, seen=()):
        if name in reflective or name in namespaces:
            return True
        if name in seen:
            return False
        return any(origin is True or isinstance(origin, ast.AST) and unsafe(origin, seen + (name,))
                   for origin in bindings.get(name, ()))

    def unsafe(value, seen=()):
        for item in ast.walk(value):
            if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Load) and unsafe_name(item.id, seen):
                return True
            if isinstance(item, ast.Attribute) and (item.attr in reflective | frame_attributes
                    or item.attr.startswith("__")):
                return True
            if isinstance(item, (ast.Import, ast.ImportFrom)) and any(
                    alias.name.split(".")[0] in namespaces or alias.name in reflective
                    or isinstance(item, ast.ImportFrom) and (item.module or "").split(".")[0] in namespaces
                    for alias in item.names):
                return True
            if isinstance(item, ast.Call):
                target = item.func
                while isinstance(target, ast.Attribute):
                    target = target.value
                if not isinstance(target, ast.Name):
                    return True
        return False

    if unsafe(node):
        raise ValueError("dictionary resource binding excludes dynamic code, namespace reflection and dynamic call targets")


def _dictionary_field_call(node, parameter, keys, field, evidence, path, cell, tree):
    """Literal dictionary reads, lexical captures and single-assignment aliases.

    This checks an original resource destination, not callback execution or
    scheduling. No source or resource value is transformed by this rule.
    """
    _finite_dictionary_scope(node, tree)
    parents = {child: parent for parent in ast.walk(node) for child in ast.iter_child_nodes(parent)}

    def owners(value):
        result = []
        while value in parents:
            value = parents[value]
            if isinstance(value, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                result.append(value)
        return result

    def dictionary_read(value):
        parts, dynamic = [], False
        while isinstance(value, ast.Subscript):
            if not isinstance(value.slice, ast.Constant) or not isinstance(value.slice.value, str):
                dynamic = True
            else:
                parts.insert(0, value.slice.value)
            value = value.value
        if isinstance(value, ast.Name) and value.id == parameter:
            if dynamic:
                raise ValueError("dictionary resource reads require literal string keys")
            return tuple(parts)
        return None

    writes, assignments = {}, {}
    for item in ast.walk(node):
        if isinstance(item, ast.Name) and isinstance(item.ctx, (ast.Store, ast.Del)):
            writes[item.id] = writes.get(item.id, 0) + 1
            if item.id == parameter:
                raise ValueError("dictionary resource parameter is reassigned")
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and item.name == parameter:
            raise ValueError("dictionary resource parameter is shadowed")
        if item is not node and isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            writes[item.name] = writes.get(item.name, 0) + 1
        if isinstance(item, (ast.Import, ast.ImportFrom)):
            for alias in item.names:
                name = alias.asname or alias.name.split('.')[0]
                writes[name] = writes.get(name, 0) + 1
        if isinstance(item, ast.ExceptHandler) and item.name:
            if item.name == parameter:
                raise ValueError("dictionary resource parameter is shadowed")
            writes[item.name] = writes.get(item.name, 0) + 1
        if isinstance(item, (ast.Import, ast.ImportFrom)) and any(
                (alias.asname or alias.name.split('.')[0]) == parameter for alias in item.names):
            raise ValueError("dictionary resource parameter is shadowed")
        if isinstance(item, (ast.Global, ast.Nonlocal)) and parameter in item.names:
            raise ValueError("dictionary resource parameter has an unsupported scope declaration")
        if item is not node and isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            args = item.args.posonlyargs + item.args.args + item.args.kwonlyargs
            args += [arg for arg in (item.args.vararg, item.args.kwarg) if arg is not None]
            if any(arg.arg == parameter for arg in args):
                raise ValueError("nested callable shadows the dictionary resource parameter")
        if isinstance(item, ast.Name) and item.id == parameter and isinstance(item.ctx, ast.Load):
            parent = parents.get(item)
            if not isinstance(parent, ast.Subscript) or parent.value is not item or any(
                    isinstance(owner, (ast.ClassDef, ast.Lambda)) for owner in owners(item)):
                raise ValueError("dictionary resource parameter escapes its literal-key reads")
        if isinstance(item, ast.Subscript) and dictionary_read(item) is not None:
            if not isinstance(item.ctx, ast.Load):
                raise ValueError("dictionary resource fields are mutated")
            parent = parents.get(item)
            parts = dictionary_read(item)
            if (len(parts) < len(keys) and tuple(keys[:len(parts)]) == parts
                    and not (isinstance(parent, ast.Subscript) and parent.value is item)):
                raise ValueError("dictionary resource prefix escapes its literal-key reads")
        if isinstance(item, ast.Assign) and len(item.targets) == 1 and isinstance(item.targets[0], ast.Name):
            assignments[item.targets[0].id] = item
        elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name) and item.value is not None:
            assignments[item.target.id] = item

    # Collect only aliases of this exact field. Prefix dictionaries may never
    # escape above; leaf aliases must likewise keep their visible original use.
    aliases = set()
    def field_value(value):
        if dictionary_read(value) == tuple(keys):
            return True
        if isinstance(value, ast.Name):
            return value.id in aliases
        return (field == "base_url" and isinstance(value, ast.BinOp) and isinstance(value.op, ast.Add)
                and isinstance(value.right, ast.Constant) and isinstance(value.right.value, str)
                and field_value(value.left))
    changed = True
    while changed:
        changed = False
        for name, statement in assignments.items():
            if name not in aliases and field_value(statement.value):
                aliases.add(name)
                changed = True
    for item in ast.walk(node):
        if isinstance(item, (ast.Global, ast.Nonlocal)) and aliases.intersection(item.names):
            raise ValueError("dictionary resource alias has an unsupported scope declaration")
        if isinstance(item, ast.Name) and item.id in aliases and isinstance(item.ctx, (ast.Store, ast.Del)):
            if writes.get(item.id) != 1 or parents.get(item) is not assignments.get(item.id):
                raise ValueError("dictionary resource alias is reassigned or deleted")
        if not ((isinstance(item, ast.Name) and item.id in aliases and isinstance(item.ctx, ast.Load))
                or isinstance(item, ast.Subscript) and dictionary_read(item) == tuple(keys)):
            continue
        value, parent = item, parents.get(item)
        while (field == "base_url" and isinstance(parent, ast.BinOp) and isinstance(parent.op, ast.Add)
                and parent.left is value and isinstance(parent.right, ast.Constant)
                and isinstance(parent.right.value, str)):
            value, parent = parent, parents.get(parent)
        if isinstance(parent, (ast.Assign, ast.AnnAssign)) and parent.value is value:
            target = parent.targets[0] if isinstance(parent, ast.Assign) and len(parent.targets) == 1 else getattr(parent, "target", None)
            if isinstance(target, ast.Name) and target.id in aliases:
                continue
        call = parents.get(parent) if isinstance(parent, ast.keyword) else parent
        if isinstance(call, ast.Call) and _field_call(call, field, lambda candidate: candidate is value):
            continue
        raise ValueError("dictionary resource field or alias escapes its original field call")

    def matches(value, call, seen=()):
        if dictionary_read(value) == tuple(keys):
            return True
        # An original fixed endpoint suffix remains in the source call. The
        # selected connection must already use the source's expected base.
        if (field == "base_url" and isinstance(value, ast.BinOp) and isinstance(value.op, ast.Add)
                and isinstance(value.right, ast.Constant) and isinstance(value.right.value, str)):
            return matches(value.left, call, seen)
        if not isinstance(value, ast.Name) or value.id in seen or writes.get(value.id) != 1:
            return False
        statement = assignments.get(value.id)
        if statement is None or statement.lineno >= call.lineno or not _covered(evidence, path, cell, statement):
            return False
        if seen and statement.lineno >= assignments[seen[-1]].lineno:
            return False
        call_owners = owners(call)
        statement_owners = owners(statement)
        if (not statement_owners or statement_owners[0] not in call_owners
                or statement not in getattr(statement_owners[0], 'body', ())):
            return False
        for owner in call_owners:
            if isinstance(owner, (ast.ClassDef, ast.Lambda)):
                return False
            if isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = owner.args.posonlyargs + owner.args.args + owner.args.kwonlyargs
                args += [arg for arg in (owner.args.vararg, owner.args.kwarg) if arg is not None]
                if any(arg.arg == value.id for arg in args):
                    return False
            if owner is statement_owners[0]:
                break
        return matches(statement.value, call, seen + (value.id,))

    return any(isinstance(call, ast.Call) and _covered(evidence, path, cell, call)
               and not any(isinstance(owner, (ast.ClassDef, ast.Lambda)) for owner in owners(call))
               and _field_call(call, field, lambda value: matches(value, call)) for call in ast.walk(node))


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


def _mapping_shape(entries, row, mapping):
    if not isinstance(mapping, dict):
        raise ValueError("dependency mappings must contain parameter mapping objects")
    if not isinstance(mapping.get("entry_id"), str):
        raise ValueError("dependency mapping needs its original entry id")
    entry = entries.get(mapping.get("entry_id"))
    if entry is None:
        raise ValueError("dependency mapping entry is absent")
    location, parameter, field = mapping.get("location", ""), mapping.get("parameter"), mapping.get("field")
    if not isinstance(location, str) or (location not in {
            "constructor.arguments", "entry.fixed_arguments", "binding_program.arguments", "environment"}
            and not location.startswith("initializers.")):
        raise ValueError("unsupported original dependency parameter location")
    if not isinstance(parameter, str) or not parameter.isidentifier():
        raise ValueError("dependency needs an original explicit parameter name")
    if not isinstance(field, str) or (field not in {"base_url", "model", "credential", "path", "service_version"}
            and not (field.startswith("parameters.") and field[len("parameters."):])):
        raise ValueError("unsupported resource field")
    if (row["kind"] == "weight") != (field == "path"):
        raise ValueError("weight paths and service fields cannot be interchanged")
    keys = mapping.get("parameter_path", [])
    if not isinstance(keys, list) or len(keys) > 8 or any(
            not isinstance(key, str) or not key or key == "$dependency" for key in keys):
        raise ValueError("dependency parameter_path must contain literal dictionary string keys")
    if keys and (row["kind"] != "service" or location != "entry.fixed_arguments"):
        raise ValueError("dictionary resource fields currently require an original function argument and service")
    return entry, location, parameter, field, keys


def _check_mapping(inspector, entries, identity, row, mapping, evidence):
    entry, location, parameter, field, keys = _mapping_shape(entries, row, mapping)
    try:
        ref = reference(_destination(entry, location, parameter, keys))
    except ValueError as error:
        raise MappingGap(str(error), "candidate_incomplete",
            "在该入口的声明目标位置补齐真实资源引用，或修正映射位置；不要删除必需资源来消除缺口。",
            ["mappings.location", "mappings.parameter", "mappings.parameter_path"]) from error
    if ref != {"id": identity, "field": field}:
        raise MappingGap("dependency reference does not occupy its declared original destination", "candidate_incomplete",
            "核对原目标位置的资源引用 id/field 与这条映射一致，再完整重检。", ["mappings"])
    if "configuration_key" in mapping or "cli_override" in mapping:
        if row.get("read_time") != "process_start":
            raise MappingGap("CLI configuration injection must declare read_time=process_start", "candidate_incomplete",
                "将原 CLI 配置注入时点声明为 process_start，再核对原剩余选项和覆盖顺序。", ["read_time"])
        flow = configuration_override_flow(inspector, entry, mapping, evidence)
        if not flow["proved"]:
            raise MappingGap(flow.get("reason") or "configuration override lacks complete original source evidence",
                flow["issue_code"], "补齐原参数解析、YACS 对象、配置合并顺序、字段及加载调用证据；动态或后续覆盖保留缺口。",
                ["mappings.evidence"] if flow["missing"] else ["mappings"], flow["missing"] or flow["facts"])
        return flow
    if location == "environment":
        if entry.get("kind") != "cli":
            raise MappingGap("environment injection requires an original CLI process-start binding; in-process environment is never changed",
                "parameter_flow_unsupported", "同进程函数不支持临时环境注入；调查原 CLI 或原显式参数，保留未支持范围。", ["mappings.location"])
        if row.get("read_time") != "process_start":
            raise MappingGap("CLI environment injection must declare read_time=process_start", "candidate_incomplete",
                "将原 CLI 环境注入时点声明为 process_start；构造器/请求中的实际消费位置保留源码证据，再核对配置传递。",
                ["read_time"])
        flow = environment_flow(inspector, entry, parameter, field, evidence)
        if not flow["proved"]:
            forwarded = process_environment_flow(inspector, entry, parameter, field, evidence)
            if forwarded.get("proved") or forwarded.get("facts"):
                flow = forwarded
        if not flow["proved"]:
            raise MappingGap("environment configuration must reach an original field/loader/authentication consumer",
                flow["issue_code"], "补读环境来源、配置字段、原调用链及消费者；动态覆盖或子进程转交仍须保留缺口。",
                ["mappings.evidence"] if flow["missing"] else ["mappings"], flow["missing"] or flow["facts"])
        return flow
    else:
        if location not in {"constructor.arguments", "entry.fixed_arguments", "binding_program.arguments"} and not location.startswith("initializers."):
            raise ValueError("unsupported original dependency parameter location")
        node, path, cell = _callable(inspector, entry, location)
        args = node.args.posonlyargs + node.args.args + node.args.kwonlyargs
        original = next((arg for arg in args if arg.arg == parameter), None)
        if original is None:
            raise MappingGap("dependency destination is not an original explicit parameter", "candidate_incomplete",
                             "选择原函数确实存在的参数，不生成新参数。", ["mappings.parameter"])
        if not _covered(evidence, path, cell, original):
            raise MappingGap("dependency evidence must cover its original parameter definition", "evidence_incomplete",
                             "补充覆盖原参数定义的冻结源码范围。", ["mappings.evidence"],
                             [dict(path=path, unit=cell, start=original.lineno, end=original.end_lineno)])
        matches = lambda value: isinstance(value, ast.Name) and value.id == parameter
        direct = (_dictionary_field_call(node, parameter, keys, field, evidence, path, cell,
                    ast.parse(_source(inspector, path, cell))) if keys else
                  any(_field_call(call, field, matches) and _covered(evidence, path, cell, call) for call in _calls(node)))
        path_flow = (field == "path" and location == "constructor.arguments" and
                     _constructor_path_call(inspector, node, path, cell, parameter, evidence))
        reassigned = any(
            isinstance(n, ast.Name) and n.id == parameter and isinstance(n.ctx, (ast.Store, ast.Del))
            or isinstance(n, (ast.Import, ast.ImportFrom)) and any(
                (alias.asname or alias.name.split(".")[0]) == parameter for alias in n.names)
            or n is not node and isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                and n.name == parameter for n in ast.walk(node))
        if reassigned and not path_flow:
            raise MappingGap("reassigned resource parameter needs a separate original data-flow binding", "parameter_flow_unsupported",
                             "保留原赋值及配置分支；核对更大的原入口，当前规则不替换被覆盖的参数。")
        if not direct and not path_flow:
            flow = parameter_flow(inspector, node, path, cell, parameter, field, evidence, entry) if not keys else {}
            if not flow.get("proved"):
                missing = flow.get("missing", [])
                raise MappingGap("dependency evidence must cover the original field/loader/authentication call",
                    "evidence_incomplete" if missing else "parameter_flow_unsupported",
                    "补读下列完整定义、赋值和调用范围后修正证据。" if missing else
                    "原参数尚未连接到受支持的资源使用点；保留原入口并核对调用目标、配置分支或 SDK。",
                    ["mappings.evidence"] if missing else ["mappings"], missing)
            return flow
        return dict(proved=True, trace=[], rule="direct_or_constructor_path")


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
        row.update(status="MISSING", reason=None, execution="NOT_RUN", entry_checks={},
                   selection_allowed=False, selection_reason="original dependency mapping is unresolved")
        scope_checked = False
        affected = []
        failure_code, failure_action = "candidate_incomplete", "补齐候选中缺失或格式错误的原字段，再完整重检。"
        try:
            identity = requirement.get("id")
            if not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", identity) or identity in seen:
                raise ValueError("dependency needs a unique nonempty id")
            seen.add(identity)
            mappings = row.get("mappings", [])
            if not isinstance(mappings, list):
                raise ValueError("dependency mappings must be an array")
            declared = row.get("entry_ids", [])
            if (not isinstance(declared, list) or any(not isinstance(entry_id, str) for entry_id in declared)
                    or set(declared) - set(entries)):
                raise ValueError("dependency entry_ids must reference proposed entries")
            affected = list(dict.fromkeys(declared))
            for mapping in mappings:
                if (not isinstance(mapping, dict) or not isinstance(mapping.get("entry_id"), str)
                        or mapping["entry_id"] not in entries):
                    raise ValueError("dependency mapping entry is absent or ambiguous")
                if mapping["entry_id"] not in affected:
                    affected.append(mapping["entry_id"])
            if not affected:
                raise ValueError("dependency must identify its affected original entries")
            scope_checked = True
            row["entry_ids"] = affected
            if row.get("kind") not in {"service", "weight"}:
                raise ValueError("dependency kind must be service or weight")
            if type(row.get("configurable")) is not bool or type(row.get("required", True)) is not bool:
                raise ValueError("dependency needs explicit configurable and boolean required")
            if row.get("read_time") not in {"constructor", "call", "process_start"} or not isinstance(row.get("owner_scope"), str) or not row["owner_scope"].strip():
                raise ValueError("dependency needs its original read time and owner scope")
            evidence = row.get("evidence")
            failure_code, failure_action = "evidence_incomplete", "提供有效冻结源码范围，覆盖完整定义与调用。"
            texts = _evidence_sources(inspector, evidence)
            failure_code, failure_action = "candidate_incomplete", "核对映射目的位置、原参数和资源字段。"
            # Malformed fields are row-level errors. Only checks of an identified
            # entry's actual source/destination may leave another entry usable.
            for mapping in mappings:
                _mapping_shape(entries, row, mapping)
                if "evidence" in mapping:
                    failure_code, failure_action = "evidence_incomplete", "提供有效的逐映射源码证据；空或越界证据不能回退。"
                    _evidence_sources(inspector, mapping["evidence"])
            failure_code, failure_action = "candidate_incomplete", "补齐候选中缺失或格式错误的原字段，再完整重检。"
            configuration_evidence = evidence + [item for mapping in mappings for item in mapping.get("evidence", [])]
            row["configuration_flow"] = configuration_facts(inspector, configuration_evidence)
            if row["kind"] == "weight":
                origin_evidence = evidence + [item for mapping in mappings for item in mapping.get("evidence", [])]
                row["resource_origins"] = resource_origin_facts(inspector, origin_evidence)
            errors, proofs = {}, {}
            for mapping_index, mapping in enumerate(mappings):
                try:
                    proof = _check_mapping(inspector, entries, identity, row, mapping,
                                           mapping.get("evidence", evidence))
                    proofs.setdefault(mapping["entry_id"], []).append(dict(parameter=mapping["parameter"],
                        field=mapping["field"], location=mapping["location"], proof=proof))
                except (ValueError, KeyError, TypeError, SyntaxError, OSError) as error:
                    reason = str(error) if isinstance(error, ValueError) else "original source parameter mapping could not be checked"
                    detail = getattr(error, "issue", dict(issue_code="parameter_flow_unsupported",
                        repair_action="核对原参数及完整调用；未支持的结构保留缺口。", field_paths=["mappings"]))
                    detail = dict(detail, field_paths=[
                        "mappings[" + str(mapping_index) + "]" + value[len("mappings"):]
                        if value == "mappings" or value.startswith("mappings.") else value
                        for value in detail.get("field_paths", [])],
                        parameter=mapping["parameter"], location=mapping["location"], field=mapping["field"])
                    errors.setdefault(mapping["entry_id"], []).append(dict(detail, reason=reason))
            if not row["configurable"]:
                if row["kind"] != "weight" or mappings:
                    raise ValueError("fixed placement supports original weight locations without parameter overrides")
                placement = row.get("placement") or {}
                expression = placement.get("expression")
                if not isinstance(expression, str) or not expression.strip():
                    raise ValueError("fixed weight dependency needs placement.expression from its original source")
                failure_code, failure_action = "evidence_incomplete", "补查固定路径的原表达式与完整加载调用。"
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
                for fact in row.get("resource_origins", []):
                    if (fact.get("source_kind") in {"hub_or_local", "hub_id", "cache_directory"} and
                            (fact.get("expression") == expression or fact.get("expression") in aliases)):
                        raise MappingGap("Hub identifiers and cache directories are not verified fixed model paths",
                            "resource_locator_unsupported", "核对原模型标识、版本、配置分支和缓存位置；不能把标识或缓存当权重文件路径。",
                            ["placement"], [fact])
                if not any(isinstance(call, ast.Call) and _field_call(call, "path", matches) and
                           _covered(evidence, item["path"], item.get("unit", "file"), call) for call in ast.walk(tree)):
                    if any(fact.get("accepted_origins") in {"hub_id", "hub_or_local"} for fact in row.get("resource_origins", [])):
                        raise MappingGap("fixed placement does not resolve the original Hub/local model loader or configuration branch",
                            "resource_locator_unsupported", "保留原加载器及选择分支，核对模型标识与本地缓存；扩大证据范围不能把分支变量变成固定路径。",
                            ["placement"], row["resource_origins"])
                    raise ValueError("fixed placement evidence must cover its original model-loader call")
                required = placement.get("required_files", [])
                if not isinstance(required, list) or any(not isinstance(name, str) or not name or Path(name).is_absolute()
                    or ".." in Path(name).parts for name in required):
                    raise ValueError("fixed placement requires original relative necessary-file names")
                path = Path(_path_expression(node, inspector.root, inspector.root / item["path"]))
                path = (inspector.root / path).resolve() if not path.is_absolute() else path.resolve()
                row["placement"] = dict(placement, resolved_path=str(path), execution_environment="platform_process",
                    original_cwd=str(inspector.root), contents="NOT_LOADED")
            for entry_id in affected:
                count = sum(mapping["entry_id"] == entry_id for mapping in mappings)
                issues = errors.get(entry_id, [])
                if row["configurable"] and not count:
                    merged_configuration = any(f["kind"] == "configuration_merge" for f in row["configuration_flow"])
                    issues = [dict(reason=("original configuration merge/override to loader is unresolved" if merged_configuration else
                        "configurable dependency has no original parameter mapping for this affected entry"),
                        issue_code="configuration_flow_unsupported" if merged_configuration else "candidate_incomplete", field_paths=["mappings"],
                        evidence=row["configuration_flow"] if merged_configuration else [],
                        repair_action="核对原配置来源、合并次序、条件覆盖和字段加载；保留原入口与未知，不把 YAML 字段冒充函数参数。" if merged_configuration else
                        "为这个入口保留原资源需求，提出真实原参数映射；无法绑定时记录支持缺口。")]
                row["entry_checks"][entry_id] = dict(status="MISSING" if issues else "MAPPING_CHECKED",
                    reason="; ".join(dict.fromkeys(i["reason"] for i in issues)) if issues else None,
                    mapping_count=count, issues=issues, mapping_proofs=proofs.get(entry_id, []),
                    **({k: issues[0][k] for k in ("issue_code", "repair_action", "field_paths")} if issues else {}))
            missing = [entry_id + ": " + check["reason"] for entry_id, check in row["entry_checks"].items()
                       if check["status"] != "MAPPING_CHECKED"]
            row.update(status="MISSING" if missing else "MAPPING_CHECKED", reason="; ".join(missing) if missing else None,
                source_version=inspector.request["source_version"],
                semantic_match="SOURCE_AND_PARAMETER_BOUND_PER_ENTRY; capability compatibility remains unverified")
            row["selection_allowed"] = row["configurable"] and any(
                check["status"] == "MAPPING_CHECKED" for check in row["entry_checks"].values())
            row["selection_reason"] = (None if row["selection_allowed"] else
                "original fixed resource must be prepared in place" if not row["configurable"] else row["reason"])
        except (ValueError, KeyError, TypeError, SyntaxError, OSError) as error:
            # Keep source-derived details but never echo runtime credentials.
            row["reason"] = str(error) if isinstance(error, ValueError) else "source requirement, original destination or finite placement could not be checked"
            detail = getattr(error, "issue", dict(issue_code=failure_code, repair_action=failure_action,
                field_paths=["dependencies"]))
            if not scope_checked:
                # An unresolvable required scope must not silently affect no entry.
                affected = [entry_id for entry_id in entries if isinstance(entry_id, str)]
                row["entry_ids"] = affected
            invalid_mappings = row.get("mappings", [])
            invalid_mappings = invalid_mappings if isinstance(invalid_mappings, list) else []
            row["entry_checks"] = {entry_id: dict(status="MISSING", reason=row["reason"],
                **detail, issues=[dict(detail, reason=row["reason"])],
                mapping_count=sum(isinstance(mapping, dict) and mapping.get("entry_id") == entry_id
                                  for mapping in invalid_mappings))
                for entry_id in affected}
            row["selection_reason"] = row["reason"]
        row["definition_digest"] = fingerprint(requirement)
        result.append(row)
    return result


def _entry_mapping_check(row, entry_id):
    checks = row.get("entry_checks")
    if isinstance(checks, dict):
        return checks.get(entry_id, dict(status="MISSING", reason="entry has no checked original resource mapping"))
    # Persisted records from before per-entry reporting keep their old meaning;
    # actual candidate binding always recomputes the current finite checks.
    return dict(status=row.get("status"), reason=row.get("reason"))


def _selection_allowed(row):
    return row.get("selection_allowed", row.get("status") == "MAPPING_CHECKED") is True


class DependencyBindings:
    """An installation's frozen nonsecret configuration and private key resolver."""
    def __init__(self, registry, inspector, candidate):
        self.registry, self.inspector = registry, inspector
        self.entries = {e.get("id"): e for e in candidate.get("entries", []) if isinstance(e, dict)}
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

    def _selection(self, identity, entry_id):
        row = self.rows.get(identity)
        checked = _entry_mapping_check(row, entry_id) if row else {}
        if not row or checked.get("status") != "MAPPING_CHECKED":
            raise ValueError("resource dependency " + identity + " is unresolved for entry " + str(entry_id)
                             + ": " + str(checked.get("reason") or "no checked original mapping"))
        selection = self.selected.get(identity)
        if not selection or selection.get("definition_digest") != row["definition_digest"]:
            raise ValueError("resource dependency is unselected or its original mapping has changed")
        return row, selection

    def value(self, value, entry_id, location, parameter, runtime=False, parameter_path=()):
        ref = reference(value)
        if ref is None:
            return value
        row, selection = self._selection(ref["id"], entry_id)
        if not any(m.get("entry_id") == entry_id and m.get("location") == location and
                   m.get("parameter") == parameter and m.get("field") == ref["field"]
                   and tuple(m.get("parameter_path", [])) == tuple(parameter_path) for m in row["mappings"]):
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
        def resolve(value, parameter, keys=()):
            if reference(value) is not None:
                return self.value(value, entry_id, location, parameter, runtime, parameter_path=keys)
            if isinstance(value, dict):
                return {key: resolve(child, parameter, keys + (key,)) for key, child in value.items()}
            if isinstance(value, list):
                return [resolve(child, parameter, keys + (index,)) for index, child in enumerate(value)]
            return value
        return {parameter: resolve(value, parameter) for parameter, value in values.items()}

    def check_entry(self, entry_id):
        for identity, row in self.rows.items():
            entry = self.entries.get(entry_id, {})
            if entry_id not in row.get("entry_ids", []) or (row.get("required", True) is False and
                    not entry_uses_dependency(entry, identity)):
                continue
            checked = _entry_mapping_check(row, entry_id)
            if checked.get("status") != "MAPPING_CHECKED":
                raise ValueError("required resource " + identity + " is unresolved for entry " + str(entry_id)
                                 + ": " + str(checked.get("reason") or "no checked original mapping"))
            if row["configurable"]:
                mappings = [mapping for mapping in row["mappings"] if mapping["entry_id"] == entry_id]
                if not mappings:
                    raise ValueError("required resource " + identity + " has no original parameter mapping for entry " + str(entry_id))
                self._selection(identity, entry_id)
                for mapping in mappings:
                    value = {"$dependency": {"id": identity, "field": mapping["field"]}}
                    self.value(value, entry_id, mapping["location"], mapping["parameter"], runtime=False,
                               parameter_path=mapping.get("parameter_path", []))
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
            if (row and _selection_allowed(row) and selection.get("status") == "SELECTED"
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
                or not _selection_allowed(row)):
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
            if inside_platform_checkout(self.resources.home / "sources", resource.get("path")):
                raise ValueError("这份权重的路径在平台来源克隆里，不能绑定到当前来源。请改登记到独立目录。")
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
