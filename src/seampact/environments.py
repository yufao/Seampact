"""Static runtime declarations from frozen source; no repository code is executed.

Discovery keeps each original manifest separate.  A suggestion is not evidence
that dependencies installed, an image built, or a Harness target ran.
"""

import ast
import configparser
import re
from pathlib import PurePosixPath
from urllib.parse import urlsplit

SUPPORTED_PYTHONS = ("3.10", "3.11", "3.12", "3.13")
IMAGE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/:@-]*")
PACKAGE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._\-\[\],<>=!~;:+@/ ]*")


def _version(value):
    if isinstance(value, (tuple, list)) and 2 <= len(value) <= 3 and all(type(n) is int and n >= 0 for n in value):
        return tuple(value)
    match = re.fullmatch(r"\s*(\d+)\.(\d+)(?:\.(\d+))?\s*", str(value))
    return tuple(int(n) for n in match.groups() if n is not None) if match else None


def _clauses(specifications):
    clauses = []
    for specification in specifications:
        if not isinstance(specification, str) or not specification.strip():
            raise ValueError("Python 要求须为非空版本约束")
        expression = re.sub(r"\s+(?=[<>!=~^])", ",", specification.strip())
        for raw in expression.split(","):
            match = re.fullmatch(r"\s*(~=|==|!=|>=|<=|>|<|\^|~|=)?\s*(\d+(?:\.\d+){0,2})(\.\*)?\s*", raw)
            if not match:
                return None
            operator, number, wildcard = match.groups()
            numbers = tuple(map(int, number.split(".")))
            operator = operator or "=="
            if wildcard and operator not in {"==", "!=", "="}:
                return None
            if operator in {"^", "~", "~="}:
                if wildcard or len(numbers) < 2:
                    return None
                clauses.append((">=", numbers, False))
                boundary = (numbers[0] + 1,) if operator == "^" or (operator == "~=" and len(numbers) == 2) else (numbers[0], numbers[1] + 1)
                clauses.append(("<", boundary, False))
            else:
                clauses.append(("==" if operator == "=" else operator, numbers, bool(wildcard)))
    return clauses


def _matches(version, clause):
    operator, expected, wildcard = clause
    if wildcard:
        equal = version[:len(expected)] == expected
        return equal if operator == "==" else not equal
    expected = expected + (0,) * (3 - len(expected))
    return {"==": version == expected, "!=": version != expected, ">=": version >= expected,
            "<=": version <= expected, ">": version > expected, "<": version < expected}[operator]


def check_python_version(version, specifications):
    """Check explicit interpreter facts, or feasibility of a selected minor.

    Pass all three version components inside workers.  A two-component value
    asks whether some patch release in that minor satisfies every declaration.
    Unsupported constraint syntax stays UNKNOWN rather than being ignored.
    """
    parsed = _version(version)
    if not parsed or not isinstance(specifications, (list, tuple)):
        raise ValueError("需要明确的 Python 版本和约束列表")
    clauses = _clauses(specifications)
    text = ".".join(map(str, parsed))
    if clauses is None:
        return dict(status="UNKNOWN", python=text, missing=["存在当前有限版本规则不能解释的 Python 约束"])
    patches = {0, 1}
    if len(parsed) == 2:
        for _, boundary, _ in clauses:
            if len(boundary) == 3 and boundary[:2] == parsed:
                patches.update(max(0, boundary[2] + delta) for delta in (-1, 0, 1))
        versions = [parsed + (patch,) for patch in sorted(patches)]
    else:
        versions = [parsed]
    valid = any(all(_matches(candidate, clause) for clause in clauses) for candidate in versions)
    return dict(status="SUPPORTED" if valid else "CONFLICT", python=text,
                missing=[] if valid else ["所选 Python 与冻结源码声明不兼容"])


def _scope(path):
    parent = str(PurePosixPath(path).parent)
    return "." if parent == "." else parent


def _evidence(inspector, path, line, end=None):
    return dict(path=path, unit="file", start=line, end=end or line,
                sha256=inspector.files[path]["sha256"], scope=_scope(path))


def _line(text, expression):
    match = re.search(expression, text, re.MULTILINE)
    return text[:match.start()].count("\n") + 1 if match else 1


def _text(inspector, path):
    from .inspection import decode_text
    return decode_text(path, inspector.data(path))[0]


def _python_from_image(image):
    match = re.fullmatch(r"(?:docker\.io/library/|library/)?python:(3\.\d+)(?:\.\d+)?(?:[-@].*)?", image)
    return match.group(1) if match else None


def discover_environment(inspector, project=""):
    """Find declarations and unmerged installation choices in frozen source.

    ``project`` selects the directory whose authoritative Python declarations
    apply.  Without it the root is selected.  Nested manifests remain visible
    evidence and are never silently merged into the root environment.
    """
    declarations, requirements, projects, images, conda, missing = [], [], [], [], [], []

    def declare(path, text, specification, kind, source, expression, line=None):
        if isinstance(specification, str) and specification.strip():
            declarations.append(dict(_evidence(inspector, path, line or _line(text, expression)),
                specification=specification.strip(), kind=kind, source=source))

    for path in sorted(inspector.files):
        name = PurePosixPath(path).name.lower()
        interesting = name in {"pyproject.toml", "setup.cfg", "setup.py", ".python-version", "environment.yml", "environment.yaml", "dockerfile"}
        req = (name.startswith("requirements") and name.endswith((".txt", ".in"))) or (
            "requirements" in PurePosixPath(path).parts[:-1] and name.endswith((".txt", ".in")))
        dockerfile = name == "dockerfile" or name.startswith("dockerfile.")
        documentation = name.startswith(("readme", "install")) and name.endswith((".md", ".rst", ".txt"))
        if not interesting and not req and not dockerfile and not documentation:
            continue
        try:
            text = _text(inspector, path)
        except (ValueError, KeyError):
            missing.append(dict(path=path, reason="环境声明不在可读冻结范围内"))
            continue
        if req:
            requirements.append(dict(_evidence(inspector, path, 1, max(1, len(text.splitlines()))), format="pip-requirements"))
        if name == "pyproject.toml":
            projects.append(dict(_evidence(inspector, path, 1), project=_scope(path), format="pyproject"))
            try:
                try:
                    import tomllib
                except ModuleNotFoundError:
                    import tomli as tomllib
                content = tomllib.loads(text)
                declare(path, text, content.get("project", {}).get("requires-python"), "requirement", "project.requires-python", r"^\s*requires-python\s*=")
                poetry = content.get("tool", {}).get("poetry", {}).get("dependencies", {}).get("python")
                if isinstance(poetry, dict):
                    poetry = poetry.get("version")
                declare(path, text, poetry, "requirement", "tool.poetry.dependencies.python", r"^\s*python\s*=")
                if "requires-python" in content.get("project", {}).get("dynamic", []):
                    missing.append(dict(path=path, reason="原项目动态计算 requires-python；未执行构建代码"))
            except (ValueError, ModuleNotFoundError):
                missing.append(dict(path=path, reason="不能静态读取原 pyproject 声明"))
        elif name == "setup.cfg":
            projects.append(dict(_evidence(inspector, path, 1), project=_scope(path), format="setuptools-config"))
            parser = configparser.ConfigParser(interpolation=None)
            try:
                parser.read_string(text)
                declare(path, text, parser.get("options", "python_requires", fallback=""), "requirement", "options.python_requires", r"^\s*python_requires\s*=")
            except configparser.Error:
                missing.append(dict(path=path, reason="不能静态读取原 setup.cfg 声明"))
        elif name == "setup.py":
            projects.append(dict(_evidence(inspector, path, 1), project=_scope(path), format="setuptools-script"))
            try:
                tree = ast.parse(text)
                for call in ast.walk(tree):
                    if not isinstance(call, ast.Call) or not ((isinstance(call.func, ast.Name) and call.func.id == "setup") or
                            (isinstance(call.func, ast.Attribute) and call.func.attr == "setup")):
                        continue
                    for node in call.keywords:
                        if node.arg == "python_requires":
                            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                                declare(path, text, node.value.value, "requirement", "setup.python_requires", "", node.lineno)
                            else:
                                missing.append(dict(path=path, reason="原 setup.py 动态计算 python_requires；未执行原代码"))
            except SyntaxError:
                missing.append(dict(path=path, reason="平台解释器不能静态解析原 setup.py"))
        elif name == ".python-version":
            for line, value in enumerate(text.splitlines(), 1):
                value = value.split("#", 1)[0].strip()
                if _version(value):
                    declare(path, text, value, "pin", "python-version", "", line)
                elif value:
                    missing.append(dict(path=path, reason="原 .python-version 不是明确 CPython 版本"))
        elif name in {"environment.yml", "environment.yaml"}:
            conda.append(dict(_evidence(inspector, path, 1, max(1, len(text.splitlines()))), format="conda"))
            for line, value in enumerate(text.splitlines(), 1):
                match = re.match(r"\s*-\s*['\"]?python\s*([=<>!~].*?)['\"]?\s*(?:#.*)?$", value)
                if match:
                    specification = match.group(1).strip().rstrip("'\"").strip()
                    if re.fullmatch(r"=\d+\.\d+(?:\.\d+)?", specification):
                        specification = "=" + specification + (".*" if specification.count(".") == 1 else "")
                    declare(path, text, specification, "requirement", "conda.python", "", line)
        if dockerfile:
            for line, value in enumerate(text.splitlines(), 1):
                match = re.match(r"\s*FROM\s+(?:--platform=\S+\s+)?(\S+)", value, re.IGNORECASE)
                if match and IMAGE.fullmatch(match.group(1)):
                    image = match.group(1)
                    images.append(dict(_evidence(inspector, path, line), base_image=image,
                        python=_python_from_image(image), format="dockerfile", preserve_original=True))
        if documentation:
            for line, value in enumerate(text.splitlines(), 1):
                match = re.search(r"\b(?:Python|python)\s*[:=]?\s*`?\s*((?:[<>]=?|==|~=)\s*)?(3\.\d+(?:\.\d+)?)", value)
                if match:
                    declare(path, text, (match.group(1) or "") + match.group(2), "recommendation", "documentation", "", line)

    scope = project or "."
    active = [item for item in declarations if item["scope"] == scope]
    hard = [item["specification"] for item in active if item["kind"] == "requirement"]
    candidates = [version for version in SUPPORTED_PYTHONS if check_python_version(version, hard)["status"] == "SUPPORTED"]
    conflicts = []
    if hard and not candidates:
        if check_python_version("3.11", hard)["status"] == "UNKNOWN":
            missing.append(dict(scope=scope, reason="存在当前有限版本规则不能解释的原 Python 要求"))
        else:
            conflicts.append(dict(scope=scope, reason="原 Python 要求在支持范围 3.10–3.13 内没有兼容版本"))
    preferred = [".".join(map(str, _version(item["specification"])[:2])) for item in active
                 if item["kind"] == "pin" and _version(item["specification"])]
    recommendations = {".".join(map(str, _version(item["specification"])[:2])) for item in active
                       if item["kind"] == "recommendation" and _version(item["specification"])}
    if not preferred and len(recommendations) == 1:
        preferred = list(recommendations)
    if any(version not in candidates for version in preferred):
        missing.append(dict(scope=scope, reason="作者版本 pin／推荐与当前支持范围或硬性版本要求不一致，需核对"))
    if len(set(preferred)) > 1:
        missing.append(dict(scope=scope, reason="作者版本 pin 有多个候选，需明确选择"))
    python = next((version for version in preferred if version in candidates), "3.11" if "3.11" in candidates else (candidates[0] if len(candidates) == 1 else ""))
    if not python and candidates:
        missing.append(dict(scope=scope, reason="有多个兼容 Python 版本，需明确选择"))
    unique_projects = list(dict.fromkeys(item["project"] for item in projects))
    suggested_project = scope if scope in unique_projects else ""
    scoped_requirements = [item["path"] for item in requirements if item["scope"] == scope]
    selected_requirements = scoped_requirements if len(scoped_requirements) == 1 and not suggested_project else []
    if scoped_requirements and suggested_project:
        missing.append(dict(scope=scope, reason="同时发现原项目安装声明与依赖清单，请按原安装说明选择组合；不会自动合并"))
    if len(scoped_requirements) > 1 and not suggested_project:
        missing.append(dict(scope=scope, reason="发现多个依赖清单，请选择原安装所用清单；不会自动合并"))
    if unique_projects and not suggested_project:
        missing.append(dict(scope=scope, reason="存在子项目安装目录，需明确选择原安装范围"))
    scoped_images = [item for item in images if item["scope"] == scope]
    base = scoped_images[0]["base_image"] if len(scoped_images) == 1 and scoped_images[0].get("python") in {None, python} else ("python:" + python + "-slim" if python else "")
    if len(scoped_images) > 1:
        missing.append(dict(scope=scope, reason="原 Dockerfile 含多个基础阶段，需选择已准备原依赖的基础镜像；不会自动合并阶段"))
    if conda:
        missing.append(dict(reason="Conda 声明仅保留原文件依据；需要使用已准备该环境的基础镜像，未转换为 pip 安装"))
    return dict(schema="seampact-environment-discovery/1", source_version=inspector.request.get("source_version"), scope=scope,
        status="CONFLICT" if conflicts else "MISSING" if missing else "DECLARED", supported_pythons=list(SUPPORTED_PYTHONS),
        python_declarations=declarations, python_requirements=hard, python_candidates=candidates,
        requirements=requirements, projects=projects, base_images=images, conda_files=conda,
        conflicts=conflicts, missing=missing, discovery_only=True,
        suggested=dict(python=python, base_image=base, requirements=selected_requirements,
            project=suggested_project, packages=[], pip_extra_index_url=""))


def normalize_environment(value, inspector):
    """Validate user choices without changing the original installation files."""
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError("environment 须为对象")
    project = value.get("project") or ""
    if not isinstance(project, str) or "\\" in project or project.startswith("/") or ".." in PurePosixPath(project).parts:
        raise ValueError("project 须为冻结源码内的相对项目目录")
    if project:
        project = str(PurePosixPath(project))
        prefix = "" if project == "." else project + "/"
        if not any(prefix + name in inspector.files for name in ("pyproject.toml", "setup.py", "setup.cfg")):
            raise ValueError("project 没有冻结的原项目安装声明")
    discovery = discover_environment(inspector, project)
    python = value.get("python") or "3.11"
    if python not in SUPPORTED_PYTHONS:
        raise ValueError("运行组 Python 仅支持 3.10、3.11、3.12、3.13")
    checked = check_python_version(python, discovery["python_requirements"])
    if checked["status"] != "SUPPORTED":
        raise ValueError("；".join(checked["missing"]))
    base = value.get("base_image") or "python:" + python + "-slim"
    if not isinstance(base, str) or not IMAGE.fullmatch(base):
        raise ValueError("base_image 须为镜像引用")
    image_python = _python_from_image(base)
    if image_python and image_python != python:
        raise ValueError("基础镜像的 Python 版本与所选 Python 不一致")
    requirements = value.get("requirements", [])
    if not isinstance(requirements, list) or any(not isinstance(item, str) or item not in inspector.files for item in requirements):
        raise ValueError("依赖清单须为冻结源码内的文件路径")
    if any(PurePosixPath(item).name.lower() in {"environment.yml", "environment.yaml", "pyproject.toml", "setup.cfg", "setup.py"} for item in requirements):
        raise ValueError("requirements 仅接受原 pip 清单；项目安装使用 project，Conda 环境使用已准备的基础镜像")
    packages = value.get("packages", [])
    if not isinstance(packages, list) or any(not isinstance(item, str) or not PACKAGE.fullmatch(item) or
            any("@" in urlsplit(url).netloc for url in re.findall(r"https?://\S+", item)) for item in packages):
        raise ValueError("packages 须为 pip 需求说明")
    index = value.get("pip_extra_index_url") or ""
    if not isinstance(index, str) or (index and (urlsplit(index).scheme not in {"http", "https"} or not urlsplit(index).netloc or "@" in urlsplit(index).netloc or urlsplit(index).query or urlsplit(index).fragment)):
        raise ValueError("pip_extra_index_url 须为不含凭据的 HTTP 地址")
    return dict(base_image=base, requirements=list(dict.fromkeys(requirements)), packages=list(packages),
        pip_extra_index_url=index, python=python, project=project, python_requirements=discovery["python_requirements"],
        discovery=discovery, weights_in_image=False, secrets_in_image=False)
