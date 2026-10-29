"""Runs inside the source-free platform environment build, never imports Harnesses."""
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile


def pip(*args, channel_only=False):
    env = None
    if channel_only:
        env = dict(os.environ)
        # A base image's extra index/find-links must not override the explicit
        # torch wheel channel. Keep its proxy/certificate/cache settings.
        for name in ("PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL", "PIP_FIND_LINKS", "PIP_NO_INDEX"):
            env.pop(name, None)
        env["PIP_CONFIG_FILE"] = os.devnull
    subprocess.run([sys.executable, "-m", "pip", *args], check=True, env=env)


def locked_lines(text):
    lines = sorted({line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")})
    if not lines or any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*==[A-Za-z0-9.+!_-]+", line) for line in lines):
        raise ValueError("environment lock must contain exact package==version entries only")
    if len({re.sub(r"[-_.]+", "-", line.split("==")[0].lower()) for line in lines}) != len(lines):
        raise ValueError("duplicate packages in environment lock")
    return "\n".join(lines) + "\n"


def main():
    root = pathlib.Path("/opt/seampact-environment")
    request = json.loads((root / "request.json").read_text())
    indexes = []
    if request["pip_index_url"]:
        indexes += ["--index-url", request["pip_index_url"]]
    if request["pip_extra_index_url"]:
        indexes += ["--extra-index-url", request["pip_extra_index_url"]]
    lock = root / "input.lock"
    if lock.read_text().strip():
        lock.write_text(locked_lines(lock.read_text()))
        if request["torch_index_url"]:
            entries = lock.read_text().splitlines()
            torch_rows = [entry for entry in entries if entry.split("==")[0].lower() == "torch"]
            if len(torch_rows) != 1:
                raise ValueError("explicit torch channel needs one exact torch entry in the lock")
            with tempfile.TemporaryDirectory() as folder:
                pip("download", "--no-deps", "--only-binary=:all:", "--index-url",
                    request["torch_index_url"], "--dest", folder, torch_rows[0], channel_only=True)
                wheels = list(pathlib.Path(folder).glob("*.whl"))
                if len(wheels) != 1:
                    raise ValueError("torch channel did not produce exactly one locked wheel")
                pip("install", *indexes, "--no-deps", str(wheels[0]), *[r for r in entries if r not in torch_rows])
        else:
            pip("install", *indexes, "--no-deps", "-r", str(lock))
    else:
        pip("install", *indexes, *request["build_requirements"])
        requirements = list(request["requirements"])
        if request["torch_index_url"] and request["torch_requirement"]:
            # Select the torch wheel only from the explicit channel. Its other
            # dependencies are resolved from the ordinary configured indexes.
            with tempfile.TemporaryDirectory() as folder:
                pip("download", "--no-deps", "--only-binary=:all:", "--index-url",
                    request["torch_index_url"], "--dest", folder, request["torch_requirement"], channel_only=True)
                wheels = list(pathlib.Path(folder).glob("*.whl"))
                if len(wheels) != 1:
                    raise ValueError("torch channel did not produce exactly one wheel")
                requirements.remove(request["torch_requirement"])
                pip("install", *indexes, str(wheels[0]), *requirements)
        elif requirements:
            pip("install", *indexes, *requirements)
    pip("check")
    # pip freeze may record the temporary wheel path; installed distributions
    # provide the exact versions without local build paths in the lock.
    from importlib.metadata import distributions
    frozen = locked_lines("\n".join(d.metadata["Name"] + "==" + d.version for d in distributions()))
    # Optional extras are not checked by pip check; verify the original package
    # declarations too, including a user's exact torch override.
    from importlib.metadata import version
    from pip._vendor.packaging.requirements import Requirement
    for declaration in request["declared_requirements"] + request["requirements"] + request["build_requirements"]:
        requirement = Requirement(declaration)
        if requirement.marker and not any(requirement.marker.evaluate({"extra": extra})
                for extra in request["extras"] + [""]):
            continue
        if not requirement.specifier.contains(version(requirement.name), prereleases=True):
            raise ValueError("installed dependency violates the platform declaration: " + requirement.name)
    if lock.read_text().strip() and frozen != locked_lines(lock.read_text()):
        raise ValueError("installed environment differs from the frozen lock; refresh dependencies explicitly")
    (root / "environment.lock").write_text(frozen)
    actual = {"python": sys.version, "torch": None, "cuda": None}
    if request["torch_requirement"]:
        import torch
        actual.update(torch=torch.__version__, cuda=torch.version.cuda)
    (root / "installed.json").write_text(json.dumps(actual, indent=2))


if __name__ == "__main__":
    main()
