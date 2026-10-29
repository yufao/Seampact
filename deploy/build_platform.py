"""One platform environment builder for deploy.sh and manual Compose deployments.

Only dependency fields enter the environment key. A successful resolution is
saved as an exact lock; missing images rebuild from that lock, not new versions.
No running container, mount, source or model is modified by this helper.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from urllib.parse import urlsplit
import uuid

from install_environment import locked_lines


def digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    os.chmod(temp, 0o600)
    temp.replace(path)


class Docker:
    def __init__(self, host=None):
        self.prefix = ["docker"] + (["--host", host] if host else [])
        self.env = dict(os.environ)
        if host:
            for name in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"):
                self.env.pop(name, None)

    def run(self, args, *, check=True, visible=False, data=None):
        result = subprocess.run(self.prefix + args, input=data, text=True, env=self.env,
            stdout=None if visible else subprocess.PIPE, stderr=None if visible else subprocess.PIPE)
        if check and result.returncode:
            raise RuntimeError("Docker operation failed: " + " ".join(args[:2]))
        return result

    def inspect(self, image):
        result = self.run(["image", "inspect", image], check=False)
        return json.loads(result.stdout)[0] if result.returncode == 0 else None

    def resolve_base(self, reference):
        result = self.inspect(reference)
        if result is None:
            self.run(["pull", reference], visible=True)
            result = self.inspect(reference)
        if not result:
            raise ValueError("base image could not be resolved")
        return result


def metadata(code, docker, base_id):
    content = (code / "pyproject.toml").read_text(encoding="utf-8")
    try:
        import tomllib
        return tomllib.loads(content)
    except ImportError:
        # Deployment hosts may have Python <3.11; the selected platform base
        # supplies the required parser without installing host dependencies.
        program = "import json,sys,tomllib; print(json.dumps(tomllib.loads(sys.stdin.read())))"
        result = docker.run(["run", "--rm", "-i", "--network", "none", "--entrypoint", "python",
                             base_id, "-c", program], data=content)
        return json.loads(result.stdout)


def index_url(value):
    if not value:
        return ""
    url = urlsplit(value)
    if url.scheme not in {"https", "http"} or not url.netloc or url.username or url.password or url.query or url.fragment:
        raise ValueError("index URL must be HTTP(S) without credentials, query or fragment")
    return value.rstrip("/")


def dependency_request(project, extras="", pip_index_url="", pip_extra_index_url="",
                       torch_version="", torch_index_url="", lock=""):
    if {"dependencies", "optional-dependencies"} & set(project["project"].get("dynamic", [])):
        raise ValueError("platform dependencies must be declared statically for source-free installation")
    selected = sorted({name.strip() for name in extras.split(",") if name.strip()})
    optional = project["project"].get("optional-dependencies", {})
    if any(name not in optional for name in selected):
        raise ValueError("unknown platform extras")
    requirements = list(project["project"].get("dependencies", []))
    for name in selected:
        requirements.extend(optional[name])
    declared = sorted(set(requirements))
    torch_rows = [row for row in requirements if re.match(r"^torch(?:\s*[<>=!~;\[]|$)", row, re.I)]
    if (torch_version or torch_index_url) and len(torch_rows) != 1:
        raise ValueError("torch options require a platform extra declaring exactly one torch requirement")
    if torch_version:
        if not re.fullmatch(r"[0-9][A-Za-z0-9.+!_-]*", torch_version):
            raise ValueError("torch-version must be an exact version, optionally with CPU/CUDA suffix")
        requirements[requirements.index(torch_rows[0])] = "torch==" + torch_version
        torch_rows = ["torch==" + torch_version]
    backend = project["build-system"]["build-backend"]
    if backend != "setuptools.build_meta":
        raise ValueError("platform build backend changed; update the environment recipe")
    build = list(project["build-system"]["requires"])
    if not any(re.match(r"^wheel(?:\W|$)", row, re.I) for row in build):
        build.append("wheel")
    return dict(schema="seampact-platform-environment/1", extras=selected,
        requirements=sorted(set(requirements)), declared_requirements=declared, build_requirements=sorted(set(build)),
        pip_index_url=index_url(pip_index_url), pip_extra_index_url=index_url(pip_extra_index_url),
        torch_requirement=torch_rows[0] if torch_rows else None, torch_index_url=index_url(torch_index_url),
        supplied_lock_sha256=digest(lock.encode()) if lock else None)


def environment_key(request, base_id, code):
    recipes = {name: digest((code / "deploy" / name).read_bytes())
               for name in ("Dockerfile.dependencies", "install_environment.py")}
    return digest(dict(request=request, base_id=base_id, recipes=recipes))


def matches(image, key, base_id, expected_id=None):
    labels = ((image or {}).get("Config") or {}).get("Labels") or {}
    return bool(image and (not expected_id or image["Id"] == expected_id)
        and labels.get("io.seampact.platform-environment") == key
        and labels.get("io.seampact.environment.base") == base_id)


def read_environment(docker, image_id):
    program = ("import json,pathlib; p=pathlib.Path('/opt/seampact-environment'); "
               "print(json.dumps({'lock':(p/'environment.lock').read_text(), "
               "'installed':json.loads((p/'installed.json').read_text())}))")
    result = docker.run(["run", "--rm", "--network", "none", "--entrypoint", "python", image_id, "-c", program])
    value = json.loads(result.stdout)
    value["lock"] = locked_lines(value["lock"])
    return value


def build_reference(docker, image_id):
    # BuildKit FROM requires an image reference, not Docker's local config ID.
    # Give the verified ID its own local tag and check that mapping before use.
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
        raise ValueError("invalid resolved image ID")
    reference = "seampact-build-input:sha256-" + image_id.split(":", 1)[1]
    docker.run(["tag", image_id, reference])
    inspected = docker.inspect(reference)
    if not inspected or inspected["Id"] != image_id:
        raise ValueError("local build reference does not match the verified image ID")
    return reference


def prepare(code, state_dir, docker, *, base_image="python:3.11-slim", refresh=False,
            lock="", **options):
    code, state_dir = Path(code), Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    base = docker.resolve_base(base_image)
    project = metadata(code, docker, base["Id"])
    lock = locked_lines(lock) if lock else ""
    request = dependency_request(project, lock=lock, **options)
    key = environment_key(request, base["Id"], code)
    tag = "seampact-platform-environment:" + key
    record_path = state_dir / (key + ".json")
    prior = json.loads(record_path.read_text(encoding="utf-8")) if record_path.exists() else None
    if prior and (prior.get("key") != key or prior.get("request") != request or
                  digest(prior["lock"].encode()) != prior.get("lock_sha256")):
        raise ValueError("saved platform dependency record does not match its identity")
    if not lock and prior and not refresh:
        lock = locked_lines(prior["lock"])
    image = None if refresh else docker.inspect(prior["image_id"] if prior else tag)
    expected = prior["image_id"] if prior and not refresh else None
    reused = matches(image, key, base["Id"], expected)
    if reused:
        observed = read_environment(docker, image["Id"])
        if lock and observed["lock"] != lock:
            raise ValueError("environment image does not match the saved dependency lock")
    else:
        base_reference = build_reference(docker, base["Id"])
        with tempfile.TemporaryDirectory(prefix="build-", dir=state_dir) as temporary:
            context = Path(temporary)
            save(context / "request.json", request)
            (context / "input.lock").write_text(lock, encoding="utf-8")
            for name in ("Dockerfile.dependencies", "install_environment.py"):
                shutil.copyfile(code / "deploy" / name, context / name)
            command = ["buildx", "build", "--load", "--builder", "default", "-f", str(context / "Dockerfile.dependencies"),
                "--build-arg", "BASE_IMAGE=" + base_reference, "--build-arg", "ENVIRONMENT_KEY=" + key,
                "--build-arg", "RESOLVED_BASE=" + base["Id"], "-t", tag]
            if refresh:
                command += ["--build-arg", "RESOLVE_NONCE=" + uuid.uuid4().hex]
            docker.run(command + [str(context)], visible=True)
        image = docker.inspect(tag)
        if not matches(image, key, base["Id"]):
            raise ValueError("built environment identity could not be verified")
        observed = read_environment(docker, image["Id"])
        if lock and observed["lock"] != lock:
            raise ValueError("rebuilt environment differs from the saved dependency lock")
    record = dict(schema="seampact-platform-build/1", key=key, request=request, base_image=base_image,
        resolved_base_id=base["Id"], base_digests=base.get("RepoDigests", []), image=tag, image_id=image["Id"],
        lock=observed["lock"], lock_sha256=digest(observed["lock"].encode()), installed=observed["installed"])
    # Preserve previous actual locks if dependencies were deliberately refreshed.
    if prior and prior["image_id"] != record["image_id"]:
        save(state_dir / (key + "-" + prior["lock_sha256"] + ".json"), prior)
    save(record_path, record)
    return dict(record, status="REUSED" if reused else "BUILT", version=project["project"]["version"])


def build_platform(docker, code, environment, image, revision="UNKNOWN", owner=None):
    reference = build_reference(docker, environment["image_id"])
    args = ["buildx", "build", "--load", "--builder", "default", "-f", str(Path(code) / "deploy/Dockerfile"),
        "--build-arg", "DEPENDENCY_IMAGE=" + reference,
        "--build-arg", "SEAMPACT_EXTRAS=" + ",".join(environment["request"]["extras"]),
        "--build-arg", "SEAMPACT_VERSION=" + environment["version"],
        "--build-arg", "SEAMPACT_REVISION=" + revision, "-t", image]
    if owner:
        args += ["--label", "io.seampact.deployment=" + owner]
    docker.run(args + [str(code)], visible=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--code-dir", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--state-dir", type=Path, required=True, help="private persistent environment records")
    parser.add_argument("--docker-host")
    parser.add_argument("--base-image", default="python:3.11-slim")
    parser.add_argument("--extras", default="")
    parser.add_argument("--pip-index-url", default="")
    parser.add_argument("--pip-extra-index-url", default="")
    parser.add_argument("--torch-version", default="")
    parser.add_argument("--torch-index-url", default="")
    parser.add_argument("--dependency-lock", type=Path)
    parser.add_argument("--refresh-dependencies", action="store_true")
    parser.add_argument("--image", help="also build the final platform image")
    parser.add_argument("--revision", default="UNKNOWN")
    parser.add_argument("--owner")
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--compose-env", type=Path, help="write build identity variables for Compose")
    args = parser.parse_args(argv)
    if args.refresh_dependencies and args.dependency_lock:
        parser.error("refresh-dependencies and an explicit dependency-lock are mutually exclusive")
    os.umask(0o077)
    docker = Docker(args.docker_host)
    result = prepare(args.code_dir.resolve(), args.state_dir.resolve(), docker, base_image=args.base_image,
        refresh=args.refresh_dependencies,
        lock=args.dependency_lock.read_text(encoding="utf-8") if args.dependency_lock else "",
        extras=args.extras, pip_index_url=args.pip_index_url, pip_extra_index_url=args.pip_extra_index_url,
        torch_version=args.torch_version, torch_index_url=args.torch_index_url)
    if args.image:
        build_platform(docker, args.code_dir, result, args.image, args.revision, args.owner)
    if args.receipt:
        save(args.receipt, result)
    if args.compose_env:
        reference = build_reference(docker, result["image_id"])
        args.compose_env.parent.mkdir(parents=True, exist_ok=True)
        args.compose_env.write_text("\n".join([
            "SEAMPACT_DEPENDENCY_IMAGE=" + reference,
            "SEAMPACT_EXTRAS=" + ",".join(result["request"]["extras"]),
            "SEAMPACT_VERSION=" + result["version"], "SEAMPACT_REVISION=" + args.revision]) + "\n", encoding="utf-8")
        os.chmod(args.compose_env, 0o600)
    print("Platform dependency environment: " + result["status"] + "; lock " + result["lock_sha256"][:16])
    if result["installed"].get("torch"):
        print("Learner torch: " + result["installed"]["torch"] + "; CUDA runtime: " + str(result["installed"]["cuda"]))


if __name__ == "__main__":
    main()
