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
    early = [install + "-r " + shlex.quote(path) for path in environment["requirements"]] if files else []
    late = [] if files else [install + "-r " + shlex.quote(path) for path in environment["requirements"]]
    if environment["project"]:
        project = "." if environment["project"] == "." else "./" + environment["project"]
        late.append(install + shlex.quote(project))
    if environment["packages"]:
        late.append(install + " ".join(shlex.quote(item) for item in environment["packages"]))
    late += ["python -m pip check", "python -m pip freeze --all > /work/environment.lock"]
    python_check = "import sys; assert '.'.join(map(str, sys.version_info[:2])) == " + repr(environment["python"])
    dockerfile = (
        "# Generated runtime-group image; frozen manifests and original installation order.\n"
        "# Weights and credentials are supplied at start. BuildKit package cache stays outside the image.\n"
        "ARG BASE_IMAGE=" + environment["base_image"] + "\nFROM ${BASE_IMAGE} AS dependencies\n"
        "ARG PIP_EXTRA_INDEX_URL=\nENV PIP_CACHE_DIR=/root/.cache/pip\n"
        "WORKDIR /work/source\nRUN python -c " + shlex.quote(python_check) + "\n")
    if environment["python"] == "3.10":
        dockerfile += cached_run + install + "'tomli>=2,<3'\n"
    if early:
        dockerfile += "COPY dependency-inputs/ /work/source/\n" + cached_run + " \\\n && ".join(early) + "\n"
    dockerfile += (
        "FROM dependencies AS worker\nARG PIP_EXTRA_INDEX_URL=\nCOPY core /app\n"
        "ENV PYTHONPATH=/app PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1\nWORKDIR /work\n" +
        "RUN python -c " + shlex.quote(check_python) + "\n"
        "COPY source /work/source\nWORKDIR /work/source\n" + cached_run + " \\\n && ".join(late) + "\n"
        "COPY snapshots /work/snapshots\nCOPY worker.json /work/worker.json\nWORKDIR /work\n"
        "ENV SEAMPACT_ENVIRONMENT_LOCK=/work/environment.lock\n"
        'ENTRYPOINT ["python","-B","-m","seampact","worker","--spec","/work/worker.json","--host","0.0.0.0","--port","8890"]\n')
    (destination / "Dockerfile").write_text(dockerfile, encoding="utf-8")
    dependency_key = dict(schema="seampact-dependency-image/1", base_image=environment["base_image"],
        python=environment["python"], requirements=environment["requirements"], files=plan["files"],
        pip_extra_index_url=environment["pip_extra_index_url"],
        recipe_sha256=digest(dockerfile.split("FROM dependencies AS worker", 1)[0].encode()))
    dependency_image = ("seampact-environment:" + digest(json.dumps(dependency_key, sort_keys=True).encode())[:24]
        if files else None)
    reuse = dict(schema="seampact-build-reuse/1", dependency_stage="dependencies", dependency_image=dependency_image, requirements=plan,
        package_cache=dict(kind="buildkit-cache-mount", id="seampact-pip-v1", in_worker_image=False),
        scope="same builder/base and identical dependency inputs may reuse installed layers; other compatible downloads use pip cache",
        observed_cache_hit="NOT_CHECKED")
    (destination / "build-reuse.json").write_text(json.dumps(reuse, ensure_ascii=False, indent=2), encoding="utf-8")
    return reuse
