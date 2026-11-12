"""Capability IR and the generated package that carries it.

One versioned description serves code generation, installation, tool display,
scheduling and the worker. This module imports nothing from the platform: the
worker image loads it to verify the exact package it is about to execute.

A package is a folder:

    capability.json   manifest: capability descriptions, environment, resource
                      requirements and the sha256 of every other file
    adapter/...       generated adapter code
    source/...        frozen repository files the adapter reuses

The adapter exposes one class. An instance of it belongs to one worker process:

    initialize(bindings)      create the state and resources this instance owns
    <method>(input, context)  run one complete capability; default name "execute"
    close()                   release what initialize created
"""

import hashlib
import json
import math
import re
from pathlib import Path, PurePosixPath

IR_SCHEMA = "capability-ir/1"
PACKAGE_SCHEMA = "capability-package/1"
MANIFEST = "capability.json"
SOURCE_PREFIX = "source/"
WORKER_PROFILE = "capability-package-worker/1"
IDENTITY_PROTOCOL = "process-identity/1"

IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")
MODULE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")
SHA256 = re.compile(r"[0-9a-f]{64}")
PYTHON = re.compile(r"3\.(?:10|11|12|13)")

OUTPUT_KINDS = {"json", "text", "files"}
STATE_SCOPES = {"call", "instance"}
EFFECT_KINDS = {"filesystem", "network", "external"}
EFFECT_ACCESS = {"read", "write"}
RESOURCE_KINDS = {"weight", "service", "credential", "device", "parameter", "data"}
CANCEL_MODES = {"none", "cooperative"}

_TYPES = {"object": dict, "array": list, "string": str, "integer": int,
          "number": (int, float), "boolean": bool, "null": type(None)}
_ANNOTATIONS = {"description", "title", "default", "examples"}
_KEYWORDS = {"type", "properties", "required", "additionalProperties", "items", "enum",
             "minimum", "maximum", "minLength", "maxLength", "minItems", "maxItems"}


class ContractError(ValueError):
    """The description, package or a value does not satisfy the contract."""


def canonical(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    data = value if isinstance(value, bytes) else canonical(value).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _require(condition, message):
    if not condition:
        raise ContractError(message)


def _text(value, label):
    _require(isinstance(value, str) and value.strip(), label + " 需要非空文字")
    return value


# Interface schemas: the subset of JSON Schema that is actually enforced.

def check_schema(schema, location="schema"):
    """Reject keywords this module does not enforce.

    A declared interface must never be wider than what is checked at the call.
    """
    _require(isinstance(schema, dict), location + " 须为对象")
    unknown = set(schema) - _KEYWORDS - _ANNOTATIONS
    _require(not unknown, location + " 含未支持的约束：" + ", ".join(sorted(unknown)))
    kinds = schema.get("type")
    if kinds is not None:
        names = kinds if isinstance(kinds, list) else [kinds]
        _require(names and all(isinstance(name, str) and name in _TYPES for name in names),
                 location + ".type 须为 JSON 类型名称")
    if "properties" in schema:
        _require(isinstance(schema["properties"], dict), location + ".properties 须为对象")
        for name, child in schema["properties"].items():
            _require(isinstance(name, str) and name, location + ".properties 的名称须为非空文字")
            check_schema(child, location + "." + name)
    if "required" in schema:
        required = schema["required"]
        _require(isinstance(required, list) and all(isinstance(name, str) for name in required)
                 and len(set(required)) == len(required), location + ".required 须为不重复的名称列表")
        _require(set(required) <= set(schema.get("properties", {})), location + ".required 含未声明的属性")
    if "additionalProperties" in schema:
        _require(isinstance(schema["additionalProperties"], bool), location + ".additionalProperties 须为布尔值")
    if "items" in schema:
        check_schema(schema["items"], location + "[]")
    if "enum" in schema:
        values = schema["enum"]
        _require(isinstance(values, list) and values and all(
            value is None or type(value) in (str, int, float, bool) for value in values),
            location + ".enum 须为非空的标量列表")
    for name in ("minimum", "maximum"):
        if name in schema:
            _require(type(schema[name]) in (int, float) and math.isfinite(schema[name]), location + "." + name + " 须为有限数")
    for name in ("minLength", "maxLength", "minItems", "maxItems"):
        if name in schema:
            _require(type(schema[name]) is int and schema[name] >= 0, location + "." + name + " 须为非负整数")
    return schema


def _is(value, name):
    if name == "integer":
        return type(value) is int
    if name == "number":
        return type(value) in (int, float) and math.isfinite(value)
    if name == "boolean":
        return type(value) is bool
    return isinstance(value, _TYPES[name]) and type(value) is not bool


def validate_value(value, schema, location="value"):
    """Check a JSON value against a schema accepted by check_schema."""
    kinds = schema.get("type")
    if kinds is not None:
        names = kinds if isinstance(kinds, list) else [kinds]
        _require(any(_is(value, name) for name in names), location + " 的类型应为 " + "/".join(names))
    if "enum" in schema:
        _require(any(type(value) is type(option) and value == option for option in schema["enum"]),
                 location + " 不在允许的取值内")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        missing = [name for name in schema.get("required", []) if name not in value]
        _require(not missing, location + " 缺少必需属性：" + ", ".join(missing))
        if schema.get("additionalProperties") is False:
            extra = sorted(set(value) - set(properties))
            _require(not extra, location + " 含未声明的属性：" + ", ".join(extra))
        for name, item in value.items():
            if name in properties:
                validate_value(item, properties[name], location + "." + name)
    if isinstance(value, list):
        _require(len(value) >= schema.get("minItems", 0), location + " 的元素少于下限")
        _require("maxItems" not in schema or len(value) <= schema["maxItems"], location + " 的元素多于上限")
        if "items" in schema:
            for index, item in enumerate(value):
                validate_value(item, schema["items"], location + "[" + str(index) + "]")
    if isinstance(value, str):
        _require(len(value) >= schema.get("minLength", 0), location + " 短于下限")
        _require("maxLength" not in schema or len(value) <= schema["maxLength"], location + " 长于上限")
    if type(value) in (int, float):
        _require("minimum" not in schema or value >= schema["minimum"], location + " 小于下限")
        _require("maximum" not in schema or value <= schema["maximum"], location + " 大于上限")
    return value


# Capability description.

def check_capability(capability, resource_ids=(), source_paths=None):
    _require(isinstance(capability, dict), "能力描述须为对象")
    _require(capability.get("schema") == IR_SCHEMA, "能力描述版本须为 " + IR_SCHEMA)
    identity = capability.get("id")
    _require(isinstance(identity, str) and IDENTIFIER.fullmatch(identity), "能力 id 须为字母开头的 ASCII 标识")
    label = "能力 " + identity
    unknown = set(capability) - {"schema", "id", "name", "purpose", "method", "input", "output", "state",
                                 "effects", "resources", "provenance"}
    _require(not unknown, label + " 含未知字段：" + ", ".join(sorted(unknown)))
    _text(capability.get("name"), label + " 的 name")
    _text(capability.get("purpose"), label + " 的 purpose")
    method = capability.get("method", "execute")
    _require(isinstance(method, str) and IDENTIFIER.fullmatch(method) and method not in {"initialize", "close"},
             label + " 的 method 须为适配类上的执行方法名")
    check_schema(capability.get("input"), label + ".input")
    _require(capability["input"].get("type") == "object", label + ".input 须为 object")
    output = capability.get("output")
    _require(isinstance(output, dict) and output.get("kind") in OUTPUT_KINDS,
             label + ".output.kind 须为 json、text 或 files")
    _require(set(output) <= {"kind", "schema", "description"}, label + ".output 含未知字段")
    if "schema" in output:
        _require(output["kind"] == "json", label + " 只有 json 输出可以声明 schema")
        check_schema(output["schema"], label + ".output.schema")
    state = capability.get("state")
    _require(isinstance(state, dict) and state.get("scope") in STATE_SCOPES,
             label + ".state.scope 须为 call（仅属本次调用）或 instance（由实例跨调用持有）")
    for effect in capability.get("effects", []):
        _require(isinstance(effect, dict) and effect.get("kind") in EFFECT_KINDS
                 and effect.get("access") in EFFECT_ACCESS and isinstance(effect.get("target"), str),
                 label + ".effects 每项需要 kind、access 与 target")
    used = capability.get("resources", [])
    _require(isinstance(used, list) and all(isinstance(item, str) for item in used) and len(set(used)) == len(used),
             label + ".resources 须为不重复的资源 id 列表")
    _require(set(used) <= set(resource_ids), label + " 使用了包内未声明的资源")
    for item in capability.get("provenance", []):
        _require(isinstance(item, dict) and isinstance(item.get("path"), str)
                 and type(item.get("start")) is int and type(item.get("end")) is int
                 and 1 <= item["start"] <= item["end"], label + ".provenance 每项需要 path、start、end")
        if source_paths is not None:
            _require(item["path"] in source_paths, label + " 的来源依据不在冻结源码内：" + item["path"])
    return capability


# Package manifest.

def _relative(path):
    _require(isinstance(path, str) and path and "\\" not in path, "包内路径须为 POSIX 相对路径")
    parts = PurePosixPath(path).parts
    _require(not path.startswith("/") and ".." not in parts and "." not in parts and str(PurePosixPath(path)) == path,
             "包内路径不能离开包目录：" + path)
    _require(path != MANIFEST and "__pycache__" not in parts, "包内路径不可用：" + path)
    return path


def check_manifest(manifest):
    _require(isinstance(manifest, dict) and manifest.get("schema") == PACKAGE_SCHEMA,
             "能力包清单版本须为 " + PACKAGE_SCHEMA)
    _text(manifest.get("name"), "能力包 name")
    source = manifest.get("source")
    _require(isinstance(source, dict) and all(source.get(key) is None or isinstance(source[key], str)
                                              for key in ("id", "version")), "能力包 source 须给出来源 id 与 version（可为空）")
    generator = manifest.get("generator")
    _require(isinstance(generator, dict) and isinstance(generator.get("template"), str) and generator["template"],
             "能力包 generator 须记录所用模板")
    environment = manifest.get("environment")
    _require(isinstance(environment, dict) and isinstance(environment.get("python"), str)
             and PYTHON.fullmatch(environment["python"]), "能力包 environment.python 须为 3.10–3.13")
    implementation = manifest.get("implementation")
    _require(isinstance(implementation, dict) and isinstance(implementation.get("module"), str)
             and MODULE.fullmatch(implementation["module"]) and isinstance(implementation.get("symbol"), str)
             and IDENTIFIER.fullmatch(implementation["symbol"]), "能力包 implementation 须给出适配模块与类名")
    instance = manifest.get("instance")
    _require(isinstance(instance, dict) and type(instance.get("concurrency")) is int and instance["concurrency"] >= 1
             and type(instance.get("replicable")) is bool and instance.get("cancel") in CANCEL_MODES,
             "能力包 instance 须声明 concurrency、replicable 与 cancel")
    resources = manifest.get("resources")
    _require(isinstance(resources, list), "能力包 resources 须为列表")
    resource_ids = []
    for resource in resources:
        _require(isinstance(resource, dict) and isinstance(resource.get("id"), str)
                 and IDENTIFIER.fullmatch(resource["id"]) and resource.get("kind") in RESOURCE_KINDS
                 and type(resource.get("required")) is bool, "能力包资源需要 id、kind 与 required")
        _text(resource.get("description"), "资源 " + resource["id"] + " 的 description")
        resource_ids.append(resource["id"])
    _require(len(set(resource_ids)) == len(resource_ids), "能力包资源 id 重复")
    files = manifest.get("files")
    _require(isinstance(files, list) and files, "能力包 files 须列出适配代码与所用源码")
    paths = []
    for record in files:
        _require(isinstance(record, dict) and set(record) == {"path", "sha256"} and isinstance(record["sha256"], str)
                 and SHA256.fullmatch(record["sha256"]), "能力包 files 每项需要 path 与 sha256")
        paths.append(_relative(record["path"]))
    _require(len(set(paths)) == len(paths), "能力包 files 路径重复")
    module = implementation["module"].replace(".", "/")
    _require(module + ".py" in paths or module + "/__init__.py" in paths, "能力包没有包含 implementation 指向的适配模块")
    _require(not implementation["module"].startswith("source"), "适配模块不能位于 source/ 下")
    source_paths = {path[len(SOURCE_PREFIX):] for path in paths if path.startswith(SOURCE_PREFIX)}
    capabilities = manifest.get("capabilities")
    _require(isinstance(capabilities, list) and capabilities, "能力包至少包含一个能力")
    for capability in capabilities:
        check_capability(capability, resource_ids, source_paths)
    identities = [capability["id"] for capability in capabilities]
    methods = [capability.get("method", "execute") for capability in capabilities]
    _require(len(set(identities)) == len(identities), "能力 id 重复")
    _require(len(set(methods)) == len(methods), "不同能力不能指向同一执行方法")
    known = {"schema", "name", "source", "generator", "environment", "implementation", "instance",
             "resources", "capabilities", "files"}
    _require(set(manifest) <= known, "能力包清单含未知字段：" + ", ".join(sorted(set(manifest) - known)))
    return manifest


def package_digest(manifest):
    """Identity of source version, generated code, template and environment together."""
    return digest(check_manifest(manifest))


def _separate_source(root, source_root):
    return source_root is not None and Path(source_root).resolve() != (Path(root) / "source").resolve()


def scan_files(root, source_root=None):
    """List the files of a package folder in manifest form.

    ``source_root`` names a source tree kept beside the package, as in a
    worker image; by default the source is the package's own source/ folder.
    """
    root = Path(root)
    bases = [(root, "")]
    if _separate_source(root, source_root):
        bases.append((Path(source_root), SOURCE_PREFIX))
    records = []
    for base, prefix in bases:
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            relative = path.relative_to(base).as_posix()
            if (not path.is_file() or "__pycache__" in PurePosixPath(relative).parts
                    or (not prefix and relative == MANIFEST)):
                continue
            records.append(dict(path=prefix + relative, sha256=digest(path.read_bytes())))
    return sorted(records, key=lambda record: record["path"])


def package_path(root, relative, source_root=None):
    if relative.startswith(SOURCE_PREFIX) and _separate_source(root, source_root):
        return Path(source_root).joinpath(*PurePosixPath(relative[len(SOURCE_PREFIX):]).parts)
    return Path(root).joinpath(*PurePosixPath(relative).parts)


def load_package(root, source_root=None):
    """Read a package and verify that it holds exactly the declared bytes.

    Installing dependencies inside an image can leave build products in a
    separate source tree; those are not package content. Generated code and a
    package's own source/ folder admit no undeclared file.
    """
    root = Path(root)
    path = root / MANIFEST
    _require(path.is_file(), "能力包缺少 " + MANIFEST)
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as error:
        raise ContractError("能力包清单不是有效 JSON：" + str(error)) from None
    check_manifest(manifest)
    declared = {record["path"]: record["sha256"] for record in manifest["files"]}
    actual = {record["path"]: record["sha256"] for record in scan_files(root, source_root)}
    missing = sorted(set(declared) - set(actual))
    _require(not missing, "能力包缺少清单列出的文件：" + ", ".join(missing[:5]))
    changed = sorted(name for name, value in declared.items() if actual[name] != value)
    _require(not changed, "能力包文件与清单身份不一致：" + ", ".join(changed[:5]))
    separate = _separate_source(root, source_root)
    extra = sorted(name for name in set(actual) - set(declared)
                   if not (separate and name.startswith(SOURCE_PREFIX)))
    _require(not extra, "能力包含清单之外的文件：" + ", ".join(extra[:5]))
    return manifest


# Resource bindings: what an instance receives for each declared resource.
# Secrets are named by environment variable and never stored in a binding.

BINDING_FORMS = {"weight": "path", "service": "service", "credential": "secret",
                 "device": "value", "parameter": "value", "data": "project"}
ENVIRONMENT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
PROJECT_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
PROJECT_RESERVED = re.compile(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])\Z", re.I)
INDEX_NAMESPACE = re.compile(r"[0-9a-f]{24}\Z")


def project_binding_path(binding):
    """Derive the only worker path admitted for a managed project resource."""
    _require(isinstance(binding, dict) and binding.get("form") == "project"
             and set(binding) <= {"form", "project_id", "area", "read_only", "namespace"},
             "项目资料绑定只接受项目身份、区域与访问模式")
    project = binding.get("project_id")
    _require(isinstance(project, str) and PROJECT_ID.fullmatch(project)
             and not PROJECT_RESERVED.fullmatch(project), "项目资料绑定需要有效的 project_id")
    area = binding.get("area")
    _require(area in {"data", "indexes"} and type(binding.get("read_only")) is bool,
             "项目资料绑定需要 data/indexes 区域与 read_only")
    if area == "data":
        _require(binding["read_only"] and "namespace" not in binding, "项目共用资料必须只读且不能指定索引命名空间")
        return "/work/projects/" + project + "/data"
    namespace = binding.get("namespace")
    _require(isinstance(namespace, str) and INDEX_NAMESPACE.fullmatch(namespace),
             "项目索引绑定需要平台生成的 namespace")
    return "/work/projects/" + project + "/indexes/" + namespace


def check_binding(resource, binding):
    label = "资源 " + resource["id"] + " 的绑定"
    form = BINDING_FORMS[resource["kind"]]
    _require(isinstance(binding, dict) and binding.get("form") == form, label + "须为 " + form + " 形式")
    if form == "path":
        _require(set(binding) <= {"form", "path", "resource_id", "sha256"} and isinstance(binding.get("path"), str)
                 and binding["path"], label + "需要实例内可见的 path")
    elif form == "project":
        project_binding_path(binding)
    elif form == "service":
        _require(set(binding) <= {"form", "base_url", "model", "api_key_env", "connection_id", "parameters"}
                 and isinstance(binding.get("base_url"), str) and binding["base_url"], label + "需要 base_url")
        _require(binding.get("api_key_env") is None or (isinstance(binding["api_key_env"], str)
                 and ENVIRONMENT_NAME.fullmatch(binding["api_key_env"])), label + "的 api_key_env 须为环境变量名")
    elif form == "secret":
        _require(set(binding) <= {"form", "env", "connection_id"} and isinstance(binding.get("env"), str)
                 and ENVIRONMENT_NAME.fullmatch(binding["env"]), label + "需要环境变量名 env")
    else:
        _require(set(binding) == {"form", "value"}, label + "需要 value")
        canonical(binding["value"])
    return binding


def check_bindings(manifest, bindings):
    """Validate supplied bindings; return the ids of required resources still unbound."""
    _require(isinstance(bindings, dict), "资源绑定须为对象")
    resources = {resource["id"]: resource for resource in manifest["resources"]}
    unknown = sorted(set(bindings) - set(resources))
    _require(not unknown, "绑定了包内未声明的资源：" + ", ".join(unknown))
    for identity, binding in bindings.items():
        check_binding(resources[identity], binding)
        if binding["form"] == "project" and binding["area"] == "indexes":
            _require(binding["namespace"] == package_digest(manifest)[:24],
                     "索引命名空间必须来自本能力包身份")
    projects = {binding["project_id"] for binding in bindings.values() if binding["form"] == "project"}
    _require(len(projects) <= 1, "一个能力实例只能绑定一个项目")
    return [identity for identity, resource in resources.items() if resource["required"] and identity not in bindings]


def secret_names(bindings):
    """Environment variables whose values an instance needs at start."""
    names = [binding.get("api_key_env") if binding["form"] == "service" else binding.get("env")
             for binding in bindings.values() if binding["form"] in {"service", "secret"}]
    return sorted({name for name in names if name})


def tool_definition(name, capability):
    """What a model sees: the capability's own input interface."""
    return dict(type="function", function=dict(name=name, description=capability["purpose"],
                                               parameters=capability["input"]))
