"""Run the existing source tools in a selected, isolated Python interpreter.

The checker image contains platform code and its parser only. Original source
is transported as frozen bytes and parsed; it is never imported or installed by
this protocol. Harness dependencies belong to the later runtime-group image.
"""

import ast
import base64
import copy
import json
import os
import sys
import tempfile
from functools import lru_cache
from pathlib import Path
from types import MethodType, SimpleNamespace

from . import backends
from .inspection import Inspector, digest, safe_path


TOOLS = {"inspect_symbols", "inspect_construction", "inspect_imports", "entry_candidates",
         "inspect_harness", "classify_target", "check_candidate", "check_preprocessing", "check_requirements", "inspect_candidate"}
ENV_FIELDS = ("python", "base_image", "requirements", "project", "packages", "pip_extra_index_url",
              "python_requirements")


def environment_selection(environment):
    return {key: copy.deepcopy(environment[key]) for key in ENV_FIELDS if key in environment}


CORE_SCOPES = {"worker": "worker", "checker": "target_runtime"}


@lru_cache(maxsize=128)
def _implementation_source(data, relative):
    """Cache parsing by exact bytes, so same-size/mtime edits remain visible."""
    tree = ast.parse(data, filename=relative)
    native_calls = set()
    if relative == "units.py":
        # Only this explicit frozen-source loader is outside the platform
        # module closure. Other dynamic imports, even in units.py, must fail.
        for owner in tree.body:
            if isinstance(owner, ast.ClassDef) and owner.name == "NativeBindings":
                for method in owner.body:
                    if isinstance(method, ast.FunctionDef) and method.name == "_namespace":
                        native_calls.update(id(node) for node in ast.walk(method)
                            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                            and isinstance(node.func.value, ast.Name) and node.func.value.id == "importlib"
                            and node.func.attr == "import_module" and len(node.args) == 1
                            and isinstance(node.args[0], ast.Name) and node.args[0].id == "module"
                            and not node.keywords)
    imports, reads_version, metadata_hash = [], False, None
    version_name = "__" + "version__"
    for node in ast.walk(tree):
        if relative != "__init__.py" and (
                isinstance(node, ast.Name) and node.id == version_name
                or isinstance(node, ast.Attribute) and node.attr == version_name
                or isinstance(node, ast.Constant) and node.value == version_name):
            reads_version = True
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            imports.append(node)
        elif isinstance(node, ast.Call) and isinstance(node.func, (ast.Name, ast.Attribute)):
            name = node.func.id if isinstance(node.func, ast.Name) else node.func.attr
            if name in {"import_module", "__import__"} and id(node) not in native_calls:
                imports.append(node)
    if relative == "__init__.py":
        # Only a metadata-only initializer can omit its literal package version.
        metadata = all(
            isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
            or isinstance(node, ast.Assign) and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name) and node.targets[0].id == version_name
            and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
            for node in tree.body)
        if metadata:
            normalized = ast.Module(body=[node for node in tree.body if not isinstance(node, ast.Assign)], type_ignores=[])
            metadata_hash = digest(ast.dump(normalized, include_attributes=False).encode())
    return digest(data), tuple(imports), reads_version, metadata_hash


def core_identity(scope="worker"):
    """Hash the complete local import closure of the actual image entrypoint.

    Imports in every branch/function are included, not just modules imported by
    the current process. Native source loaded by Unit bindings has its separate
    frozen-source identity; it does not select platform implementation modules.
    No coordinator, UI or training module is excluded if the closure imports it.
    """
    if scope not in CORE_SCOPES:
        raise ValueError("unknown platform implementation scope")
    root = Path(__file__).parent
    package = root.name
    pending, sources = [CORE_SCOPES[scope], ""], {}
    reads_version = False
    metadata_hash = None

    def path_for(module):
        if not module:
            return root / "__init__.py"
        path = root.joinpath(*module.split("."))
        initializer = path / "__init__.py"
        return initializer if initializer.is_file() else path.with_suffix(".py")

    def add(module):
        path = path_for(module)
        if not path.is_file():
            raise ValueError("platform import has no source file: " + package + "." + module)
        pending.append(module)
        parts = module.split(".")[:-1]
        while parts:
            pending.append(".".join(parts))
            parts.pop()

    while pending:
        module = pending.pop()
        path = path_for(module)
        relative = path.relative_to(root).as_posix()
        if relative in sources:
            continue
        source_hash, imports, observes_version, normalized = _implementation_source(path.read_bytes(), relative)
        sources[relative] = source_hash
        reads_version = reads_version or observes_version
        if relative == "__init__.py":
            metadata_hash = normalized
        parts = module.split(".") if module else []
        current_package = parts if path.name == "__init__.py" else parts[:-1]
        for node in imports:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == package or alias.name.startswith(package + "."):
                        add(alias.name[len(package):].lstrip("."))
                        reads_version = reads_version or alias.name == package
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    if node.level > len(current_package) + 1:
                        raise ValueError("platform relative import leaves package: " + relative)
                    prefix = current_package[:len(current_package) + 1 - node.level]
                    imported = ".".join(prefix + ([node.module] if node.module else []))
                elif node.module == package or (node.module or "").startswith(package + "."):
                    imported = node.module[len(package):].lstrip(".")
                else:
                    continue
                add(imported)
                for alias in node.names:
                    child = ".".join(part for part in (imported, alias.name) if part)
                    if alias.name != "*" and path_for(child).is_file():
                        add(child)
                    elif not imported:
                        # Package attributes (including star imports) observe
                        # initializer metadata, unlike `from . import module`.
                        reads_version = True
            elif isinstance(node, ast.Call) and isinstance(node.func, (ast.Name, ast.Attribute)):
                name = node.func.id if isinstance(node.func, ast.Name) else node.func.attr
                if name not in {"import_module", "__import__"}:
                    continue
                argument = node.args[0] if node.args else None
                if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                    imported = argument.value
                    if imported == package or imported.startswith(package + "."):
                        add(imported[len(package):].lstrip("."))
                        reads_version = reads_version or imported == package
                    elif imported.startswith("."):
                        raise ValueError("dynamic relative platform imports need an explicit import: " + relative)
                else:
                    raise ValueError("dynamic platform import needs an explicit source dependency: " + relative)

    files = dict(sorted(sources.items()))
    if metadata_hash is not None and not reads_version:
        files["__init__.py"] = metadata_hash
    return dict(schema="seampact-executable-closure/1", scope=scope,
                entrypoint=package + "." + CORE_SCOPES[scope], files=files)


def checker_identity(environment):
    # Dependencies/weights are deliberately not installed by an analysis tool.
    specification = dict(python=environment["python"], base_image=environment["base_image"], core=core_identity("checker"))
    return digest(json.dumps(specification, sort_keys=True).encode())


def copy_core(destination, implementation):
    """Bundle only identity-covered modules; leave full research archives alone."""
    if implementation != core_identity(implementation["scope"]):
        raise ValueError("平台执行代码已改变；请重新生成构建工件")
    root = Path(__file__).parent
    for relative, expected in implementation["files"].items():
        data = safe_path(root, relative).read_bytes()
        source_hash, _, _, metadata_hash = _implementation_source(data, relative)
        if expected not in {source_hash, metadata_hash}:
            raise ValueError("平台执行代码在生成构建工件时改变：" + relative)
        target = safe_path(destination, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


def prepare_checker(home, environment, config):
    identity = checker_identity(environment)
    folder = Path(home) / "deployments" / "interpreters" / identity
    folder.mkdir(parents=True, exist_ok=True)
    copy_core(folder / "core" / "seampact", core_identity("checker"))
    minor = tuple(map(int, environment["python"].split(".")))
    steps = ["python -c " + json.dumps("import sys; assert sys.version_info[:2] == " + repr(minor))]
    if minor == (3, 10):
        steps.append("python -m pip install --no-cache-dir 'tomli>=2,<3'")
    (folder / "Dockerfile").write_text(
        "ARG BASE_IMAGE\nFROM ${BASE_IMAGE}\nWORKDIR /check\nRUN " + " && ".join(steps) +
        "\nCOPY core /app\nENV PYTHONPATH=/app PYTHONDONTWRITEBYTECODE=1\n"
        'ENTRYPOINT ["python","-B","-m","seampact.target_runtime"]\n', encoding="utf-8")
    image = "seampact-interpreter:" + identity[:24]
    receipt = backends.build(folder, image, environment, config)
    return dict(receipt, status="READY" if receipt["status"] == "BUILT" else receipt["status"],
                identity=identity, python=environment["python"], base_image=environment["base_image"],
                scope="source checks only; Harness dependencies, resources and execution are not ready")


class TargetRuntime:
    def __init__(self, inspector, record, timeout=120):
        self.inspector, self.record, self.timeout = inspector, copy.deepcopy(record), timeout
        self.cache = {}

    def call(self, method, **arguments):
        if method not in TOOLS:
            raise ValueError("unknown target-interpreter source operation")
        environment = self.inspector.request.get("runtime_environment") or {}
        if (self.record.get("status") != "READY" or not self.record.get("image_id")
                or self.record.get("identity") != checker_identity(environment)):
            raise ValueError("请先准备与所选环境及当前平台代码一致的目标解释器；尚未调用分析模型")
        frozen = self.inspector.request.get("target_runtime") or {}
        if frozen and any(frozen.get(key) != self.record.get(key) for key in ("identity", "image_id")):
            raise ValueError("目标解释器镜像与原分析冻结身份不同；请按当前环境零模型重新核对候选")
        request = copy.deepcopy(self.inspector.request)
        payload = dict(schema="target-source-tools/1", method=method, arguments=arguments, request=request,
                       files={path: base64.b64encode(self.inspector.data(path)).decode("ascii")
                              for path in self.inspector.files})
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        key = digest(encoded.encode())
        if key not in self.cache:
            gap = backends.docker_available(min(self.timeout, 20))
            if gap:
                raise ValueError("BACKEND_UNAVAILABLE：目标解释器需要本机可用的 Docker 后端；" + gap)
            command = ["docker", "run", "--rm", "-i", "--network", "none",
                       self.record["image_id"]]
            result = backends.run(command, self.timeout, stdin=encoded)
            if result["returncode"] != 0:
                raise ValueError("目标解释器源码核对失败或不可用：" + result["output"][-1000:])
            try:
                answer = json.loads(result["stdout"])
            except ValueError:
                raise ValueError("目标解释器没有返回完整的源码核对记录") from None
            if answer.get("schema") != "target-source-tools-result/1" or answer.get("source_version") != request["source_version"]:
                raise ValueError("目标解释器返回了不同来源或未知协议的记录")
            if answer.get("error"):
                raise ValueError(answer["error"])
            if "result" not in answer:
                raise ValueError("目标解释器未返回所请求工具的核对结果")
            self.cache[key] = answer["result"]
        return copy.deepcopy(self.cache[key])


def attach_target(inspector, record, timeout=120):
    if inspector.request.get("runtime_environment"):
        inspector.target_runtime = TargetRuntime(inspector, record or {}, timeout)
    return inspector


def install_catalog(catalog, inspector, candidate, analysis_id, installation_id):
    """Keep target-checked entries deferred until an actual worker registers."""
    report = inspector.target_runtime.call("inspect_candidate", candidate=candidate)
    values = candidate.get("entries")
    checks = report.get("entries")
    if (report.get("schema") != "native-entry-report/1" or
            report.get("source_version") != inspector.request["source_version"] or
            not isinstance(values, list) or not isinstance(checks, list) or len(values) != len(checks)):
        raise ValueError("目标解释器入口报告与冻结候选不一致")
    rows = []
    for index, (value, checked) in enumerate(zip(values, checks)):
        entry = dict(value) if isinstance(value, dict) else {}
        identity = entry.get("id") or "entry-" + str(index)
        if checked.get("id") != identity or any(checked.get(key) != entry.get(key, "file" if key == "unit" else None)
                for key in ("path", "unit", "symbol", "kind")):
            raise ValueError("目标解释器返回了不同的原源码入口锚点")
        rows.append((index, identity, entry, checked))
    counts = {}
    for _, entry_id, _, _ in rows:
        counts[str(entry_id)] = counts.get(str(entry_id), 0) + 1
    requirements = inspector.target_runtime.call("check_requirements", candidate=candidate)
    selections = copy.deepcopy(inspector.request.get("dependency_bindings", {}))
    def resource_snapshot(entry_ids=None):
        selected = [row for row in requirements if entry_ids is None or set(row.get("entry_ids", [])) & set(entry_ids)]
        return copy.deepcopy(dict(requirements=selected,
            bindings={row["id"]: selections[row["id"]] for row in selected if row["id"] in selections}))
    source_id = inspector.request.get("source_id")
    for previous in catalog.store.list("unit"):
        if previous.get("source_id") == source_id and previous.get("installation_id", previous.get("analysis_id")) != installation_id:
            previous.update(status="SUPERSEDED", superseded_by=analysis_id)
            catalog.store.save("unit", previous["id"], previous, "unit_superseded")
    resources = SimpleNamespace(snapshot=resource_snapshot)
    binding = SimpleNamespace(inspector=inspector, candidate=copy.deepcopy(candidate), detached=False,
                              active_calls=0, resource_binding=resources, module_paths={}, modules={}, notebooks={})
    # The coordinator can check external port values (for example ndarray), but
    # it has no original source classes loaded in this target-only binding.
    from .units import NativeBindings
    binding.native_matches = MethodType(NativeBindings.native_matches, binding)
    installed = []
    for index, entry_id, entry, checked in rows:
        identity = str(installation_id) + ":" + str(entry_id)
        if counts[str(entry_id)] > 1:
            identity += ":" + str(index)
        unit = dict(schema="native-unit/1", id=identity, entry_id=entry_id,
            name=entry.get("name") or entry_id, tool_name="unit_" + digest(identity.encode())[:32],
            path=entry.get("path"), source_unit=entry.get("unit", "file"), symbol=entry.get("symbol"),
            kind=entry.get("kind"), description=entry.get("description") or entry.get("symbol") or "Native entry",
            source_id=source_id, source_version=inspector.request["source_version"],
            semantic_key=inspector.request["source_version"][:16] + ":" + str(entry_id),
            analysis_id=analysis_id, installation_id=installation_id,
            initialization="NOT_RUN", result_type=None, parallel=False, remote=False,
            boundary_ids=entry.get("boundary_ids", []))
        from .store import now
        unit["installed_at"] = now()
        unit.update({key: copy.deepcopy(value) for key, value in checked.items()
                     if key not in {"id", "path", "unit", "symbol", "kind"}})
        if counts[str(entry_id)] == 1 and (checked["status"] == "READY" or checked.get("host_condition")):
            gaps = list((checked.get("host_condition") or {}).get("gaps", []))
            gaps.append("该能力在独立环境核对；请构建并登记匹配的运行组实例")
            unit.update(status="MISSING", reason="; ".join(gaps),
                        host_condition=dict(status="ENVIRONMENT_DEFERRED", gaps=gaps))
            catalog.deferred[identity] = (binding, entry)
        elif counts[str(entry_id)] > 1:
            unit.pop("host_condition", None)
            unit.update(status="MISSING", reason="entry IDs must be unique ASCII identifiers")
        catalog.store.save("unit", identity, unit, "unit_installed")
        installed.append(unit)
    for group in report["runtime_groups"]["groups"]:
        group.update(installation_id=installation_id, analysis_id=analysis_id,
                     runtime_environment=environment_selection(inspector.request["runtime_environment"]),
                     units=[unit["id"] for unit in installed if unit["entry_id"] in group["entries"]])
        catalog.store.save("runtime_group", group["id"], group)
        for unit in installed:
            if unit["id"] not in group["units"]:
                continue
            unit["runtime_group"] = group["id"]
            unit["operations"]["remote"] = group["remote_conditions"][unit["entry_id"]]
            unit["operations"]["group_host"] = group["host"]["members"][unit["entry_id"]]
            catalog.store.save("unit", unit["id"], unit)
    return installed


def _runtime_paths(value, source):
    """Expose the generated worker's source root, never a disposable checker path."""
    if isinstance(value, str):
        return value.replace(source + os.sep, "/work/source/").replace(source, "/work/source")
    if isinstance(value, list):
        return [_runtime_paths(item, source) for item in value]
    if isinstance(value, dict):
        return {key: _runtime_paths(item, source) for key, item in value.items()}
    return value


def execute(payload):
    if payload.get("schema") != "target-source-tools/1" or payload.get("method") not in TOOLS:
        raise ValueError("unknown source-tool request")
    request, files = payload["request"], payload["files"]
    source_files = request["files"]
    paths = [row["path"] for row in source_files]
    if len(set(paths)) != len(paths) or set(files) != set(paths):
        raise ValueError("目标解释器须接收唯一且完整的冻结文件清单")
    withheld = request.get("withheld_sources")
    version = digest(json.dumps(dict(files=source_files, withheld_sources=withheld) if withheld
                                else source_files, sort_keys=True).encode())
    if request["source_version"] != version:
        raise ValueError("目标解释器的来源身份与冻结文件清单不一致")
    from .environments import check_python_version
    environment = request["runtime_environment"]
    if ".".join(map(str, sys.version_info[:2])) != environment["python"]:
        raise ValueError("目标解释器实际 Python 与所选环境不一致")
    check = check_python_version(".".join(map(str, sys.version_info[:3])), environment.get("python_requirements", []))
    if check["status"] != "SUPPORTED":
        raise ValueError("目标解释器不满足原库 Python 约束：" + "; ".join(check["missing"]))
    with tempfile.TemporaryDirectory(prefix="seampact-source-") as temporary:
        root = Path(temporary)
        snapshots = root / "snapshots"
        snapshots.mkdir()
        source = root / "source"
        source.mkdir()
        for row in request["files"]:
            data = base64.b64decode(files[row["path"]], validate=True)
            if digest(data) != row["sha256"]:
                raise ValueError("frozen source digest changed during interpreter transfer")
            (snapshots / row["sha256"]).write_bytes(data)
            target = safe_path(source, row["path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        inspector = Inspector(source, snapshots, request)
        from .store import Store
        from .units import UnitCatalog
        from .worker import HostDependencies
        catalog = UnitCatalog(root, Store(":memory:"), None)
        catalog.resource_registry = HostDependencies(request)
        method, arguments = payload["method"], payload["arguments"]
        if method == "inspect_candidate":
            result = catalog.inspect_candidate(inspector, arguments["candidate"])
        elif method == "check_preprocessing":
            from .preprocessing import check_preprocessing
            result = check_preprocessing(inspector, arguments["artifact"], catalog.inspect_candidate)
        elif method == "check_candidate":
            from .verification import check_candidate
            result = check_candidate(inspector, arguments["candidate"])
        elif method == "check_requirements":
            from .dependencies import check_requirements
            result = check_requirements(inspector, arguments["candidate"])
        elif method == "classify_target":
            from .classification import classify_target
            result = classify_target(inspector, arguments["target"])
        else:
            result = getattr(inspector, method)(**arguments)
        return _runtime_paths(result, str(source))


def main():
    payload = json.load(sys.stdin)
    answer = dict(schema="target-source-tools-result/1", source_version=payload.get("request", {}).get("source_version"))
    try:
        answer["result"] = execute(payload)
    except (ValueError, KeyError, TypeError, SyntaxError, OSError, ImportError) as error:
        answer["error"] = str(error)
    json.dump(answer, sys.stdout, ensure_ascii=False, allow_nan=False)


if __name__ == "__main__":
    main()
