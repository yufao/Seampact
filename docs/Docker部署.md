# Docker 部署与网络选择

更新：2026-10-22，SeamPact 0.10.0。完整远端命令从[服务器部署](服务器部署.md)开始；本页解释镜像、路径、host／Compose 两条路线、独立 Harness 环境及可选 Docker 客户端。二者选择一条，不同时读写同一状态目录。以下自动部署配置是代码提供的操作入口，不代表已在实际 Docker／K8S／GPU 环境运行成功。

## 1. 推荐的 Linux host 路线

rootless Docker 可选择[服务器部署第3节](服务器部署.md#3-主平台-a构建与启动)的回环端口发布方式，或本页第2节 Compose 路线。个人服务由 `systemctl --user` 管理，使用当前用户自己的 context。

host 路线的网络能力按目标 daemon 与版本核对，见[Docker 官方版本说明](https://docs.docker.com/engine/security/rootless/troubleshoot/#--net-host-doesnt-listen-ports-on-the-host-network-namespace)。rootless 写入当前用户私有绑定目录时使用容器 `0:0`；系统 daemon 使用宿主实际 UID/GID。bridge 与 host 路线选择后使用各自的地址语义。

主平台 `docker run --network host`，程序监听 `127.0.0.1`。容器与Linux宿主共享网络视角，能访问宿主模型服务、同机worker和SSH回环转发。主机端口由 `--port` 决定，无需 `-p`。通过SSH转发在电脑打开网页。

代码和私有目录分开：

| 主机 | 容器 |
|---|---|
| `~/seampact-workspace/Seampact` | 构建源，不作为运行状态目录 |
| `~/seampact-workspace/seampact-data` | `/data` |
| `~/seampact-workspace/harnesses` | `/harnesses` |

镜像标签使用实际commit，避免同一版本标签掩盖不同代码。平台镜像仍是 Python 3.11，版本不是由宿主系统 Python 决定；独立运行组另选目标 Python 和基础镜像。系统daemon的平台容器使用宿主实际UID／GID，rootless使用容器0:0；完整构建与run命令见服务器指南第3节。Host网络依据[Docker官方文档](https://docs.docker.com/engine/network/drivers/host/)，本指南面向Linux，不要求修改Windows Docker Desktop设置。

## 2. Compose 桥接路线：只先启动平台服务

仓库现有 `deploy/compose.yaml` 使用bridge网络，把容器端口发布到宿主回环。适合先使用云端模型、分析和本地能力；它与host路线的回环视角不同。

在Linux终端：

```bash
umask 077
SEAMPACT_ROOT="$HOME/seampact-workspace"
SEAMPACT_CODE_DIR="$SEAMPACT_ROOT/Seampact"
export SEAMPACT_DATA_DIR="$SEAMPACT_ROOT/seampact-data"
export SEAMPACT_HARNESS_DIR="$SEAMPACT_ROOT/harnesses"
export SEAMPACT_UID="$(id -u)"
export SEAMPACT_GID="$(id -g)"
export SEAMPACT_PORT=8880
SEAMPACT_COMPOSE_PROJECT="seampact-prototype-$(id -u)"
mkdir -p "$SEAMPACT_DATA_DIR" "$SEAMPACT_HARNESS_DIR"
```

**rootless Docker 还需在同一终端覆盖容器 UID/GID：** 容器 root 映射为启动 daemon 的宿主用户，可以写入其私有绑定目录。系统 daemon 仍使用上面的实际 UID/GID，跳过此覆盖。

```bash
docker context use rootless
export SEAMPACT_UID=0
export SEAMPACT_GID=0
```

确认没有另一套平台使用这些目录和端口后：

```bash
docker compose -p "$SEAMPACT_COMPOSE_PROJECT" -f "$SEAMPACT_CODE_DIR/deploy/compose.yaml" up -d --build
docker compose -p "$SEAMPACT_COMPOSE_PROJECT" -f "$SEAMPACT_CODE_DIR/deploy/compose.yaml" ps
docker compose -p "$SEAMPACT_COMPOSE_PROJECT" -f "$SEAMPACT_CODE_DIR/deploy/compose.yaml" logs --tail 80 platform
```

容器监听0.0.0.0:8880，宿主只发布127.0.0.1:$SEAMPACT_PORT。电脑SSH转发18880→A的8880，打开http://127.0.0.1:18880。数据和来源挂载与host路线相同；输入仍使用容器路径 `/harnesses/<项目>`。

当前 Compose 镜像标签为 `seampact-core:0.10.0${SEAMPACT_IMAGE_SUFFIX:-}`；更新用 `--build` 和 `--force-recreate` 重建／替换原 Compose 项目，并保持原数据目录、环境文件和挂载。只 pull 代码或 restart 旧容器不会加载镜像里的新代码。Host 主路线用 commit 标签更容易识别实际版本。使用第 6 节的工具镜像时，按该节先分别构建镜像，再使用两个 Compose 文件和 `--no-build`。

## 3. bridge 平台怎样接 worker

**Compose 平台不能用 `http://127.0.0.1:8890` 访问宿主 host 网络 worker**：那是平台容器自身。跨机 SSH 反向端口也一样。rootless 环境的同机 worker 可使用本节同网络方案。

若已有Compose平台，且只在同一机器接worker，可以把worker加入同一个Compose网络，使用服务名通信。包仍必须由这个Compose平台导出。在宿主先按[运行组指南](运行组与K8S部署.md)取得本次包、真实组ID、worker镜像，完成构建后，在同一个终端定义：

```bash
SEAMPACT_COMPOSE_NETWORK="${SEAMPACT_COMPOSE_PROJECT}_default"
SEAMPACT_WORKER_NAME="seampact-owner-${SEAMPACT_GROUP_ID#rg-}-$(id -u)"
docker network inspect "$SEAMPACT_COMPOSE_NETWORK" --format '{{.Name}}'
```

网络存在后启动：

```bash
docker run -d --name "$SEAMPACT_WORKER_NAME" --init \
  --network "$SEAMPACT_COMPOSE_NETWORK" \
  "$SEAMPACT_WORKER_IMAGE" \
  --coordinator http://platform:8880 \
  --host 0.0.0.0 --port 8890 \
  --advertise-url "http://$SEAMPACT_WORKER_NAME:8890"
docker logs --tail 80 "$SEAMPACT_WORKER_NAME"
```

这里不发布worker端口到宿主，平台和worker使用Docker DNS。容器重建后身份仍须重新登记，不能恢复原会话。[服务器指南第6节](服务器部署.md#6-跨机器放置运行组)的跨机host网络＋SSH方法要求平台和Docker worker都选host路线（系统daemon或29.5及以上rootless），不能混用本节bridge的Docker单机DNS及回环视角。

## 4. 模型配置、来源与依赖

优先在网页配置云端模型，密钥写入 `/data/config/` 私有文件；Linux密钥文件权限0600。Windows DPAPI配置不可直接迁移。`--env-file` 是可选方式：仓库 `.env.example` 只含空密钥模板，把副本放在自己的项目私有目录，按实际UID／GID和路径填写，不提交该副本。它不替代网页中接口地址和型号的配置。

命名服务、权重和模型角色可在完整配置页管理，操作见[资源配置操作](资源配置操作.md)。Linux 完整凭据路径按私有目录0700、文件及临时文件0600处理，按实际映射 UID／GID 核对；这不是磁盘加密。资源路径采用容器内路径，宿主权重需由用户按现有挂载方式提供，平台不自动搬运文件。环境变量只属于已启动的容器进程，宿主后来修改不自动改变旧运行。

公开Git URL可直接接入；私有源不自动继承宿主SSH凭据，可先由宿主取得后挂载。路径使用容器实际视角，不填电脑Windows路径。模型和数据保留私有挂载，不放进Git或核心镜像。

核心镜像仅有平台、Python 3.11、Git 和证书，不包含 Docker、kubectl、kind 或所有 Harness 依赖。需要在容器内自动准备目标解释器、构建和管理 Docker worker 时，显式选用第 6 节的工具镜像；默认镜像不增加这些工具。

接入来源后，“配置 → Harness 接入 → 运行环境”展示冻结仓库的 Python、依赖清单、项目安装与 Dockerfile 依据。建议与缺口分别保留；不会自动合并多份清单、执行原 Dockerfile 或改写原依赖。可明确选择 Python 3.10–3.13、基础镜像、依赖清单、项目安装目录和额外 pip 需求。目标解释器镜像只提供源码核对工具；依赖在后续运行组镜像安装，权重和凭据另行挂载／注入。

保存环境后先准备目标解释器。已有完整候选可沿用它零模型调用重新核对；没有候选时由分析器继续发现入口和有限边界。运行组页面采用已选环境生成工件，再显式构建、使镜像对后端可达和启动实例。修改基础镜像会实际改变构建环境，且须与目标 Python 相符；已有实例不因此升级。依赖文件引用其他文件、源码包或本地扩展时，仍需保留原构建结构。Conda、CUDA 和自定义扩展不能仅凭版本字段判定可用，按原要求准备基础镜像并保留缺口。

Host平台可以连接宿主上127.0.0.1的模型API；bridge中的127.0.0.1只指容器自身。bridge模型服务需本就可达的容器／网络地址，不能仅换成host-gateway就假定原回环服务可达。

worker 只执行已通过对应规则的有限标量／原生组入口，跨版本解释器不自动扩展语法和生命周期支持。工件生成、解释器可用、依赖安装、资源存在与真实 worker 就绪分别记录；没有运行结果时保持 NOT_RUN 或具体缺口。当前托管配额不是共享服务器的 GPU 显存分配器。

## 5. 停止与更新

Host路线按服务器指南更新自己的命名容器，保留宿主数据。Compose只操作自己的项目：

```bash
docker compose -p "$SEAMPACT_COMPOSE_PROJECT" -f "$SEAMPACT_CODE_DIR/deploy/compose.yaml" stop platform
```

调用收尾、代码更新后：

```bash
docker compose -p "$SEAMPACT_COMPOSE_PROJECT" -f "$SEAMPACT_CODE_DIR/deploy/compose.yaml" up -d --build --force-recreate platform
```

主平台重启后，重启其worker以获取新session；只登记旧worker URL不能更新旧会话。重新分析／恢复来源改变安装身份后要新导出包。不要同时让新旧平台写同一目录。

需要移除这个Compose项目的容器与网络时，只在自己的项目名下使用 `down`，不加 `-v`：

```bash
docker compose -p "$SEAMPACT_COMPOSE_PROJECT" -f "$SEAMPACT_CODE_DIR/deploy/compose.yaml" down
```

独立docker run加入网络的worker须先停止、移除自己的该容器，否则Docker网络仍在使用。上述操作不删宿主绑定的数据目录；不做全局prune或删除其他项目。

## 6. 可选：容器内使用 Docker 自动准备与部署

只有希望平台直接操作本机 Docker daemon 时才使用本节。`deploy/Dockerfile.tools` 从已构建的平台镜像增加官方 Docker CLI 和 buildx；它不启动 daemon，也不包含 kubectl／kind。官方 CLI 的文件位置见 [docker-library/docker 源码](https://github.com/docker-library/docker/blob/master/29/cli/Dockerfile)。工具镜像继承平台的 Python、代码版本和启动命令，不改变 Harness 选择的运行环境。

挂载 Docker socket 后，平台可以通过所选 daemon 创建、删除容器并挂载宿主目录；这是本节显式选择的自动部署权限。使用自己的 daemon 和数据目录，不将 socket 暴露为无认证 TCP 服务，也不通过 `chmod 666` 处理权限。

### 6.1 分别构建核心与工具镜像

沿用第 2 节中已确定的源码、数据、端口与 UID/GID 变量。在调用与研究运行收尾后，先构建两份镜像；旧平台可以在构建期间继续运行：

```bash
export SEAMPACT_REVISION="$(git -C "$SEAMPACT_CODE_DIR" rev-parse --short=12 HEAD)"
SEAMPACT_CORE_IMAGE="seampact-core:$SEAMPACT_REVISION"
export SEAMPACT_TOOLS_IMAGE="$SEAMPACT_CORE_IMAGE-tools"
SEAMPACT_DOCKER_CLI_IMAGE="docker:29-cli"

docker build \
  --build-arg "SEAMPACT_REVISION=$SEAMPACT_REVISION" \
  --build-arg "SEAMPACT_EXTRAS=${SEAMPACT_EXTRAS:-}" \
  --build-arg "SEAMPACT_PIP_EXTRA_INDEX_URL=${SEAMPACT_PIP_EXTRA_INDEX_URL:-}" \
  -f "$SEAMPACT_CODE_DIR/deploy/Dockerfile" \
  -t "$SEAMPACT_CORE_IMAGE" "$SEAMPACT_CODE_DIR"

docker build \
  --build-arg "PLATFORM_IMAGE=$SEAMPACT_CORE_IMAGE" \
  --build-arg "DOCKER_CLI_IMAGE=$SEAMPACT_DOCKER_CLI_IMAGE" \
  -f "$SEAMPACT_CODE_DIR/deploy/Dockerfile.tools" \
  -t "$SEAMPACT_TOOLS_IMAGE" "$SEAMPACT_CODE_DIR"
```

每条构建成功后再继续。正式研究可把 Docker CLI 镜像固定为已确认的完整版本或 digest；工具镜像的两份基础镜像必须与目标机器架构一致。构建工具镜像仍可能需要拉取官方 CLI 镜像；它没有额外的 apt、curl 或安装脚本步骤，默认平台构建也不会因此拉取 CLI 镜像。

### 6.2 确认 socket 与共享网络

下面按当前 Docker context 查找本机 Unix socket，不根据用户名猜路径。rootless 的默认路径与系统 daemon 不同，见 [Docker rootless 客户端说明](https://docs.docker.com/engine/security/rootless/tips/#client)。本节要求当前 CLI 没有另外使用 `DOCKER_HOST` 覆盖 context；若设置了该变量，先确认实际目标，不能直接将另一个 context 的 socket 挂入平台。SSH／TCP context 不使用这个本机 socket override。

```bash
docker context show
docker context inspect "$(docker context show)" \
  --format '{{.Endpoints.docker.Host}}'
```

确认返回本机 `unix:///.../docker.sock` 后，在同一终端设置：

```bash
SEAMPACT_DOCKER_ENDPOINT="$(docker context inspect "$(docker context show)" --format '{{.Endpoints.docker.Host}}')"
export DOCKER_SOCKET_PATH="${SEAMPACT_DOCKER_ENDPOINT#unix://}"
test -S "$DOCKER_SOCKET_PATH"
export SEAMPACT_DOCKER_GID="$(stat -c '%g' "$DOCKER_SOCKET_PATH")"
export SEAMPACT_RUNTIME_NETWORK="${SEAMPACT_COMPOSE_PROJECT}-runtime"
```

`test -S` 失败则停在这里。系统 daemon 的平台使用已有实际 UID/GID，override 根据 socket 的 GID 增加补充组；rootless 平台仍按第 2 节使用容器 `0:0`。不要为了工具镜像改变已有数据目录的拥有者。

首次创建本项目专用共享网络；已存在时仅确认后复用：

```bash
docker network create "$SEAMPACT_RUNTIME_NETWORK"
docker network inspect "$SEAMPACT_RUNTIME_NETWORK" --format '{{.Name}}'
```

备份平台数据后，用同一项目和原挂载替换平台容器：

```bash
docker compose -p "$SEAMPACT_COMPOSE_PROJECT" \
  -f "$SEAMPACT_CODE_DIR/deploy/compose.yaml" \
  -f "$SEAMPACT_CODE_DIR/deploy/compose.tools.yaml" \
  up -d --no-build --force-recreate platform

docker compose -p "$SEAMPACT_COMPOSE_PROJECT" \
  -f "$SEAMPACT_CODE_DIR/deploy/compose.yaml" \
  -f "$SEAMPACT_CODE_DIR/deploy/compose.tools.yaml" \
  logs --tail 80 platform
```

工具 override 继承基础 Compose 的构建配置，所以这里必须使用 `--no-build`，镜像由 6.1 显式构建；不要把这条命令改为 `--build`。后续停止或更新也持续使用两个文件。原来用 `docker run` 启动的容器不会自动变成 Compose 项目，应先按原部署方式备份、停止并保留原容器，再采用新项目；不能同时启动两个平台写同一 `/data`。

### 6.3 接通运行组后端

在“运行组与部署”的后端 JSON 中选择 Docker，并把网络名称替换为上面变量的实际值：

```json
{
  "kind": "docker",
  "coordinator_url": "http://seampact-platform:8880",
  "docker_network": "<SEAMPACT_RUNTIME_NETWORK 的实际值>",
  "image_access": "local"
}
```

平台与 worker 使用同一自建网络，通过容器名通信。`coordinator_url` 使用平台容器端口 8880，不能填电脑 SSH 转发端口或 worker 自己的 `127.0.0.1`。Docker DNS 与网络按 [Docker 自建 bridge 网络说明](https://docs.docker.com/engine/network/drivers/bridge/#differences-between-user-defined-bridges-and-the-default-bridge)执行。

需要权重时，可在该 JSON 增加原资源所需的挂载，例如：

```json
{
  "mounts": [
    {
      "source": "<宿主 Harness 目录的绝对路径>/weights",
      "target": "/harnesses/weights",
      "read_only": true
    }
  ]
}
```

这是添加到完整后端 JSON 的片段。`mounts.source` 由 daemon 按宿主路径解释；`target` 才是 worker 内路径。平台里的 `/data`、`/harnesses` 不能直接当作宿主 source，除非宿主确实有同名目录。示例只挂选定权重子目录，不把整个平台状态和凭据目录交给 worker。原配置中的资源路径应与 worker 的 `target` 一致。[Docker bind mount 说明](https://docs.docker.com/engine/storage/bind-mounts/)区分客户端视角与 daemon 宿主路径。

随后从来源环境表单准备解释器、分析或重新核对，再在运行组卡片生成工件、构建镜像、使镜像可达和启动实例。工具可用不表示原依赖已安装，也不表示原目标已调用成功。K8S 节点仍按已有 `IfNotPresent` 拉取选定 worker 镜像；不增加另一套拉取器。需要 K8S／kind 自动操作时，平台进程还必须已有可用的 kubectl／kind、上下文和必要配置，或由具备这些工具的宿主环境启动平台；本工具镜像不提供它们。
