"""Source-only, multi-axis investigation hints; never grants binding permissions."""

import ast
from pathlib import PurePosixPath

from .inspection import SourceWithheld, units


FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
SCOPE_NODES = (*FUNCTIONS, ast.ClassDef, ast.Lambda)
EVIDENCE_LIMIT = 8
LABELS = {
    "unknown": "尚未确定", "function": "函数入口形态", "object": "对象与方法形态",
    "cli": "命令行入口线索", "workflow": "多调用控制流程线索",
    "retrieval": "检索／RAG 线索", "tool_agent": "工具注册／Agent 线索",
    "guardrail": "检查／约束线索", "critic_revision": "反馈／修订线索",
    "model_inference": "模型推理线索", "offline_training_evaluation": "离线训练／评测线索",
    "async": "异步调用", "loop": "循环控制", "branch": "条件分支", "exception": "异常与清理控制",
    "parameters": "显式参数", "instance_state": "实例属性访问", "state_write": "可见状态写入",
    "lifecycle": "原类初始化", "cleanup": "显式清理调用", "generator": "生成器／流式产出",
    "model_load": "本地模型加载线索", "service": "外部服务调用线索",
    "configuration": "配置读取线索", "file_io": "文件读写线索", "process": "子进程调用线索",
    "dynamic_call": "动态调用／导入未决", "external_call": "调用效果未核对",
}


def _profile(inspector, path, unit="file", symbol=None, kind="module"):
    return dict(path=path, unit=unit, symbol=symbol, kind=kind,
        source_version=inspector.request.get("source_version"), forms=[], families=[], traits=[], recipes=[],
        unknowns=[], execution="NOT_RUN", admission_granted=False,
        scope="仅所列冻结源码范围及明确标出的原类初始化；标签不证明功能、绑定、W/H/R、并行或远程许可")


def _evidence(inspector, profile, node=None, kind="syntax", **extra):
    record = getattr(inspector, "files", {}).get(profile["path"], {})
    return dict(path=profile["path"], unit=profile["unit"],
        start=getattr(node, "lineno", 1), end=getattr(node, "end_lineno", getattr(node, "lineno", 1)),
        sha256=record.get("sha256"), kind=kind, **extra)


def _tag(profile, axis, identity, evidence=None, status="SOURCE_SIGNAL"):
    existing = next((item for item in profile[axis] if item["id"] == identity), None)
    if existing is None:
        existing = dict(id=identity, label=LABELS[identity], status=status, evidence=[],
                        evidence_count=0, evidence_truncated=False)
        profile[axis].append(existing)
    _append_evidence(existing, evidence)


def _append_evidence(row, evidence):
    if evidence and evidence not in row["evidence"]:
        row["evidence_count"] += 1
        if len(row["evidence"]) < EVIDENCE_LIMIT:
            row["evidence"].append(evidence)
        row["evidence_truncated"] = row["evidence_count"] > len(row["evidence"])


def _unknown(profile, identity, reason, evidence=None):
    row = next((item for item in profile["unknowns"] if item["id"] == identity), None)
    if row is None:
        row = dict(id=identity, status="UNKNOWN", reason=reason, evidence=[],
                   evidence_count=0, evidence_truncated=False)
        profile["unknowns"].append(row)
    _append_evidence(row, evidence)


def _walk(node):
    """Keep nested callable bodies separate from the selected callable's facts."""
    yield node
    for child in ast.iter_child_nodes(node):
        if isinstance(child, SCOPE_NODES):
            continue
        yield from _walk(child)


def _name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _name(node.value)
        return base + "." + node.attr if base else ""
    return ""


def _imports(tree):
    result = {}
    # These imports only resolve call spelling. Unused imports never label a target.
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                result[alias.asname or alias.name.split(".")[0]] = alias.name if alias.asname else alias.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                result[alias.asname or alias.name] = (node.module or "") + "." + alias.name
    return result


def _qualified(node, imports):
    name = _name(node)
    head, _, tail = name.partition(".")
    return imports.get(head, head) + ("." + tail if tail else "") if name else ""


def _is_main(node):
    if not isinstance(node, ast.If) or not isinstance(node.test, ast.Compare):
        return False
    test = node.test
    return len(test.ops) == 1 and isinstance(test.ops[0], ast.Eq) and any(
        isinstance(item, ast.Name) and item.id == "__name__" for item in [test.left, *test.comparators]) and any(
        isinstance(item, ast.Constant) and item.value == "__main__" for item in [test.left, *test.comparators])


def _recipes(profile):
    path, unit = profile["path"], profile["unit"]
    hints = {item["id"] for axis in ("forms", "families", "traits") for item in profile[axis]}
    def add(identity, title, reason, steps):
        profile["recipes"].append(dict(id=identity, title=title, reason=reason, steps=steps))
    def step(tool, purpose, **arguments):
        return dict(tool=tool, arguments=arguments, purpose=purpose)
    def read(purpose, *tags):
        anchors = [ev["start"] for axis in ("forms", "families", "traits") for tag in profile[axis]
                   if tag["id"] in tags for ev in tag["evidence"]]
        start = min(anchors) if anchors else profile.get("source_range", {}).get("start", 1)
        return step("read_file", purpose, path=path, unit=unit, start=max(1, start - 12), count=160)
    add("source_scope", "先核对原入口与调用位置", "分类只提供线索；继续调查未命中区域，不以标签排除源码。", [
        step("inspect_symbols", "核对本文件符号与真实位置", path=path),
        read("从目标附近读取原控制流与参数上下文；超出窗口的部分继续补读")])
    if hints & {"object", "workflow", "lifecycle", "instance_state", "cli", "dynamic_call"}:
        add("construction_lifecycle", "核对原构造、状态与生命周期", "保持原对象、调用顺序及清理范围；动态关系仍需补读。", [
            step("inspect_construction", "核对原参数、对象来源和调用顺序", path=path, unit=unit)])
    if hints & {"model_load", "service", "configuration", "model_inference", "retrieval"}:
        add("resource_mapping", "核对资源读取与原参数映射", "加载/API 语法不证明文件存在、服务可达或当前映射受支持。", [
            step("inspect_imports", "查明原加载器与客户端来源", path=path, unit=unit),
            read("核对资源字段、读取时点及 owner", "model_load", "service", "configuration")])
    recipes = [
        ("retrieval", "retrieval_flow", "追查检索输入、索引与结果去向",
         "检索可能只是辅助调用；先核对检索结果是否进入目标输出及原控制流程。",
         "沿检索调用查索引来源、嵌入模型、查询输入及结果消费位置；保留共享索引和缓存的 owner。"),
        ("tool_agent", "tool_registration", "追查工具注册与原控制器",
         "工具列表是原生交接的候选线索；需由已有规则核对实际注册位置。",
         "核对工具列表、签名及注册前后时点，追查实际发起工具调用的 controller 和回调状态。"),
        ("guardrail", "guardrail_gate", "追查检查结果如何控制后续执行",
         "出现 check/validate 不能证明它是安全护栏，也不能证明检查点可任意移动。",
         "核对检查发生在输入、输出还是工具调用前后，追踪通过、拒绝、异常和修复分支及返回值消费。"),
        ("critic_revision", "feedback_loop", "追查反馈、修订与终止条件",
         "反馈和修订可能跨轮共享状态；拆分不能更改原迭代、停止或接受条件。",
         "追踪被评对象、反馈输入、修订结果和下一轮状态，核对停止、重试次数及接受结果的位置。"),
        ("model_inference", "inference_lifecycle", "追查模型加载与单次推理边界",
         "推理调用与模型构造具有不同生命周期；保留原加载、设备和输出处理语义。",
         "核对模型和处理器构造、预处理、推理及后处理，明确真实任务参数、驻留对象和关闭位置。"),
        ("offline_training_evaluation", "offline_online_role", "核对离线与在线调用角色",
         "训练／评测线索不表示整个文件都离线，也不能以辅助入口替代目标在线能力。",
         "核对训练或评测调用的实际上下文、数据和产物；区分准备过程、原在线入口和评价工具。"),
    ]
    for family, identity, title, reason, purpose in recipes:
        if family in hints:
            add(identity, title, reason, [read(purpose, family),
                step("inspect_construction", "核对上述调用的原坐标、参数来源及控制上下文", path=path, unit=unit)])


def _analyze(inspector, path, unit, tree, selected=None, owner=None, kind="module", symbol=None):
    profile = _profile(inspector, path, unit, symbol, kind)
    imports = _imports(tree)
    if selected is None:
        roots = list(tree.body)
        if any(isinstance(n, FUNCTIONS) for n in roots):
            _tag(profile, "forms", "function", _evidence(inspector, profile, next(n for n in roots if isinstance(n, FUNCTIONS))), "SOURCE_FACT")
        for node in roots:
            if isinstance(node, ast.ClassDef):
                _tag(profile, "forms", "object", _evidence(inspector, profile, node), "SOURCE_FACT")
        # File profiles cover callable bodies, but do not mix them into target profiles.
        scopes = [n for n in ast.walk(tree) if isinstance(n, FUNCTIONS)]
        roots += scopes
    else:
        roots = [selected]
        profile["source_range"] = dict(start=selected.lineno, end=selected.end_lineno)
        _tag(profile, "forms", "object" if owner or isinstance(selected, ast.ClassDef) else "cli" if kind == "cli" else "function",
             _evidence(inspector, profile, selected), "SOURCE_FACT")
        if isinstance(selected, ast.ClassDef):
            roots += [node for node in selected.body if isinstance(node, FUNCTIONS)]
            owner = selected
        descriptors = {_name(item) for item in getattr(selected, "decorator_list", [])}
        if owner and not descriptors.intersection({"staticmethod", "classmethod"}):
            constructor = next((n for n in owner.body if isinstance(n, FUNCTIONS) and n.name == "__init__"), None)
            if constructor is not None:
                if constructor is not selected:
                    roots.append(constructor)
                _tag(profile, "traits", "lifecycle", _evidence(inspector, profile, constructor, relation="same_class_initializer"), "SOURCE_FACT")
            if owner.bases:
                _unknown(profile, "inherited_lifecycle", "继承类初始化与方法解析未沿跨类关系核对。", _evidence(inspector, profile, owner))
    nodes = list({id(node): node for root in roots for node in _walk(root)}.values())
    calls = [n for n in nodes if isinstance(n, ast.Call)]
    callback_parameters = {arg.arg for root in roots if isinstance(root, FUNCTIONS)
        for arg in [*root.args.posonlyargs, *root.args.args, *root.args.kwonlyargs]}
    controls = [n for n in nodes if isinstance(n, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try, ast.With, ast.AsyncWith))]
    if controls and len(calls) > 1:
        _tag(profile, "forms", "workflow", _evidence(inspector, profile, controls[0]))
    for node in nodes:
        ev = _evidence(inspector, profile, node)
        if _is_main(node):
            _tag(profile, "forms", "cli", ev)
        if isinstance(node, (ast.AsyncFunctionDef, ast.Await, ast.AsyncFor, ast.AsyncWith)):
            _tag(profile, "traits", "async", ev, "SOURCE_FACT")
        if isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
            _tag(profile, "traits", "loop", ev, "SOURCE_FACT")
        if isinstance(node, (ast.If, ast.IfExp, ast.Match)):
            _tag(profile, "traits", "branch", ev, "SOURCE_FACT")
        if isinstance(node, (ast.Try, ast.With, ast.AsyncWith)):
            _tag(profile, "traits", "exception", ev, "SOURCE_FACT")
        if isinstance(node, (ast.Yield, ast.YieldFrom)):
            _tag(profile, "traits", "generator", ev, "SOURCE_FACT")
        if isinstance(node, FUNCTIONS):
            args = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
            if args or node.args.vararg or node.args.kwarg:
                _tag(profile, "traits", "parameters", _evidence(inspector, profile, node,
                    parameters=[arg.arg for arg in args], varargs=bool(node.args.vararg), kwargs=bool(node.args.kwarg)), "SOURCE_FACT")
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in {"self", "cls"}:
            _tag(profile, "traits", "instance_state", ev)
            if isinstance(node.ctx, (ast.Store, ast.Del)):
                _tag(profile, "traits", "state_write", ev, "SOURCE_FACT")
        if isinstance(node, (ast.Global, ast.Nonlocal)):
            _tag(profile, "traits", "state_write", ev)
    for call in calls:
        name = _qualified(call.func, imports)
        leaf = name.rsplit(".", 1)[-1]
        ev = _evidence(inspector, profile, call, "call", call=name or "dynamic expression")
        if not name or name in callback_parameters or leaf in {"getattr", "eval", "exec", "__import__", "import_module"}:
            _tag(profile, "traits", "dynamic_call", ev)
            _unknown(profile, "dynamic_resolution", "动态调用目标或导入无法由本次语法分类确定。", ev)
        if name.startswith("argparse.") or leaf in {"parse_args", "parse_known_args"} or name.startswith(("click.", "typer.")):
            _tag(profile, "forms", "cli", ev)
        if leaf in {"search", "retrieve", "similarity_search", "get_relevant_documents", "as_retriever", "query_points"}:
            _tag(profile, "families", "retrieval", ev, "HYPOTHESIS")
        if leaf in {"register_tool", "register_tools", "bind_tools", "create_agent", "create_react_agent", "add_tool"} or any(k.arg == "tools" for k in call.keywords):
            _tag(profile, "families", "tool_agent", ev, "HYPOTHESIS")
        if name.startswith(("guardrails.", "nemoguardrails.")) or leaf in {"validate", "check", "check_input", "check_output"}:
            _tag(profile, "families", "guardrail", ev, "HYPOTHESIS")
        if leaf in {"critique", "revise", "revision", "feedback", "reflect"}:
            _tag(profile, "families", "critic_revision", ev, "HYPOTHESIS")
        if leaf in {"generate", "predict", "inference", "inference_mode"} or ".chat.completions.create" in name or ".responses.create" in name:
            _tag(profile, "families", "model_inference", ev, "HYPOTHESIS")
        if leaf in {"fit", "train", "evaluate", "backward", "benchmark"}:
            _tag(profile, "families", "offline_training_evaluation", ev, "HYPOTHESIS")
        if leaf in {"from_pretrained", "load_state_dict"} or name in {"torch.load", "joblib.load", "safetensors.torch.load_file"}:
            _tag(profile, "traits", "model_load", ev)
        if name.startswith(("requests.", "httpx.", "aiohttp.", "openai.")) or ".chat.completions.create" in name or ".responses.create" in name:
            _tag(profile, "traits", "service", ev)
        if name in {"os.getenv", "os.environ.get", "json.load", "yaml.safe_load", "tomllib.load"}:
            _tag(profile, "traits", "configuration", ev)
        if name == "open" or leaf in {"read_text", "read_bytes", "write_text", "write_bytes"}:
            _tag(profile, "traits", "file_io", ev)
        if name.startswith(("subprocess.", "os.system", "os.popen")):
            _tag(profile, "traits", "process", ev)
        if leaf in {"close", "aclose", "shutdown", "cleanup"}:
            _tag(profile, "traits", "cleanup", ev)
    if calls:
        _tag(profile, "traits", "external_call", _evidence(inspector, profile, calls[0]))
    for axis in ("forms", "families", "traits"):
        if not profile[axis]:
            _tag(profile, axis, "unknown", status="UNKNOWN")
    _unknown(profile, "semantic_family", "功能标签仅是原语法/API 调用线索；真实业务含义和未命中区域仍需源码调查。")
    _unknown(profile, "resource_availability", "未读取资源、导入源码、加载模型或访问服务；资源需求完整性、可用性与兼容性未核对。")
    _unknown(profile, "state_effect_closure", "未证明跨调用共享状态、外部效果及清理闭包；未授予任何执行或拆分许可。")
    _recipes(profile)
    return profile


def _unavailable(inspector, path, unit, symbol, kind, reason, identity):
    profile = _profile(inspector, path, unit, symbol, kind)
    for axis in ("forms", "families", "traits"):
        _tag(profile, axis, "unknown", status="UNKNOWN")
    reasons = {"WITHHELD": "源码因认证字面量被排除，本次不读取或分类。",
        "SOURCE_NOT_FROZEN": "源码不在当前冻结文件范围内。",
        "NON_PYTHON_NOT_INTERPRETED": "非 Python 文件仅保留范围；未解释配置、脚本或其它语言的实际语义。",
        "SOURCE_UNAVAILABLE_OR_INVALID": "冻结源码不可读或结构无效，未推断功能。",
        "PARSE_UNSUPPORTED": "当前解释器无法解析该源码单元，未推断其功能。",
        "NO_CODE_UNIT": "该文件没有可分析的 Python 代码单元。"}
    _unknown(profile, identity, reasons.get(reason, reason))
    return profile


def _parsed(inspector, path):
    """Return parsed frozen code units, or an explicit unknown without source text."""
    blocked = any(row.get("path") == path for row in inspector.request.get("withheld_sources", []))
    if blocked:
        return [("file", None, "WITHHELD")]
    if path not in getattr(inspector, "files", {}):
        return [("file", None, "SOURCE_NOT_FROZEN")]
    if PurePosixPath(path).suffix.lower() not in {".py", ".pyi", ".ipynb"}:
        return [("file", None, "NON_PYTHON_NOT_INTERPRETED")]
    try:
        available = units(path, inspector.data(path))
    except SourceWithheld:
        return [("file", None, "WITHHELD")]
    except (ValueError, TypeError, AttributeError, KeyError, UnicodeError, OSError):
        return [("file", None, "SOURCE_UNAVAILABLE_OR_INVALID")]
    result = []
    for unit, source in available:
        try:
            tree = ast.parse(source)
        except (SyntaxError, ValueError, TypeError):
            result.append((unit, None, "PARSE_UNSUPPORTED"))
        else:
            result.append((unit, tree, None))
    return result or [("file", None, "NO_CODE_UNIT")]


def inspect_harness(inspector, path="", offset=0, count=20):
    """Page frozen source files; notebooks may yield several cell profiles per file."""
    if not isinstance(path, str) or type(offset) is not int or offset < 0 or type(count) is not int or count < 1:
        raise ValueError("classification requires a path prefix, nonnegative offset and positive file count")
    prefix = path.replace("\\", "/")
    paths = sorted(set(getattr(inspector, "files", {})) | {
        row["path"] for row in inspector.request.get("withheld_sources", []) if isinstance(row.get("path"), str)})
    paths = [name for name in paths if name.startswith(prefix)]
    selected = paths[offset:offset + count]
    profiles = []
    for name in selected:
        for unit, tree, error in _parsed(inspector, name):
            profiles.append(_unavailable(inspector, name, unit, None, "module", error, error) if error else
                _analyze(inspector, name, unit, tree, kind="cell" if unit.startswith("cell:") else "module"))
    return dict(schema="harness-classification/1", source_version=inspector.request.get("source_version"),
        profiles=profiles, total=len(paths), next_offset=offset + count if offset + count < len(paths) else None,
        scope=dict(path=prefix, pagination_unit="file", files=selected, offset=offset,
                   whole_repository_coverage=False, detail="仅当前页冻结文件；未命中与未解析部分不表示没有能力或依赖。"),
        execution="NOT_RUN", admission_granted=False)


def classify_target(inspector, target):
    """Classify only the named original target plus its direct class initializer."""
    if getattr(inspector, "target_runtime", None):
        return inspector.target_runtime.call("classify_target", target=target)
    if not isinstance(target, dict) or not isinstance(target.get("path"), str) or not target["path"]:
        return _unavailable(inspector, "", "file", None, "unknown",
                            "目标缺少有效的冻结源码路径，需修正候选后重新检查。", "INVALID_TARGET")
    path, unit = target["path"], target.get("unit", "file")
    symbol, kind = target.get("symbol"), target.get("kind", "function")
    for available_unit, tree, error in _parsed(inspector, path):
        if error and available_unit == "file" and tree is None:
            return _unavailable(inspector, path, unit, symbol, kind, error, error)
        if available_unit != unit:
            continue
        if error:
            return _unavailable(inspector, path, unit, symbol, kind, error, error)
        selected, owner = None, None
        if kind == "cli":
            # Native CLI records may name the script rather than a Python symbol.
            # Classify its file scope, without pretending to resolve the runtime call graph.
            profile = _analyze(inspector, path, unit, tree, kind=kind, symbol=symbol)
            profile["scope"] = "命令行目标的冻结脚本范围；包含文件内定义，不证明实际 CLI 调用路径、参数或执行许可"
            return profile
        if isinstance(symbol, str):
            scope = tree
            for part in symbol.split("."):
                matches = [node for node in getattr(scope, "body", []) if isinstance(node, (*FUNCTIONS, ast.ClassDef)) and node.name == part]
                if len(matches) != 1:
                    selected = None
                    break
                selected = matches[0]
                if isinstance(scope, ast.ClassDef):
                    owner = scope
                scope = selected
        if selected is None or not isinstance(selected, (*FUNCTIONS, ast.ClassDef)):
            return _unavailable(inspector, path, unit, symbol, kind, "目标符号未在指定冻结源码单元中唯一定位。", "TARGET_NOT_LOCATED")
        return _analyze(inspector, path, unit, tree, selected, owner, kind, symbol)
    return _unavailable(inspector, path, unit, symbol, kind, "指定源码单元不存在或无法读取。", "UNIT_NOT_LOCATED")
