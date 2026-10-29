"""Keep ordinary external dependencies independent of frozen project code.

Unknown/local requirement forms retain the original source-first installation.
The original manifests and pip command order are preserved in either case.
"""

import json
import posixpath
import re
import shlex
from urllib.parse import urlsplit

from .inspection import digest, safe_path


RECIPE_LABEL = "io.seampact.environment.recipe"
BASE_LABEL = "io.seampact.environment.base"


def dependency_image_reference(recipe_sha256, base_image_id):
    """A tag is a lookup hint; callers must also verify its image labels."""
    identity = dict(schema="seampact-dependency-image/2", recipe_sha256=recipe_sha256,
                    base_image_id=base_image_id)
    return "seampact-environment:" + digest(json.dumps(identity, sort_keys=True).encode())


def _remote(value):
    if re.search(r"\s", value):
        return False
    url = urlsplit(value)
    return url.scheme in {"http", "https", "git+http", "git+https"} and bool(url.netloc)


def _external_requirement(line):
    if "$" in line:
        return False
    if _remote(line):
        return True
    direct = re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*(?:\[[\w,.-]+\])?\s*@\s*(\S+)(?:\s*;.*)?", line)
    if direct:
        return _remote(direct[1])
    # These options reference no local project files. Unknown options stay late.
    option = re.fullmatch(r"(--index-url|--extra-index-url|--find-links)(?:\s+|=)(\S+)", line)
    if option:
        return _remote(option[2])
    if re.fullmatch(r"--trusted-host(?:\s+|=)[A-Za-z0-9.:-]+", line):
        return True
    requirement = line.split(";", 1)[0].strip()
    requirement = re.sub(r"\s+--hash(?:=|\s+)\S+", "", requirement)
    return bool(re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._-]*(?:\[[\w,.-]+\])?"
        r"(?:\s*[!<>=~]+\s*[A-Za-z0-9.*+!_-]+(?:\s*,\s*[!<>=~]+\s*[A-Za-z0-9.*+!_-]+)*)?",
        requirement))


def dependency_inputs(inspector, requirements):
    """Return a complete frozen include closure, or keep installation source-first."""
    files, visiting = {}, set()

    def visit(path):
        if path in visiting:
            raise ValueError("依赖清单循环引用")
        if path in files:
            return
        if path not in inspector.files:
            raise ValueError("依赖引用不在冻结文件内：" + path)
        visiting.add(path)
        data = inspector.data(path)
        text = data.decode("utf-8-sig")
        pending = ""
        for raw in text.splitlines():
            line = re.sub(r"(^|\s+)#.*$", "", pending + raw).strip()
            if line.endswith("\\"):
                pending = line[:-1] + " "
                continue
            pending = ""
            if not line:
                continue
            include = re.fullmatch(r"(?:-[rc]\s*|--(?:requirement|constraint)(?:\s+|=))(.+)", line)
            if include:
                parts = shlex.split(include[1])
                if len(parts) != 1 or "$" in parts[0] or "\\" in parts[0]:
                    raise ValueError("依赖引用需要原源码上下文：" + path)
                child = posixpath.normpath(posixpath.join(posixpath.dirname(path), parts[0]))
                if child.startswith("/") or child == ".." or child.startswith("../"):
                    raise ValueError("依赖引用超出冻结源码：" + path)
                visit(child)
            elif not _external_requirement(line):
                raise ValueError("存在本地、可编辑或未覆盖的依赖形式：" + path)
        if pending:
            raise ValueError("依赖清单有未结束的续行：" + path)
        files[path] = data
        visiting.remove(path)

    try:
        for path in requirements:
            visit(path)
    except (ValueError, UnicodeError, KeyError) as error:
        return {}, dict(status="SOURCE_CONTEXT", reason=str(error), files=[])
    return files, dict(status="DEPENDENCY_ONLY", reason=None,
        files=[dict(path=path, sha256=digest(data)) for path, data in sorted(files.items())])


def worker_dockerfile(destination, inspector, environment, check_python):
    """Emit a reusable dependency stage plus a source-specific worker stage."""
    files, plan = dependency_inputs(inspector, environment["requirements"])
    if files:
        folder = destination / "dependency-inputs"
        folder.mkdir()
        for path, data in files.items():
            target = safe_path(folder, path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
    # A cache belongs to this Docker builder, not a source/group/worker image.
    # HTTP and wheel cache keys are still managed by pip. Its cache is optional:
    # eviction never changes the requested installation or the frozen lock.
    cached_run = "RUN --mount=type=cache,id=seampact-pip-v1,target=/root/.cache/pip,sharing=locked "
    index = '${PIP_EXTRA_INDEX_URL:+--extra-index-url "$PIP_EXTRA_INDEX_URL"} '
    install = "python -m pip install " + index
    independent = plan["status"] == "DEPENDENCY_ONLY"
    early = [install + "-r " + shlex.quote(path) for path in environment["requirements"]] if independent else []
    late = [] if independent else [install + "-r " + shlex.quote(path) for path in environment["requirements"]]
    installation = dict(early=[], late=[])
    if environment["requirements"]:
        installation["early" if independent else "late"].append(dict(kind="requirements",
            inputs=list(environment["requirements"]), reason=("全部引用均为冻结的外部依赖清单" if independent else plan["reason"])))
    if environment["project"]:
        project = "." if environment["project"] == "." else "./" + environment["project"]
        late.append(install + shlex.quote(project))
        installation["late"].append(dict(kind="project", inputs=[environment["project"]],
            reason="原项目安装可能读取源码或动态生成依赖；保持 requirements → project → packages 顺序"))
    if environment["packages"]:
        extra_early = not late and all(_external_requirement(item) for item in environment["packages"])
        (early if extra_early else late).append(install + " ".join(shlex.quote(item) for item in environment["packages"]))
        installation["early" if extra_early else "late"].append(dict(kind="packages", inputs=list(environment["packages"]),
            reason=("外部包且前序安装均不需要源码，保持原安装顺序" if extra_early else
                    "保留前序源码依赖／项目安装之后的顺序，或额外包本身需要源码上下文")))
    late += ["python -m pip check", "python -m pip freeze --all > /work/environment.lock"]
    python_check = "import sys; assert '.'.join(map(str, sys.version_info[:2])) == " + repr(environment["python"])
    dependency_recipe = (
        "ARG PIP_EXTRA_INDEX_URL=\nENV PIP_CACHE_DIR=/root/.cache/pip\n"
        "WORKDIR /work/source\nRUN python -c " + shlex.quote(python_check) + "\n")
    if environment["python"] == "3.10":
        dependency_recipe += cached_run + install + "'tomli>=2,<3'\n"
        installation["early"].insert(0, dict(kind="worker-parser", inputs=["tomli>=2,<3"],
            reason="Python 3.10 的平台解析器依赖，不读取 Harness 源码"))
    if files:
        dependency_recipe += "COPY dependency-inputs/ /work/source/\n"
    if early:
        dependency_recipe += cached_run + " \\\n && ".join(early) + "\n"
    # Late project/source changes cannot alter this recipe. The resolved base
    # image ID is added by the backend, after Docker has inspected it.
    dependency_key = dict(schema="seampact-dependency-recipe/2", files=plan["files"],
        pip_extra_index_url=environment["pip_extra_index_url"], recipe=dependency_recipe)
    recipe_sha256 = digest(json.dumps(dependency_key, sort_keys=True).encode())
    dockerfile = (
        "# syntax=docker/dockerfile:1\n"
        "# Generated runtime-group image; frozen manifests and original installation order.\n"
        "# Weights and credentials are supplied at start. BuildKit package cache stays outside the image.\n"
        "ARG BASE_IMAGE=" + environment["base_image"] + "\nARG DEPENDENCY_IMAGE=dependencies\n"
        "FROM ${BASE_IMAGE} AS dependencies\n"
        "ARG SEAMPACT_ENVIRONMENT_RECIPE\nARG SEAMPACT_ENVIRONMENT_BASE\n"
        "LABEL " + RECIPE_LABEL + '="${SEAMPACT_ENVIRONMENT_RECIPE}" ' + BASE_LABEL + '="${SEAMPACT_ENVIRONMENT_BASE}"\n' +
        dependency_recipe)
    dockerfile += (
        "FROM ${DEPENDENCY_IMAGE} AS worker\nARG PIP_EXTRA_INDEX_URL=\nCOPY core /app\n"
        "ENV PYTHONPATH=/app PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1\nWORKDIR /work\n" +
        "RUN python -c " + shlex.quote(check_python) + "\n"
        "COPY source /work/source\nWORKDIR /work/source\n" + cached_run + " \\\n && ".join(late) + "\n"
        "COPY snapshots /work/snapshots\nCOPY worker.json /work/worker.json\nWORKDIR /work\n"
        "ENV SEAMPACT_ENVIRONMENT_LOCK=/work/environment.lock\n"
        'ENTRYPOINT ["python","-B","-m","seampact.worker","--spec","/work/worker.json","--host","0.0.0.0","--port","8890"]\n')
    (destination / "Dockerfile").write_text(dockerfile, encoding="utf-8")
    dependency_image = "seampact-environment:recipe-" + recipe_sha256[:24]
    reuse = dict(schema="seampact-build-reuse/2", dependency_stage="dependencies", dependency_image=dependency_image,
        recipe_sha256=recipe_sha256, requested_base_image=environment["base_image"], requirements=plan, installation=installation,
        package_cache=dict(kind="buildkit-cache-mount", id="seampact-pip-v1", in_worker_image=False),
        scope="verified recipe and resolved base ID select an installed environment; final worker uses a local reference verified against that image ID",
        observed_cache_hit="NOT_CHECKED")
    (destination / "build-reuse.json").write_text(json.dumps(reuse, ensure_ascii=False, indent=2), encoding="utf-8")
    return reuse
