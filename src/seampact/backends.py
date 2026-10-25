"""Explicit Docker/Kubernetes client operations for runtime-group instances.

Every operation runs a visible argv (no shell) and reports its actual return
code. Without a reachable backend the operation reports BACKEND_UNAVAILABLE and
the remaining manual commands; it never reports a deployment that did not run.
"""

import copy
import json
import re
import shlex
import shutil
import socket
import subprocess
import time
from pathlib import Path

KINDS = {"none", "docker", "kubernetes"}
IMAGE_ACCESS = {"local", "kind", "registry", "preloaded"}
NAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def validate_config(body):
    value = copy.deepcopy(body or {})
    kind = value.get("kind", "none")
    if kind not in KINDS:
        raise ValueError("部署后端只支持 none、docker、kubernetes")
    from urllib.parse import urlsplit
    coordinator = value.get("coordinator_url", "")
    if kind != "none" and (urlsplit(coordinator).scheme not in {"http", "https"} or not urlsplit(coordinator).netloc):
        raise ValueError("coordinator_url 须为运行进程可访问的平台 HTTP 地址")
    access = value.get("image_access", "local" if kind == "docker" else "preloaded")
    if access not in IMAGE_ACCESS or (kind == "docker" and access not in {"local", "registry", "preloaded"}):
        raise ValueError("镜像可达方式须为 local、kind、registry 或 preloaded（docker 后端不用 kind）")
    if access == "registry" and not (isinstance(value.get("registry"), str) and value["registry"].strip("/")):
        raise ValueError("registry 方式需要镜像仓库前缀")
    if access == "kind" and not NAME.fullmatch(str(value.get("kind_cluster", "kind"))):
        raise ValueError("kind 集群名称格式不正确")
    if kind == "kubernetes" and not NAME.fullmatch(str(value.get("namespace", ""))):
        raise ValueError("K8S 命名空间格式不正确")
    if any(not isinstance(name, str) or not ENV_NAME.fullmatch(name) for name in value.get("pass_env", [])):
        raise ValueError("pass_env 只列环境变量名称；值由平台进程环境传入，不写入配置")
    for mount in value.get("mounts", []):
        if not isinstance(mount, dict) or not isinstance(mount.get("source"), str) or not isinstance(mount.get("target"), str):
            raise ValueError("mounts 每项需要 source 与 target")
    if value.get("env_secret") is not None and not NAME.fullmatch(str(value["env_secret"])):
        raise ValueError("env_secret 须为已有 K8S Secret 名称")
    if (not isinstance(value.get("image_pull_secrets", []), list)
            or any(not isinstance(name, str) or not NAME.fullmatch(name) for name in value.get("image_pull_secrets", []))):
        raise ValueError("image_pull_secrets 须为已有 K8S 镜像仓库 Secret 名称列表")
    for key in ("resources", "volumes", "volume_mounts"):
        if key in value and not isinstance(value[key], (dict, list)):
            raise ValueError(key + " 须为 K8S 原生结构")
    if value.get("docker_network") is not None and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", str(value["docker_network"])):
        raise ValueError("docker_network 名称格式不正确")
    for key, default in (("build_timeout_s", 7200), ("operation_timeout_s", 120), ("start_timeout_s", 900)):
        value[key] = value.get(key, default)
        if type(value[key]) is not int or value[key] <= 0:
            raise ValueError(key + " 须为正整数秒")
    value.update(kind=kind, image_access=access)
    return value


def _tail(text, limit=4000):
    return text[-limit:] if text else ""


def _text(value):
    # TimeoutExpired can contain bytes even when subprocess.run used text=True.
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value or ""


def _command_result(argv, started, returncode, stdout="", stderr="", **facts):
    stdout, stderr = _text(stdout), _text(stderr)
    return dict(argv=argv, returncode=returncode, duration_ms=(time.monotonic() - started) * 1000,
                stdout=stdout, stderr=stderr, output=_tail(stdout + stderr), **facts)


def run(argv, timeout, stdin=None):
    started = time.monotonic()
    try:
        completed = subprocess.run(argv, input=stdin, capture_output=True, text=True, timeout=timeout,
                                   encoding="utf-8", errors="replace")
        return _command_result(argv, started, completed.returncode, completed.stdout, completed.stderr)
    except subprocess.TimeoutExpired as error:
        return _command_result(argv, started, None, error.stdout, error.stderr, timeout=True)
    except OSError as error:
        return _command_result(argv, started, None, stderr=str(error))


def manual(commands, reason):
    return dict(status="BACKEND_UNAVAILABLE", reason=reason,
                manual_commands=[shlex.join(command) for command in commands])


def _result(steps, success, **facts):
    failed = next((step for step in steps if step["returncode"] != 0), None)
    # Full machine output is used only by the caller; persisted/displayed receipts
    # keep bounded logs and preserve the two output channels separately.
    logs = [dict({key: value for key, value in step.items() if key not in {"stdout", "stderr"}},
                 stdout_tail=_tail(step.get("stdout", "")), stderr_tail=_tail(step.get("stderr", "")))
            for step in steps]
    return dict(status=success if failed is None else "FAILED", steps=logs,
                reason=None if failed is None else "命令失败或超时：" + shlex.join(failed["argv"]), **facts)


def docker_available(timeout=20):
    if not shutil.which("docker"):
        return "未找到 docker 客户端"
    step = run(["docker", "info", "--format", "{{.ServerVersion}}"], timeout)
    return None if step["returncode"] == 0 else "Docker 守护进程不可用：" + step["output"][-300:]


def kubectl_available(config):
    if not shutil.which("kubectl"):
        return "未找到 kubectl 客户端"
    step = run(_kubectl(config) + ["get", "namespace", config["namespace"], "-o", "name"], config["operation_timeout_s"])
    return None if step["returncode"] == 0 else "K8S 上下文或命名空间不可用：" + step["output"][-300:]


def _kubectl(config):
    argv = ["kubectl"]
    if config.get("context"):
        argv += ["--context", config["context"]]
    return argv + ["-n", config["namespace"]]


def build(folder, image, environment, config, dependency_image=None):
    argv = ["docker", "build", "-t", image]
    for key, value in (("BASE_IMAGE", environment.get("base_image")),
                       ("PIP_EXTRA_INDEX_URL", environment.get("pip_extra_index_url"))):
        if value:
            argv += ["--build-arg", key + "=" + value]
    argv.append(str(folder))
    dependencies = None
    if dependency_image:
        dependencies = list(argv)
        dependencies[dependencies.index("-t") + 1] = dependency_image
        dependencies[-1:-1] = ["--target", "dependencies"]
    gap = docker_available()
    if gap:
        commands = [dependencies] if dependencies else []
        return manual(commands + [argv, ["docker", "image", "inspect", "--format", "{{.Id}}", image]], gap)
    deadline = time.monotonic() + config["build_timeout_s"]
    steps = []
    if dependency_image:
        # Export a source-free dependency image. The worker build reuses this
        # same stage; compatible later sources may select it as their base.
        steps.append(run(dependencies, max(1, deadline - time.monotonic())))
    if not steps or steps[-1]["returncode"] == 0:
        steps.append(run(argv, max(1, deadline - time.monotonic())))
    image_id = None
    if all(step["returncode"] == 0 for step in steps):
        steps.append(run(["docker", "image", "inspect", "--format", "{{.Id}}", image], config["operation_timeout_s"]))
        image_id = steps[-1]["stdout"].strip() if steps[-1]["returncode"] == 0 else None
    return _result(steps, "BUILT", image=image, image_id=image_id, dependency_image=dependency_image)


def publish(image, config, image_id=None):
    access = config["image_access"]
    if access == "local":
        return dict(status="NOT_REQUIRED", image=image, reference=image_id or image,
                    reason="本机 Docker 后端使用实际构建镜像 ID" if image_id else "本机 Docker 后端直接使用本地镜像")
    if access == "preloaded":
        return dict(status="OPERATOR_REPORTED", image=image, reference=image,
                    reason="由操作者保证目标节点已存在该镜像；平台未核对节点镜像")
    if access == "kind":
        argv = ["kind", "load", "docker-image", image, "--name", config.get("kind_cluster", "kind")]
        if not shutil.which("kind"):
            return manual([argv], "未找到 kind 客户端")
        return _result([run(argv, config["build_timeout_s"])], "PUBLISHED", image=image, reference=image)
    reference = config["registry"].rstrip("/") + "/" + image
    commands = [["docker", "tag", image, reference], ["docker", "push", reference],
                ["docker", "image", "inspect", "--format", "{{json .RepoDigests}}", reference]]
    gap = docker_available()
    if gap:
        return manual(commands, gap)
    steps = []
    for argv in commands:
        steps.append(run(argv, config["build_timeout_s"]))
        if steps[-1]["returncode"] != 0:
            break
    digests = None
    if steps[-1]["returncode"] == 0 and len(steps) == 3:
        try:
            digests = json.loads(steps[-1]["stdout"].strip())
        except ValueError:
            digests = None
    repository = reference.rsplit(":", 1)[0]
    immutable = next((item for item in digests or [] if isinstance(item, str) and item.startswith(repository + "@sha256:")), None)
    return _result(steps, "PUBLISHED", image=image, reference=immutable or reference,
                   tag_reference=reference, repo_digests=digests)


def _free_port(host):
    with socket.socket() as probe:
        probe.bind((host, 0))
        return probe.getsockname()[1]


def worker_args(record, coordinator, advertise):
    return ["--coordinator", coordinator, "--advertise-url", advertise,
            "--instance-id", record["id"], "--generation", str(record["generation"])]


def start(record, reference, folder, config):
    if config["kind"] == "docker":
        name = "seampact-" + record["name"]
        argv = ["docker", "run", "-d", "--name", name, "--label", "seampact.group=" + record["group_id"],
                "--label", "seampact.instance=" + record["id"]]
        if config.get("docker_network"):
            argv += ["--network", config["docker_network"]]
            advertise = "http://" + name + ":8890"
        else:
            host = config.get("publish_host", "127.0.0.1")
            port = _free_port(host)
            argv += ["-p", host + ":" + str(port) + ":8890"]
            advertise = "http://" + config.get("advertise_host", host) + ":" + str(port)
        if config.get("gpus"):
            argv += ["--gpus", str(config["gpus"])]
        for name_ in config.get("pass_env", []):
            argv += ["-e", name_]
        for mount in config.get("mounts", []):
            argv += ["-v", mount["source"] + ":" + mount["target"] + (":ro" if mount.get("read_only", True) else "")]
        argv += [reference] + worker_args(record, config["coordinator_url"], advertise)
        gap = docker_available()
        if gap:
            return dict(manual([argv], gap), handle=dict(kind="docker", container=name), applied=False)
        return _result([run(argv, config["operation_timeout_s"])], "APPLIED",
                       handle=dict(kind="docker", container=name))
    labels = {"app": "seampact-worker", "seampact.group": record["group_id"], "seampact.instance": record["id"]}
    container = dict(name="owner", image=reference, imagePullPolicy="IfNotPresent",
        args=worker_args(record, config["coordinator_url"], "http://$(POD_IP):8890"),
        env=[dict(name="POD_IP", valueFrom=dict(fieldRef=dict(fieldPath="status.podIP")))] +
            [dict(name=name_, valueFrom=dict(secretKeyRef=dict(name=config["env_secret"], key=name_, optional=True)))
             for name_ in config.get("pass_env", []) if config.get("env_secret")],
        ports=[dict(containerPort=8890)])
    if config.get("resources"):
        container["resources"] = copy.deepcopy(config["resources"])
    if config.get("volume_mounts"):
        container["volumeMounts"] = copy.deepcopy(config["volume_mounts"])
    pod = dict(containers=[container], restartPolicy="Always")
    if config.get("image_pull_secrets"):
        pod["imagePullSecrets"] = [dict(name=name) for name in config["image_pull_secrets"]]
    if config.get("volumes"):
        pod["volumes"] = copy.deepcopy(config["volumes"])
    manifest = dict(apiVersion="apps/v1", kind="Deployment",
        metadata=dict(name=record["name"], namespace=config["namespace"], labels=labels),
        spec=dict(replicas=1, strategy=dict(type="Recreate"), selector=dict(matchLabels={"seampact.instance": record["id"]}),
                  template=dict(metadata=dict(labels=labels), spec=pod)))
    target = Path(folder) / "instances" / (record["name"] + ".json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    gap = kubectl_available(config)
    handle = dict(kind="kubernetes", deployment=record["name"], namespace=config["namespace"],
                  context=config.get("context"))
    if not handle["context"] and not gap:
        context = run(["kubectl", "config", "current-context"], config["operation_timeout_s"])
        if context["returncode"] == 0 and context["stdout"].strip():
            handle["context"] = context["stdout"].strip()
    handle["context_unresolved"] = not bool(handle["context"])
    frozen = _handle_config(handle, config)
    argv = _kubectl(frozen) + ["apply", "-f", str(target)]
    if gap:
        return dict(manual([argv], gap), manifest=str(target), handle=handle, applied=False)
    return _result([run(argv, config["operation_timeout_s"])], "APPLIED", manifest=str(target),
                   handle=handle)


def _handle_config(handle, config):
    """Use the recorded object location even if the backend settings changed."""
    frozen = dict(config)
    for key in ("namespace", "context"):
        if key in handle:
            frozen[key] = handle[key]
    return frozen


def _location_unconfirmed(handle):
    # Older receipts did not freeze the object location. The current settings
    # cannot establish where those objects were actually created.
    return (handle.get("context_unresolved") or not handle.get("namespace") or not handle.get("context"))


def observe(record, config):
    handle = record.get("handle") or {}
    config = _handle_config(handle, config)
    if "container" in handle:
        if docker_available(10):
            return dict(status="UNKNOWN", reason="Docker 不可用，未读取实际状态")
        step = run(["docker", "inspect", "--format", "{{json .State}}", handle["container"]], config["operation_timeout_s"])
        if step["returncode"] != 0:
            return dict(status="ABSENT", reason=step["output"][-300:])
        try:
            state = json.loads(step["stdout"].strip())
        except ValueError:
            return dict(status="UNKNOWN", reason="无法读取容器状态")
        return dict(status="RUNNING" if state.get("Running") else "EXITED", exit_code=state.get("ExitCode"),
                    restart_count=None, started_at=state.get("StartedAt"))
    if "deployment" in handle:
        if _location_unconfirmed(handle):
            return dict(status="UNKNOWN", reason="启动时未确认 K8S 上下文；待操作者确认对象位置")
        if kubectl_available(config):
            return dict(status="UNKNOWN", reason="kubectl 不可用，未读取实际状态")
        owner = run(_kubectl(config) + ["get", "deployment", handle["deployment"], "--ignore-not-found=true", "-o", "json"],
                    config["operation_timeout_s"])
        if owner["returncode"] != 0:
            return dict(status="UNKNOWN", reason=owner["output"][-300:])
        step = run(_kubectl(config) + ["get", "pods", "-l", "seampact.instance=" + record["id"], "-o", "json"],
                   config["operation_timeout_s"])
        if step["returncode"] != 0:
            return dict(status="UNKNOWN", reason=step["output"][-300:])
        try:
            deployment = json.loads(owner["stdout"]) if owner["stdout"].strip() else None
            pods = json.loads(step["stdout"]).get("items", [])
        except ValueError:
            return dict(status="UNKNOWN", reason="无法读取 Deployment／Pod 状态")
        if not pods:
            return dict(status="ABSENT" if deployment is None else "PENDING",
                        reason="Deployment 及 Pod 均不存在" if deployment is None else "Deployment 存在，尚无 Pod")
        if deployment is None:
            return dict(status="TERMINATING", reason="Deployment 已删除，仍有 Pod 待释放")
        status = pods[0].get("status", {})
        restarts = sum(item.get("restartCount", 0) for item in status.get("containerStatuses", []))
        return dict(status=status.get("phase", "UNKNOWN").upper(), pod=pods[0]["metadata"]["name"],
                    pod_ip=status.get("podIP"), node=pods[0].get("spec", {}).get("nodeName"), restart_count=restarts)
    return dict(status="UNKNOWN", reason="实例由操作者在平台外启动；平台没有后端句柄")


def stop(record, config):
    handle = record.get("handle") or {}
    config = _handle_config(handle, config)
    if "container" in handle:
        commands = [["docker", "stop", "-t", "30", handle["container"]], ["docker", "rm", handle["container"]]]
        gap = docker_available()
        if gap:
            return manual(commands, gap)
        steps = [run(commands[0], config["operation_timeout_s"])]
        if steps[0]["returncode"] == 0:
            steps.append(run(commands[1], config["operation_timeout_s"]))
        return _result(steps, "STOPPED")
    if "deployment" in handle:
        location_gap = _location_unconfirmed(handle)
        if location_gap:
            config = dict(config, namespace=handle.get("namespace") or "<confirmed-namespace>",
                          context=handle.get("context") or "<confirmed-context>")
        argv = _kubectl(config) + ["delete", "deployment", handle["deployment"], "--ignore-not-found=true", "--wait=true",
                                    "--timeout=" + str(config["operation_timeout_s"]) + "s"]
        inspect = _kubectl(config) + ["get", "deployment", handle["deployment"], "--ignore-not-found=true", "-o", "json"]
        inspect_pods = _kubectl(config) + ["get", "pods", "-l", "seampact.instance=" + record["id"], "-o", "json"]
        if location_gap:
            return dict(manual([argv, inspect, inspect_pods], "启动时未确认 K8S 对象位置；须确认并替换命令占位符后手动释放"),
                        status="STOP_PENDING")
        gap = kubectl_available(config)
        if gap:
            return manual([argv, inspect, inspect_pods], gap)
        steps = [run(argv, config["operation_timeout_s"] + 30)]
        if steps[-1]["returncode"] != 0:
            return _result(steps, "STOPPED")
        steps += [run(inspect, config["operation_timeout_s"]), run(inspect_pods, config["operation_timeout_s"])]
        absent = False
        if all(step["returncode"] == 0 for step in steps[-2:]):
            try:
                absent = not steps[-2]["stdout"].strip() and not json.loads(steps[-1]["stdout"]).get("items", [])
            except ValueError:
                pass
        result = _result(steps, "STOPPED")
        if not absent:
            result.update(status="STOP_PENDING", reason="尚未确认 Deployment 及其 Pod 已删除；待确认释放",
                          manual_commands=[shlex.join(command) for command in (inspect, inspect_pods)])
        return result
    return None
