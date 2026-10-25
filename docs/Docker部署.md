# Docker 网络、路径与运行组工具

更新：2026-10-25，SeamPact 0.13.0。平台首次部署、旧版本接管、更新、备份和旧平台镜像清理统一见[服务器部署](服务器部署.md)。本页解释容器相关配置；运行组的手动构建、同机与跨机命令见[运行组远端操作](运行组远端操作.md)。模型和 embedding 服务须独立启动，绑定 READY 不表示服务始终在线。本文命令和配置不代表已在实际 Linux、Docker、GPU 或 K8S 环境运行成功，真实运行状态仍按已有记录保留。

0.13.0 的依赖镜像复用、已有来源重新核对及过期工件处理见[统一构建说明](0.13_依赖镜像复用与统一构建.md)。构建成功、实例就绪和原目标调用成功分别记录。


## 依赖镜像、CUDA 与存储位置

0.13.0 使用 BuildKit 共享 pip 包缓存，并把可独立安装的第三方依赖放在来源源码之前。可复用依赖镜像的选择和已有来源更新见[统一构建说明](0.13_依赖镜像复用与统一构建.md)，手动两阶段命令见[运行组远端操作](运行组远端操作.md)。相同环境可复用安装层；不同清单的部分包相同，不等于完整环境相同。

PyTorch 下载的 cuBLAS、cuDNN 等 CUDA 用户态运行库安装在构建镜像中，不替换宿主显卡驱动、系统 CUDA 或系统 Python。镜像推理仍依赖宿主可见 GPU、兼容驱动及相应容器运行条件，须用真实目标调用核对。

| 内容 | 实际存放位置由什么决定 |
|---|---|
| 镜像、构建层、BuildKit 包缓存 | 当前 Docker daemon 的数据目录及存储配置 |
| 平台 `/data` 中的状态与分析材料 | 平台容器的实际持久挂载源 |
| 原库权重、数据和索引 | 下载位置及平台／worker 各自的挂载配置 |

容器内 `/data` 与宿主同名目录没有天然对应关系。将权重放到大容量分区不会同时迁移 Docker 镜像缓存，平台已有的权重挂载也不会自动传给独立 worker。先在宿主核对实际路径，不把 `df` 的文件系统剩余空间当作个人配额或自动扩容保证：

```bash
docker info --format 'Root={{.DockerRootDir}} Driver={{.Driver}}'
docker inspect "$SEAMPACT_PLATFORM_NAME" \
  --format '{{range .Mounts}}{{println .Source "->" .Destination}}{{end}}'
```

本页只说明位置核对，不自动变更共享 Docker、驱动、磁盘挂载或个人配额。存储迁移须依据管理员提供的可写目录、文件系统和所用 daemon 配置另行处理。

## 1. 新部署与已有部署的网络

`deploy.sh install` 默认创建本部署专用的自建 bridge 网络，平台使用自己的容器名作为网络别名，默认名为 `seampact-platform`。容器监听 `0.0.0.0:8880`，宿主仅发布 `127.0.0.1:8880`。电脑通过 SSH 转发访问宿主发布端口。同机 worker 加入相同网络，通过 Docker DNS 双向通信；不需要向宿主发布 worker 端口。[Docker 自建 bridge 说明](https://docs.docker.com/engine/network/drivers/bridge/#differences-between-user-defined-bridges-and-the-default-bridge)。

更新已有部署时保留原网络、端口和挂载。接管旧 host 容器不会自动转换为 bridge；已有 Compose 容器也不会因接管而改变地址语义。`<部署根目录>/deployment.json` 保存本机部署配置，应作为私有文件保留；其中可能包含旧容器的环境变量，不要整体复制到共享文档或 Git。

仓库的 `deploy/compose.yaml`、`deploy/compose.tools.yaml` 是手动 Compose 配置模板，不是另一份更新流程。已有 Compose 项目使用原项目名和完整文件组合管理；不要同时用 Compose 和脚本替换同一个容器。默认 Compose 网络与脚本创建的运行组网络也不能仅凭名称推定为同一个网络。

## 2. host 网络与回环地址

host 模式下，平台、worker 与目标 Docker daemon 宿主共享网络命名空间，`127.0.0.1` 才能用于访问同机宿主服务；`-p` 不参与 host 网络的端口映射。[Docker host 网络说明](https://docs.docker.com/engine/network/drivers/host/)。

bridge 平台中的 `127.0.0.1` 指向平台容器自身。宿主模型 API、宿主 worker 和 SSH 反向转发不会因此自动可达；仅添加 `host-gateway` 也不能把只监听宿主回环的服务改成容器可达服务。模型服务应使用实际可达的容器／专用网络地址。

旧 host 部署可继续使用[跨机 SSH 方法](运行组远端操作.md#4-跨机器host-网络与双向-ssh)。该方法要求两端实际共享各自宿主的回环视角。rootless 在 Docker Engine 29.5 之前的 host 网络位于 RootlessKit 命名空间，不能直接套用宿主回环地址；按目标 daemon 的版本与网络行为确认，见[官方 rootless 历史限制](https://docs.docker.com/engine/security/rootless/troubleshoot/#historical-limitations)。跨机 bridge 或 K8S 使用各自可路由的网络，不能套用单机 Docker DNS 或 host 回环命令。

## 3. bridge 平台怎样接 worker

在“运行组与部署”的后端 JSON 中使用实际运行组网络：

```json
{
  "kind": "docker",
  "coordinator_url": "http://seampact-platform:8880",
  "docker_network": "<deployment.json 中 runtime_network 的实际值>",
  "image_access": "local"
}
```

上述 coordinator 别名用于脚本默认容器名的新建 bridge 部署；自定义容器名或旧部署须使用实际网络别名。端口填容器监听端口，不能填电脑 SSH 转发端口。`runtime_network` 为 null 时，先核对旧容器实际网络，不自行填入默认名称。相同 Docker daemon 下的自建 bridge 仅覆盖同机容器，不连接另一台机器。

配置保存后，从来源环境表单准备目标解释器、分析或重新核对，在运行组卡片生成工件、构建镜像、使镜像可达、启动实例。默认工具镜像提供 Docker 操作能力；仅用基础镜像或后端不可达时，页面返回 `BACKEND_UNAVAILABLE` 和手动命令。手动执行路径映射及同网络 `docker run` 见[运行组远端操作](运行组远端操作.md)。

## 4. 数据、源码和资源的路径视角

新部署的默认对应关系如下，已有部署以保存的配置和实际挂载为准：

| 宿主／daemon 路径 | 平台容器路径 | 用途 |
|---|---|---|
| `<部署根目录>/Seampact` | 构建时复制进镜像 | 平台源码，更新镜像后生效 |
| `<部署根目录>/data` | `/data` | 状态库、模型配置、凭据、分析与运行组工件 |
| `<部署根目录>/harnesses` | `/harnesses` | 用户提供的来源、数据和权重 |

前端填写执行进程能看到的路径，例如 `/harnesses/<项目>`；不填电脑路径。私有来源可先由宿主取得后挂载，平台不自动继承宿主 Git SSH 凭据。模型和数据使用持久挂载，不放进核心镜像或 Git。

worker 权重挂载示例是后端 JSON 的附加片段：

```json
{
  "mounts": [
    {
      "source": "<daemon 宿主上的权重绝对路径>",
      "target": "/harnesses/weights",
      "read_only": true
    }
  ],
  "pass_env": ["EXAMPLE_PROVIDER_API_KEY"]
}
```

`source` 由 Docker daemon 在其宿主解释；`target` 是 worker 内路径，须与资源配置一致。平台中的 `/data`、`/harnesses` 不能直接当成宿主 `source`，除非宿主确实存在同名目录。只挂所需资源，不把整个平台状态与凭据目录交给 worker。[Docker bind mount 说明](https://docs.docker.com/engine/storage/bind-mounts/)。

`pass_env` 只保存变量名，值来自已启动的平台进程环境；宿主后来修改变量不会改变旧容器。网页保存的私有凭据不会自动变成 worker 环境变量。连接使用凭据引用时，按[资源配置操作](资源配置操作.md)核对实际变量名与调用进程。GPU 参数由实际机器和原来源要求决定，平台不推断显卡配置。

Linux 网页凭据在 `/data/config/` 使用私有目录和文件权限保存，Windows DPAPI 配置不能直接迁移。`.env.example` 只是空值模板；实际环境文件留在私有目录，不替代网页中的接口和型号配置。

## 5. 平台环境与独立运行组环境

平台基础镜像使用 Python 3.11；工具镜像在同一平台镜像上增加 Docker CLI 和 buildx。平台解释器不由宿主系统 Python 决定，也不决定 Harness 的目标 Python。

运行环境表单可选择 Python 3.10–3.13、匹配基础镜像、冻结源码内依赖清单、项目安装目录和额外 pip 需求。工件 `environment.json` 与 `worker.json` 冻结该选择，worker 实际解释器须匹配所选 major.minor，并满足原库的 Python 要求。目标解释器准备只提供源码核对环境，后续运行组镜像才安装其声明依赖；权重和凭据另行提供。

不自动合并依赖清单、执行来源 Dockerfile 或改写原依赖。Conda、CUDA、本地扩展和依赖中的相对文件仍按原构建要求处理。修改环境不升级已有实例，应重新生成工件、构建镜像并启动新实例。详见[0.10 异构运行环境](0.10_异构运行环境.md)。

工件生成、解释器可用、依赖安装、资源存在、worker 就绪和真实目标调用是不同状态；没有执行结果时保留 `NOT_RUN` 或具体缺口。有限标量／原生组规则不因换了解释器而扩展，托管配额也不是共享服务器的 GPU 显存分配器。

## 6. 可选：容器内使用 Docker 自动准备与部署

0.11.1 脚本新部署默认选择工具镜像；基础镜像与旧容器是否包含这些工具，以实际部署配置为准。`deploy/Dockerfile.tools` 从已构建的平台镜像加入官方 Docker CLI 和 buildx，继承同版平台代码与启动命令，不启动 daemon，不增加 kubectl／kind，也不改变 Harness 的运行环境。

平台通过本机 Unix socket 操作选定 daemon，可在其上构建镜像、创建／停止 worker 和挂载宿主目录。脚本新部署会记录本机 socket 及所需组权限；旧部署更新保留原挂载，不能因选择工具镜像就推定已有 socket 访问权。不要把 socket 改成无认证 TCP 服务或使用 `chmod 666` 解决权限。

排查工具与 daemon 时，先在宿主确认当前实际目标：

```bash
docker context show
docker context inspect "$(docker context show)" --format '{{.Endpoints.docker.Host}}'
docker info --format '{{.ServerVersion}}'
```

若设置了 `DOCKER_HOST`，它可能覆盖 context，应先确认其实际目标。本机 socket 通常为 `unix:///var/run/docker.sock`，rootless 默认位于自己的运行目录；以实际返回为准，不猜用户名或 UID。[Docker rootless 客户端说明](https://docs.docker.com/engine/security/rootless/tips/#client)。SSH／TCP context 不使用本机 socket 挂载方式。

系统 daemon 使用宿主实际 UID/GID，并按 socket 实际 GID 配补充组。rootless 新部署使用容器 `0:0` 映射当前 daemon 用户，不把宿主 socket 的数字 GID 直接追加到容器 `GroupAdd`：宿主 subordinate GID 可能超出容器映射，导致 runc 在程序启动前报 `setgroups: invalid argument`。已有显式附加组保留；接管自定义非 root 用户时需明确其 socket 访问方式，不自动改用户。已有数据的拥有者和旧容器用户以接管结果为准，不为更新递归修改共享目录拥有者。

客户端不存在、socket 不可用、socket 无权限与原依赖安装失败分别处理，不能以一种错误掩盖另一种。镜像构建网络依赖、平台数据备份及旧镜像清理只按[服务器部署](服务器部署.md)操作；不运行全局 prune，不自动清理运行组、来源镜像、共享网络或卷。

K8S／kind 还要求实际调用进程具有 kubectl／kind、对应上下文和必要配置；工具镜像不提供这些条件，详见[运行组与 K8S 部署](运行组与K8S部署.md)。
